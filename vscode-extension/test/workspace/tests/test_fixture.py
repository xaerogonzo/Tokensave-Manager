"""Fixture tests for the live Test Explorer suite.

Deliberately covers the three shapes the tree has to render differently: a
module-level test, a class method, and a parametrised test whose one
definition fans out to several node ids at run time.

Nothing here is a test OF the extension — these exist to BE discovered.
"""

import pytest


def test_module_level():
    assert True


@pytest.mark.parametrize("value", ["a", "b"])
def test_parametrised(value):
    assert value in ("a", "b")


class TestGrouped:
    def test_method(self):
        assert True
