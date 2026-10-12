"""Ограниченный протокол прогресса стадии импорта между worker и ядром.

Worker или поток-копировщик атомарно заменяет небольшой JSON внутри
``/work/tmp/progress.<stage>.json``; контроллер читает его из event-loop и переносит фактические
счётчики в состояние черновика. Модуль не ходит в сеть, не знает про Store и
не выводит ничего наружу: ни путей, ни заголовков, ни ответов сервера.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from uuid import uuid4


SCHEMA_VERSION = 1
# Файл прогресса лежит прямо в tmp задания, чтобы отказ не оставлял лишних
# каталогов, а очистка дерева не зависела от отдельной папки.
PROGRESS_PREFIX = "progress."
MAX_PROGRESS_BYTES = 4096
UNITS = ("bytes", "items")


def progress_path(root: Path, stage: str) -> Path:
    """Путь progress-файла одной стадии внутри рабочего дерева."""
    return root / "tmp" / (PROGRESS_PREFIX + stage + ".json")


def discard_progress(root: Path, stage: str) -> None:
    """Удалить progress-файл стадии, не поднимая ошибок файловой системы."""
    try:
        progress_path(root, stage).unlink()
    except OSError:
        pass


def _valid(completed, total, unit) -> bool:
    """Проверить типы и границы счётчиков до записи или чтения."""
    if unit not in UNITS or type(completed) is not int or completed < 0:
        return False
    if total is not None and (type(total) is not int or total < completed):
        return False
    return True


def write_progress(root: Path, stage: str, *, completed: int, total: int | None,
                   unit: str) -> None:
    """Атомарно заменить progress-файл стадии в её рабочем дереве."""
    if not isinstance(stage, str) or not stage or "/" in stage or "\\" in stage:
        raise ValueError("invalid stage")
    if not _valid(completed, total, unit):
        raise ValueError("invalid progress")
    payload = json.dumps({"schema_version": SCHEMA_VERSION, "stage": stage, "unit": unit,
                          "completed": completed, "total": total, "updated_at": time.time()},
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_PROGRESS_BYTES:
        raise ValueError("progress too large")
    directory = root / "tmp"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = directory / (PROGRESS_PREFIX + stage + "." + uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
        os.replace(temporary, progress_path(root, stage))
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def read_progress(root: Path, stage: str, unit: str | None = None) -> dict | None:
    """Прочитать ограниченный progress-файл стадии или ``None`` без доверия к нему."""
    try:
        fd = os.open(progress_path(root, stage), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(MAX_PROGRESS_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_PROGRESS_BYTES:
        return None
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    if value.get("stage") != stage or value.get("unit") not in UNITS:
        return None
    if unit is not None and value["unit"] != unit:
        return None
    completed, total = value.get("completed"), value.get("total")
    if not _valid(completed, total, value["unit"]):
        return None
    return {"completed": completed, "total": total, "unit": value["unit"]}


class ProgressWriter:
    """Писать прогресс стадии с ограничением частоты атомарных замен."""

    def __init__(self, root: Path, stage: str, unit: str, *, min_interval: float = 0.25):
        self.root, self.stage, self.unit = root, stage, unit
        self.min_interval = min_interval
        self._last = float("-inf")

    def update(self, completed: int, total: int | None = None) -> None:
        """Записать счётчики, если с прошлой замены прошло достаточно времени."""
        now = time.monotonic()
        if now - self._last < self.min_interval:
            return
        self._last = now
        try:
            write_progress(self.root, self.stage, completed=completed, total=total, unit=self.unit)
        except (OSError, ValueError):
            # Сбой учёта прогресса не должен срывать фактическую загрузку.
            pass
