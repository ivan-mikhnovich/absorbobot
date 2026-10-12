"""Границы ограниченного протокола прогресса стадии импорта."""

from __future__ import annotations

import asyncio
import json

import pytest

from absorbobot.workers import progress



def test_read_rejects_missing_wrong_stage_and_wrong_unit(tmp_path):
    assert progress.read_progress(tmp_path, "copy", "bytes") is None
    progress.write_progress(tmp_path, "copy", completed=1, total=None, unit="items")
    assert progress.read_progress(tmp_path, "copy", "bytes") is None
    assert progress.read_progress(tmp_path, "external", "bytes") is None


def test_read_rejects_malformed_negative_and_oversized(tmp_path):
    directory = tmp_path / "tmp"
    directory.mkdir()
    target = directory / "progress.copy.json"
    target.write_text("{not json", encoding="utf-8")
    assert progress.read_progress(tmp_path, "copy") is None
    target.write_text(json.dumps({"schema_version": 1, "stage": "copy", "unit": "bytes",
                                  "completed": -1, "total": None, "updated_at": 1.0}), encoding="utf-8")
    assert progress.read_progress(tmp_path, "copy") is None
    target.write_bytes(b"x" * (progress.MAX_PROGRESS_BYTES + 1))
    assert progress.read_progress(tmp_path, "copy") is None


@pytest.mark.parametrize("kwargs", [
    {"completed": -1, "total": None, "unit": "bytes"},
    {"completed": 1, "total": -2, "unit": "bytes"},
    {"completed": 1, "total": None, "unit": "percent"},
    {"completed": True, "total": None, "unit": "bytes"},
])
def test_write_rejects_invalid_counters(tmp_path, kwargs):
    with pytest.raises(ValueError):
        progress.write_progress(tmp_path, "copy", **kwargs)


def test_writer_replaces_atomically_without_leftovers(tmp_path):
    writer = progress.ProgressWriter(tmp_path, "copy", "bytes", min_interval=0.0)
    writer.update(5, 10)
    assert progress.read_progress(tmp_path, "copy", "bytes")["completed"] == 5
    writer.update(10, 10)
    assert progress.read_progress(tmp_path, "copy", "bytes")["completed"] == 10
    assert sorted(path.name for path in (tmp_path / "tmp").iterdir()) == ["progress.copy.json"]


def test_writer_throttles_repeated_updates(tmp_path):
    writer = progress.ProgressWriter(tmp_path, "copy", "bytes", min_interval=3600.0)
    writer.update(5, 10)
    writer.update(9, 10)
    # Второе обновление отброшено ограничением частоты.
    assert progress.read_progress(tmp_path, "copy", "bytes")["completed"] == 5


def test_discard_removes_only_stage_file(tmp_path):
    progress.write_progress(tmp_path, "copy", completed=1, total=2, unit="bytes")
    progress.write_progress(tmp_path, "external", completed=1, total=2, unit="bytes")
    progress.discard_progress(tmp_path, "copy")
    assert not (tmp_path / "tmp" / "progress.copy.json").exists()
    assert progress.read_progress(tmp_path, "external", "bytes") is not None
    # Повторная очистка отсутствующего файла не поднимает ошибок.
    progress.discard_progress(tmp_path, "copy")


# Стадии, которые движок передаёт в сообщение о прогрессе: подпись стадии
# берётся из каталога, поэтому пропущенный ключ роняет службу целиком.
def test_every_progress_stage_has_a_localized_text():
    from absorbobot.i18n import catalogues
    from absorbobot.intake import STAGES

    stages = [*(f"input.{stage}" for stage in STAGES),
              # Приём записи и статусы заданий.
              "input.queued", "input.ready", "input.failed",
              "status.queued", "status.delivered", "status.failed", "status.cancelled",
              # Этапы отчёта движка и ядра Absorbent.
              "stage.delivery", "stage.render", "stage.package",
              "stage.stt", "stage.extract_frames", "stage.select_keyframes",
              "stage.describe_keyframes", "stage.describe_slides", "stage.prepare_slides",
              "stage.synthesize", "stage.summarize"]
    for language in ("ru", "en"):
        missing = sorted(stage for stage in stages if stage not in catalogues()[language])
        assert not missing, (language, missing)


def test_track_rejects_stage_without_localized_text(tmp_path):
    """Стадия вне списка не должна попасть ни в состояние черновика, ни в сообщение."""
    from absorbobot.intake import Intake, STAGES

    class Stub(Intake):
        def __init__(self):
            pass

        def _store_import_progress(self, *args, **kwargs):
            raise AssertionError("прогресс не должен записываться")

    assert "queued" not in STAGES

    async def scenario():
        async with Stub()._track(1, "uuid", tmp_path, "queued", "bytes"):
            pass

    with pytest.raises(ValueError):
        asyncio.run(scenario())


