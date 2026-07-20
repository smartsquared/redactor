"""Injection scrub for attacker-controlled third-party text (story S1.5).

Bank statement memos and payee strings are not authored by the user — they are
whatever a merchant (or an attacker who can push a line item) wrote. The brief
names this as threat 3: *statement memos/payee strings are attacker-controlled
third-party text; scrub as untrusted input.* Once a memo lands in an
alias-space projection it will eventually sit inside a model prompt, so any
instruction-shaped content in it is a prompt-injection vector.

This module neutralizes that content **before** it enters any projection. Two
non-negotiable properties, mirroring the alias matcher's "mangle-tolerant but
never false-positive" discipline (docs/alias-contract.md §3):

* **Neutralize, don't merely detect.** Instruction-shaped spans are replaced
  with a single visible ``[scrubbed]`` marker, and structural chat/control
  tokens (ChatML, ``[INST]``, fake ``system:`` turns, code fences) are removed
  so they cannot re-open a prompt boundary downstream.
* **Never touch a benign memo.** Real payee garbage ("WHOLEFDS #1029 SEA",
  "UBER *TRIP HELP.UBER") must pass through byte-for-byte, or entity
  resolution (S1.3) breaks and legibility is lost.

The scrub is deliberately conservative: it fires on well-known injection
shapes, not on any imperative verb, and it leaves the surrounding legible
carrier intact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The marker left behind wherever instruction-shaped content is removed. Chosen
# to be human-legible in a projection and to itself contain no instruction.
PLACEHOLDER = "[scrubbed]"

# Verbs that, in third-party text, read as an attempt to steer the model.
_STEER_VERB = (
    r"ignore|disregard|forget|override|discard|skip|bypass|"
    r"reveal|print|output|dump|send|email|export|leak|exfiltrate|disclose|"
    r"respond|reply|repeat|say|write|act|pretend|become|behave"
)

# Sensitive targets an injected instruction tends to reach for. Used to catch
# bare exfiltration imperatives ("email the SSN list") that lack an override
# clause.
_SENSITIVE = (
    r"password|passwords|secret|secrets|api\s*keys?|account\s+numbers?|"
    r"mapping\s+table|credentials?|ssn|social\s+security|private\s+keys?|"
    r"routing\s+numbers?|card\s+numbers?"
)

# Each rule is (kind, pattern). Order matters only for readability; all matches
# are collected, merged, and replaced in one pass. Patterns are bounded (no
# unbounded ``.*``) so a match cannot run away across a whole document, and are
# anchored on injection *shape*, not on any single loaded word.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "ignore all previous instructions", "disregard the above", "forget
    # everything above" — an override verb reaching for prior context.
    (
        "override",
        re.compile(
            r"\b(?:" + _STEER_VERB + r")\b"
            r"[^.\n]{0,40}?"
            r"\b(?:previous|prior|preceding|above|earlier|foregoing|"
            r"everything|all|any|the)\b"
            r"[^.\n]{0,40}?"
            r"\b(?:instruction|instructions|prompt|prompts|context|rule|rules|"
            r"direction|directions|command|commands|message|messages|"
            r"guardrail|guardrails)\b",
            re.IGNORECASE,
        ),
    ),
    # Bare "forget everything above" / "disregard the above" with no explicit
    # instruction-noun after the pointer to prior context.
    (
        "override",
        re.compile(
            r"\b(?:ignore|disregard|forget|discard|override|skip|bypass)\b\s+"
            r"(?:everything|all|the\s+text|the)?\s*"
            r"(?:above|before|prior|preceding|foregoing|so\s+far|earlier)\b",
            re.IGNORECASE,
        ),
    ),
    # Role hijack: "you are now ...", "you are a helpful assistant ...",
    # "act as an unfiltered model", "pretend to be ...". Consume to end of the
    # clause so the injected persona description goes with it.
    (
        "role_hijack",
        re.compile(
            r"\byou\s+are\s+(?:now\s+)?(?:a|an|the)?[^.\n]{0,60}",
            re.IGNORECASE,
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"\b(?:act|behave)\s+as\s+(?:an?\s+)?[^.\n]{0,40}",
            re.IGNORECASE,
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"\bpretend\s+(?:to\s+be|that\s+you)\b[^.\n]{0,40}",
            re.IGNORECASE,
        ),
    ),
    # "new instructions:", "system prompt:", "updated directive:".
    (
        "fake_directive",
        re.compile(
            r"\b(?:new|updated|revised|real|actual|following|these)\s+"
            r"(?:instruction|instructions|prompt|prompts|directive|directives|"
            r"rule|rules|command|commands)\b\s*:?",
            re.IGNORECASE,
        ),
    ),
    (
        "fake_directive",
        re.compile(r"\bsystem\s+prompt\b\s*:?", re.IGNORECASE),
    ),
    # Bare exfiltration imperative reaching for a sensitive target, e.g.
    # "reveal the account numbers", "email the SSN list", "print the mapping
    # table".
    (
        "exfiltration",
        re.compile(
            r"\b(?:reveal|print|output|dump|send|email|export|leak|exfiltrate|"
            r"disclose|list|show)\b"
            r"[^.\n]{0,30}?"
            r"\b(?:" + _SENSITIVE + r")\b"
            r"[^.\n]{0,30}",
            re.IGNORECASE,
        ),
    ),
    # Fake chat turn markers at a boundary: "system:", "assistant:", "user:".
    (
        "role_marker",
        re.compile(
            r"(?:(?<=^)|(?<=[\s.>|\]]))(?:system|assistant|user|human|ai)\s*:",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    # Special / control tokens used to fake a prompt boundary.
    (
        "control_token",
        re.compile(
            r"<\|?\s*(?:im_start|im_end|endoftext|system|user|assistant|eot_id|"
            r"start_header_id|end_header_id)\s*\|?>",
            re.IGNORECASE,
        ),
    ),
    (
        "control_token",
        re.compile(r"\[/?\s*INST\s*\]", re.IGNORECASE),
    ),
    (
        "control_token",
        re.compile(r"<</?\s*SYS\s*>>", re.IGNORECASE),
    ),
    # Code / markdown fences that could break out of a quoted data block.
    (
        "fence",
        re.compile(r"`{3,}|~{3,}"),
    ),
)


@dataclass(frozen=True)
class ScrubHit:
    """One neutralized span."""

    kind: str              # rule class, e.g. "override" | "control_token"
    raw: str               # the exact substring that was removed
    span: tuple[int, int]  # (start, end) offsets in the *original* text


@dataclass(frozen=True)
class ScrubResult:
    """Outcome of scrubbing one third-party string."""

    text: str                          # neutralized text, safe to project
    modified: bool                     # True iff any span was neutralized
    hits: list[ScrubHit] = field(default_factory=list)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent (start, end) spans into a minimal set."""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _collapse_whitespace(text: str) -> str:
    """Tidy the seams left by removed spans without disturbing benign text."""
    # Space(s) hugging a placeholder collapse to one; drop a leading/trailing
    # space and any space stranded before terminal punctuation.
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    return text.strip()


