"""The ``redactor`` command-line interface.

Subcommands:

  * ``init`` — create the encrypted store and custody a generated high-entropy
    key in the OS keychain (story S0.1, ADR-0001 "Key custody").
  * ``status`` — report store location / schema version / key availability
    without touching data.
  * ``lens`` — inbound re-substitution (story S2.2): render a model's alias-space
    text back to plain language, **only where the mapping table lives**. It
    opens the store with ``create=False`` and refuses (non-zero exit, nothing
    rendered) when no decryptable table is present — un-redaction off-machine
    would defeat the whole gateway (docs/brief.md §"Un-redaction mechanism").
  * ``ingest`` — the batch statement importer of story S0.3. It runs the
    tolerant loaders (:mod:`redactor.loaders`) over one or more real bank
    exports, writes raw rows into the local store, and prints a per-file summary
    that is **alias-space only**: counts and a lint verdict, never a raw memo or
    account id. Parse failures are reported value-free (row + field name); the
    offending cell is shown only behind ``--show-raw``.

Key custody, in priority order, is shared across subcommands (:func:`_resolve_key`):
``--key`` > ``$REDACTOR_KEY`` > the OS keychain. The first two are CI/test
overrides; the keychain is the human default so no passphrase lives in shell
history or the process environment. **No subcommand ever prints or logs the key.**

Real values only ever go to stdout for ``lens``, the local render surface.
Everywhere else stdout carries alias-space text only; errors and summaries the
user might copy-paste stay value-free.
"""
from __future__ import annotations

import argparse
import os
import sys

from redactor import keychain
from redactor.fakeness import scan_text
from redactor.ingest import ingest_statement
from redactor.lens import lens, resolver_from_table
from redactor.loaders import LoadError, load_statement_file
from redactor.projection import AliasSpaceProjection
from redactor.store import BadKeyError, StoreError, open_store

# Env var checked when --key is not passed, so the passphrase need not appear in
# shell history or the process table.
KEY_ENV = "REDACTOR_KEY"


def _override_key(args: argparse.Namespace) -> str | None:
    """The explicit CI/test key override: ``--key`` then ``$REDACTOR_KEY``.

    Returns ``None`` when neither is set, meaning "fall back to the keychain".
    """
    return args.key if args.key is not None else os.environ.get(KEY_ENV)


def _resolve_key(args: argparse.Namespace) -> str | None:
    """Resolve the store key: override first, then the OS keychain.

    Returns ``None`` if no key can be found anywhere (the caller turns that into
    an actionable message). A :class:`keychain.KeychainUnavailable` from the
    backend propagates — callers report it rather than silently continuing.
    """
    override = _override_key(args)
    if override:
        return override
    return keychain.get_key(args.store)


# -- init ---------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    from pathlib import Path

    store_path = Path(args.store)
    if store_path.exists() and store_path.stat().st_size > 0:
        print(
            f"redactor init: a store already exists at {store_path}; refusing to "
            f"overwrite it. Remove it first, or run `redactor status --store "
            f"{store_path}` to inspect it.",
            file=sys.stderr,
        )
        return 1

    override = _override_key(args)
    if override:
        # An explicit key is a CI/test override: use it, but do not write it to
        # the keychain — custody is for the human default, not for ephemeral
        # test/CI keys.
        key = override
        custodied = False
    else:
        # Human default: mint a high-entropy key and custody it in the keychain.
        # A key never displayed and never typed cannot leak through history.
        key = keychain.generate_key()
        try:
            keychain.set_key(store_path, key)
        except keychain.KeychainUnavailable as exc:
            print(f"redactor init: {exc}", file=sys.stderr)
            return 2
        custodied = True

    try:
        open_store(store_path, key, create=True).close()
    except StoreError as exc:
        # Roll back custody so a failed init leaves nothing half-built behind.
        if not override:
            keychain.delete_key(store_path)
        print(f"redactor init: could not create store: {exc}", file=sys.stderr)
        return 1

    where = "the OS keychain" if custodied else f"the supplied override (${KEY_ENV}/--key)"
    print(
        f"redactor init: created encrypted store at {store_path}; key custodied in {where}."
    )
    return 0


