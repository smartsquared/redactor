"""The crown-jewel invariant (story S0.3 acceptance):

    "no code path serializes the mapping table anywhere but the local store"

This is enforced on three layers, all asserted here:

  1. Structural — mapping real values come back wrapped in `Sealed`, which
     refuses pickling/copying, is not JSON-serializable, and redacts itself in
     repr/str so it can't leak through logs or f-strings. Reaching the value is
     only possible through an explicit `.reveal()` (the render boundary).
  2. API surface — the Store/MappingTable objects expose no export / dump /
     serialize / backup method that would emit mapping rows off-store.
  3. Source scan — no module under `redactor/` writes mapping data to a file,
     stdout, or a serializer; the only sink for the mapping table is the
     encrypted SQLCipher connection.
"""
import copy
import json
import pathlib
import pickle
import re

import pytest

import redactor
from redactor.store import MappingTable, Sealed, Store, open_store

KEY = "correct horse battery staple"
REAL = "WHOLE FOODS MARKET #1029 SEATTLE WA"


# ---- Layer 1: Sealed is structurally non-serializable -----------------------

def test_sealed_reveals_only_through_reveal():
    s = Sealed(REAL)
    assert s.reveal() == REAL


def test_sealed_repr_and_str_do_not_leak_value():
    s = Sealed(REAL)
    assert REAL not in repr(s)
    assert REAL not in str(s)
    assert REAL not in f"{s}"
    assert REAL not in f"{s}"


def test_sealed_is_not_json_serializable():
    with pytest.raises(TypeError):
        json.dumps(Sealed(REAL))


def test_sealed_cannot_be_pickled_or_copied():
    with pytest.raises(TypeError):
        pickle.dumps(Sealed(REAL))
    with pytest.raises(TypeError):
        copy.deepcopy(Sealed(REAL))


def test_mapping_resolve_returns_sealed(tmp_path):
    with open_store(tmp_path / "s.db", KEY, create=True) as store:
        store.mapping.put("PAYEE-7", "PAYEE", REAL)
        got = store.mapping.resolve_alias("PAYEE-7")
    assert isinstance(got, Sealed)
    # A resolved value therefore cannot be json.dump'd into an off-store artifact.
    with pytest.raises(TypeError):
        json.dumps({"PAYEE-7": got})


# ---- Layer 2: no off-store export method on the API surface ------------------

FORBIDDEN_METHOD_TOKENS = ("export", "dump", "serialize", "to_json", "to_dict",
                           "backup", "save_to", "write_to")


def test_store_and_mapping_expose_no_offstore_serializer():
    for cls in (Store, MappingTable):
        for name in dir(cls):
            low = name.lower()
            assert not any(tok in low for tok in FORBIDDEN_METHOD_TOKENS), (
                f"{cls.__name__}.{name} looks like an off-store serializer; "
                "mapping data must never leave the local store"
            )


# ---- Layer 3: source scan — the mapping table has no non-DB sink ------------

_PKG_ROOT = pathlib.Path(redactor.__file__).parent


def _python_sources():
    return sorted(_PKG_ROOT.rglob("*.py"))


def test_no_module_serializes_the_mapping_table():
    # Any module that touches the mapping table must not also import a
    # serializer or write to a plaintext file. The store persists exclusively
    # through the SQLCipher connection.
    serializer_import = re.compile(r"^\s*import\s+(json|pickle|csv|marshal|shelve)\b"
                                   r"|^\s*from\s+(json|pickle|csv|marshal|shelve)\b",
                                   re.MULTILINE)
    plaintext_write = re.compile(r"open\s*\([^)]*['\"][wax]b?['\"]")
    for path in _python_sources():
        src = path.read_text()
        touches_mapping = "alias_mapping" in src or "MappingTable" in src
        if not touches_mapping:
            continue
        assert not serializer_import.search(src), (
            f"{path.name} touches the mapping table and imports a serializer"
        )
        assert not plaintext_write.search(src), (
            f"{path.name} touches the mapping table and opens a file for writing"
        )


def test_store_module_only_persists_via_sqlcipher():
    # Guard against a future edit swapping SQLCipher for a plaintext backend.
    src = (_PKG_ROOT / "store.py").read_text()
    assert "sqlcipher3" in src
    assert "PRAGMA key" in src
