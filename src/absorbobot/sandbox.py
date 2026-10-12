"""Изоляция фаз, доказуемая остановка namespace и ресурсы payload."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from .config import ConfigError, check_storage, create_work_tree


class SandboxError(Exception):
    """Безопасная причина без секретов, argv и серверных путей."""

    def __init__(self, code: str, **details: object):
        super().__init__(code)
        self.code = code
        self.details = details


_PUBLIC_ENV = frozenset({
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "HTTP_PROXY", "HTTPS_PROXY",
    "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "CUDA_MODULE_LOADING", "PYTORCH_CUDA_ALLOC_CONF", "PYTORCH_ALLOC_CONF",
})
_PROXY_ENV = frozenset(name for name in _PUBLIC_ENV if name.lower().endswith("proxy"))
_KEY_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_PHASE = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_GRACE_SECONDS = 10.0
_STARTUP_SECONDS = 30.0
_READ_LIMIT = 65536


@dataclass(frozen=True)
class _Process:
    pid: int
    start_time: int
    parent: int
    pgid: int
    session_id: int
    state: str
    uid: int

    @property
    def alive(self) -> bool:
        return self.state not in ("Z", "X")


def _process(pid: int) -> _Process | None:
    if type(pid) is not int or pid <= 1:
        raise SandboxError("invalid_binding")
    try:
        root = Path("/proc") / str(pid)
        # comm может содержать пробелы и закрывающие скобки.
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        return _Process(pid, int(fields[19]), int(fields[1]), int(fields[2]),
                        int(fields[3]), fields[0], root.stat().st_uid)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError):
        raise SandboxError("identity_unavailable") from None


def _boot_id() -> str:
    try:
        return str(UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
    except (OSError, ValueError):
        raise SandboxError("identity_unavailable") from None


def _namespace_id(pid: int) -> int:
    try:
        return (Path("/proc") / str(pid) / "ns/pid").stat().st_ino
    except OSError:
        raise SandboxError("identity_unavailable") from None


def _safe_pid(pid: int) -> None:
    ancestor = os.getpid()
    while ancestor > 1:
        if ancestor == pid:
            raise SandboxError("unsafe_identity")
        process = _process(ancestor)
        if process is None:
            break
        ancestor = process.parent


def _same_process(pid: object, start_time: object) -> _Process | None:
    if type(pid) is not int or type(start_time) is not int or start_time <= 0:
        raise SandboxError("invalid_binding")
    _safe_pid(pid)
    process = _process(pid)
    if process is None or process.start_time != start_time or not process.alive:
        return None
    if process.uid != os.getuid():
        raise SandboxError("unsafe_identity")
    return process


def _bounded_json(path: Path, root: Path) -> dict | None:
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            raise SandboxError("unsafe_work_tree")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise SandboxError("invalid_report") from None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SandboxError("invalid_report")
        raw = stream.read(_READ_LIMIT + 1)
    if len(raw) > _READ_LIMIT:
        raise SandboxError("invalid_report")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SandboxError("invalid_report") from None
    if not isinstance(value, dict):
        raise SandboxError("invalid_report")
    return value


def _status_child(path: Path, root: Path) -> int | None:
    # bwrap пишет несколько JSON документов; до READY нужна только первая строка.
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            raise SandboxError("unsafe_work_tree")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise SandboxError("invalid_report") from None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SandboxError("invalid_report")
        line = stream.readline(_READ_LIMIT + 1)
    if not line.endswith(b"\n"):
        if len(line) >= _READ_LIMIT:
            raise SandboxError("invalid_report")
        return None
    try:
        value = json.loads(line)
        pid = value["child-pid"]
    except (ValueError, KeyError, TypeError):
        raise SandboxError("invalid_report") from None
    if type(pid) is not int or pid <= 1:
        raise SandboxError("invalid_report")
    return pid


def _work_bytes(root: Path, limit: int) -> int:
    total = 0
    directories = [root]
    while directories:
        with os.scandir(directories.pop()) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                    total += info.st_size
                    if stat.S_ISDIR(info.st_mode):
                        directories.append(Path(entry.path))
                except FileNotFoundError:
                    continue
                if total > limit:
                    return total
    return total


def _unknown_resources(phase_id: str, reason: str = "missing_report") -> dict:
    return {"schema_version": 1, "phase_id": phase_id, "exit_code": None,
            "cpu_user_seconds": None, "cpu_system_seconds": None,
            "ru_maxrss_kib": None, "completeness": "partial", "reason": reason}


@dataclass
class _Active:
    work_uuid: str
    phase_id: str
    root: Path
    process: subprocess.Popen
    binding: dict
    wait_task: asyncio.Task
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Sandbox:
    """Один execution lock; очистка binding допустима только после reap."""

    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self._lock = asyncio.Lock()
        self._active: _Active | None = None
        self._closed = False
        self._fatal = False
        self._bwrap = shutil.which("bwrap")
        self._workers = Path(__file__).parent / "workers"

    def _root(self, work_uuid: str) -> Path:
        try:
            if str(UUID(work_uuid)) != work_uuid:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise SandboxError("invalid_work_uuid") from None
        root = self.settings.storage_dir / "jobs" / work_uuid
        if (root.is_symlink() or root.parent.is_symlink()
                or not root.resolve().is_relative_to(self.settings.storage_dir.resolve())):
            raise SandboxError("unsafe_work_tree")
        return root

    def _runtime(self, runtime: dict) -> tuple[Path, Path, Path]:
        if not isinstance(runtime, dict):
            raise SandboxError("invalid_runtime")
        paths = []
        for key in ("tool_env", "python_executable", "python_base_prefix"):
            value = runtime.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute() or any(c in value for c in "\n\r\x00"):
                raise SandboxError("invalid_runtime")
            paths.append(Path(value))
        environment, python, base = paths
        forbidden = [Path("/"), Path("/home"), Path.home(), Path("/root"), Path("/etc"),
                     Path("/var"), Path("/run"), Path("/tmp"), self.settings.storage_dir,
                     self.settings.state_dir]
        for path in (environment, base):
            resolved = path.resolve()
            if any(resolved == item.resolve() or item.resolve().is_relative_to(resolved) for item in forbidden):
                raise SandboxError("invalid_runtime")
            if resolved.is_relative_to(self.settings.state_dir.resolve()) or resolved.is_relative_to(self.settings.storage_dir.resolve()):
                raise SandboxError("invalid_runtime")
            if not path.is_dir():
                raise SandboxError("invalid_runtime")
        if (not (environment / "pyvenv.cfg").is_file()
                or not python.is_relative_to(environment / "bin")
                or not python.is_file() or not os.access(python, os.X_OK)
                or not python.resolve().is_relative_to(base.resolve())):
            raise SandboxError("invalid_runtime")
        # Не разрешать монтирование произвольного предка под видом Python runtime.
        libraries = base / "lib"
        if not libraries.is_dir() or not any(
            re.fullmatch(r"python\d+\.\d+", entry.name) and entry.is_dir()
            for entry in libraries.iterdir()
        ):
            raise SandboxError("invalid_runtime")
        return environment, python, base

    def _python_base_aliases(self, python: Path, base: Path) -> tuple[Path, ...]:
        """Сохранить точные пути Python, на которые ссылается окружение uv."""
        aliases = set()
        cursor = python
        for _ in range(40):
            if not cursor.is_symlink():
                return tuple(sorted(aliases))
            target = Path(os.readlink(cursor))
            if not target.is_absolute():
                target = cursor.parent / target
            cursor = Path(os.path.abspath(target))
            for parent in cursor.parents:
                if parent != base and parent.resolve() == base.resolve():
                    if (parent.is_relative_to(self.settings.state_dir)
                            or parent.is_relative_to(self.settings.storage_dir)):
                        raise SandboxError("invalid_runtime")
                    aliases.add(parent)
                    break
        raise SandboxError("invalid_runtime")

    def build_environment(self, phase: str, *, runtime: dict, env: dict | None = None,
                          network: bool = True, selected_key_names: tuple[str, ...] = ()) -> dict[str, str]:
        """Выбрать публичные переменные и только ключи выбранного синтеза."""
        environment, _, _ = self._runtime(runtime)
        supplied = {} if env is None else env
        if not isinstance(supplied, dict):
            raise SandboxError("invalid_environment")
        values = {key: supplied.get(key, os.environ.get(key)) for key in _PUBLIC_ENV}
        clean = {key: value for key, value in values.items()
                 if isinstance(value, str) and "\x00" not in value and key not in _PROXY_ENV}
        # Прокси задаётся только явной настройкой: окружение ненадёжно и не должно
        # менять маршрут загрузок незаметно для администратора.
        clean.update({
            "PATH": f"{environment}/bin:/usr/bin:/bin", "LANG": clean.get("LANG", "C.UTF-8"),
            "HOME": "/work/tmp/home", "TMPDIR": "/work/tmp", "TMP": "/work/tmp", "TEMP": "/work/tmp",
            "XDG_CACHE_HOME": "/work/tmp/cache", "XDG_CONFIG_HOME": "/work/tmp/home/.config",
            "XDG_DATA_HOME": "/work/tmp/home/.local/share", "XDG_STATE_HOME": "/work/tmp/home/.local/state",
            "UV_CACHE_DIR": "/work/tmp/cache/uv", "UV_TOOL_DIR": "/work/tmp/uv-tools",
            "UV_TOOL_BIN_DIR": "/work/tmp/bin", "UV_NO_CONFIG": "1", "UV_OFFLINE": "1",
            "MPLCONFIGDIR": "/work/tmp/cache/matplotlib", "TORCH_HOME": "/work/tmp/cache/torch",
            "HF_HOME": "/work/tmp/cache/huggingface", "TEXMFVAR": "/work/tmp/texmf",
            "TEXMFCACHE": "/work/tmp/texmf", "PYTHONNOUSERSITE": "1",
        })
        if network and phase in ("synthesize", "synthesis"):
            for name in selected_key_names:
                if (not isinstance(name, str) or not _KEY_NAME.fullmatch(name)
                        or name in clean or name in _PUBLIC_ENV
                        or name.startswith(("ABSORBOBOT", "TELEGRAM", "PYTHON", "LD_", "UV_", "CUDA_"))
                        or name in ("HOME", "PATH", "TMP", "TEMP", "TMPDIR")):
                    raise SandboxError("invalid_key_name")
                value = supplied.get(name, os.environ.get(name))
                if not isinstance(value, str) or not value or "\x00" in value:
                    raise SandboxError("missing_model_key")
                clean[name] = value
        return clean

    def _tex_roots(self, runtime: dict, tex_roots: tuple[str, ...]) -> list[Path]:
        roots = tex_roots or runtime.get("tex_roots", ())
        if not isinstance(roots, (tuple, list)):
            raise SandboxError("invalid_runtime")
        result = []
        for value in roots:
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise SandboxError("invalid_runtime")
            path = Path(value).resolve()
            allowed = (path.is_relative_to(Path("/usr/share"))
                       or path.is_relative_to(Path("/etc/texmf"))
                       or path.is_relative_to(Path("/var/lib/texmf")))
            if not allowed or not path.is_dir():
                raise SandboxError("invalid_runtime")
            result.append(path)
        return result

    def _whisper_root(self) -> Path | None:
        for path in (self.settings.storage_dir / "models/whisper", Path.home() / ".cache/whisper"):
            if path.is_dir() and not path.is_symlink():
                return path
        return None

    def build_command(self, work_uuid: str, phase: str, argv: list[str], *, runtime: dict,
                      network: bool = True, tex_roots: tuple[str, ...] = (),
                      phase_id: str | None = None, status_fd: int | None = None) -> list[str]:
        """Публичный профиль для smoke; никакого запасного запуска вне bwrap."""
        if not self._bwrap:
            raise SandboxError("sandbox_unavailable")
        if not isinstance(phase, str) or not _PHASE.fullmatch(phase):
            raise SandboxError("invalid_phase")
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or "\x00" in v for v in argv) or not argv[0]:
            raise SandboxError("invalid_argv")
        phase_id = str(uuid4()) if phase_id is None else phase_id
        try:
            if str(UUID(phase_id)) != phase_id:
                raise ValueError
        except (ValueError, TypeError):
            raise SandboxError("invalid_phase") from None
        root = self._root(work_uuid)
        environment, python, base = self._runtime(runtime)
        command = [self._bwrap, "--unshare-user", "--unshare-pid", "--as-pid-1",
                   "--unshare-ipc", "--unshare-uts", "--new-session", "--die-with-parent",
                   "--cap-drop", "ALL", "--hostname", "absorbobot", "--chdir", "/work"]
        if not network:
            command.append("--unshare-net")
        if status_fd is not None:
            command.extend(["--json-status-fd", str(status_fd)])
        command.extend(["--ro-bind", "/usr", "/usr"])
        for name in ("bin", "lib", "lib64", "sbin"):
            source = Path("/") / name
            if source.is_symlink():
                command.extend(["--symlink", os.readlink(source), str(source)])
            elif source.is_dir():
                command.extend(["--ro-bind", str(source), str(source)])
        for name in ("ssl/cert.pem", "ssl/certs", "ca-certificates/extracted",
                     "pki/ca-trust/extracted", "fonts", "resolv.conf", "hosts",
                     "nsswitch.conf", "passwd", "group", "ld.so.cache", "localtime"):
            source = Path("/etc") / name
            if source.exists():
                command.extend(["--ro-bind", str(source.resolve()), str(source)])
        command.extend(["--proc", "/proc", "--dev", "/dev", "--ro-bind", "/sys", "/sys"])
        for device in Path("/dev").glob("nvidia*"):
            if device.is_dir():
                for child in device.iterdir():
                    if stat.S_ISCHR(child.stat().st_mode):
                        command.extend(["--dev-bind", str(child), str(child)])
            elif stat.S_ISCHR(device.stat().st_mode):
                command.extend(["--dev-bind", str(device), str(device)])
        dri = Path("/dev/dri")
        if dri.is_dir():
            for device in dri.iterdir():
                if stat.S_ISCHR(device.stat().st_mode):
                    command.extend(["--dev-bind", str(device), str(device)])
        for path in (base, environment):
            if not path.is_relative_to(Path("/usr")):
                command.extend(["--ro-bind", str(path.resolve()), str(path)])
        for alias in self._python_base_aliases(python, base):
            command.extend(["--ro-bind", str(base.resolve()), str(alias)])
        if phase == "render":
            roots = self._tex_roots(runtime, tex_roots)
            if not roots:
                raise SandboxError("invalid_runtime")
            for path in roots:
                if not path.is_relative_to(Path("/usr")):
                    command.extend(["--ro-bind", str(path), str(path)])
        whisper = self._whisper_root()
        if whisper is not None and phase in ("synthesize", "synthesis"):
            command.extend(["--ro-bind", str(whisper), "/models/whisper"])
        command.extend(["--bind", str(root), "/work", "--ro-bind", str(self._workers), "/workers"])
        alias = environment.name if re.fullmatch(r"[A-Za-z0-9_-]+", environment.name) else "absorbobot"
        command.extend(["--dir", "/work/tmp/uv-tools", "--symlink", str(environment), f"/work/tmp/uv-tools/{alias}"])
        command.extend(["--", str(python), "/workers/supervise.py", "--phase-id", phase_id,
                        "--ready", f"/work/tmp/resources/{phase_id}.ready.json",
                        "--go", f"/work/tmp/resources/{phase_id}.go.json",
                        "--report", f"/work/tmp/resources/{phase_id}.report.json", "--", *argv])
        return command

    def _prepare(self, root: Path, phase: str) -> None:
        for relative in ("tmp/resources", "tmp/logs", "tmp/home", "tmp/cache", "tmp/texmf", "tmp/bin"):
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise SandboxError("unsafe_work_tree")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        links = [(root / "tmp/home/.cache", "/work/tmp/cache")]
        if self._whisper_root() is not None and phase in ("synthesize", "synthesis"):
            links.append((root / "tmp/cache/whisper", "/models/whisper"))
        for path, target in links:
            if path.is_symlink():
                if os.readlink(path) != target:
                    raise SandboxError("unsafe_work_tree")
            elif path.exists():
                raise SandboxError("unsafe_work_tree")
            else:
                path.symlink_to(target)

    def _launcher(self, binding: dict) -> _Process | None:
        process = _same_process(binding.get("launcher_pid"), binding.get("launcher_start_time"))
        if process is not None:
            try:
                executable = os.readlink(f"/proc/{process.pid}/exe")
            except OSError:
                raise SandboxError("identity_unavailable") from None
            if executable != binding.get("launcher_executable"):
                raise SandboxError("unsafe_identity")
        return process

    def _namespace(self, binding: dict, *, group: bool = False) -> _Process | None:
        if "namespace_pid" not in binding:
            return None
        process = _same_process(binding["namespace_pid"], binding.get("namespace_start_time"))
        if process is None:
            return None
        if (_namespace_id(process.pid) != binding.get("namespace_id")
                or _namespace_id(process.pid) == _namespace_id(os.getpid())):
            raise SandboxError("unsafe_identity")
        if group:
            launcher = self._launcher(binding)
            forbidden = {os.getpgrp(), launcher.pgid if launcher is not None else -1}
            if (process.pgid != binding.get("pgid") or process.session_id != binding.get("session_id")
                    or process.pgid != process.pid or process.session_id != process.pid
                    or process.pgid in forbidden):
                raise SandboxError("unsafe_identity")
        return process

    def _enrich_child(self, active: _Active, pid: int) -> None:
        process = _process(pid)
        launcher = self._launcher(active.binding)
        if process is None or not process.alive or launcher is None:
            raise SandboxError("handshake_failed")
        _safe_pid(pid)
        if process.parent != launcher.pid or process.uid != os.getuid() or _namespace_id(pid) == _namespace_id(os.getpid()):
            raise SandboxError("unsafe_identity")
        active.binding.update(namespace_pid=pid, namespace_start_time=process.start_time,
                              namespace_id=_namespace_id(pid))
        self.store.bind_process(active.work_uuid, dict(active.binding))

    async def _handshake(self, active: _Active, status_path: Path, timeout: float) -> None:
        deadline = time.monotonic() + min(timeout, _STARTUP_SECONDS)
        ready_path = active.root / "tmp/resources" / f"{active.phase_id}.ready.json"
        while True:
            if active.cancelled.is_set():
                raise SandboxError("cancelled")
            if active.wait_task.done():
                raise SandboxError("sandbox_unavailable")
            await self._guard(active.root)
            if "namespace_pid" not in active.binding:
                pid = await asyncio.to_thread(_status_child, status_path, active.root)
                if pid is not None:
                    self._enrich_child(active, pid)
            ready = await asyncio.to_thread(_bounded_json, ready_path, active.root)
            if ready is not None and "namespace_pid" in active.binding:
                if ready != {"schema_version": 1, "phase_id": active.phase_id, "pid": 1, "pgid": 1, "session_id": 1}:
                    raise SandboxError("handshake_failed")
                process = self._namespace(active.binding)
                launcher = self._launcher(active.binding)
                if process is None or launcher is None or process.parent != launcher.pid:
                    raise SandboxError("handshake_failed")
                # JSON child-pid появился до setsid: PGID разрешается ТОЛЬКО после READY.
                active.binding.update(pgid=process.pgid, session_id=process.session_id)
                self._namespace(active.binding, group=True)
                self.store.bind_process(active.work_uuid, dict(active.binding))
                return
            if time.monotonic() >= deadline:
                raise SandboxError("handshake_failed")
            await asyncio.sleep(0.03)

    async def _guard(self, root: Path) -> None:
        try:
            await asyncio.to_thread(check_storage, self.settings)
            size = await asyncio.to_thread(_work_bytes, root, self.settings.max_work_bytes)
        except (ConfigError, OSError):
            raise SandboxError("storage_unavailable") from None
        if size > self.settings.max_work_bytes:
            raise SandboxError("work_limit", limit=self.settings.max_work_bytes)

    def _signal_pid(self, process: _Process, signum: int) -> None:
        try:
            fd = os.pidfd_open(process.pid)
        except ProcessLookupError:
            return
        try:
            current = _same_process(process.pid, process.start_time)
            if current is not None:
                signal.pidfd_send_signal(fd, signum)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)

    async def _stop_binding(self, binding: dict, wait_task: asyncio.Task | None = None) -> None:
        if binding.get("boot_id") != _boot_id():
            if wait_task is not None:
                await asyncio.shield(wait_task)
            return
        launcher = self._launcher(binding)
        namespace = self._namespace(binding, group="pgid" in binding)
        if namespace is not None and launcher is not None and "pgid" in binding:
            # Monitor остаётся живым: PID-1 supervisor успевает wait4 и flush отчёта.
            try:
                os.killpg(namespace.pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif launcher is not None:
            # До READY нет безопасного PGID; смерть monitor убирает namespace.
            self._signal_pid(launcher, signal.SIGTERM)
        deadline = time.monotonic() + _GRACE_SECONDS
        while time.monotonic() < deadline:
            if self._namespace(binding) is None and self._launcher(binding) is None:
                break
            await asyncio.sleep(0.05)
        namespace = self._namespace(binding)
        if namespace is not None:
            self._signal_pid(namespace, signal.SIGKILL)
        launcher = self._launcher(binding)
        if launcher is not None:
            self._signal_pid(launcher, signal.SIGKILL)
        if wait_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), _GRACE_SECONDS)
            except asyncio.TimeoutError:
                raise SandboxError("cleanup_failed") from None
        deadline = time.monotonic() + _GRACE_SECONDS
        while self._namespace(binding) is not None or self._launcher(binding) is not None:
            if time.monotonic() >= deadline:
                raise SandboxError("cleanup_failed")
            await asyncio.sleep(0.05)

    async def _finish(self, active: _Active) -> None:
        try:
            await self._stop_binding(active.binding, active.wait_task)
            self.store.bind_process(active.work_uuid, None)
        except BaseException:
            self._fatal = True
            raise
        finally:
            active.done.set()
            self._active = None

    def _go(self, active: _Active) -> None:
        # Атомарная замена: supervisor никогда не прочитает половину сигнала запуска.
        path = active.root / "tmp/resources" / f"{active.phase_id}.go.json"
        if not path.parent.resolve().is_relative_to(active.root.resolve()):
            raise SandboxError("unsafe_work_tree")
        temporary = path.with_name(path.name + ".writing")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "phase_id": active.phase_id, "action": "go"}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def run(self, work_uuid: str, phase: str, argv: list[str], *, runtime: dict,
                  env: dict | None = None, network: bool = True,
                  selected_key_names: tuple[str, ...] = (), tex_roots: tuple[str, ...] = (),
                  timeout: float | None = None) -> dict:
        timeout = self.settings.max_processing_seconds if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise SandboxError("invalid_timeout")
        timeout = min(timeout, self.settings.max_processing_seconds)
        async with self._lock:
            if self._closed or self._fatal:
                raise SandboxError("sandbox_closed")
            if self.store.process_bindings():
                raise SandboxError("recovery_required")
            self._root(work_uuid)
            try:
                root = await asyncio.to_thread(create_work_tree, self.settings, work_uuid)
            except ConfigError:
                raise SandboxError("storage_unavailable") from None
            self._prepare(root, phase)
            phase_id = str(uuid4())
            clean_env = self.build_environment(phase, runtime=runtime, env=env,
                                               network=network, selected_key_names=selected_key_names)
            stdout_path = root / "tmp/logs" / f"{phase_id}.stdout.log"
            stderr_path = root / "tmp/logs" / f"{phase_id}.stderr.log"
            status_path = root / "tmp/resources" / f"{phase_id}.status.jsonl"
            streams = []
            active = None
            started = time.monotonic()
            try:
                for path in (stdout_path, stderr_path, status_path):
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    streams.append(os.fdopen(fd, "wb", buffering=0))
                command = self.build_command(work_uuid, phase, argv, runtime=runtime, network=network,
                                             tex_roots=tex_roots, phase_id=phase_id, status_fd=streams[2].fileno())
                # Shield не позволяет потерять Popen при отмене во время exec handshake.
                launch = asyncio.create_task(asyncio.to_thread(subprocess.Popen, command, env=clean_env,
                    stdin=subprocess.DEVNULL, stdout=streams[0], stderr=streams[1],
                    pass_fds=(streams[2].fileno(),), close_fds=True))
                cancelled_during_launch = False
                try:
                    process = await asyncio.shield(launch)
                except asyncio.CancelledError:
                    cancelled_during_launch = True
                    process = await launch
                except OSError:
                    raise SandboxError("sandbox_unavailable") from None
                wait_task = asyncio.create_task(asyncio.to_thread(process.wait))
                identity = _process(process.pid)
                if identity is None:
                    await asyncio.shield(wait_task)
                    raise SandboxError("sandbox_unavailable")
                binding = {"schema_version": 1, "work_uuid": work_uuid, "phase_id": phase_id,
                           "phase": phase, "boot_id": _boot_id(), "launcher_pid": process.pid,
                           "launcher_start_time": identity.start_time,
                           "launcher_executable": str(Path(self._bwrap).resolve())}
                active = _Active(work_uuid, phase_id, root, process, binding, wait_task)
                self._active = active
                self.store.bind_process(work_uuid, dict(binding))
                if cancelled_during_launch:
                    raise asyncio.CancelledError
                await self._handshake(active, status_path, timeout)
                if active.cancelled.is_set() or self._closed:
                    raise SandboxError("cancelled")
                self._go(active)
                while not wait_task.done():
                    if active.cancelled.is_set():
                        raise SandboxError("cancelled")
                    if time.monotonic() - started >= timeout:
                        raise SandboxError("timeout", limit=timeout)
                    await self._guard(root)
                    await asyncio.sleep(min(0.25, self.settings.metrics_interval_seconds))
                exit_code = await asyncio.shield(wait_task)
                await self._guard(root)
                report_path = root / "tmp/resources" / f"{phase_id}.report.json"
                resource_report = await asyncio.to_thread(self._resources, report_path, root, phase_id)
                return {"exit_code": exit_code, "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
                        "resource_report": resource_report, "binding": dict(active.binding)}
            finally:
                if active is not None:
                    cleanup = asyncio.create_task(self._finish(active))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
                for stream in streams:
                    stream.close()

    def _resources(self, path: Path, root: Path, phase_id: str) -> dict:
        try:
            value = _bounded_json(path, root)
        except SandboxError:
            return _unknown_resources(phase_id, "invalid_report")
        if value is None:
            return _unknown_resources(phase_id)
        if (value.get("schema_version") != 1 or value.get("phase_id") != phase_id
                or type(value.get("exit_code")) is not int
                or value.get("completeness") not in ("complete", "partial")):
            return _unknown_resources(phase_id, "invalid_report")
        for name in ("cpu_user_seconds", "cpu_system_seconds", "ru_maxrss_kib"):
            number = value.get(name)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                return _unknown_resources(phase_id, "invalid_report")
        # Только публичная фиксированная схема, не произвольные поля worker JSON.
        return {name: value[name] for name in ("schema_version", "phase_id", "exit_code",
                "cpu_user_seconds", "cpu_system_seconds", "ru_maxrss_kib", "completeness")}

    async def cancel(self, work_uuid: str) -> None:
        active = self._active
        if active is None or active.work_uuid != work_uuid:
            return
        active.cancelled.set()
        await asyncio.shield(active.done.wait())
        if self._fatal:
            raise SandboxError("cleanup_failed")

    async def recover(self, bindings: list[dict]) -> None:
        async with self._lock:
            if self._active is not None:
                raise SandboxError("execution_busy")
            for row in bindings:
                binding = row.get("binding", row)
                work_uuid = row.get("work_uuid", binding.get("work_uuid")) if isinstance(binding, dict) else None
                self._root(work_uuid)
                if not isinstance(binding, dict) or binding.get("schema_version") != 1 or binding.get("work_uuid") != work_uuid:
                    self._fatal = True
                    raise SandboxError("invalid_binding")
                try:
                    await self._stop_binding(binding)
                    self.store.bind_process(work_uuid, None)
                except BaseException:
                    self._fatal = True
                    raise
            self._fatal = False

    async def shutdown(self) -> None:
        self._closed = True
        active = self._active
        if active is not None:
            active.cancelled.set()
            await asyncio.shield(active.done.wait())
        async with self._lock:
            if self._fatal:
                raise SandboxError("cleanup_failed")
