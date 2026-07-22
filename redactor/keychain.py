"""OS-keychain key custody — the ``keyring`` seam (story S0.1).

ADR-0001 split two questions apart:

  * **How is the store encrypted?** SQLCipher (``redactor.store``).
  * **Where does the key live?** The OS keychain — *this module*.

SQLCipher's own KDF secures the passphrase, but a passphrase that lives in shell
history or ``$REDACTOR_KEY`` is a passphrase that leaks. The human default is a
generated high-entropy key custodied by the OS keychain (macOS Keychain first),
unlocked with the login session, never typed and never displayed.

The seam is deliberately thin: everything routes through :func:`_default_backend`,
which returns an object with the three ``keyring`` methods we use
(``get_password`` / ``set_password`` / ``delete_password``). The real backend is
the ``keyring`` library; tests inject an in-memory fake. Non-macOS keychains are
a documented later story — the seam is here, the backend swap is all that's left
(docs/m02-stories.md, "Deferred").

**The key is a secret.** No function here returns it in a ``repr``, logs it, or
puts it in an exception message. Callers get the bare string only from
:func:`get_key`, and only to hand straight to :func:`redactor.store.open_store`.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from redactor.store import PathLike, StoreError

# The keychain "service" all redactor secrets are filed under. The per-store
# "account" is the store's absolute path, so one keychain can custody keys for
# several stores without collision (see :func:`_account_for`).
SERVICE = "redactor"

# Bytes of entropy for a generated key. token_urlsafe(32) yields a 43-char
# URL-safe string — comfortably beyond brute-force, and printable so SQLCipher's
# text ``PRAGMA key`` accepts it verbatim.
_KEY_ENTROPY_BYTES = 32


class KeychainError(StoreError):
    """Base class for key-custody errors."""


class KeychainUnavailable(KeychainError):
    """The OS keychain backend could not be reached (e.g. the ``keyring``
    library is not installed, or no Secret Service is available on a headless
    box). The message tells the caller how to proceed; it never contains a key.
    """


def generate_key() -> str:
    """Return a fresh high-entropy key suitable for SQLCipher's text key.

    URL-safe base64 so it is printable and PRAGMA-safe. Each call is independent
    — there is no derivation from anything guessable.
    """
    return secrets.token_urlsafe(_KEY_ENTROPY_BYTES)


def _account_for(store_path: PathLike) -> str:
    """The keychain account name for a store: its absolute path.

    ``Path.resolve`` normalises ``.``/``..``/symlinks so the same store is
    always looked up under the same account, whether or not it exists yet.
    """
    return str(Path(store_path).resolve())


def _default_backend():
    """Return the real ``keyring`` backend, or raise :class:`KeychainUnavailable`.

    Isolated behind a function so tests can monkeypatch the whole seam and so the
    ``keyring`` import stays lazy — the library is only needed on the human's
    machine, not in CI, which always injects a fake or uses ``$REDACTOR_KEY``.
    """
    try:  # pragma: no cover - the real backend is never imported under test
        import keyring
    except ImportError as exc:  # pragma: no cover
        raise KeychainUnavailable(
            "the 'keyring' package is not installed, so no key can be custodied "
            "in the OS keychain. Install it (`pip install keyring`), or supply a "
            "key explicitly with --key or $REDACTOR_KEY."
        ) from exc
    return keyring


def get_key(store_path: PathLike, *, backend=None) -> str | None:
    """Return the custodied key for ``store_path``, or ``None`` if none is set.

    ``backend`` overrides the seam (used by tests); production callers pass none
    and get the OS keychain.
    """
    b = backend if backend is not None else _default_backend()
    return b.get_password(SERVICE, _account_for(store_path))


def set_key(store_path: PathLike, key: str, *, backend=None) -> None:
    """Custody ``key`` for ``store_path`` in the keychain."""
    b = backend if backend is not None else _default_backend()
    b.set_password(SERVICE, _account_for(store_path), key)


def delete_key(store_path: PathLike, *, backend=None) -> None:
    """Remove the custodied key for ``store_path``. No-op if none is set."""
    b = backend if backend is not None else _default_backend()
    try:
        b.delete_password(SERVICE, _account_for(store_path))
    except Exception:
        # keyring raises PasswordDeleteError when nothing is stored; deletion is
        # idempotent from the caller's view.
        pass
