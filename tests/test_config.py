import pytest

from app.cli import init_env, main
from app.core.config import ConfigurationError, Settings, load_settings
from tests.conftest import TEST_TOKEN


def test_settings_do_not_expose_token_or_admin_ids(settings):
    assert TEST_TOKEN not in repr(settings)
    assert "111" not in repr(settings)
    assert settings.app_timezone == "Asia/Tehran"


@pytest.mark.parametrize("ids", [[], [True], [0], [-1], ["111"], [111, 111], [2**52]])
def test_invalid_admin_identifiers_rejected(ids):
    with pytest.raises(ValueError):
        Settings(_env_file=None, bot_token=TEST_TOKEN, bootstrap_admin_ids=ids)


def test_dotenv_load_and_environment_override(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(f"BOT_TOKEN={TEST_TOKEN}\nBOOTSTRAP_ADMIN_IDS=[111]\n", encoding="utf-8-sig")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_IDS", "[222]")
    assert load_settings(path).bootstrap_admin_ids == (222,)


def test_invalid_token_error_contains_no_input(tmp_path):
    path = tmp_path / ".env"
    path.write_text("BOT_TOKEN=private-secret\nBOOTSTRAP_ADMIN_IDS=[111]", encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        load_settings(path)
    assert "private-secret" not in str(error.value)
    assert "BOT_TOKEN" in str(error.value)


def test_malformed_json_does_not_expose_input(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"BOT_TOKEN={TEST_TOKEN}\nBOOTSTRAP_ADMIN_IDS=private-person-data", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError) as error:
        load_settings(path)
    assert "private-person-data" not in str(error.value)
    assert TEST_TOKEN not in str(error.value)


def test_unknown_env_key_does_not_echo_sensitive_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"BOT_TOKEN={TEST_TOKEN}\nBOOTSTRAP_ADMIN_IDS=[111]\nprivate-secret=bad",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as error:
        load_settings(path)
    assert "private-secret" not in str(error.value)


def test_invalid_timezone_rejected():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            bot_token=TEST_TOKEN,
            bootstrap_admin_ids=[111],
            app_timezone="Wrong/Zone",
        )


def test_init_env_never_overwrites_existing_secrets(tmp_path):
    path = tmp_path / ".env"
    init_env(path)
    path.write_text("existing-secret", encoding="utf-8")
    with pytest.raises(FileExistsError):
        init_env(path)
    assert path.read_text() == "existing-secret"


def test_cli_missing_config_is_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["check-config"]) == 2
    assert "BOT_TOKEN" in capsys.readouterr().out


def test_cli_valid_config_is_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_IDS", "[111]")
    assert main(["check-config"]) == 0
    assert TEST_TOKEN not in capsys.readouterr().out
