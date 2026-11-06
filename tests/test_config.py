import json

import pytest

from subs_renamer import config


def test_development_dotenv_and_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('# Private\nGEMINI_API_KEY="synthetic-key-${LITERAL}"\n', encoding="utf-8")
    assert config.development_key(env_file) == "synthetic-key-${LITERAL}"
    monkeypatch.setenv("GEMINI_API_KEY", "ci-key")
    assert config.development_key(env_file) == "ci-key"


def test_frozen_key_does_not_depend_on_dotenv_or_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config, "resource_dir", lambda: tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "wrong-external-key")
    (tmp_path / config.EMBEDDED_CONFIG).write_text(json.dumps({"gemini_api_key": "built-in-key"}))
    assert config.api_key() == "built-in-key"
    (tmp_path / config.EMBEDDED_CONFIG).unlink()
    with pytest.raises(RuntimeError, match="В EXE отсутствует"):
        config.api_key()


def test_missing_key_error_contains_no_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        config.development_key(tmp_path / ".env")
