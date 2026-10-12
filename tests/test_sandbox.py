"""Границы изоляции: профиль bwrap, идентичность процесса и отчёт о ресурсах."""

import asyncio
import itertools
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from absorbobot import config, sandbox
from absorbobot.workers import supervise

WORK = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
OTHER = "ffffffff-ffff-ffff-ffff-ffffffffffff"
WORKERS = Path(sandbox.__file__).parent / "workers"
SUPERVISE = str(WORKERS / "supervise.py")


class FakeStore:
    """Минимальная замена Store: sandbox касается только привязок процессов."""

    def __init__(self, bindings=()):
        self.bindings = list(bindings)
        self.writes = []

    def process_bindings(self):
        return list(self.bindings)

    def bind_process(self, work_uuid, binding):
        self.writes.append((work_uuid, None if binding is None else dict(binding)))


@pytest.fixture
def settings(tmp_path):
    return config.Settings(
        storage_dir=tmp_path / "disk" / "data", backing_mount=tmp_path / "disk",
        state_dir=tmp_path / "state", secrets_file=tmp_path / "secrets.env", min_free_bytes=1)


@pytest.fixture
def store():
    return FakeStore()


def make_runtime(root):
    """Собрать установленное окружение: base_prefix с библиотекой и venv с receipt."""
    base = root / "env" / "base"
    environment = root / "env" / "venv"
    (base / "bin").mkdir(parents=True)
    (base / "lib" / "python3.13").mkdir(parents=True)
    interpreter = base / "bin" / "python"
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    (environment / "bin").mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = " + str(base / "bin") + "\n")
    (environment / "bin" / "python").symlink_to(interpreter)
    return {"tool_env": str(environment), "python_executable": str(environment / "bin" / "python"),
            "python_base_prefix": str(base)}


def make_box(settings, store, *, bwrap="/usr/bin/bwrap"):
    box = sandbox.Sandbox(settings, store)
    box._bwrap = bwrap
    return box


def mounts(command, flag):
    """Пары источник/цель конкретного флага монтирования."""
    return [(command[index + 1], command[index + 2])
            for index, token in enumerate(command[:-2]) if token == flag]


def go_document(phase):
    return json.dumps({"schema_version": 1, "phase_id": phase, "action": "go"})


def dead_pid():
    """Собственный завершённый потомок: /proc о нём больше не знает."""
    process = subprocess.Popen(["/bin/true"])
    process.wait()
    return process.pid


# --- Установленное окружение ---


@pytest.mark.parametrize("change", [
    {"tool_env": "relative/env"},
    {"tool_env": "/"},
    {"tool_env": 5},
    {"python_executable": "/usr/bin/python3"},
    {"python_base_prefix": "/"},
])
def test_runtime_rejects_unsafe_paths(settings, store, tmp_path, change):
    runtime = make_runtime(tmp_path)
    runtime.update(change)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        make_box(settings, store)._runtime(runtime)
    assert excinfo.value.code == "invalid_runtime"


def test_runtime_requires_venv_receipt(settings, store, tmp_path):
    runtime = make_runtime(tmp_path)
    (Path(runtime["tool_env"]) / "pyvenv.cfg").unlink()
    with pytest.raises(sandbox.SandboxError) as excinfo:
        make_box(settings, store)._runtime(runtime)
    assert excinfo.value.code == "invalid_runtime"


def test_runtime_requires_base_library(settings, store, tmp_path):
    runtime = make_runtime(tmp_path)
    (Path(runtime["python_base_prefix"]) / "lib" / "python3.13").rmdir()
    with pytest.raises(sandbox.SandboxError) as excinfo:
        make_box(settings, store)._runtime(runtime)
    assert excinfo.value.code == "invalid_runtime"


def test_runtime_inside_state_or_storage_rejected(settings, store):
    for root in (settings.state_dir, settings.storage_dir):
        with pytest.raises(sandbox.SandboxError) as excinfo:
            make_box(settings, store)._runtime(make_runtime(root))
        assert excinfo.value.code == "invalid_runtime"


# --- Окружение worker ---


