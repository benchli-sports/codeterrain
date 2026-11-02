import pytest


@pytest.fixture
def client(client, flavor):
    return {"inner": client, "flavor": flavor}


@pytest.fixture(scope="module", autouse=True)
def _api_module_marker(mod_cache):
    mod_cache["api"] = True
