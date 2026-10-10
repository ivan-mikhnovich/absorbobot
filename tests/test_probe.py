"""Проверки происхождения установленного ядра и динамического probe-каталога."""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from absorbobot import config
from absorbobot.workers import probe


@pytest.fixture
def probe_data(tmp_path):
    tool = tmp_path / "tools" / "absorbent"
    models = {}
    for category in ("stt", "vision", "llm"):
        name = f"configured-{category}"
        spec = {"name": name, "provider": "configured", "backend": "", "whisper_model": "", "model": "remote-model", "default": True, "api_model": "remote-model"}
        models[category] = {name: {"spec": spec, "provider": {"name": "configured", "base_url": "https://models.example/v1", "api_key_env": "CONFIGURED_KEY"}, "local": False}}
    return {"version_info": {"version": "1.2.3", "commit": "a" * 40, "dirty": False}, "installed_editable": False, "models": models, "defaults": {c: f"configured-{c}" for c in models}, "media_extensions": [".custom"], "material_extensions": [".customslide"], "runtime": {"tool_env": str(tool), "python_executable": str(tool / "bin/python"), "python_base_prefix": "/usr", "tex_roots": ["/etc/texmf"]}}


def test_valid_probe_catalog_not_pinned(probe_data):
    tool = Path(probe_data["runtime"]["tool_env"])
    assert config.validate_probe(probe_data, tool) is probe_data
    assert set(probe_data["models"]["llm"]) == {"configured-llm"}


@pytest.mark.parametrize("field,value", [("commit", None), ("commit", "short"), ("dirty", True), ("dirty", None), ("version", "unknown")])
def test_unknown_or_dirty_engine_rejected(probe_data, field, value):
    probe_data["version_info"][field] = value
    with pytest.raises(config.ConfigError):
        config.validate_probe(probe_data, Path(probe_data["runtime"]["tool_env"]))


def test_editable_probe_rejected(probe_data):
    probe_data["installed_editable"] = True
    with pytest.raises(config.ConfigError):
        config.validate_probe(probe_data, Path(probe_data["runtime"]["tool_env"]))


def test_disposable_uvx_runtime_rejected(probe_data, tmp_path):
    with pytest.raises(config.ConfigError):
        config.validate_probe(probe_data, tmp_path / "real-tools/absorbent")


@pytest.mark.parametrize("change", ["missing-key", "extra-key", "invalid-model", "missing-default", "invalid-extension", "relative-runtime", "outside-python"])
def test_probe_schema_rejected(probe_data, change):
    tool = Path(probe_data["runtime"]["tool_env"])
    if change == "missing-key":
        del probe_data["defaults"]
    elif change == "extra-key":
        probe_data["secret"] = "private"
    elif change == "invalid-model":
        probe_data["models"]["vision"]["configured-vision"]["provider"] = {"api_key": "private"}
    elif change == "missing-default":
        probe_data["defaults"]["llm"] = "unavailable"
    elif change == "invalid-extension":
        probe_data["media_extensions"] = ["../../file"]
    elif change == "relative-runtime":
        probe_data["runtime"]["python_base_prefix"] = "relative"
    else:
        probe_data["runtime"]["python_executable"] = "/usr/bin/python"
    with pytest.raises(config.ConfigError) as caught:
        config.validate_probe(probe_data, tool)
    assert "private" not in str(caught.value)


def test_missing_receipt_never_runs_disposable_environment(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(config, "_run_metadata", lambda argv, **kw: calls.append(argv) or str(tmp_path))
    with pytest.raises(config.ConfigError):
        config.probe_core(config.Settings())
    assert len(calls) == 1
    assert calls[0][1:3] == ["tool", "dir"]


@pytest.mark.parametrize("body", ["broken TOML =", '[tool]\nrequirements = [{ name = "other" }]', '[tool]\nrequirements = [{ name = "absorbent", editable = true }]'])
def test_invalid_tool_receipt(monkeypatch, tmp_path, body):
    tool = tmp_path / "absorbent"
    tool.mkdir()
    (tool / "uv-receipt.toml").write_text(body)
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(config, "_run_metadata", lambda argv, **kw: str(tmp_path))
    with pytest.raises(config.ConfigError):
        config._receipt_env()


def test_probe_invocation_and_redacted_environment(monkeypatch, tmp_path, probe_data):
    tool = Path(probe_data["runtime"]["tool_env"])
    tool.mkdir(parents=True)
    (tool / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "absorbent", git = "https://example.org/core" }]\n')
    calls = []
    monkeypatch.setenv("ABSORBOBOT_TOKEN", "never-show-bot-token")
    monkeypatch.setenv("TELEGRAM_API_HASH", "never-show-app-secret")
    monkeypatch.setenv("CONFIGURED_KEY", "never-show-model-key")
    monkeypatch.setattr(config.shutil, "which", lambda name: "/usr/bin/uv")
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = str(tool.parent) if argv[1:3] == ["tool", "dir"] else json.dumps(probe_data)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    monkeypatch.setattr(config.subprocess, "run", run)
    settings = config.Settings(absorbent_config=tmp_path / "server.yaml")
    assert config.probe_core(settings) == probe_data
    assert calls[1][0][1:8] == ["tool", "run", "--offline", "--no-config", "--from", "absorbent", "python"]
    assert calls[1][0][-1] == str(settings.absorbent_config)
    for _, kwargs in calls:
        assert not {"ABSORBOBOT_TOKEN", "TELEGRAM_API_HASH", "CONFIGURED_KEY"} & set(kwargs["env"])
        assert kwargs["check"] is False


