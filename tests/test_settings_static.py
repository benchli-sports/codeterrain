from __future__ import annotations

from codeterrain.cache import Cache
from codeterrain.code_static import Index, update
from codeterrain.config import load_config
from codeterrain.settings_static import settings_fields


def _repo(tmp_path, files):
    root = tmp_path / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root, Index(update(Cache(root, tmp_path / "cache"), load_config(root)))


SETTINGS = '''\
from pydantic import Field
from pydantic_settings import BaseSettings as Base, SettingsConfigDict


class Common(Base):
    model_config = SettingsConfigDict(env_prefix="APP_")
    database_url: str = ""
    token: str = Field("", validation_alias="SERVICE_TOKEN")
    _private: int = 0


class Worker(Common):
    queue: str = "jobs"


class Strict(Base):
    model_config = SettingsConfigDict(env_prefix="S_", case_sensitive=True)
    level: str = "info"


class Legacy(Base):
    class Config:
        env_prefix = "OLD_"

    host: str = ""


class NotSettings:
    database_url: str = ""
'''


def test_fields_read_prefix_and_name_in_any_case_through_an_aliased_base(tmp_path):
    root, index = _repo(tmp_path, {"app/__init__.py": "", "app/settings.py": SETTINGS})
    found = {
        (f["cls"], f["field"], f["env"])
        for f in settings_fields(index, root, "app_database_url")
    }
    # The subclass inherits the field and the prefix.
    assert found == {
        ("app.settings.Common", "app.settings.Common.database_url", "APP_database_url"),
        ("app.settings.Worker", "app.settings.Common.database_url", "APP_database_url"),
    }
    assert [f["env"] for f in settings_fields(index, root, "APP_QUEUE")] == ["APP_queue"]


def test_an_alias_is_read_as_written_and_case_sensitive_settings_match_exactly(tmp_path):
    root, index = _repo(tmp_path, {"app/__init__.py": "", "app/settings.py": SETTINGS})
    assert {f["cls"] for f in settings_fields(index, root, "SERVICE_TOKEN")} == {
        "app.settings.Common", "app.settings.Worker",
    }
    assert settings_fields(index, root, "APP_TOKEN") == []
    assert [f["field"] for f in settings_fields(index, root, "S_level")] == [
        "app.settings.Strict.level"
    ]
    assert settings_fields(index, root, "S_LEVEL") == []
    # An inner `Config` sets the prefix in pydantic 1; a plain class reads nothing; nor does a
    # private attribute.
    assert [f["field"] for f in settings_fields(index, root, "OLD_HOST")] == [
        "app.settings.Legacy.host"
    ]
    assert settings_fields(index, root, "DATABASE_URL") == []
    assert settings_fields(index, root, "APP__PRIVATE") == []
