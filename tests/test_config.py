"""Границы настроек, секретов и запрета SSD fallback."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from absorbobot import config


@pytest.fixture
def settings(tmp_path):
    return config.Settings(storage_dir=tmp_path / "disk" / "data", backing_mount=tmp_path / "disk", state_dir=tmp_path / "state", secrets_file=tmp_path / "secrets.env", min_free_bytes=1)


def mounted(monkeypatch, settings, *, free=1000, readonly=False):
    settings.backing_mount.mkdir()
    monkeypatch.setattr(config.os.path, "ismount", lambda p: p == settings.backing_mount)
    monkeypatch.setattr(config.psutil, "disk_partitions", lambda all: [SimpleNamespace(mountpoint=str(settings.backing_mount), fstype="fuseblk")])
    monkeypatch.setattr(config.os, "statvfs", lambda p: SimpleNamespace(f_bavail=free, f_frsize=1, f_flag=config.os.ST_RDONLY if readonly else 0))


@pytest.mark.parametrize("value", [
    "", "  ", "127.0.0.1:1081", "socks5://127.0.0.1:1080", "http://127.0.0.1",
    "http://user:pass@127.0.0.1:1081", "http://127.0.0.1:1081/path",
    "http://127.0.0.1:1081?x=1", "http://127.0.0.1:1081#f", 1081, " http://127.0.0.1:1081",
])
def test_download_proxy_rejects_malformed_values(tmp_path, value):
    body = f"admin_ids: [1]\ndownload_proxy: {json.dumps(value)}\n"
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_settings(path)


@pytest.mark.parametrize("value", ["http://127.0.0.1:1081", "https://proxy.example:8443"])
def test_download_proxy_accepts_http_and_https(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(f"admin_ids: [1]\ndownload_proxy: {json.dumps(value)}\n", encoding="utf-8")
    assert config.load_settings(path).download_proxy == value


def test_download_proxy_defaults_to_direct_connection(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("admin_ids: [1]\n", encoding="utf-8")
    assert config.load_settings(path).download_proxy is None


@pytest.mark.parametrize("body", ["[]", "null", "admin_ids: []", "admin_ids: [true]", "admin_ids: [-1]", "admin_ids: [1]\nunknown: 2", "storage_dir: /media/storage/absorbobot", "admin_ids: [1]\nquota_jobs: 0", "admin_ids: [1]\nquota_jobs: 1.1"])
def test_invalid_settings(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(body)
    with pytest.raises(config.ConfigError):
        config.load_settings(path)


def test_expanded_paths_and_source_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("admin_ids: [7, 7]\nstate_dir: ~/.local/state/absorbobot\n")
    settings = config.load_settings(path)
    assert settings.admin_ids == (7,)
    assert settings.state_dir == tmp_path / ".local/state/absorbobot"
    assert settings.config_path == path


@pytest.mark.parametrize("url", ["https://127.0.0.1:8081", "http://example.org:8081", "http://127.0.0.1:80", "http://x:secret@127.0.0.1:8081", "http://127.0.0.1:8081/x", "http://127.0.0.1:8081?x=y", "http://localhost:8081", "http://127.0.0.1:8081#x", "bad", 8081, "http://127.0.0.1:\n8081"])
def test_insecure_api_url(settings, url):
    with pytest.raises(config.ConfigError):
        replace(settings, bot_api_url=url)


@pytest.mark.parametrize("field,value", [("metrics_interval_seconds", float("nan")), ("metrics_interval_seconds", float("inf")), ("max_media_seconds", True), ("telegram_part_bytes", 2000000001)])
def test_nonfinite_boolean_and_api_limits(settings, field, value):
    with pytest.raises(config.ConfigError):
        replace(settings, **{field: value})


def test_storage_must_be_explicit_mount_child(settings):
    with pytest.raises(config.ConfigError):
        replace(settings, backing_mount=Path("/"))
    with pytest.raises(config.ConfigError):
        replace(settings, storage_dir=settings.backing_mount)
    with pytest.raises(config.ConfigError):
        replace(settings, state_dir=settings.storage_dir / "state")


def test_absent_mount_never_creates_fallback(settings, monkeypatch):
    monkeypatch.setattr(config.os.path, "ismount", lambda p: False)
    with pytest.raises(config.ConfigError):
        config.create_work_tree(settings, str(uuid4()))
    assert not settings.backing_mount.exists()
    assert not settings.storage_dir.exists()


def test_storage_check_is_read_only(settings, monkeypatch):
    mounted(monkeypatch, settings)
    result = config.check_storage(settings)
    assert result["writable"] is True
    assert result["free_bytes"] == 1000
    assert not settings.storage_dir.exists()
    assert not settings.state_dir.exists()
    assert not settings.secrets_file.exists()


@pytest.mark.parametrize("readonly,free", [(True, 1000), (False, 0)])
def test_unusable_storage(settings, monkeypatch, readonly, free):
    mounted(monkeypatch, settings, readonly=readonly, free=free)
    with pytest.raises(config.ConfigError):
        config.check_storage(settings)
    assert not settings.storage_dir.exists()


def test_nested_mount_is_not_declared_backing(settings, monkeypatch):
    mounted(monkeypatch, settings)
    settings.storage_dir.mkdir()
    monkeypatch.setattr(config.psutil, "disk_partitions", lambda all: [SimpleNamespace(mountpoint=str(settings.backing_mount), fstype="ext4"), SimpleNamespace(mountpoint=str(settings.storage_dir), fstype="ext4")])
    with pytest.raises(config.ConfigError):
        config.check_storage(settings)


def test_writable_check(settings, monkeypatch):
    mounted(monkeypatch, settings)
    monkeypatch.setattr(config.os, "access", lambda *args: False)
    with pytest.raises(config.ConfigError):
        config.check_storage(settings)


def test_create_own_uuid_only(settings, monkeypatch):
    mounted(monkeypatch, settings)
    identifier = str(uuid4())
    root = config.create_work_tree(settings, identifier)
    assert root == settings.storage_dir / "jobs" / identifier
    assert {p.name for p in root.iterdir()} == {"input", "output", "tmp"}
    with pytest.raises(config.ConfigError):
        config.create_work_tree(settings, "../outside")


def test_work_symlink_cannot_escape(settings, monkeypatch, tmp_path):
    mounted(monkeypatch, settings)
    identifier = str(uuid4())
    (settings.storage_dir / "jobs").mkdir(parents=True)
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (settings.storage_dir / "jobs" / identifier).symlink_to(outside, target_is_directory=True)
    with pytest.raises(config.ConfigError):
        config.create_work_tree(settings, identifier)
    assert list(outside.iterdir()) == []


def test_secrets_override_and_no_execution(settings, monkeypatch, tmp_path):
    marker = tmp_path / "must-not-exist"
    settings.secrets_file.write_text(f"# Comment.\nABSORBOBOT_TOKEN='file-value'\nCUSTOM_KEY=\"$(touch {marker}) $HOME `echo bad`\"\nEMPTY=\nSPACES=one  two\nMULTI='one\ntwo'\nCONT=one\\\ntwo\n")
    settings.secrets_file.chmod(0o600)
    monkeypatch.setenv("ABSORBOBOT_TOKEN", "process-value")
    env = config.load_secrets(settings)
    assert env["ABSORBOBOT_TOKEN"] == "process-value"
    assert env["CUSTOM_KEY"] == f"$(touch {marker}) $HOME `echo bad`"
    assert env["EMPTY"] == ""
    assert env["SPACES"] == "one  two"
    assert env["MULTI"] == "one\ntwo"
    assert env["CONT"] == "onetwo"
    assert not marker.exists()


@pytest.mark.parametrize("permissions", [0o644, 0o640, 0o400, 0o777])
def test_secrets_permissions_fail_without_leak(settings, permissions):
    settings.secrets_file.write_text("ABSORBOBOT_TOKEN=never-show-this")
    settings.secrets_file.chmod(permissions)
    with pytest.raises(config.ConfigError) as caught:
        config.load_secrets(settings)
    assert "never-show-this" not in str(caught.value)


def test_secret_symlink_rejected(settings, tmp_path):
    actual = tmp_path / "actual.env"
    actual.write_text("KEY=private")
    actual.chmod(0o600)
    settings.secrets_file.symlink_to(actual)
    with pytest.raises(config.ConfigError):
        config.load_secrets(settings)


@pytest.mark.parametrize("body", ["KEY='private", "KEY=\"private\"junk", "NOT A NAME=private", "private-without-assignment"])
def test_invalid_envfile_diagnostic_redacted(settings, body):
    settings.secrets_file.write_text(body)
    settings.secrets_file.chmod(0o600)
    with pytest.raises(config.ConfigError) as caught:
        config.load_secrets(settings)
    assert "private" not in str(caught.value)


def test_key_requirement_only_selected_models():
    selected = {"models": {"llm": {"provider": {"api_key_env": "SELECTED_KEY"}}, "stt": {"provider": {"api_key_env": ""}}}}
    config.require_key_names(selected, {"SELECTED_KEY": "private-value"})
    with pytest.raises(config.ConfigError) as caught:
        config.require_key_names(selected, {"OPTIONAL_KEY": "private-value"})
    assert "SELECTED_KEY" in str(caught.value)
    assert "private-value" not in str(caught.value)
