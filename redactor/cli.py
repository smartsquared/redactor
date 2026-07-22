"""The ``redactor`` command-line interface.

Currently exposes one subcommand, ``lens`` — the inbound re-substitution of
story S2.2. The lens turns a model's alias-space text back into plain language
for a human to read, and it does so **only where the mapping table lives**: it
opens the local encrypted store with ``create=False`` and refuses (non-zero
exit, nothing rendered) when no decryptable table is present. That refusal is
the security property, not an ergonomic nicety — un-redaction off-machine would
defeat the whole gateway (docs/brief.md §"Un-redaction mechanism").

Real values only ever go to stdout, the local render surface. Errors and the
optional substitution summary go to stderr, so stdout stays a clean, pipeable
stream of rendered text.
"""
from __future__ import annotations

import argparse
import os
import sys

from redactor.lens import lens, resolver_from_table
from redactor.lint import format_findings, lint_file
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


def _cmd_lint(args: argparse.Namespace) -> int:
    """Detection-based leak-lint of an alias-space artifact (story S0.2).

    Needs no key and no store: it scans text and structure for real-looking
    identifiers, knowing only the *shapes* a real identifier takes — never a
    mapping-table binding. Exit 0 = clean, 1 = findings. Findings echo the
    offending substrings (that is the point of a lint), so they go to stderr and
    stay on the local render surface; stdout carries only the clean verdict.
    """
    try:
        findings = lint_file(args.file)
    except OSError as exc:
        print(f"redactor lint: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    if findings:
        print(format_findings(findings), file=sys.stderr)
        return 1
    print(format_findings(findings))
    return 0


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

    lint_p = sub.add_parser(
        "lint",
        help="Detection-based leak-lint: fail if an alias-space artifact carries "
        "any real-looking identifier.",
        description=(
            "Scan a file for real-looking identifiers (checksum-valid routing / "
            "card numbers, SSNs, account-id patterns) and for alias-contract "
            "conformance (identifying fields must be canonical tokens). Zero "
            "findings = clean (exit 0); any finding exits 1. Needs no key or "
            "store — it reads only text, never the mapping table."
        ),
    )
    lint_p.add_argument(
        "file",
        metavar="FILE",
        help="Path to the alias-space artifact (projection JSON, report, ...) to lint.",
    )
    lint_p.set_defaults(func=_cmd_lint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
