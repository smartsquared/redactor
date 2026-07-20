"""Local encrypted store + alias mapping table schema (story S0.3).

Two of the four privacy artifacts (docs/brief.md) live here, together, in a
single SQLCipher-encrypted SQLite file:

  - Artifact 1: raw transactions (statements, memos, amounts) — local only.
  - Artifact 2: the alias mapping table (``PAYEE-7`` ↔ real value) — the crown
    jewel. Holding it reverses every redacted record, so it must never be
    serialized anywhere but this encrypted store.

Encryption at rest is provided by SQLCipher (transparent page-level AES-256).
The decision and the alternatives considered (OS keychain, age) are recorded in
docs/adr/0001-encryption-at-rest.md.

The module exposes ``open_store(path, key)`` returning a :class:`Store`. Mapping
values are handed back wrapped in :class:`Sealed`, a deliberately
non-serializable box whose value is reachable only through an explicit
``reveal()`` — modelling the render boundary, where un-redaction is allowed to
happen only on the machine that holds the table.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from redactor.alias import canonical

try:  # pragma: no cover - exercised only when the dependency is absent
    import sqlcipher3 as _sqlcipher
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "redactor.store requires 'sqlcipher3' for encryption at rest. "
        "Install it with `pip install sqlcipher3` (ships a wheel with "
        "libsqlcipher bundled). See docs/adr/0001-encryption-at-rest.md."
    ) from exc

# Bump when a migration is added; keep _MIGRATIONS in step (index i migrates
# user_version i -> i+1).
SCHEMA_VERSION = 2

PathLike = str | os.PathLike


class StoreError(Exception):
    """Base class for store errors."""


class BadKeyError(StoreError):
    """The supplied key could not decrypt the store."""


class MigrationError(StoreError):
    """The store schema could not be migrated to the current version."""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _escape_key(key: str) -> str:
    # PRAGMA does not accept bound parameters, so the passphrase is inlined.
    # SQL-escape single quotes by doubling them (standard SQLCipher practice).
    return key.replace("'", "''")


class Sealed:
    """A mapping-table real value that refuses to be serialized off-store.

    The alias↔real mapping is the crown jewel: leaking one row de-anonymizes a
    record forever. :class:`Sealed` makes accidental egress structurally hard:

      * it is not JSON-serializable (``json.dumps`` raises ``TypeError``);
      * it cannot be pickled or copied (``__reduce__`` raises), which blocks the
        generic ``pickle``/``copy``/``deepcopy`` serialization path;
      * its ``repr``/``str`` redact the value, so it cannot leak through logs,
        f-strings, or a stray ``print``.

    The real value is reachable only through the explicit :meth:`reveal` — which
    is the render boundary: un-redaction happens on purpose, and only where the
    mapping table lives.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """Return the underlying real value. Call this only at a local render
        boundary — never on a value bound for anything that leaves the machine."""
        return self._value

    def __repr__(self) -> str:
        return "<Sealed ███ redacted>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError(
            "Sealed mapping values must not be serialized off the local store"
        )

    def __setattr__(self, *_args):  # immutable
        raise AttributeError("Sealed is immutable")


