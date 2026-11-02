import pytest


def test_get_item(client):
    assert client["flavor"] in {"a", "b"}


@pytest.mark.parametrize("n", [1, 2])
def test_param(client, n):
    assert n in {1, 2}


@pytest.mark.usefixtures("renamed")
def test_marked():
    pass


def test_dynamic(request):
    assert request.getfixturevalue("mod_cache") is not None


class TestThings:
    @pytest.fixture(scope="class")
    def thing(self, mod_cache):
        return mod_cache

    def test_one(self, thing):
        assert thing is not None
