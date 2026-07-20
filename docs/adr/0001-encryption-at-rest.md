# ADR 0001 — Encryption at rest for the local store and mapping table

- **Status:** Accepted
- **Date:** 2026-07-20
- **Story:** m01 · S0.3 — Local store + mapping table schema
- **Deciders:** grand-dingo (agent-lab), for human review
- **Context doc:** [docs/brief.md](../brief.md) (four-artifact privacy model, open question: "Encryption-at-rest choice for local store + mapping table")

## Context

redactor keeps two of the four privacy artifacts on the user's machine and
nowhere else:

- **Artifact 1 — raw transactions:** statements, memos, real amounts.
- **Artifact 2 — the alias mapping table:** `PAYEE-7` ↔ real value. The brief
  calls this the crown jewel — holding it reverses *every* redacted record.

Both must be **unreadable at rest without a key**, and the store must remain a
**queryable local database** (ingest writes raw rows; the alias layer and the
render-boundary "lens" read and write mapping rows continuously). The product is
local-first and single-user by design, so the mechanism must work on a laptop
with no server and no cloud KMS.

The brief left the mechanism open, listing three candidates: **OS keychain**,
**age**, and **SQLCipher**. This ADR resolves it.

## Options considered

### A. OS keychain (macOS Keychain / Linux Secret Service / Windows Credential Manager)

Keychains are **secret stores for small blobs** — passwords and keys — fronted
by the OS and unlocked with the user's login session.

- 👍 First-class key custody; unlock tied to the OS login; no passphrase to
  remember.
- 👎 **Not a bulk-data store.** It safeguards a *key*, not a database of
  thousands of transactions. It answers "where does the key live", not "how is
  the store encrypted".
- 👎 Per-OS APIs and headless/CI gaps (no Secret Service on a bare CI runner),
  which would push encryption behind a platform matrix.

**Verdict:** the right tool for *key custody*, the wrong tool for *store
encryption*. Complementary, not a substitute — see "Key custody" below.

### B. age (file encryption)

[age](https://github.com/FiloSottile/age) encrypts a **file blob** with a
passphrase or keypair.

- 👍 Simple, modern, auditable format; excellent for encrypting an *export* or a
  cold backup.
- 👎 **Blob-oriented, not queryable.** To read or write one transaction you
  decrypt the whole store to plaintext (a temp file or a full in-memory copy),
  mutate, and re-encrypt. That reintroduces a plaintext-on-disk window — exactly
  the risk we are removing — and scales badly as the store grows.
- 👎 No page-level integrity for a live, continuously-mutated database.

**Verdict:** good for at-rest *file* transfer (a future backup/export path), but
a poor fit for the live queryable store this schema module needs.

### C. SQLCipher (transparent full-database encryption)  ✅

[SQLCipher](https://www.zetetic.net/sqlcipher/) is SQLite with transparent
page-level **AES-256** encryption plus per-page HMAC. The database file is
ciphertext at rest; pages are decrypted in memory only while a connection is
open with the correct key.

- 👍 The store **stays a real, queryable SQLite database** — no decrypt-to-temp
  window, ordinary SQL, single-file, local-first.
- 👍 "**Unreadable without the key**" is satisfied at the file level: the header
  is not `SQLite format 3\x00`, a plain `sqlite3` client cannot open it, and a
  wrong key fails the HMAC check. (All three are asserted in
  `tests/test_store.py`.)
- 👍 Per-page HMAC gives tamper detection, relevant to threat 3 (attacker-
  controlled memo text) at the storage layer.
- 👍 `sqlcipher3` ships a manylinux wheel with `libsqlcipher` **bundled** — no
  system package, so CI and a user laptop install identically via pip.
- 👎 A native dependency (mitigated by the bundled wheel).
- 👎 SQLCipher's own KDF secures the passphrase, but *where the passphrase lives*
  is a separate problem — addressed next.

**Verdict:** chosen. It is the only candidate that keeps the store queryable
while making the file unreadable without the key.

## Decision

**Encrypt the local store with SQLCipher** (`sqlcipher3`, AES-256, page HMAC).
Raw transactions (artifact 1) and the alias mapping table (artifact 2) live in
one encrypted SQLite file. The schema module (`redactor/store.py`) opens it with
`PRAGMA key`, creates the schema on first use, and migrates via
`PRAGMA user_version`.

### Key custody (composes with the decision, not in scope for S0.3)

SQLCipher answers *how the store is encrypted*; it does not answer *where the
key lives*. The intended custody model, deferred to a later key-management
story, is: **store the SQLCipher passphrase/key in the OS keychain** (option A,
in its correct role), with a passphrase prompt as the portable fallback. This is
why option A is complementary rather than rejected — keychain-for-key,
SQLCipher-for-store. For S0.3 the key is supplied by the caller to
`open_store(path, key)`.

### Off-store serialization ban (crown-jewel invariant)

Encryption at rest protects the file; it does **not** stop application code from
copying a decrypted mapping row into a log line, a JSON report, or a PR body.
That egress path is closed structurally: `MappingTable` reads return a `Sealed`
value that is non-JSON-serializable, un-pickleable, and self-redacting in
`repr`/`str`; the real value is reachable only via an explicit `reveal()` at a
local render boundary. The store exposes no export/dump/serialize method. These
guarantees are test-asserted in
`tests/test_mapping_no_offstore_serialization.py`, satisfying the S0.3
acceptance criterion "no code path serializes the mapping table anywhere but the
local store".

## Consequences

- New runtime dependency: `sqlcipher3` (bundled `libsqlcipher`; pip-installable
  on Linux/macOS wheels).
- The store is a single encrypted SQLite file — easy to back up (as ciphertext)
  and to reason about; a future encrypted **export/transfer** path is a natural
  fit for **age** (option B), reusing that tool in its correct role.
- Losing the key means losing the data — acceptable and correct for a
  crown-jewel store; there is deliberately no recovery backdoor.
- Follow-up (separate story): **key management** — keychain integration + a
  passphrase fallback, and key-rotation via SQLCipher `PRAGMA rekey`.
