"""The ``redactor`` command-line interface.

Two subcommands today:

  * ``lens`` — the inbound re-substitution of story S2.2. It turns a model's
    alias-space text back into plain language for a human to read, and does so
    **only where the mapping table lives**: it opens the local encrypted store
    with ``create=False`` and refuses (non-zero exit, nothing rendered) when no
    decryptable table is present. That refusal is the security property, not an
    ergonomic nicety — un-redaction off-machine would defeat the whole gateway
    (docs/brief.md §"Un-redaction mechanism").
  * ``ingest`` — the batch statement importer of story S0.3. It runs the
    tolerant loaders (:mod:`redactor.loaders`) over one or more real bank
    exports, writes raw rows into the local store, and prints a per-file summary
    that is **alias-space only**: counts and a lint verdict, never a raw memo or
    account id. Parse failures are reported value-free (row + field name); the
    offending cell is shown only behind ``--show-raw``.

Real values only ever go to stdout for ``lens``, the local render surface.
Everywhere else stdout carries alias-space text only; errors and summaries the
user might copy-paste stay value-free.
"""
from __future__ import annotations

import argparse
import os
import sys

from redactor.fakeness import scan_text
from redactor.ingest import ingest_statement
from redactor.lens import lens, resolver_from_table
from redactor.loaders import LoadError, load_statement_file
from redactor.projection import AliasSpaceProjection
from redactor.store import StoreError, open_store

# Env var checked when --key is not passed, so the passphrase need not appear in
# shell history or the process table.
KEY_ENV = "REDACTOR_KEY"


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


def _resolve_key(args: argparse.Namespace) -> str | None:
    return args.key if args.key is not None else os.environ.get(KEY_ENV)


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
    key = _resolve_key(args)
    if not key:
        print(
            f"redactor ingest: an encryption key is required "
            f"(pass --key or set ${KEY_ENV}); the store is never plaintext.",
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
