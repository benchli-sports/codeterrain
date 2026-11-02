import pytest


@pytest.fixture(scope="session", autouse=True)
def _env_session():
    yield "env"


@pytest.fixture(scope="session")
def db(_env_session):
    return {}


@pytest.fixture(scope="module")
def mod_cache(db):
    return {}


@pytest.fixture(autouse=True)
def _reset_state(db):
    db.clear()


@pytest.fixture(autouse=True, scope="function")
def _clock(_reset_state):
    return 0


@pytest.fixture(params=["a", "b"])
def flavor(request):
    return request.param


@pytest.fixture
def client(db):
    return db


@pytest.fixture(name="renamed")
def _renamed_fixture():
    return 1
