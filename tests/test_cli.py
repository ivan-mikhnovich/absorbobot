"""Read-only команды, безопасный getMe и пути собственных служб."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace
import shutil
import subprocess
from pathlib import Path

import pytest

from absorbobot import cli, config


@pytest.fixture
def settings(tmp_path):
    core_config = tmp_path / "catalog.yaml"
    core_config.write_text("providers:\n  dynamic:\n    base_url: https://api.example/v1\n    api_key_env: DYNAMIC_CLOUD_KEY\n  optional:\n    base_url: https://optional.example/v1\n    api_key_env: OPTIONAL_CLOUD_KEY\n")
    return config.Settings(storage_dir=tmp_path / "volume" / "new data", backing_mount=tmp_path / "volume", state_dir=tmp_path / "SSD state", absorbent_config=core_config, config_path=tmp_path / "new config.yaml", secrets_file=tmp_path / "protected.env", api_binary=tmp_path / "tools" / "telegram-bot-api", bot_api_url="http://127.0.0.1:8081")


def test_install_only_own_units_and_reload(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setattr(cli, "check_storage", lambda value: {"writable": True})
    directory = cli.unit_directory()
    directory.mkdir(parents=True)
    unrelated = directory / "unrelated.service"
    unrelated.write_text("untouched")
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0))
    cli.install_units(settings)
    assert unrelated.read_text() == "untouched"
    assert {p.name for p in directory.iterdir()} == {"unrelated.service", "absorbobot.service", "absorbobot-api.service"}
    assert calls == [["/usr/bin/systemctl", "--user", "daemon-reload"]]
    assert (settings.state_dir / "telegram-api").is_dir()
    assert (settings.storage_dir / "telegram-api-files").is_dir()
    assert (settings.storage_dir / "telegram-api-tmp").is_dir()
    assert not settings.secrets_file.exists()


def test_install_missing_mount_creates_no_service_directories(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    def missing_mount(value):
        raise config.ConfigError("Backing mount отсутствует.")
    monkeypatch.setattr(cli, "check_storage", missing_mount)
    with pytest.raises(config.ConfigError):
        cli.install_units(settings)
    assert not settings.state_dir.exists()
    assert not settings.storage_dir.exists()
    assert not cli.unit_directory().exists()


def test_stale_units_detected(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    directory = cli.unit_directory()
    directory.mkdir(parents=True)
    for name, content in cli.render_units(settings).items():
        (directory / name).write_text(content)
    cli.check_unit_paths(settings)
    (directory / "absorbobot-api.service").write_text("old-settings")
    with pytest.raises(config.ConfigError):
        cli.check_unit_paths(settings)


def test_check_storage_does_not_load_secrets_or_call_api(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "check_storage", lambda value: {"mount": str(value.backing_mount), "writable": True, "free_bytes": 100})
    def forbidden(*args, **kwargs):
        pytest.fail("Команда check-storage вызвала запрещённую операцию.")
    monkeypatch.setattr(cli, "load_secrets", forbidden)
    monkeypatch.setattr(cli, "check_runtime", forbidden)
    monkeypatch.setattr(cli, "check_telegram", forbidden)
    assert cli.main(["check-storage", "--config", "any.yaml"]) == 0
    assert json.loads(capsys.readouterr().out)["free_bytes"] == 100


def test_check_is_read_only_and_has_no_sensitive_output(settings, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "check_runtime", lambda value: {"core": {"version_info": {"version": "1.2.3", "commit": "c" * 40, "dirty": False}}, "storage": {"free_bytes": 10}})
    monkeypatch.setattr(cli, "check_unit_paths", lambda value: None)
    monkeypatch.setattr(cli, "load_secrets", lambda value: {"ABSORBOBOT_TOKEN": "private-token"})
    async def identity(value, env):
        calls.append("getMe")
        return {"id": 1, "username": "absorbobot"}
    monkeypatch.setattr(cli, "check_telegram", identity)
    assert cli.main(["check", "--config", "any.yaml"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "private-token" not in captured.out + captured.err
    assert calls == ["getMe"]
    assert not settings.state_dir.exists()
    assert not settings.storage_dir.exists()


def test_serve_preflight_failure_does_not_import_bot(settings, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    def fail(value):
        raise config.ConfigError("Preflight отказал.")
    monkeypatch.setattr(cli, "check_runtime", fail)
    before = sys.modules.get("absorbobot.bot")
    assert cli.main(["serve", "--config", "any.yaml"]) == 2
    assert sys.modules.get("absorbobot.bot") is before




@pytest.mark.parametrize("mode", ["ok", "wrong-bot", "unauthorized", "malformed"])
def test_getme_only_and_safe_token_errors(settings, monkeypatch, mode):
    import aiogram
    import aiogram.client.session.aiohttp as session_module
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.methods import GetMe
    from aiogram.utils.token import TokenValidationError
    events = []
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["api"].is_local
        async def close(self):
            events.append("close")
    class Bot:
        def __init__(self, token, session):
            if mode == "malformed":
                raise TokenValidationError("private-token")
        async def get_me(self):
            events.append("getMe")
            if mode == "unauthorized":
                raise TelegramUnauthorizedError(method=GetMe(), message="private-token")
            return SimpleNamespace(username="absorbobot" if mode == "ok" else "differentbot", id=10, is_bot=True)
    monkeypatch.setattr(aiogram, "Bot", Bot)
    monkeypatch.setattr(session_module, "AiohttpSession", Session)
    if mode == "ok":
        assert asyncio.run(cli.check_telegram(settings, {"ABSORBOBOT_TOKEN": "private-token"}))["username"] == "absorbobot"
    else:
        with pytest.raises(config.ConfigError) as caught:
            asyncio.run(cli.check_telegram(settings, {"ABSORBOBOT_TOKEN": "private-token"}))
        assert "private-token" not in str(caught.value)
        if mode in ("unauthorized", "malformed"):
            assert "Неверный ABSORBOBOT_TOKEN" in str(caught.value)
    assert events[-1] == "close"
    assert set(events) <= {"getMe", "close"}


def test_generated_units_limit_restart_storm(settings):
    units = cli.render_units(settings, uv_binary="/usr/bin/true")
    assert set(units) == {"absorbobot.service", "absorbobot-api.service"}
    for name, unit in units.items():
        assert "\nStartLimitIntervalSec=60\n" in unit, name
        assert "\nStartLimitBurst=10\n" in unit, name
        assert "\nRestartSec=5s\n" in unit, name
        assert "\nRestart=on-failure\n" in unit, name


def test_missing_token_distinguished(settings):
    with pytest.raises(config.ConfigError, match="Не задана переменная ABSORBOBOT_TOKEN"):
        asyncio.run(cli.check_telegram(settings, {}))


@pytest.mark.parametrize("api_url", ["http://127.0.0.1:8081", "https://api.telegram.org"])
def test_generated_units_are_accepted_by_systemd_with_space_and_percent_paths(settings, tmp_path, api_url):
    from dataclasses import replace
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze не установлен.")
    changed = replace(settings, state_dir=tmp_path / "SSD state%value",
                      secrets_file=tmp_path / "protected key.env",
                      api_binary=Path("/usr/bin/true"), bot_api_url=api_url)
    units = cli.render_units(changed, uv_binary="/usr/bin/true")
    for name, content in units.items():
        (tmp_path / name).write_text(content)
    result = subprocess.run(
        [analyzer, "--user", "verify", *(str(tmp_path / name) for name in units)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
