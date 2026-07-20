"""Inbound re-substitution — the local lens (story S2.2).

The gateway is a bidirectional proxy (docs/brief.md §"Un-redaction mechanism").
This module is its **inbound** half: model→human, it substitutes alias tokens
back to their real values so a human can read a model's alias-space answer in
plain language.

Two invariants govern it, both inherited from the alias contract
(docs/alias-contract.md §3):

  * **Mangle-tolerant.** Models don't echo ``PAYEE-7`` verbatim — they
    lowercase, re-space, re-punctuate, pluralise. Recognition is delegated to
    :func:`redactor.alias.find_aliases`, the single executable definition of the
    matching rule, so the lens can never drift from the contract.
  * **Never false-positive, and unknown tokens pass through.** Text that isn't
    one of our tokens is left byte-for-byte untouched; a syntactically valid
    token that isn't in *this* machine's table is also left untouched — it was
    simply never issued here (§3.2).

Crucially, this module holds **no** binding of alias→real itself. It takes a
*resolver*: a callable that maps a canonical token to its real value, or returns
``None`` when the token is unknown. The only resolver wired to real data is
:func:`resolver_from_table`, which reads the crown-jewel mapping table and
crosses the :class:`~redactor.store.Sealed` render boundary via ``reveal()``.
That keeps un-redaction structurally confined to the machine that holds the
table — the CLI (:mod:`redactor.cli`) refuses to run without it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from redactor.alias import find_aliases

# A resolver maps a canonical alias token ("PAYEE-7") to its real value, or to
# ``None`` when the token is not bound in the local table.
Resolver = Callable[[str], str | None]


@dataclass(frozen=True)
class LensResult:
    """The outcome of running the lens over one string.

    ``substituted`` lists the *canonical* tokens that were replaced. Canonical
    tokens are non-sensitive (they are just ``CLASS-n`` shapes), so this is safe
    to log or count — unlike the real values now inlined in ``text``, which are
    for the local render boundary only.
    """

    text: str
    substituted: list[str]


def lens(text: str, resolve: Resolver) -> LensResult:
    """Substitute every known alias token in *text* with its real value.

    Recognition is mangle-tolerant (via :func:`redactor.alias.find_aliases`);
    substitution replaces the whole matched token — including any case /
    separator / possessive mangling — with the resolved real value. Tokens the
    *resolve* callable maps to ``None`` (unknown to this table) are left exactly
    as they were, as is all non-alias text.
    """
    matches = find_aliases(text)
    if not matches:
        return LensResult(text, [])

    out: list[str] = []
    substituted: list[str] = []
    pos = 0
    for m in matches:
        start, end = m.span
        real = resolve(m.canonical)
        if real is None:
            continue  # unknown token: leave it untouched (contract §3.2)
        out.append(text[pos:start])
        out.append(real)
        substituted.append(m.canonical)
        pos = end
    out.append(text[pos:])
    return LensResult("".join(out), substituted)


def resolver_from_table(mapping) -> Resolver:
    """Build a :data:`Resolver` backed by the local mapping table.

    Reveals the :class:`~redactor.store.Sealed` real value — the explicit render
    boundary. Call this only where the mapping table lives; the sealed value's
    ``reveal()`` is the one sanctioned un-redaction point (docs/brief.md).
    """

    def resolve(token: str) -> str | None:
        sealed = mapping.resolve_alias(token)
        return None if sealed is None else sealed.reveal()

    return resolve