def test_probe_errors_never_expose_stderr(monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="private", stderr="private-token"))
    with pytest.raises(config.ConfigError) as caught:
        config._run_metadata(["uv"])
    assert "private" not in str(caught.value)


@dataclass
class Spec:
    name: str
    provider: str = "configured"
    backend: str = ""
    whisper_model: str = ""
    model: str = "dynamic-api-name"
    default: bool = True

    @property
    def api_model(self):
        return self.model or self.name


@dataclass
class Provider:
    name: str = "configured"
    base_url: str = "http://127.0.0.1:9999/v1"
    api_key_env: str = "DYNAMIC_ENV"


@pytest.mark.parametrize("suffix", ["first", "changed"])
def test_worker_uses_config_dataclasses_and_parser_defaults(monkeypatch, suffix):
    specs = {c: {f"{c}-{suffix}": Spec(f"{c}-{suffix}")} for c in ("stt", "vision", "llm")}
    cfg = SimpleNamespace(**specs)
    cfg.default_name = lambda category: next(iter(specs[category]))
    cfg.resolve = lambda category, name=None: (Provider(), specs[category][name or cfg.default_name(category)])
    parser = argparse.ArgumentParser()
    parser.add_argument("--fps", type=float, default=3.25)
    modules = {
        "absorbent": ModuleType("absorbent"),
        "absorbent.cli": SimpleNamespace(MEDIA_EXTENSIONS={".one"}, build_parser=lambda: parser),
        "absorbent.config": SimpleNamespace(load_config=lambda path: cfg),
        "absorbent.slides": SimpleNamespace(MATERIAL_EXTENSIONS={".two"}),
        "absorbent.version": SimpleNamespace(get_version_info=lambda: {"version": "1.2.3", "commit": "b" * 40, "dirty": False}),
    }
    for key, value in modules.items():
        monkeypatch.setitem(sys.modules, key, value)
    monkeypatch.setattr(probe, "distribution", lambda name: SimpleNamespace(read_text=lambda path: '{"dir_info":{"editable":false}}'))
    monkeypatch.setattr(probe, "_tex_roots", lambda: ["/etc/texmf"])
    result = probe.collect("server-config")
    assert result["defaults"]["fps"] == 3.25
    assert result["defaults"]["llm"] == f"llm-{suffix}"
    assert result["models"]["vision"][f"vision-{suffix}"]["spec"]["api_model"] == "dynamic-api-name"
    assert result["models"]["llm"][f"llm-{suffix}"]["local"] is True
    assert result["media_extensions"] == [".one"]
    assert result["material_extensions"] == [".two"]
    assert set(result) == {"version_info", "installed_editable", "models", "defaults", "media_extensions", "material_extensions", "runtime"}
    assert "DYNAMIC_ENV" in json.dumps(result)
    assert "api_key" not in result["models"]["llm"][f"llm-{suffix}"]["provider"]


def test_worker_safe_failure(monkeypatch, capsys):
    def fail(path):
        raise ValueError("private-api-key")
    monkeypatch.setattr(probe, "collect", fail)
    assert probe.main(["config"]) == 2
    assert "private-api-key" not in capsys.readouterr().err


def test_tex_format_discovery_has_correct_engine(monkeypatch, tmp_path):
    fmt = tmp_path / "texmf" / "luahbtex" / "lualatex.fmt"
    fmt.parent.mkdir(parents=True)
    fmt.write_text("format")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=str(fmt if "-format=fmt" in argv else tmp_path / "texmf"))
    monkeypatch.setattr(probe.subprocess, "run", run)
    roots = probe._tex_roots()
    assert str(fmt.parent) in roots
    assert str(tmp_path / "texmf") in roots
    assert all("-engine=luahbtex" in argv and "-progname=lualatex" in argv for argv in calls)