def test_environment_allowlist_and_network_proxy(settings, store, tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("SECRET_TOKEN", "leak")
    box = make_box(settings, store)
    online = box.build_environment("analyze", runtime=runtime, network=True)
    assert online["LANG"] == "ru_RU.UTF-8"
    # Маршрут загрузок задаёт только явная настройка, а не окружение.
    assert "HTTPS_PROXY" not in online
    assert "SECRET_TOKEN" not in online
    assert online["HOME"] == "/work/tmp/home"
    assert online["TMPDIR"] == "/work/tmp"
    assert online["XDG_CACHE_HOME"] == "/work/tmp/cache"
    assert online["UV_OFFLINE"] == "1" and online["UV_TOOL_DIR"] == "/work/tmp/uv-tools"
    assert online["PATH"] == runtime["tool_env"] + "/bin:/usr/bin:/bin"
    offline = box.build_environment("analyze", runtime=runtime, network=False)
    assert "HTTPS_PROXY" not in offline


def test_environment_exposes_writable_tex_cache(settings, store, tmp_path):
    clean = make_box(settings, store).build_environment("render", runtime=make_runtime(tmp_path),
                                                        network=False)
    assert clean["TEXMFVAR"] == "/work/tmp/texmf" and clean["TEXMFCACHE"] == "/work/tmp/texmf"


def test_environment_rejects_non_mapping(settings, store, tmp_path):
    with pytest.raises(sandbox.SandboxError) as excinfo:
        make_box(settings, store).build_environment("analyze", runtime=make_runtime(tmp_path),
                                                    env=["LANG=x"])
    assert excinfo.value.code == "invalid_environment"


def test_environment_drops_broken_values(settings, store, tmp_path):
    clean = make_box(settings, store).build_environment("analyze", runtime=make_runtime(tmp_path),
                                                        env={"LANG": "a\x00b"})
    assert clean["LANG"] == "C.UTF-8"


def test_selected_keys_only_for_networked_synthesis(settings, store, tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    box = make_box(settings, store)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-value")
    synthesis = box.build_environment("synthesize", runtime=runtime, network=True,
                                      selected_key_names=("DEEPSEEK_API_KEY",))
    assert synthesis["DEEPSEEK_API_KEY"] == "secret-value"
    render = box.build_environment("render", runtime=runtime, network=False,
                                   selected_key_names=("DEEPSEEK_API_KEY",))
    assert "DEEPSEEK_API_KEY" not in render


def test_selected_key_from_supplied_environment(settings, store, tmp_path):
    clean = make_box(settings, store).build_environment(
        "synthesize", runtime=make_runtime(tmp_path), env={"MISTRAL_API_KEY": "k"},
        selected_key_names=("MISTRAL_API_KEY",))
    assert clean["MISTRAL_API_KEY"] == "k"


@pytest.mark.parametrize("name", [
    "ABSORBOBOT_TOKEN", "TELEGRAM_API_HASH", "PYTHONPATH", "CUDA_VISIBLE_DEVICES",
    "LD_PRELOAD", "lower", "WITH-DASH", "HOME", "PATH", ""])
def test_selected_key_names_rejected(settings, store, tmp_path, name):
    with pytest.raises(sandbox.SandboxError) as excinfo:
        make_box(settings, store).build_environment("synthesize", runtime=make_runtime(tmp_path),
                                                    selected_key_names=(name,))
    assert excinfo.value.code == "invalid_key_name"


def test_selected_key_without_value_rejected(settings, store, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        make_box(settings, store).build_environment("synthesize", runtime=make_runtime(tmp_path),
                                                    selected_key_names=("OPENAI_API_KEY",))
    assert excinfo.value.code == "missing_model_key"


# --- Профиль монтирования ---


def test_command_requires_bwrap(settings, store, tmp_path):
    box = make_box(settings, store, bwrap=None)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        box.build_command(WORK, "analyze", ["/usr/bin/python3"], runtime=make_runtime(tmp_path))
    assert excinfo.value.code == "sandbox_unavailable"


@pytest.mark.parametrize("phase", ["", "Bad", "1bad", "a" * 49, "phase.png", None])
def test_command_rejects_invalid_phase(settings, store, tmp_path, phase):
    with pytest.raises(sandbox.SandboxError) as excinfo:
        make_box(settings, store).build_command(WORK, phase, ["/usr/bin/python3"],
                                                runtime=make_runtime(tmp_path))
    assert excinfo.value.code == "invalid_phase"


@pytest.mark.parametrize("argv", [None, "python", [], [""], ["python", 5], ["python", "a\x00b"]])
def test_command_rejects_invalid_argv(settings, store, tmp_path, argv):
    with pytest.raises(sandbox.SandboxError) as excinfo:
        make_box(settings, store).build_command(WORK, "analyze", argv,
                                                runtime=make_runtime(tmp_path))
    assert excinfo.value.code == "invalid_argv"






def test_tex_roots_reject_foreign_and_missing(settings, store):
    box = make_box(settings, store)
    for roots in (["relative"], ["/opt/tex"], ["/usr/share/absent-directory"], [5]):
        with pytest.raises(sandbox.SandboxError) as excinfo:
            box._tex_roots({}, tuple(roots))
        assert excinfo.value.code == "invalid_runtime"


def test_tex_roots_accept_system_share_and_runtime(settings, store):
    box = make_box(settings, store)
    share = sandbox.Path("/usr/share")
    assert box._tex_roots({}, ("/usr/share",)) == [share]
    assert box._tex_roots({"tex_roots": ["/usr/share"]}, ()) == [share]


def test_render_phase_requires_and_binds_tex_roots(settings, store, tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    box = make_box(settings, store)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        box.build_command(WORK, "render", ["/usr/bin/python3"], runtime=runtime)
    assert excinfo.value.code == "invalid_runtime"
    foreign = tmp_path / "texlive"
    foreign.mkdir()
    monkeypatch.setattr(sandbox.Sandbox, "_tex_roots", lambda self, runtime, roots: [foreign])
    command = box.build_command(WORK, "render", ["/usr/bin/python3"], runtime=runtime, network=False)
    assert (str(foreign), str(foreign)) in mounts(command, "--ro-bind")


def test_root_rejects_foreign_uuid(settings, store):
    for work_uuid in ("", "abc", WORK.upper(), WORK.replace("-", ""), None):
        with pytest.raises(sandbox.SandboxError) as excinfo:
            make_box(settings, store)._root(work_uuid)
        assert excinfo.value.code == "invalid_work_uuid"


def test_root_rejects_symlinked_tree(settings, store, tmp_path):
    box = make_box(settings, store)
    jobs = settings.storage_dir / "jobs"
    jobs.mkdir(parents=True)
    (jobs / WORK).symlink_to(settings.state_dir)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        box._root(WORK)
    assert excinfo.value.code == "unsafe_work_tree"
    (jobs / WORK).unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    jobs.rmdir()
    jobs.symlink_to(outside)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        box._root(WORK)
    assert excinfo.value.code == "unsafe_work_tree"


# --- Разбор машинных документов ---


def test_status_child_reads_first_bwrap_document(tmp_path):
    root = tmp_path / "root"
    resources = root / "tmp" / "resources"
    resources.mkdir(parents=True)
    status = resources / "status.jsonl"
    status.write_bytes(b'{ "child-pid": 4242, "ipc-namespace": 7 }\n{ "exit-code": 0 }\n')
    assert sandbox._status_child(status, root) == 4242
    status.write_bytes(b'{ "child-pid": 4242 }')
    assert sandbox._status_child(status, root) is None
    status.write_bytes(b'not json\n')
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox._status_child(status, root)
    assert excinfo.value.code == "invalid_report"
    status.write_bytes(b'{ "child-pid": 1 }\n')
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox._status_child(status, root)
    assert excinfo.value.code == "invalid_report"
    status.unlink()
    status.symlink_to(resources)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox._status_child(status, root)
    assert excinfo.value.code == "invalid_report"
    assert sandbox._status_child(resources / "missing.jsonl", root) is None
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox._status_child(tmp_path / "outside.jsonl", root)
    assert excinfo.value.code == "unsafe_work_tree"


def test_bounded_json_limits_and_shape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    good = root / "report.json"
    good.write_text('{"schema_version": 1}')
    assert sandbox._bounded_json(good, root) == {"schema_version": 1}
    assert sandbox._bounded_json(root / "none.json", root) is None
    (root / "array.json").write_text("[1]")
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox._bounded_json(root / "array.json", root)
    assert excinfo.value.code == "invalid_report"
    big = root / "big.json"
    big.write_text("[" + "0," * 40000 + "0]")
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox._bounded_json(big, root)
    assert excinfo.value.code == "invalid_report"
    link = root / "link.json"
    link.symlink_to(good)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox._bounded_json(link, root)
    assert excinfo.value.code == "invalid_report"


def test_work_bytes_counts_and_stops(tmp_path):
    root = tmp_path / "root"
    (root / "input").mkdir(parents=True)
    (root / "input" / "file.bin").write_bytes(b"x" * 100)
    assert sandbox._work_bytes(root, 10 ** 9) >= 100
    assert sandbox._work_bytes(root, 1) > 1


def test_resource_report_keeps_only_public_schema(settings, store, tmp_path):
    box = make_box(settings, store)
    root = tmp_path / "root"
    resources = root / "tmp" / "resources"
    resources.mkdir(parents=True)
    path = resources / f"{WORK}.report.json"
    base = {"schema_version": 1, "phase_id": WORK, "exit_code": 3, "cpu_user_seconds": 1.5,
            "cpu_system_seconds": 0.5, "ru_maxrss_kib": 4096, "completeness": "complete",
            "secret": "leak", "argv": ["--token", "x"]}
    path.write_text(json.dumps(base))
    assert box._resources(path, root, WORK) == {
        "schema_version": 1, "phase_id": WORK, "exit_code": 3, "cpu_user_seconds": 1.5,
        "cpu_system_seconds": 0.5, "ru_maxrss_kib": 4096, "completeness": "complete"}
    missing = box._resources(resources / "none.json", root, WORK)
    assert missing == sandbox._unknown_resources(WORK)
    assert missing["reason"] == "missing_report"
    for change in ({"phase_id": "other"}, {"schema_version": 2}, {"exit_code": "3"},
                   {"exit_code": True}, {"completeness": "maybe"}, {"cpu_user_seconds": -1},
                   {"ru_maxrss_kib": True}, {"cpu_user_seconds": float("nan")}):
        path.write_text(json.dumps({**base, **change}))
        report = box._resources(path, root, WORK)
        assert report["completeness"] == "partial"
        assert report["reason"] == "invalid_report"
    path.write_text(json.dumps([1]))
    assert box._resources(path, root, WORK)["reason"] == "invalid_report"


# --- Идентичность процессов ---


def test_process_rejects_unsafe_pid():
    for pid in (1, 0, -3, "42", None):
        with pytest.raises(sandbox.SandboxError) as excinfo:
            sandbox._process(pid)
        assert excinfo.value.code == "invalid_binding"


def test_same_process_rejects_caller_and_ancestors():
    identity = sandbox._process(os.getpid())
    assert identity is not None and identity.uid == os.getuid()
    assert identity.parent == os.getppid() and identity.alive is True
    with pytest.raises(sandbox.SandboxError) as excinfo:
        sandbox._same_process(os.getpid(), 1)
    assert excinfo.value.code == "unsafe_identity"
    for pid, start in ((os.getpid(), "1"), (os.getpid(), 0), ("x", 1)):
        with pytest.raises(sandbox.SandboxError) as excinfo:
            sandbox._same_process(pid, start)
        assert excinfo.value.code == "invalid_binding"
    assert sandbox._same_process(dead_pid(), 1) is None
    parent = os.getppid()
    if parent > 1:
        parent_identity = sandbox._process(parent)
        assert parent_identity is not None
        with pytest.raises(sandbox.SandboxError) as excinfo:
            sandbox._same_process(parent, parent_identity.start_time)
        assert excinfo.value.code == "unsafe_identity"


def test_boot_and_namespace_identity():
    boot = sandbox._boot_id()
    assert len(boot) == 36 and boot.count("-") == 4
    assert sandbox._namespace_id(os.getpid()) > 0
    dead = dead_pid()
    if not Path(f"/proc/{dead}").exists():
        with pytest.raises(sandbox.SandboxError) as excinfo:
            sandbox._namespace_id(dead)
        assert excinfo.value.code == "identity_unavailable"


def test_namespace_group_rejects_caller_and_launcher_group(settings, store, monkeypatch):
    box = make_box(settings, store)
    group = os.getpgrp()
    fake = sandbox._Process(pid=group, start_time=7, parent=1, pgid=group,
                            session_id=group, state="S", uid=os.getuid())
    monkeypatch.setattr(sandbox, "_same_process",
                        lambda pid, start: fake if pid == group else None)
    monkeypatch.setattr(sandbox, "_namespace_id", lambda pid: 99 if pid == group else 1)
    binding = {"namespace_pid": group, "namespace_start_time": 7, "namespace_id": 99,
               "pgid": group, "session_id": group}
    with pytest.raises(sandbox.SandboxError) as excinfo:
        box._namespace(binding, group=True)
    assert excinfo.value.code == "unsafe_identity"


def test_stop_binding_refuses_caller_identity(settings, store):
    box = make_box(settings, store)
    identity = sandbox._process(os.getpid())
    binding = {"schema_version": 1, "work_uuid": WORK, "phase_id": WORK,
               "boot_id": sandbox._boot_id(), "launcher_pid": os.getpid(),
               "launcher_start_time": identity.start_time,
               "launcher_executable": "/usr/bin/bwrap"}
    with pytest.raises(sandbox.SandboxError) as excinfo:
        asyncio.run(box._stop_binding(binding))
    assert excinfo.value.code == "unsafe_identity"


def test_stop_binding_refuses_namespace_pid_equal_caller(settings, store):
    box = make_box(settings, store)
    identity = sandbox._process(os.getpid())
    dead = dead_pid()
    binding = {"schema_version": 1, "work_uuid": WORK, "phase_id": WORK,
               "boot_id": sandbox._boot_id(), "launcher_pid": dead, "launcher_start_time": 1,
               "launcher_executable": "/usr/bin/bwrap", "namespace_pid": os.getpid(),
               "namespace_start_time": identity.start_time, "namespace_id": 1,
               "pgid": identity.pid, "session_id": identity.pid}
    with pytest.raises(sandbox.SandboxError) as excinfo:
        asyncio.run(box._stop_binding(binding))
    assert excinfo.value.code == "unsafe_identity"


def test_stop_binding_ignores_dead_identity(settings, store):
    box = make_box(settings, store)
    dead = dead_pid()
    binding = {"schema_version": 1, "work_uuid": WORK, "phase_id": WORK,
               "boot_id": sandbox._boot_id(), "launcher_pid": dead, "launcher_start_time": 1,
               "launcher_executable": "/usr/bin/bwrap"}
    asyncio.run(box._stop_binding(binding))


# --- Запуск, отмена и восстановление ---


def test_run_rejects_invalid_timeout(settings, store, tmp_path):
    box = make_box(settings, store)
    runtime = make_runtime(tmp_path)
    for timeout in (0, -1, float("nan"), float("inf"), True, "60"):
        with pytest.raises(sandbox.SandboxError) as excinfo:
            asyncio.run(box.run(WORK, "analyze", ["/usr/bin/python3"], runtime=runtime,
                                timeout=timeout))
        assert excinfo.value.code == "invalid_timeout"


def test_run_stops_without_real_mount(settings, store, tmp_path):
    settings.backing_mount.mkdir(parents=True)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        asyncio.run(make_box(settings, store).run(WORK, "youtube", ["/usr/bin/python3"],
                                                  runtime=make_runtime(tmp_path)))
    assert excinfo.value.code == "storage_unavailable"
    assert not (settings.storage_dir / "jobs" / WORK).exists()


def test_run_without_bwrap_never_executes_unisolated(settings, store, tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setattr(sandbox, "create_work_tree", lambda settings_, work_uuid: root)
    box = make_box(settings, store, bwrap=None)
    argv = [runtime["python_executable"], "/workers/youtube.py", "identifier"]
    for attempt in range(2):
        with pytest.raises(sandbox.SandboxError) as excinfo:
            asyncio.run(box.run(WORK, "youtube", argv, runtime=runtime))
        assert excinfo.value.code == "sandbox_unavailable"
        assert len(list((root / "tmp" / "logs").iterdir())) == 2 * (attempt + 1)
    assert store.writes == []


def test_run_refuses_while_stale_binding_exists(settings, store, tmp_path):
    store.bindings = [{"work_uuid": WORK, "binding": {"schema_version": 1, "work_uuid": WORK}}]
    with pytest.raises(sandbox.SandboxError) as excinfo:
        asyncio.run(make_box(settings, store).run(WORK, "analyze", ["/usr/bin/python3"],
                                                  runtime=make_runtime(tmp_path)))
    assert excinfo.value.code == "recovery_required"


def test_cancel_without_execution_is_noop(settings, store):
    box = make_box(settings, store)
    asyncio.run(box.cancel(WORK))
    assert box._active is None


def test_shutdown_is_idempotent_and_closes_sandbox(settings, store, tmp_path):
    box = make_box(settings, store)
    asyncio.run(box.shutdown())
    asyncio.run(box.shutdown())
    with pytest.raises(sandbox.SandboxError) as excinfo:
        asyncio.run(box.run(WORK, "analyze", ["/usr/bin/python3"], runtime=make_runtime(tmp_path)))
    assert excinfo.value.code == "sandbox_closed"


def test_recover_rejects_foreign_binding_schema(settings, store):
    box = make_box(settings, store)
    with pytest.raises(sandbox.SandboxError) as excinfo:
        asyncio.run(box.recover([{"work_uuid": WORK,
                                  "binding": {"schema_version": 2, "work_uuid": WORK}}]))
    assert excinfo.value.code == "invalid_binding"
    assert box._fatal is True


def test_recover_clears_identity_from_other_boot(settings, store):
    box = make_box(settings, store)
    binding = {"schema_version": 1, "work_uuid": WORK, "phase_id": WORK,
               "boot_id": "00000000-0000-0000-0000-000000000000", "launcher_pid": 4194304,
               "launcher_start_time": 1, "launcher_executable": "/usr/bin/bwrap"}
    asyncio.run(box.recover([{"work_uuid": WORK, "binding": binding}]))
    assert box._fatal is False
    assert store.writes == [(WORK, None)]


def test_sandbox_error_exposes_only_code():
    error = sandbox.SandboxError("work_limit", limit=10)
    assert str(error) == "work_limit"
    assert error.code == "work_limit" and error.details == {"limit": 10}


# --- Supervisor внутри namespace ---


def test_read_go_waits_for_complete_document(tmp_path):
    path = tmp_path / "go.json"
    assert supervise._read_go(path, WORK) is False
    path.write_bytes(go_document(WORK)[:-1].encode())
    assert supervise._read_go(path, WORK) is False
    path.write_text(go_document(WORK))
    assert supervise._read_go(path, WORK) is True
    assert supervise._read_go(path, OTHER) is False
    path.write_text(json.dumps({"schema_version": 1, "phase_id": WORK, "action": "stop"}))
    assert supervise._read_go(path, WORK) is False
    path.write_text(json.dumps({"schema_version": 1, "phase_id": WORK}))
    assert supervise._read_go(path, WORK) is False
    path.write_bytes(b"x" * 5000)
    with pytest.raises(ValueError):
        supervise._read_go(path, WORK)


def test_atomic_json_leaves_no_partial_file(tmp_path):
    path = tmp_path / "ready.json"
    supervise._atomic_json(path, {"schema_version": 1})
    assert json.loads(path.read_text()) == {"schema_version": 1}
    assert not list(tmp_path.glob("*.writing"))


def test_signal_handler_forwards_only_started_child(monkeypatch):
    delivered = []
    monkeypatch.setattr(supervise.os, "kill", lambda pid, signum: delivered.append((pid, signum)))
    monkeypatch.setattr(supervise, "_CHILD_PID", 4321)
    monkeypatch.setattr(supervise, "_PENDING_SIGNAL", None)
    supervise._signal(signal.SIGTERM, None)
    assert supervise._PENDING_SIGNAL == signal.SIGTERM
    assert delivered == [(4321, signal.SIGTERM)]
    monkeypatch.setattr(supervise, "_CHILD_PID", None)
    supervise._signal(signal.SIGINT, None)
    assert delivered == [(4321, signal.SIGTERM)]
    assert supervise._PENDING_SIGNAL == signal.SIGINT


def test_supervise_waits_for_go_and_reports_child(tmp_path):
    ready = tmp_path / "ready.json"
    go = tmp_path / "go.json"
    report = tmp_path / "report.json"
    go.write_text(go_document(WORK))
    result = subprocess.run(
        [sys.executable, SUPERVISE, "--phase-id", WORK, "--ready", str(ready), "--go", str(go),
         "--report", str(report), "--", sys.executable, "-c", "import sys; sys.exit(7)"],
        capture_output=True, timeout=60)
    assert result.returncode == 7
    ready_document = json.loads(ready.read_text())
    assert ready_document["schema_version"] == 1 and ready_document["phase_id"] == WORK
    assert all(type(ready_document[key]) is int and ready_document[key] > 0
               for key in ("pid", "pgid", "session_id"))
    value = json.loads(report.read_text())
    assert value["schema_version"] == 1 and value["phase_id"] == WORK and value["exit_code"] == 7
    assert value["cpu_user_seconds"] >= 0 and value["cpu_system_seconds"] >= 0
    assert value["ru_maxrss_kib"] > 0
    assert value["completeness"] in ("complete", "partial")


def test_supervise_maps_signal_death_to_shell_code(tmp_path):
    ready = tmp_path / "ready.json"
    go = tmp_path / "go.json"
    report = tmp_path / "report.json"
    go.write_text(go_document(WORK))
    result = subprocess.run(
        [sys.executable, SUPERVISE, "--phase-id", WORK, "--ready", str(ready), "--go", str(go),
         "--report", str(report), "--", sys.executable, "-c",
         "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
        capture_output=True, timeout=60)
    assert result.returncode == 137
    assert json.loads(report.read_text())["exit_code"] == -9


def test_supervise_without_go_expires_to_config_error(tmp_path, monkeypatch):
    # Часы ускоряются только в текущем процессе: срок ожидания GO наступает сразу.
    clock = itertools.count(0, 1000)
    monkeypatch.setattr(supervise.time, "monotonic", lambda: next(clock))
    ready = tmp_path / "ready.json"
    report = tmp_path / "report.json"
    saved = {signum: signal.getsignal(signum)
             for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        code = supervise.supervise(WORK, ready, tmp_path / "go.json", report,
                                   [sys.executable, "-c", "pass"])
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)
    assert code == 125
    assert json.loads(ready.read_text())["phase_id"] == WORK
    assert not report.exists()


def test_supervise_main_requires_payload_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["supervise.py", "--phase-id", WORK, "--ready",
                                      str(tmp_path / "ready.json"), "--go", str(tmp_path / "go.json"),
                                      "--report", str(tmp_path / "report.json")])
    assert supervise.main() == 125
    assert not (tmp_path / "ready.json").exists()


def test_supervise_main_strips_separator(tmp_path):
    go = tmp_path / "go.json"
    report = tmp_path / "report.json"
    go.write_text(go_document(WORK))
    result = subprocess.run(
        [sys.executable, SUPERVISE, "--phase-id", WORK, "--ready", str(tmp_path / "ready.json"),
         "--go", str(go), "--report", str(report), "--", sys.executable, "-c", "pass"],
        capture_output=True, timeout=60)
    assert result.returncode == 0
    assert json.loads(report.read_text())["exit_code"] == 0
