import pytest


@pytest.fixture
def mod_cache(db):
    return {"local": True}


def test_root(mod_cache, renamed):
    assert mod_cache["local"]
    assert renamed == 1
