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
SCHEMA_VERSION = 5

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
        """Return the real value for an alias token, sealed, or ``None``.

        Follows the merge redirect (``merged_into``): a loser token folded into a
        winner by :meth:`set_merged_into` resolves to the *winner's* real value,
        so an already-issued alias keeps un-redacting correctly after a payee
        merge (story S1.1) — alias stability across a merge, never a dangling
        token. A defensive cycle guard keeps a corrupt chain from looping.
        """
        seen: set[str] = set()
        token = alias_token
        while True:
            row = self._conn.execute(
                "SELECT real_value, merged_into FROM alias_mapping WHERE alias_token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            real_value, merged_into = row
            if merged_into is None or merged_into in seen:
                return Sealed(real_value)
            seen.add(token)
            token = merged_into

    def canonical_head(self, alias_token: str) -> str | None:
        """The canonical (un-merged) token *alias_token* folds into, or ``None``.

        Walks the ``merged_into`` chain to the entity that actually holds the
        real value. A token that was never merged is its own head."""
        seen: set[str] = set()
        token = alias_token
        while True:
            row = self._conn.execute(
                "SELECT merged_into FROM alias_mapping WHERE alias_token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            merged_into = row[0]
            if merged_into is None or merged_into in seen:
                return token
            seen.add(token)
            token = merged_into

    def merged_into(self, alias_token: str) -> str | None:
        """The token *alias_token* was directly merged into, or ``None``."""
        row = self._conn.execute(
            "SELECT merged_into FROM alias_mapping WHERE alias_token = ?",
            (alias_token,),
        ).fetchone()
        return None if row is None else row[0]

    def set_merged_into(self, loser: str, winner: str) -> None:
        """Fold *loser* into *winner* (story S1.1 merge): the loser keeps its own
        token — never renumbered — but now resolves to the winner's real value."""
        self._conn.execute(
            "UPDATE alias_mapping SET merged_into = ? WHERE alias_token = ?",
            (winner, loser),
        )
        self._conn.commit()

    def clear_merged_into(self, alias_token: str) -> None:
        """Un-fold *alias_token* (story S1.1 split): it becomes its own entity
        again, resolving to its own real value."""
        self._conn.execute(
            "UPDATE alias_mapping SET merged_into = NULL WHERE alias_token = ?",
            (alias_token,),
        )
        self._conn.commit()

    def rename_value(self, alias_token: str, real_value: str) -> None:
        """Change an entity's real value (story S1.1 rename), token unchanged.

        Raises ``ValueError`` if *alias_token* is unknown, or if *real_value* is
        already bound to a different entity of the same class — that collision
        would be a merge, not a rename (the mapping keys on the real value), so
        it is refused rather than silently coalescing two payees.
        """
        row = self._conn.execute(
            "SELECT entity_type FROM alias_mapping WHERE alias_token = ?",
            (alias_token,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown alias token: {alias_token!r}")
        try:
            self._conn.execute(
                "UPDATE alias_mapping SET real_value = ? WHERE alias_token = ?",
                (real_value, alias_token),
            )
        except _sqlcipher.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError(
                f"cannot rename {alias_token}: that label is already used by "
                f"another entity (use merge to combine them)"
            ) from exc
        self._conn.commit()

    def real_value_of(self, alias_token: str) -> Sealed | None:
        """This token's *own* real value (not following any merge), sealed."""
        row = self._conn.execute(
            "SELECT real_value FROM alias_mapping WHERE alias_token = ?",
            (alias_token,),
        ).fetchone()
        return None if row is None else Sealed(row[0])

    def canonical_tokens(self, entity_type: str) -> list[str]:
        """Every un-merged (canonical) token of a class — the review entities."""
        return [r[0] for r in self._conn.execute(
            "SELECT alias_token FROM alias_mapping "
            "WHERE entity_type = ? AND merged_into IS NULL "
            "ORDER BY alias_token",
            (entity_type.upper(),),
        )]

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
        institution: str | None = None,
    ) -> int:
        # ``institution`` is the account's institution name (a real value, so it
        # lives only here in the encrypted store). It records the account ->
        # institution link that ``redactor export`` needs to reconstruct a
        # projection's INST token later, offline, from the store alone — the raw
        # rows otherwise carry no institution (issue #41).
        cur = self._conn.execute(
            "INSERT INTO raw_transactions "
            "(account_id, posted_date, amount_cents, currency, raw_payee, raw_memo, "
            " source_file, institution, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, posted_date, amount_cents, currency, raw_payee, raw_memo,
             source_file, institution, _utcnow_iso()),
        )
        self._conn.commit()
        return cur.lastrowid

    def raw_transactions(self) -> Iterator[dict]:
        cur = self._conn.execute(
            "SELECT id, account_id, posted_date, amount_cents, currency, raw_payee, "
            "raw_memo, source_file, institution, ingested_at FROM raw_transactions ORDER BY id"
        )
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            yield dict(zip(cols, row, strict=True))

    # -- payee entity resolution (artifact 2 support, story S1.1) -------------
    #
    # The variant table records, per distinct memo string, which PAYEE entity it
    # was resolved onto and with what confidence. It holds real memo strings, so
    # it lives here in the encrypted store and never leaves it (like the mapping
    # itself). It is what the review flow counts variants and confidence from,
    # and what makes resolution stable across sessions: a known variant reuses
    # its recorded token instead of being re-guessed.

    def add_payee_variant(self, variant: str, alias_token: str, confidence: float) -> None:
        """Record a memo *variant* under a PAYEE token, first assignment wins.

        Idempotent: re-recording the same variant leaves its original token and
        confidence untouched (stability). Regrouping is an explicit mutation
        (:meth:`reassign_payee_variant`), never a silent side effect of ingest."""
        self._conn.execute(
            "INSERT INTO payee_variant (variant, alias_token, confidence, recorded_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(variant) DO NOTHING",
            (variant, alias_token, confidence, _utcnow_iso()),
        )
        self._conn.commit()

    def payee_variant(self, variant: str) -> tuple[str, float] | None:
        """The (token, confidence) a memo *variant* is recorded under, or ``None``."""
        row = self._conn.execute(
            "SELECT alias_token, confidence FROM payee_variant WHERE variant = ?",
            (variant,),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def payee_variants(self) -> list[tuple[str, str, float]]:
        """Every recorded ``(variant, token, confidence)``, in insertion order.

        Insertion order reproduces the original grouping order, so a fresh
        session can reseed the resolver deterministically."""
        return [
            (r[0], r[1], r[2])
            for r in self._conn.execute(
                "SELECT variant, alias_token, confidence FROM payee_variant ORDER BY id"
            )
        ]

    def reassign_payee_variant(self, variant: str, alias_token: str) -> None:
        """Move a memo *variant* onto a different PAYEE token (split regrouping)."""
        self._conn.execute(
            "UPDATE payee_variant SET alias_token = ? WHERE variant = ?",
            (alias_token, variant),
        )
        self._conn.commit()

    # -- payee categories (alias-space metadata) ------------------------------
    #
    # One closed-vocabulary tag per canonical PAYEE token (docs/
    # payee-categories.md). Alias-space by construction — a token and a
    # vocabulary member, never a real value — but it lives here with the rest of
    # the payee state so tagging and grouping stay in one transaction domain.
    # Vocabulary validation is the registry's job (redactor.categories); the
    # store persists what it is given.

    def set_payee_category(self, alias_token: str, category: str) -> None:
        """Bind *category* to a PAYEE token, replacing any earlier tag."""
        self._conn.execute(
            "INSERT INTO payee_category (alias_token, category, recorded_at) "
            "VALUES (?, ?, ?) ON CONFLICT(alias_token) DO UPDATE SET "
            "category = excluded.category, recorded_at = excluded.recorded_at",
            (alias_token, category, _utcnow_iso()),
        )
        self._conn.commit()

    def clear_payee_category(self, alias_token: str) -> None:
        """Remove a PAYEE token's tag. Idempotent — clearing an untagged token
        is a no-op."""
        self._conn.execute(
            "DELETE FROM payee_category WHERE alias_token = ?", (alias_token,)
        )
        self._conn.commit()

    def payee_category(self, alias_token: str) -> str | None:
        """The category tagged onto a PAYEE token, or ``None`` when untagged."""
        row = self._conn.execute(
            "SELECT category FROM payee_category WHERE alias_token = ?",
            (alias_token,),
        ).fetchone()
        return None if row is None else row[0]

    def payee_categories(self) -> dict[str, str]:
        """Every ``token -> category`` binding currently recorded."""
        return dict(
            self._conn.execute("SELECT alias_token, category FROM payee_category")
        )

    def add_payee_journal(
        self, op: str, *, winner: str | None = None, loser: str | None = None,
        note: str | None = None,
    ) -> None:
        """Append an auditable payee-mutation record (merge / split / rename).

        Alias-space by construction: it records the *tokens* involved and the
        operation, never a raw memo or real display value."""
        self._conn.execute(
            "INSERT INTO payee_journal (op, winner, loser, note, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (op, winner, loser, note, _utcnow_iso()),
        )
        self._conn.commit()

    def payee_journal(self) -> list[dict]:
        """The payee-mutation journal, oldest first (auditable history)."""
        cur = self._conn.execute(
            "SELECT id, op, winner, loser, note, recorded_at FROM payee_journal ORDER BY id"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

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


def _migrate_2_to_3(conn) -> None:
    # Story S1.1 — entity-resolution review flow.
    #
    #  * alias_mapping.merged_into: a payee merge folds a loser token into a
    #    winner without renumbering (alias stability). NULL = canonical entity.
    #  * payee_variant: per-memo grouping + confidence — what review counts and
    #    what makes resolution stable across sessions (real memo strings, so it
    #    stays in the encrypted store, never serialized off-machine).
    #  * payee_journal: an auditable log of every merge / split / rename. Records
    #    tokens and the operation only — alias-space, no real value.
    conn.executescript(
        """
        ALTER TABLE alias_mapping ADD COLUMN merged_into TEXT;

        CREATE TABLE payee_variant (
            id          INTEGER PRIMARY KEY,
            variant     TEXT NOT NULL UNIQUE,
            alias_token TEXT NOT NULL,
            confidence  REAL NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE INDEX idx_payee_variant_token ON payee_variant (alias_token);

        CREATE TABLE payee_journal (
            id          INTEGER PRIMARY KEY,
            op          TEXT NOT NULL,
            winner      TEXT,
            loser       TEXT,
            note        TEXT,
            recorded_at TEXT NOT NULL
        );
        """
    )


def _migrate_3_to_4(conn) -> None:
    # Issue #41 — `redactor export` reconstructs a month's alias-space projection
    # from the store alone. The projection's INST token per row needs the account
    # -> institution link, which the raw rows did not persist. Add it here. Old
    # rows keep NULL (they predate export); re-ingest populates it.
    conn.executescript(
        """
        ALTER TABLE raw_transactions ADD COLUMN institution TEXT;
        """
    )


def _migrate_4_to_5(conn) -> None:
    # Entity-level payee category tags (docs/payee-categories.md). One
    # closed-vocabulary tag per canonical PAYEE token — the local eligibility
    # verdict that rides into projections as `payee_category`. Alias-space only
    # (token + vocabulary member); the crown jewel gains nothing new.
    conn.executescript(
        """
        CREATE TABLE payee_category (
            alias_token TEXT PRIMARY KEY,
            category    TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        """
    )


_MIGRATIONS = [
    _migrate_0_to_1, _migrate_1_to_2, _migrate_2_to_3, _migrate_3_to_4, _migrate_4_to_5,
]


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