class MappingTable:
    """CRUD accessor for artifact 2, the alias mapping table.

    Reads return :class:`Sealed` real values. This class deliberately provides
    no export/dump/serialize method — the only persistence sink is the
    encrypted connection it wraps.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def assign(self, entity_type: str, real_value: str) -> str:
        """Return a stable alias token for ``(entity_type, real_value)`` (S1.2).

        Idempotent issuance, per alias-contract §2:

          * **Stable forever** — an already-bound entity returns its existing
            token; the call never mints a second alias for the same entity.
          * **Monotonic, gap-tolerant, never reused** — a new entity gets the
            next unused integer for its class, drawn from a durable per-class
            counter that only ever increases. Numbers are not recycled even if a
            row is later deleted, so a stale redacted record can never be
            silently re-bound to a different real entity (§2.3).
          * **Namespaced by class** — ``ACCT`` and ``PAYEE`` count independently.

        This is *issuance*, not resolution: the caller supplies the resolved
        entity key. Collapsing memo-string variants to one entity is S1.3.

        Raises ``ValueError`` if ``entity_type`` is outside the closed class set.
        """
        # The uppercase class is the identity we key both the mapping and the
        # counter on, so "PAYEE" and "payee" name the same entity space.
        cls = entity_type.upper()

        existing = self.resolve_token(cls, real_value)
        if existing is not None:
            return existing

        n = self._next_n(cls)
        # canonical() validates the class against the closed set and fixes the
        # token grammar in one place; it raises before any row is written.
        token = canonical(cls, n)
        # Insert the binding and advance the counter in one transaction. The
        # UNIQUE(alias_token) constraint is a backstop against ever colliding.
        self._conn.execute(
            "INSERT INTO alias_mapping (alias_token, entity_type, real_value, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token, cls, real_value, _utcnow_iso()),
        )
        self._conn.execute(
            "INSERT INTO alias_sequence (entity_type, next_n) VALUES (?, ?) "
            "ON CONFLICT(entity_type) DO UPDATE SET next_n = excluded.next_n",
            (cls, n + 1),
        )
        self._conn.commit()
        return token

    def _next_n(self, cls: str) -> int:
        """The next unused integer for a class, from the durable counter."""
        row = self._conn.execute(
            "SELECT next_n FROM alias_sequence WHERE entity_type = ?", (cls,)
        ).fetchone()
        return 1 if row is None else row[0]

    def put(self, alias_token: str, entity_type: str, real_value: str) -> None:
        """Insert (or replace) a mapping row: alias_token ↔ real_value."""
        self._conn.execute(
            "INSERT INTO alias_mapping (alias_token, entity_type, real_value, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(alias_token) DO UPDATE SET "
            "entity_type=excluded.entity_type, real_value=excluded.real_value",
            (alias_token, entity_type, real_value, _utcnow_iso()),
        )
        self._conn.commit()

    def resolve_alias(self, alias_token: str) -> Sealed | None:
        """Return the real value for an alias token, sealed, or ``None``."""
        row = self._conn.execute(
            "SELECT real_value FROM alias_mapping WHERE alias_token = ?",
            (alias_token,),
        ).fetchone()
        return None if row is None else Sealed(row[0])

    def resolve_token(self, entity_type: str, real_value: str) -> str | None:
        """Return the alias token for a real value, or ``None``. The alias token
        itself is non-sensitive, so it is returned bare."""
        row = self._conn.execute(
            "SELECT alias_token FROM alias_mapping WHERE entity_type = ? AND real_value = ?",
            (entity_type, real_value),
        ).fetchone()
        return None if row is None else row[0]

    def tokens(self) -> list[str]:
        """All alias tokens (non-sensitive) currently issued."""
        return [r[0] for r in self._conn.execute(
            "SELECT alias_token FROM alias_mapping ORDER BY alias_token"
        )]

    def __len__(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM alias_mapping").fetchone()[0]


class Store:
    """A handle on the local encrypted store. Use :func:`open_store` to obtain
    one. Supports use as a context manager."""

    def __init__(self, conn, path: Path) -> None:
        self._conn = conn
        self._path = path
        self._mapping = MappingTable(conn)

    # -- properties -----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def mapping(self) -> MappingTable:
        return self._mapping

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # -- raw transactions (artifact 1) ---------------------------------------

    def add_raw_transaction(
        self,
        *,
        account_id: str,
        posted_date: str,
        amount_cents: int,
        raw_payee: str | None = None,
        raw_memo: str | None = None,
        currency: str = "USD",
        source_file: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO raw_transactions "
            "(account_id, posted_date, amount_cents, currency, raw_payee, raw_memo, "
            " source_file, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, posted_date, amount_cents, currency, raw_payee, raw_memo,
             source_file, _utcnow_iso()),
        )
        self._conn.commit()
        return cur.lastrowid

    def raw_transactions(self) -> Iterator[dict]:
        cur = self._conn.execute(
            "SELECT id, account_id, posted_date, amount_cents, currency, raw_payee, "
            "raw_memo, source_file, ingested_at FROM raw_transactions ORDER BY id"
        )
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            yield dict(zip(cols, row, strict=True))

    # -- lifecycle ------------------------------------------------------------

    def migrate(self) -> None:
        """Bring the schema up to :data:`SCHEMA_VERSION`. Idempotent."""
        current = self.schema_version
        if current > SCHEMA_VERSION:
            raise MigrationError(
                f"store schema version {current} is newer than supported "
                f"version {SCHEMA_VERSION}; upgrade redactor"
            )
        for version in range(current, SCHEMA_VERSION):
            _MIGRATIONS[version](self._conn)
            # PRAGMA cannot bind parameters; version is a trusted int.
            self._conn.execute(f"PRAGMA user_version = {version + 1}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# -- migrations ---------------------------------------------------------------


def _migrate_0_to_1(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE raw_transactions (
            id           INTEGER PRIMARY KEY,
            account_id   TEXT NOT NULL,
            posted_date  TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            currency     TEXT NOT NULL DEFAULT 'USD',
            raw_payee    TEXT,
            raw_memo     TEXT,
            source_file  TEXT,
            ingested_at  TEXT NOT NULL
        );

        -- Artifact 2: the crown jewel. alias_token <-> real_value.
        CREATE TABLE alias_mapping (
            id          INTEGER PRIMARY KEY,
            alias_token TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL,
            real_value  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE (entity_type, real_value)
        );
        """
    )


def _migrate_1_to_2(conn) -> None:
    # The durable per-class issuance counter for stable aliasing (S1.2). One row
    # per entity class; next_n only ever increases, so alias numbers are never
    # reused even across deletions (alias-contract §2.3). Kept separate from
    # alias_mapping so the counter survives independently of the current rows.
    conn.executescript(
        """
        CREATE TABLE alias_sequence (
            entity_type TEXT PRIMARY KEY,
            next_n      INTEGER NOT NULL
        );
        """
    )


_MIGRATIONS = [_migrate_0_to_1, _migrate_1_to_2]


# -- open ---------------------------------------------------------------------


def open_store(path: PathLike, key: str, *, create: bool = True) -> Store:
    """Open (or create) the encrypted local store at ``path``.

    Args:
        path: filesystem path to the store file.
        key: the encryption passphrase. The store file is unreadable without it.
        create: if ``True`` (default), a missing store is created and migrated
            to the current schema. If ``False``, a missing store raises
            :class:`StoreError`.

    Raises:
        StoreError: the store is missing and ``create`` is ``False``.
        BadKeyError: the store exists but the key does not decrypt it.
    """
    if not key:
        raise StoreError("an encryption key is required; the store is never plaintext")

    p = Path(path)
    exists = p.exists() and p.stat().st_size > 0
    if not exists and not create:
        raise StoreError(f"store does not exist: {p}")

    conn = _sqlcipher.connect(str(p))
    conn.execute(f"PRAGMA key = '{_escape_key(key)}'")
    conn.execute("PRAGMA foreign_keys = ON")

    # Force a decrypt: with a wrong key this raises; with the right key (or a
    # freshly created store) it succeeds and binds the key.
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except _sqlcipher.DatabaseError as exc:
        conn.close()
        raise BadKeyError(f"cannot open store {p}: wrong key or corrupt file") from exc

    store = Store(conn, p)
    store.migrate()
    return store