def scrub_text(text: str) -> ScrubResult:
    """Neutralize instruction-shaped content in *text*.

    Returns a :class:`ScrubResult`. When nothing matches, ``text`` is returned
    unchanged and ``modified`` is ``False`` — benign memos are never touched.
    """
    raw_hits: list[ScrubHit] = []
    for kind, pattern in _RULES:
        for m in pattern.finditer(text):
            if m.group(0):  # ignore zero-width matches
                raw_hits.append(ScrubHit(kind, m.group(0), (m.start(), m.end())))

    if not raw_hits:
        return ScrubResult(text=text, modified=False, hits=[])

    merged = _merge([h.span for h in raw_hits])

    # Rebuild the string, swapping each merged span for the placeholder.
    out: list[str] = []
    cursor = 0
    for start, end in merged:
        out.append(text[cursor:start])
        out.append(PLACEHOLDER)
        cursor = end
    out.append(text[cursor:])

    cleaned = _collapse_whitespace("".join(out))

    # Report hits deduplicated to the merged spans, in document order.
    hits = sorted(raw_hits, key=lambda h: h.span)
    return ScrubResult(text=cleaned, modified=True, hits=hits)


def scrub(text: str) -> str:
    """Convenience wrapper returning only the neutralized text."""
    return scrub_text(text).text
