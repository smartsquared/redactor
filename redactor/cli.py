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
  * ``export`` — the lint-attested projection writer (issue #41). It rebuilds a
    month's alias-space projection from the store, runs the S0.2 detection lint,
    and writes the projection file with a ``provenance`` attestation block **only
    when the lint is green** — the artifact ``sakuma-finance import --projection``
    consumes. A red projection writes nothing and exits non-zero; the summary
    (month, row count, verdict) is alias-space only.

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
from pathlib import Path

from redactor import keychain
from redactor.export import export_month, store_months
from redactor.fakeness import scan_text
from redactor.ingest import ingest_statement
from redactor.lens import lens, resolver_from_table
from redactor.lint import format_findings, lint_file
from redactor.loaders import LoadError, load_statement_file
from redactor.payees import PayeeRegistry
from redactor.projection import AliasSpaceProjection
from redactor.store import BadKeyError, StoreError, open_store

# Env var checked when --key is not passed, so the passphrase need not appear in
# shell history or the process table.
KEY_ENV = "REDACTOR_KEY"

# ANSI red, used only to make a local-terminal warning loud. This is the user's
# own machine — never an off-machine artifact — so a control code here does not
# cross the privacy boundary.
_RED = "\x1b[31m"
_RESET = "\x1b[0m"


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


# A payee-per-row sanity floor (issue #37). Below roughly one distinct payee per
# _COLLAPSE_ROWS_PER_PAYEE rows, on a statement of at least _COLLAPSE_MIN_ROWS
# rows, entity resolution has very likely over-merged (the failure that folded a
# real 9,717-row statement into a single payee). A short statement legitimately
# has few payees, so the row floor keeps this from crying wolf on small files.
_COLLAPSE_MIN_ROWS = 100
_COLLAPSE_ROWS_PER_PAYEE = 100


def _collapse_warning(rows: int, distinct_payees: int) -> str | None:
    """A loud, alias-space payee-collapse warning for the ingest summary, or None.

    Compares the distinct-payee-to-row ratio against a sanity floor so a runaway
    entity-resolution merge is visible in the summary itself — before the review
    flow runs. Returns counts and a ratio only; never a memo or a display label,
    so it is safe on a terminal that might be copy-pasted."""
    if rows < _COLLAPSE_MIN_ROWS or distinct_payees == 0:
        return None
    if rows / distinct_payees < _COLLAPSE_ROWS_PER_PAYEE:
        return None
    ratio = distinct_payees / rows
    return (
        f"{_RED}redactor: WARNING — payee collapse suspected: only "
        f"{distinct_payees} distinct payee(s) for {rows} rows (ratio {ratio:.4f}). "
        f"Entity resolution may have over-merged; run "
        f"`redactor payees --review` before trusting this ingest.{_RESET}"
    )


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

    # One registry for the whole batch: it seeds from the store once and groups
    # payee variants across every file, so low-confidence groupings are surfaced
    # coherently at the end (story S1.1).
    registry = PayeeRegistry(store)

    failures = 0
    try:
        for raw_path in args.files:
            before = set(store.mapping.tokens())
            try:
                statement = load_statement_file(raw_path, column_map=column_map or None)
                records = ingest_statement(store, statement, registry=registry)
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

            # Payee-per-row sanity check (issue #37): counts the *distinct* payee
            # tokens this file's rows resolved to — new or reused — so a runaway
            # entity-resolution merge is loud in the summary even on a re-ingest
            # that issues no new aliases, before the review flow is ever run.
            distinct_payees = len({r.payee for r in records})
            warning = _collapse_warning(len(records), distinct_payees)
            if warning is not None:
                print(warning, file=sys.stderr)

        # Flag low-confidence payee groupings for review (story S1.1). This is
        # alias-space only — tokens and a confidence number, never a raw memo or
        # display label — so it is safe to copy-paste. The human resolves them
        # locally with `redactor payees --review`.
        flagged = registry.flagged()
        if flagged:
            print(
                f"review: {len(flagged)} low-confidence payee grouping(s) flagged "
                f"— run `redactor payees --review` to confirm:"
            )
            for token, confidence in flagged:
                print(f"  {token}: confidence {confidence:.2f}")
    finally:
        store.close()

    return 1 if failures else 0


def _export_one(store, month: str, out_path, *, quiet: bool = False) -> int:
    """Export a single month and print an alias-space summary. Returns 0/1.

    The summary line (month, row count, verdict, and the path written) is
    alias-space only, so it goes to stdout. A red verdict's findings echo real
    substrings by necessity (that is the point of a lint), so they go to stderr —
    the local render surface — and nothing is written."""
    result = export_month(store, month, out_path)
    if result.verdict == "green":
        print(
            f"{result.month}: {result.row_count} rows, lint: green "
            f"— wrote {result.path}"
        )
        return 0
    # Red: refuse. Value-free summary to stdout; the offending findings only to
    # stderr, and only if not suppressed by a batch caller doing its own report.
    print(
        f"{result.month}: {result.row_count} rows, lint: red — no file written"
    )
    if not quiet:
        print(
            f"{_RED}redactor export: {result.month} projection FAILED the "
            f"leak-lint; refusing to write.{_RESET}\n"
            f"{format_findings(result.findings)}",
            file=sys.stderr,
        )
    return 1


def _cmd_export(args: argparse.Namespace) -> int:
    """Write lint-attested alias-space projection files from the store (issue #41).

    Builds each month's projection from the store, runs the S0.2 detection lint,
    and writes a provenance-attested file only when the lint is green — the file
    ``sakuma-finance import --projection`` will consume. Output stays alias-space;
    a red month writes nothing and forces a non-zero exit."""
    try:
        key = _resolve_key(args)
    except keychain.KeychainUnavailable as exc:
        print(f"redactor export: {exc}", file=sys.stderr)
        return 2
    if not key:
        print(
            f"redactor export: an encryption key is required "
            f"(pass --key, set ${KEY_ENV}, or `redactor init`); the store is "
            f"never plaintext.",
            file=sys.stderr,
        )
        return 2

    # create=False: export reads an already-ingested store; it never creates one.
    try:
        store = open_store(args.store, key, create=False)
    except BadKeyError:
        print(
            f"redactor export: the key does not decrypt {args.store} "
            f"(wrong key or corrupt file).",
            file=sys.stderr,
        )
        return 1
    except StoreError as exc:
        print(f"redactor export: cannot open store: {exc}", file=sys.stderr)
        return 1

    try:
        if args.all_months:
            outdir = Path(args.out)
            outdir.mkdir(parents=True, exist_ok=True)
            months = store_months(store)
            if not months:
                print("redactor export: no transactions in the store; nothing to export.")
                return 0
            failed = 0
            for month in months:
                out_path = outdir / f"proj-{month}.json"
                failed += _export_one(store, month, out_path)
            return 1 if failed else 0
        return _export_one(store, args.month, args.out)
    finally:
        store.close()


def _cmd_payees(args: argparse.Namespace) -> int:
    """The entity-resolution review flow (story S1.1).

    ``--review`` is a **local render**: like ``lens`` it reveals real values
    (display labels), so it opens the store with ``create=False`` and refuses
    when no decryptable mapping table is present. ``--merge`` / ``--split`` /
    ``--rename`` mutate groupings and labels and print an **alias-space**
    confirmation only — tokens and counts, never a raw memo or display value.
    Every mutation is journaled in the store.
    """
    try:
        key = _resolve_key(args)
    except keychain.KeychainUnavailable as exc:
        print(f"redactor payees: {exc}", file=sys.stderr)
        return 2
    if not key:
        print(
            f"redactor payees: an encryption key is required "
            f"(pass --key, set ${KEY_ENV}, or `redactor init`); the mapping "
            f"table is never plaintext.",
            file=sys.stderr,
        )
        return 2

    # create=False is the teeth (as for `lens`): review reveals real values, so
    # it must run only where the mapping table actually lives.
    try:
        store = open_store(args.store, key, create=False)
    except BadKeyError:
        print(
            f"redactor payees: the key does not decrypt {args.store} "
            f"(wrong key or corrupt file).",
            file=sys.stderr,
        )
        return 1
    except StoreError as exc:
        print(f"redactor payees: cannot open store: {exc}", file=sys.stderr)
        return 1

    try:
        registry = PayeeRegistry(store)
        if args.merge is not None:
            return _payees_merge(registry, args.merge)
        if args.split is not None:
            return _payees_split(registry, args.split)
        if args.rename is not None:
            return _payees_rename(registry, args.rename)
        if args.tag is not None:
            return _payees_tag(registry, args.tag)
        if args.untag is not None:
            return _payees_untag(registry, args.untag)
        if args.categorize:
            return _payees_categorize(registry)
        return _payees_review(registry)
    finally:
        store.close()


def _payees_review(registry: PayeeRegistry) -> int:
    """Render the entity list — the local render boundary (reveals labels)."""
    entities = registry.review()
    if not entities:
        print("redactor payees: no payee entities resolved yet.")
        return 0
    print(f"redactor payees: {len(entities)} payee entit(y/ies):")
    for e in entities:
        flag = "  [REVIEW]" if e.low_confidence else ""
        tag = f"  [{e.category}]" if e.category else ""
        # The display label is a real value: this line is for the local terminal
        # on the machine holding the mapping table only.
        print(
            f"  {e.token}  variants={e.variant_count}  "
            f"confidence={e.confidence:.2f}{flag}{tag}  {e.display}"
        )
    return 0


def _payees_merge(registry: PayeeRegistry, pair: list[str]) -> int:
    winner, loser = pair
    try:
        registry.merge(winner, loser)
    except ValueError as exc:
        print(f"redactor payees: cannot merge: {exc}", file=sys.stderr)
        return 1
    # Alias-space confirmation only.
    print(f"redactor payees: merged {loser} into {winner} (aliases preserved).")
    return 0


def _payees_split(registry: PayeeRegistry, token: str) -> int:
    try:
        registry.split(token)
    except ValueError as exc:
        print(f"redactor payees: cannot split: {exc}", file=sys.stderr)
        return 1
    print(f"redactor payees: split {token} back into its own entity.")
    return 0


def _payees_rename(registry: PayeeRegistry, spec: list[str]) -> int:
    token, label = spec
    try:
        registry.rename(token, label)
    except ValueError as exc:
        print(f"redactor payees: cannot rename: {exc}", file=sys.stderr)
        return 1
    # Alias-space: name the token, not the new label (which is a real value).
    print(f"redactor payees: renamed {token}.")
    return 0


def _payees_tag(registry: PayeeRegistry, spec: list[str]) -> int:
    token, category = spec
    try:
        registry.tag(token, category)
    except ValueError as exc:
        # The refusal echoes only the token/category and the closed vocabulary —
        # value-free, safe on stderr.
        print(f"redactor payees: cannot tag: {exc}", file=sys.stderr)
        return 1
    # Alias-space confirmation: a token and a closed-vocabulary member.
    print(f"redactor payees: tagged {token} as {category}.")
    return 0


def _payees_untag(registry: PayeeRegistry, token: str) -> int:
    try:
        registry.untag(token)
    except ValueError as exc:
        print(f"redactor payees: cannot untag: {exc}", file=sys.stderr)
        return 1
    print(f"redactor payees: untagged {token}.")
    return 0


def _payees_categorize(registry: PayeeRegistry) -> int:
    """Propose categories for untagged entities — a local render (labels).

    Nothing is written: the human confirms each proposal with ``--tag``. Like
    ``--review``, the display labels on these lines are real values for the
    local terminal only."""
    proposals = registry.propose_categories()
    if not proposals:
        print("redactor payees: no category proposals (all matched entities are tagged).")
        return 0
    print(
        f"redactor payees: {len(proposals)} category proposal(s) — confirm each "
        f"with `redactor payees --tag TOKEN CATEGORY`:"
    )
    for p in proposals:
        print(f"  {p.token}  ->  {p.category}  ({p.display})")
    return 0


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

    export_p = sub.add_parser(
        "export",
        help="Write a lint-attested alias-space projection file (the artifact "
        "sakuma-finance import consumes).",
        description=(
            "Rebuild a month's alias-space projection from the store, run the "
            "S0.2 detection lint over it, and — only when the lint is green — "
            "write it out with a provenance attestation block (verdict, "
            "timestamp, linter version) that `sakuma-finance import --projection` "
            "requires. A projection that fails the lint writes nothing, reports "
            "findings to stderr (local render), and exits non-zero. Summary "
            "output is alias-space only (month, row count, verdict)."
        ),
    )
    export_p.add_argument(
        "--store",
        required=True,
        metavar="PATH",
        help="Path to the local encrypted store to export from.",
    )
    export_p.add_argument(
        "--key",
        default=None,
        help=f"Key override (CI/tests). Defaults to ${KEY_ENV}, else the OS keychain.",
    )
    scope = export_p.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--month",
        metavar="YYYY-MM",
        help="Export this single month. --out is the projection file to write.",
    )
    scope.add_argument(
        "--all-months",
        action="store_true",
        help="Export every month present, one file per month (proj-YYYY-MM.json). "
        "--out is the directory to write them into.",
    )
    export_p.add_argument(
        "--out",
        required=True,
        metavar="PATH",
        help="Output projection file (--month) or output directory (--all-months).",
    )
    export_p.set_defaults(func=_cmd_export)

    payees_p = sub.add_parser(
        "payees",
        help="Review, fix, and categorize payee entities (merge / split / "
        "rename / tag); runs only where the mapping table lives.",
        description=(
            "Entity-resolution review flow (story S1.1). --review lists the "
            "resolved payee entities with variant counts, confidence, and "
            "display labels (a local render, like `lens`, that reveals real "
            "values). --merge/--split/--rename fix groupings and labels while "
            "preserving alias stability (a merge aliases the loser to the "
            "winner and never renumbers); every mutation is journaled. Mutation "
            "output is alias-space only. --categorize/--tag/--untag manage "
            "entity-level closed-vocabulary category tags (the eligibility "
            "verdict that rides into projections as payee_category — see "
            "docs/payee-categories.md)."
        ),
    )
    payees_p.add_argument(
        "--store",
        required=True,
        metavar="PATH",
        help="Path to the local encrypted store holding the mapping table.",
    )
    payees_p.add_argument(
        "--key",
        default=None,
        help=f"Key override (CI/tests). Defaults to ${KEY_ENV}, else the OS keychain.",
    )
    action = payees_p.add_mutually_exclusive_group()
    action.add_argument(
        "--review",
        action="store_true",
        help="List resolved payee entities (default). Reveals display labels; "
        "local render only.",
    )
    action.add_argument(
        "--merge",
        nargs=2,
        metavar=("WINNER", "LOSER"),
        help="Fold the LOSER payee token into WINNER. The loser keeps its token "
        "and now resolves to the winner (alias stability preserved).",
    )
    action.add_argument(
        "--split",
        metavar="TOKEN",
        help="Un-merge a previously merged TOKEN back into its own entity.",
    )
    action.add_argument(
        "--rename",
        nargs=2,
        metavar=("TOKEN", "LABEL"),
        help="Change TOKEN's display label to LABEL (real value; stays local).",
    )
    action.add_argument(
        "--tag",
        nargs=2,
        metavar=("TOKEN", "CATEGORY"),
        help="Bind a closed-vocabulary category (pharmacy, medical-provider, "
        "dental, vision, mixed-retailer, non-medical) to TOKEN's entity. The tag "
        "rides into projections as payee_category.",
    )
    action.add_argument(
        "--untag",
        metavar="TOKEN",
        help="Remove TOKEN's category tag (back to untagged).",
    )
    action.add_argument(
        "--categorize",
        action="store_true",
        help="Propose categories for untagged entities from local keyword rules. "
        "Reveals display labels; local render only. Writes nothing — confirm "
        "each proposal with --tag.",
    )
    payees_p.set_defaults(func=_cmd_payees)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
