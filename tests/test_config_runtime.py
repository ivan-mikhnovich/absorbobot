"""Preflight использует только эффективные defaults, а не весь каталог ключей."""

from types import SimpleNamespace

import pytest

from absorbobot import config


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    binary = tmp_path / "telegram-bot-api"
    binary.write_text("binary")
    binary.chmod(0o700)
    settings = config.Settings(storage_dir=tmp_path / "mount/data", backing_mount=tmp_path / "mount", state_dir=tmp_path / "state", api_binary=binary)
    env = {"ABSORBOBOT_TOKEN": "private-token", "TELEGRAM_API_ID": "12345", "TELEGRAM_API_HASH": "a" * 32, "SELECTED_KEY": "private-model-key"}
    core = {"models": {}, "defaults": {}, "runtime": {"tex_roots": ["/etc/texmf"]}}
    for category in ("stt", "vision", "llm"):
        core["models"][category] = {
            "effective": {"provider": {"api_key_env": "SELECTED_KEY"}},
            "optional": {"provider": {"api_key_env": "OPTIONAL_KEY"}},
        }
        core["defaults"][category] = "effective"
    monkeypatch.setattr(config, "check_storage", lambda value: {"free_bytes": 100})
    monkeypatch.setattr(config.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(config.psutil, "disk_partitions", lambda all: [SimpleNamespace(mountpoint="/", fstype="ext4")])
    monkeypatch.setattr(config, "load_secrets", lambda value: env)
    monkeypatch.setattr(config, "probe_core", lambda value: core)
    return settings, core, env


def test_optional_missing_key_does_not_block_serve(runtime):
    settings, core, env = runtime
    assert config.check_runtime(settings)["core"] is core
    assert "OPTIONAL_KEY" not in env
    assert not settings.state_dir.exists()
    assert not settings.storage_dir.exists()


def test_selected_missing_key_blocks_serve(runtime):
    settings, core, _ = runtime
    core["defaults"]["vision"] = "optional"
    with pytest.raises(config.ConfigError, match="OPTIONAL_KEY"):
        config.check_runtime(settings)


@pytest.mark.parametrize("name", ["ABSORBOBOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"])
def test_missing_credentials_have_specific_safe_diagnostic(runtime, name):
    settings, _, env = runtime
    del env[name]
    with pytest.raises(config.ConfigError) as caught:
        config.check_runtime(settings)
    assert name in str(caught.value)
    assert "private" not in str(caught.value)


def test_missing_tools_blocks_serve(runtime, monkeypatch):
    settings, _, _ = runtime
    monkeypatch.setattr(config.shutil, "which", lambda name: None if name == "bwrap" else f"/usr/bin/{name}")
    with pytest.raises(config.ConfigError, match="bwrap"):
        config.check_runtime(settings)


def test_missing_native_api_blocks_serve(runtime):
    settings, _, _ = runtime
    settings.api_binary.unlink()
    with pytest.raises(config.ConfigError, match="telegram-bot-api"):
        config.check_runtime(settings)


def test_state_on_ntfs_blocks_serve(runtime, monkeypatch):
    settings, _, _ = runtime
    monkeypatch.setattr(config.psutil, "disk_partitions", lambda all: [SimpleNamespace(mountpoint="/", fstype="fuseblk")])
    with pytest.raises(config.ConfigError, match="NTFS"):
        config.check_runtime(settings)


def test_unknown_tex_roots_blocks_serve(runtime):
    settings, core, _ = runtime
    core["runtime"]["tex_roots"] = []
    with pytest.raises(config.ConfigError, match="TeX"):
        config.check_runtime(settings)


def test_snapshot_cannot_borrow_telegram_key():
    with pytest.raises(config.ConfigError):
        config.require_key_names({"llm": {"provider": {"api_key_env": "ABSORBOBOT_TOKEN"}}}, {"ABSORBOBOT_TOKEN": "private"})
