"""Trivial smoke test — proves the package imports and CI wiring works (S0.1)."""

import redactor


def test_package_imports():
    assert redactor.__version__
