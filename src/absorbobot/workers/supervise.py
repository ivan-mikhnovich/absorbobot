"""PID-1 supervisor: двухфазный запуск и ресурсы настоящего дочернего процесса."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path


_PENDING_SIGNAL: int | None = None
_CHILD_PID: int | None = None


def _signal(signum: int, frame: object) -> None:
    """Не завершать PID 1 до wait4 и записи отчёта."""
    global _PENDING_SIGNAL
    _PENDING_SIGNAL = signum
    if _CHILD_PID is not None:
        try:
            os.kill(_CHILD_PID, signum)
        except ProcessLookupError:
            pass


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".writing")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_go(path: Path, phase_id: str) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("Некорректный сигнал запуска.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        # Документ ещё дописывается; ждать следующий опрос, не считать отказом.
        return False
    return value == {"schema_version": 1, "phase_id": phase_id, "action": "go"}


def supervise(phase_id: str, ready: Path, go: Path, report: Path, argv: list[str]) -> int:
    global _CHILD_PID, _PENDING_SIGNAL
    _CHILD_PID = None
    _PENDING_SIGNAL = None
    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGHUP, _signal)
    _atomic_json(ready, {
        "schema_version": 1, "phase_id": phase_id,
        "pid": os.getpid(), "pgid": os.getpgrp(), "session_id": os.getsid(0),
    })
    deadline = time.monotonic() + 90
    while not _read_go(go, phase_id):
        if _PENDING_SIGNAL is not None or time.monotonic() >= deadline:
            return 125
        time.sleep(0.02)
    if _PENDING_SIGNAL is not None:
        return 125
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True)
    _CHILD_PID = child.pid
    if _PENDING_SIGNAL is not None:
        _signal(_PENDING_SIGNAL, None)
    # Только этот цикл выполняет wait4; Popen.wait/poll здесь запрещены.
    adopted_user = 0.0
    adopted_system = 0.0
    adopted_rss = 0
    while True:
        try:
            pid, status, usage = os.wait4(-1, 0)
        except InterruptedError:
            continue
        if pid == child.pid:
            exit_code = os.waitstatus_to_exitcode(status)
            child.returncode = exit_code
            _CHILD_PID = None
            break
        adopted_user += usage.ru_utime
        adopted_system += usage.ru_stime
        adopted_rss = max(adopted_rss, usage.ru_maxrss)
    # Сироты могут пережить основной payload. PID 1 завершит namespace после отчёта.
    remaining = False
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            remaining = True
            break
    _atomic_json(report, {
        "schema_version": 1, "phase_id": phase_id, "exit_code": exit_code,
        "cpu_user_seconds": usage.ru_utime + adopted_user,
        "cpu_system_seconds": usage.ru_stime + adopted_system,
        "ru_maxrss_kib": max(usage.ru_maxrss, adopted_rss),
        "completeness": "partial" if remaining else "complete",
    })
    return exit_code if exit_code >= 0 else 128 - exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase-id", required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        return 125
    try:
        return supervise(args.phase_id, args.ready, args.go, args.report, argv)
    except Exception:
        # Не публиковать исключения сторонних библиотек с путями или секретами.
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