# -- status -------------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    from pathlib import Path

    store_path = Path(args.store)
    print(f"store:  {store_path}")

    if not (store_path.exists() and store_path.stat().st_size > 0):
        print("state:  not initialized")
        print(
            f"redactor status: no store at {store_path}. Run `redactor init "
            f"--store {store_path}` to create one.",
            file=sys.stderr,
        )
        return 1

    try:
        key = _resolve_key(args)
    except keychain.KeychainUnavailable as exc:
        print("key:    unavailable")
        print(f"redactor status: {exc}", file=sys.stderr)
        return 2

    if not key:
        print("key:    not found")
        print(
            f"redactor status: no key for {store_path} in the keychain, and no "
            f"--key/${KEY_ENV} override. The store cannot be opened without it.",
            file=sys.stderr,
        )
        return 1

    key_source = "override" if _override_key(args) else "keychain"
    try:
        with open_store(store_path, key, create=False) as store:
            schema = store.schema_version
    except BadKeyError:
        print(f"key:    present ({key_source}) but does not decrypt the store")
        print(
            f"redactor status: the {key_source} key does not decrypt {store_path} "
            f"(wrong key or corrupt file).",
            file=sys.stderr,
        )
        return 1
    except StoreError as exc:
        print(f"redactor status: cannot open store: {exc}", file=sys.stderr)
        return 1

    print(f"schema: v{schema}")
    print(f"key:    available ({key_source})")
    print("state:  ready")
    return 0


def _cmd_lens(args: argparse.Namespace) -> int:
    key = args.key if args.key is not None else os.environ.get(KEY_ENV)
    if not key:
        print(
            f"redactor lens: an encryption key is required "
            f"(pass --key or set ${KEY_ENV}); the mapping table is never plaintext.",
            file=sys.stderr,
        )
        return 2

    # create=False is the teeth: a missing table means this machine cannot
    # un-redact, so the lens must refuse rather than echo the aliases back.
    try:
        store = open_store(args.store, key, create=False)
    except StoreError as exc:
        print(f"redactor lens: cannot open mapping table: {exc}", file=sys.stderr)
        return 1

    try:
        text = args.text if args.text is not None else sys.stdin.read()
        result = lens(text, resolver_from_table(store.mapping))
    finally:
        store.close()

    sys.stdout.write(result.text)
    if not result.text.endswith("\n"):
        sys.stdout.write("\n")
    if args.report:
        print(
            f"redactor lens: substituted {len(result.substituted)} token(s): "
            f"{', '.join(result.substituted) or '(none)'}",
            file=sys.stderr,
        )
    return 0


def _parse_map(specs: list[str] | None) -> dict[str, str]:
    """Parse ``--map`` specs into a ``{field: header}`` dict.

    Accepts comma-separated ``field=Header`` pairs and repeated ``--map`` flags.
    Header values may themselves contain no ``=``; a field with no value is an
    error.
    """
    mapping: dict[str, str] = {}
    for spec in specs or []:
        for pair in spec.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise ValueError(f"--map expects field=Header, got {pair!r}")
            field, header = pair.split("=", 1)
            field = field.strip()
            header = header.strip()
            if not field or not header:
                raise ValueError(f"--map expects field=Header, got {pair!r}")
            mapping[field] = header
    return mapping


def _lint_verdict(projection: AliasSpaceProjection) -> str:
    """A short, alias-space lint verdict for a projection.

    This is the S0.3 seam onto S0.2's detection-based ``redactor lint``: for now
    it runs the structural fake-ness checks (ABA/Luhn/SSN) over the serialized
    alias-space projection — the checks that need no seed manifest and so apply
    to real data. A clean projection carries only ``CLASS-n`` tokens, amounts,
    and dates, so this reports ``clean``. Findings are summarized by *kind and
    count only* — never the matched substring, which could be a real identifier.
    """
    findings = scan_text(projection.to_json())
    if not findings:
        return "clean"
    kinds = sorted({f.kind for f in findings})
    return f"{len(findings)} finding(s) [{', '.join(kinds)}]"


def _cmd_ingest(args: argparse.Namespace) -> int:
    try:
        key = _resolve_key(args)
    except keychain.KeychainUnavailable as exc:
        print(f"redactor ingest: {exc}", file=sys.stderr)
        return 2
    if not key:
        print(
            f"redactor ingest: an encryption key is required "
            f"(pass --key, set ${KEY_ENV}, or `redactor init` to custody one in "
            f"the keychain); the store is never plaintext.",
            file=sys.stderr,
        )
        return 2

    try:
        column_map = _parse_map(args.map)
    except ValueError as exc:
        print(f"redactor ingest: {exc}", file=sys.stderr)
        return 2

    # Ingest writes raw rows, so the store is opened/created for writing. S0.1's
    # `redactor init` is the human's normal way to create it; create=True here
    # keeps a bare `ingest` usable and is append-safe (aliases stay stable).
    try:
        store = open_store(args.store, key, create=True)
    except StoreError as exc:
        print(f"redactor ingest: cannot open store: {exc}", file=sys.stderr)
        return 1

    failures = 0
    try:
        for raw_path in args.files:
            before = set(store.mapping.tokens())
            try:
                statement = load_statement_file(raw_path, column_map=column_map or None)
                records = ingest_statement(store, statement)
            except LoadError as exc:
                failures += 1
                # Value-free by default; the offending cell only behind --show-raw.
                print(
                    f"redactor ingest: FAILED {exc.render(show_raw=args.show_raw)}",
                    file=sys.stderr,
                )
                continue

            after = set(store.mapping.tokens())
            new_tokens = after - before
            new_aliases = len(new_tokens)
            new_payees = sum(1 for t in new_tokens if t.upper().startswith("PAYEE-"))
            verdict = _lint_verdict(AliasSpaceProjection(records))

            # Alias-space only: the file's basename, counts, and a lint verdict.
            print(
                f"{_basename(raw_path)}: {len(records)} rows, "
                f"{new_payees} new payees, {new_aliases} aliases issued, "
                f"lint: {verdict}"
            )
    finally:
        store.close()

    return 1 if failures else 0


