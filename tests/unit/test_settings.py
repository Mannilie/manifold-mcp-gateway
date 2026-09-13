from __future__ import annotations

import base64

import pytest

from manifold.config.settings import Settings, SettingsError


def test_parses_full_environment(env, tmp_path):
    settings = Settings.from_env(env)
    assert settings.master_key == b"\x01" * 32
    assert settings.admin_emails == frozenset({"manny@example.com"})
    assert settings.base_url == "http://testserver"
    assert settings.log_level == "warning"
    assert settings.data_dir == tmp_path


def test_defaults(env):
    minimal = {
        "MANIFOLD_MASTER_KEY": env["MANIFOLD_MASTER_KEY"],
        "MANIFOLD_ADMIN_EMAILS": env["MANIFOLD_ADMIN_EMAILS"],
    }
    settings = Settings.from_env(minimal)
    assert settings.admin_emails == frozenset({"manny@example.com"})
    assert settings.base_url == "http://localhost:8800"
    assert settings.log_level == "info"
    assert str(settings.data_dir) == "/data"


def test_missing_master_key_fails_loudly():
    with pytest.raises(SettingsError, match="MANIFOLD_MASTER_KEY is not set"):
        Settings.from_env({})


def test_missing_admin_emails_fails_loudly(env):
    del env["MANIFOLD_ADMIN_EMAILS"]
    with pytest.raises(SettingsError, match="MANIFOLD_ADMIN_EMAILS is not set"):
        Settings.from_env(env)


@pytest.mark.parametrize("bad", ["not base64!!", base64.b64encode(b"short").decode()])
def test_malformed_master_key_rejected(bad):
    with pytest.raises(SettingsError):
        Settings.from_env({"MANIFOLD_MASTER_KEY": bad, "MANIFOLD_ADMIN_EMAILS": "a@b.c"})


def test_master_key_never_appears_in_repr_or_str(env):
    settings = Settings.from_env(env)
    raw = env["MANIFOLD_MASTER_KEY"]
    for rendered in (repr(settings), str(settings)):
        assert raw not in rendered
        assert "\\x01" not in rendered


def test_settings_error_messages_never_contain_the_key():
    raw = base64.b64encode(b"short").decode()
    with pytest.raises(SettingsError) as info:
        Settings.from_env({"MANIFOLD_MASTER_KEY": raw, "MANIFOLD_ADMIN_EMAILS": "a@b.c"})
    assert raw not in str(info.value)


def test_admin_emails_normalised(env):
    env["MANIFOLD_ADMIN_EMAILS"] = " A@Example.com, b@example.com ,"
    assert Settings.from_env(env).admin_emails == frozenset({"a@example.com", "b@example.com"})


def test_bad_email_rejected(env):
    env["MANIFOLD_ADMIN_EMAILS"] = "nope"
    with pytest.raises(SettingsError, match="not an email"):
        Settings.from_env(env)


def test_bad_log_level_rejected(env):
    env["MANIFOLD_LOG_LEVEL"] = "loud"
    with pytest.raises(SettingsError, match="MANIFOLD_LOG_LEVEL"):
        Settings.from_env(env)


def test_base_url_trailing_slash_stripped(env):
    env["MANIFOLD_BASE_URL"] = "https://mcp.example.com/"
    assert Settings.from_env(env).base_url == "https://mcp.example.com"
