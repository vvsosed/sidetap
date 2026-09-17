"""Proves the package imports and the toolchain is wired up."""

import sidetap


def test_package_imports():
    assert sidetap is not None