def _basename(path: str) -> str:
    return os.path.basename(str(path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="redactor",
        description="redactor — a privacy gateway (bidirectional alias proxy).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser(
        "init",
        help="Create the encrypted store and custody a generated key in the OS "
        "keychain.",
        description=(
            "Create the local encrypted store and, by default, generate a "
            "high-entropy key custodied in the OS keychain — no passphrase to "
            "remember or leak. Pass --key or set $REDACTOR_KEY to supply a key "
            "explicitly (CI/tests); an override is never written to the keychain."
        ),
    )
    init_p.add_argument(
        "--store",
        required=True,
        metavar="PATH",
        help="Path to the local encrypted store to create.",
    )
    init_p.add_argument(
        "--key",
        default=None,
        help=f"Key override (CI/tests). Defaults to ${KEY_ENV}, else the OS keychain.",
    )
    init_p.set_defaults(func=_cmd_init)

    status_p = sub.add_parser(
        "status",
        help="Report store location, schema version, and key availability.",
        description=(
            "Report the store's location, schema version, and whether a key is "
            "available to open it — without reading any data. Key resolution is "
            "--key, then $REDACTOR_KEY, then the OS keychain."
        ),
    )
    status_p.add_argument(
        "--store",
        required=True,
        metavar="PATH",
        help="Path to the local encrypted store to inspect.",
    )
    status_p.add_argument(
        "--key",
        default=None,
        help=f"Key override (CI/tests). Defaults to ${KEY_ENV}, else the OS keychain.",
    )
    status_p.set_defaults(func=_cmd_status)

    lens_p = sub.add_parser(
        "lens",
        help="Inbound re-substitution: render alias-space text back to real "
        "values, only where the local mapping table lives.",
        description=(
            "Substitute alias tokens (PAYEE-7, ACCT-1, ...) back to their real "
            "values, mangle-tolerantly. Runs only on the machine holding the "
            "encrypted mapping table; refuses otherwise."
        ),
    )
    lens_p.add_argument(
        "--store",
        required=True,
        metavar="PATH",
        help="Path to the local encrypted store holding the mapping table.",
    )
    lens_p.add_argument(
        "--key",
        default=None,
        help=f"Store passphrase. Defaults to the ${KEY_ENV} environment variable.",
    )
    lens_p.add_argument(
        "--text",
        default=None,
        help="Text to render. If omitted, text is read from stdin.",
    )
    lens_p.add_argument(
        "--report",
        action="store_true",
        help="Print a count of substituted tokens to stderr.",
    )
    lens_p.set_defaults(func=_cmd_lens)

    ingest_p = sub.add_parser(
        "ingest",
        help="Batch-ingest real bank exports (CSV/OFX) into the local store; "
        "print an alias-space per-file summary.",
        description=(
            "Load one or more statement files with the tolerant loaders "
            "(header-mapping heuristics, BOM/encoding tolerance, MM/DD/YYYY "
            "dates, OFX 1.x/2.x), write raw rows into the local encrypted store, "
            "and print a per-file summary (rows, new payees, aliases issued, "
            "lint verdict). Output is alias-space only; parse errors are "
            "value-free unless --show-raw is passed."
        ),
    )
    ingest_p.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="Statement files to ingest (CSV or OFX).",
    )
    ingest_p.add_argument(
        "--store",
        required=True,
        metavar="PATH",
        help="Path to the local encrypted store (created if absent).",
    )
    ingest_p.add_argument(
        "--key",
        default=None,
        help=f"Store passphrase. Defaults to the ${KEY_ENV} environment variable.",
    )
    ingest_p.add_argument(
        "--map",
        action="append",
        default=None,
        metavar="FIELD=HEADER",
        help="Explicit CSV column mapping, e.g. --map date=Posted,amount=Amt. "
        "Repeatable. Overrides the header heuristics for the named fields.",
    )
    ingest_p.add_argument(
        "--show-raw",
        action="store_true",
        help="Include the offending raw cell value in parse errors. Off by "
        "default so error output is safe to copy-paste.",
    )
    ingest_p.set_defaults(func=_cmd_ingest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
