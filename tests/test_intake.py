"""Границы безопасного импорта: подпись входа, разбор сообщения, копирование, жизненный цикл."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading

import pytest

from absorbobot.config import Settings
from absorbobot.intake import (
    Intake,
    IntakeError,
    copy_and_hash,
    input_signature,
    telegram_descriptor,
)
from absorbobot.store import Store, StoreError
import absorbobot.intake as intake_module


ADMIN, ALICE = 1, 2
MESSAGE_ID = 100
MEDIA = "a" * 64
DOC_ONE = "b" * 64
DOC_TWO = "c" * 64

SETTINGS = {"language": "ru", "output_language": "ru", "fps": 1.0, "hash_threshold": 8,
            "min_scene_seconds": 2.0, "slides_dpi": 150, "youtube_mode": "video"}
MODELS = {
    "llm": {"name": "big", "spec": {"backend": "openai", "api_model": "deepseek-v4-flash",
                                    "whisper_model": None},
            "provider": {"name": "cloud", "base_url": "https://api.example/v1"}, "local": False},
    "stt": {"name": "whisper", "spec": {"backend": "local", "api_model": None,
                                        "whisper_model": "large"},
            "provider": {"name": "local", "base_url": "http://127.0.0.1:9999/v1"}, "local": True},
}


def entry(role, sha, path, name=None, message=None, **extra):
    return {"role": role, "sha256": sha, "relative_path": path,
            "original_name": name or path.rsplit("/", 1)[-1],
            "source_message_id": message, "bytes": 10, **extra}


def test_signature_ignores_owner_paths_names_and_message_ids():
    first = [entry("media", MEDIA, "input/a.m4a", "recording.m4a", 1),
             entry("material", DOC_ONE, "input/s.pdf", "slides.pdf", 2)]
    second = [entry("media", MEDIA, "input/z.m4a", "other name.m4a", 77),
              entry("material", DOC_ONE, "input/deck.pdf", "lection.pdf", 78)]
    assert input_signature(first, SETTINGS, MODELS) == input_signature(second, SETTINGS, MODELS)


def test_signature_ignores_removed_material_but_not_its_order_or_type():
    plain = [entry("media", MEDIA, "input/a.m4a"), entry("material", DOC_ONE, "input/s.pdf")]
    removed = [*plain, entry("material", DOC_TWO, "input/x.pptx", removed=True)]
    assert input_signature(plain, SETTINGS, MODELS) == input_signature(removed, SETTINGS, MODELS)

    ordered = [entry("media", MEDIA, "input/a.m4a"), entry("material", DOC_TWO, "input/b.pdf"),
               entry("material", DOC_ONE, "input/s.pdf")]
    swapped = [ordered[0], ordered[2], ordered[1]]
    assert input_signature(ordered, SETTINGS, MODELS) != input_signature(swapped, SETTINGS, MODELS)

    as_slides = [entry("media", MEDIA, "input/a.m4a"), entry("material", DOC_ONE, "input/s.pptx")]
    assert input_signature(plain, SETTINGS, MODELS) != input_signature(as_slides, SETTINGS, MODELS)


@pytest.mark.parametrize("change", [
    {"language": "en"},
    {"output_language": "en"},
    {"fps": 2.0},
    {"hash_threshold": 9},
    {"min_scene_seconds": 3.0},
    {"slides_dpi": 200},
    {"youtube_mode": "audio"},
])
def test_signature_changes_with_processing_parameters(change):
    files = [entry("media", MEDIA, "input/a.m4a"), entry("material", DOC_ONE, "input/s.pdf")]
    assert input_signature(files, SETTINGS, MODELS) != input_signature(files, {**SETTINGS, **change}, MODELS)


@pytest.mark.parametrize("mutate", [
    lambda models: models["llm"]["provider"].update(base_url="https://api.other/v1"),
    lambda models: models["llm"]["spec"].update(api_model="deepseek-v4-pro"),
    lambda models: models["llm"]["provider"].update(name="other-cloud"),
    lambda models: models["stt"]["spec"].update(whisper_model="medium"),
])
def test_signature_changes_with_effective_model_endpoint(mutate):
    files = [entry("media", MEDIA, "input/a.m4a")]
    changed = deepcopy(MODELS)
    mutate(changed)
    assert input_signature(files, SETTINGS, MODELS) != input_signature(files, SETTINGS, changed)


@pytest.mark.parametrize("files,settings", [
    ([], SETTINGS),
    ([entry("media", MEDIA, "input/a.m4a"), entry("media", MEDIA, "input/b.m4a")], SETTINGS),
    ([entry("media", "zz", "input/a.m4a")], SETTINGS),
    ([entry("media", MEDIA.upper(), "input/a.m4a")], SETTINGS),
    ([entry("media", MEDIA, "input/a.m4a")], {**SETTINGS, "fps": 0}),
    ([entry("media", MEDIA, "input/a.m4a")], {**SETTINGS, "fps": float("nan")}),
    ([entry("media", MEDIA, "input/a.m4a")], {**SETTINGS, "hash_threshold": 65}),
    ([entry("media", MEDIA, "input/a.m4a")], {**SETTINGS, "slides_dpi": 0}),
    ([entry("media", MEDIA, "input/a.m4a")], {**SETTINGS, "language": "de"}),
    ([entry("media", MEDIA, "input/a.m4a")], {**SETTINGS, "youtube_mode": "both"}),
])
def test_signature_rejects_invalid_content_and_parameters(files, settings):
    with pytest.raises(ValueError):
        input_signature(files, settings, MODELS)


CORE = {"media_extensions": [".m4a", ".mp4", ".mp3", ".ogg"], "material_extensions": [".pdf", ".jpg"]}


def test_telegram_descriptor_selects_largest_photo_as_material():
    message = {"photo": [{"file_id": "small", "file_size": 10, "width": 2, "height": 2},
                         {"file_id": "big", "file_size": 20, "width": 4, "height": 4}]}
    descriptor = telegram_descriptor(message, CORE)
    assert descriptor["file_id"] == "big"
    assert descriptor["role"] == "material"
    assert descriptor["extension"] == ".jpg"


def test_telegram_descriptor_roles_and_default_extensions():
    assert telegram_descriptor({"document": {"file_id": "d", "file_size": 5,
                                             "file_name": "slides.pdf"}}, CORE)["role"] == "material"
    assert telegram_descriptor({"document": {"file_id": "d", "file_size": 5,
                                             "file_name": "record.m4a"}}, CORE)["role"] == "media"
    assert telegram_descriptor({"voice": {"file_id": "v", "file_size": 5}}, CORE) == {
        "file_id": "v", "bytes": 5, "role": "media", "extension": ".ogg", "original_name": "voice.ogg"}
    assert telegram_descriptor({"video_note": {"file_id": "n", "file_size": 5}}, CORE)["extension"] == ".mp4"
    # Имя отсутствует, но mime_type задаёт разрешение.
    assert telegram_descriptor({"audio": {"file_id": "a", "file_size": 5, "mime_type": "audio/mpeg"}},
                               CORE)["extension"] == ".mp3"


@pytest.mark.parametrize("message", [
    {},
    {"document": {"file_id": "d", "file_size": 5}, "video": {"file_id": "v", "file_size": 5}},
    {"document": {"file_id": "", "file_size": 5, "file_name": "a.pdf"}},
    {"document": {"file_id": "d", "file_name": "a.pdf"}},
    {"document": {"file_id": "d", "file_size": 0, "file_name": "a.pdf"}},
    {"document": {"file_id": "d", "file_size": True, "file_name": "a.pdf"}},
    {"document": {"file_id": "d", "file_size": 5, "file_name": "bad\x00.pdf"}},
    {"document": {"file_id": "d", "file_size": 5, "file_name": "malware.exe"}},
    {"photo": []},
    {"photo": [{"file_id": "p", "file_size": 5, "width": -1, "height": 2}]},
])
def test_telegram_descriptor_rejects_ambiguous_or_invalid_metadata(message):
    with pytest.raises(IntakeError):
        telegram_descriptor(message, CORE)


def work_tree(tmp_path):
    work = tmp_path / "work"
    (work / "input").mkdir(parents=True)
    (work / "tmp").mkdir()
    return work


def test_copy_and_hash_streams_exact_content(tmp_path):
    work = work_tree(tmp_path)
    source_root = tmp_path / "native"
    source_root.mkdir()
    data = b"payload" * 5000
    source = source_root / "media.bin"
    source.write_bytes(data)
    result = copy_and_hash(source, work / "input" / "out.bin", source_root=source_root,
                           work_root=work, byte_limit=10 ** 6, work_limit=10 ** 9, min_free_bytes=1)
    assert result == {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    assert (work / "input" / "out.bin").read_bytes() == data


def test_copy_and_hash_enforces_byte_work_and_free_space_limits(tmp_path):
    work = work_tree(tmp_path)
    source_root = tmp_path / "native"
    source_root.mkdir()
    data = b"payload" * 1000
    source = source_root / "media.bin"
    source.write_bytes(data)

    with pytest.raises(IntakeError) as error:
        copy_and_hash(source, work / "input" / "big.bin", source_root=source_root, work_root=work,
                      byte_limit=10, work_limit=10 ** 9, min_free_bytes=1)
    assert error.value.code == "input_too_large"
    assert not (work / "input" / "big.bin").exists()

    (work / "tmp" / "existing").write_bytes(b"x" * 5000)
    with pytest.raises(IntakeError) as error:
        copy_and_hash(source, work / "input" / "work.bin", source_root=source_root, work_root=work,
                      byte_limit=10 ** 6, work_limit=5000 + len(data) - 1, min_free_bytes=1)
    assert error.value.code == "work_limit"

    with pytest.raises(IntakeError) as error:
        copy_and_hash(source, work / "input" / "space.bin", source_root=source_root, work_root=work,
                      byte_limit=10 ** 6, work_limit=10 ** 9, min_free_bytes=10 ** 18)
    assert error.value.code == "storage_unavailable"
    assert not (work / "input" / "space.bin").exists()


def test_copy_and_hash_removes_partial_copy_on_cancel(tmp_path):
    work = work_tree(tmp_path)
    source_root = tmp_path / "native"
    source_root.mkdir()
    source = source_root / "media.bin"
    source.write_bytes(b"payload" * 1000)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(IntakeError) as error:
        copy_and_hash(source, work / "input" / "cancelled.bin", source_root=source_root, work_root=work,
                      byte_limit=10 ** 6, work_limit=10 ** 9, min_free_bytes=1, cancelled=cancelled)
    assert error.value.code == "cancelled"
    assert not (work / "input" / "cancelled.bin").exists()


def test_copy_and_hash_rejects_symlink_escape_and_relative_source(tmp_path):
    work = work_tree(tmp_path)
    source_root = tmp_path / "native"
    (source_root / "inner").mkdir(parents=True)
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"secret")
    (source_root / "inner" / "link").symlink_to(secret)

    with pytest.raises(IntakeError) as error:
        copy_and_hash(source_root / "inner" / "link", work / "input" / "x.bin", source_root=source_root,
                      work_root=work, byte_limit=10 ** 6, work_limit=10 ** 9, min_free_bytes=1)
    assert error.value.code == "unsafe_file_path"

    linked_root = tmp_path / "linked"
    linked_root.symlink_to(source_root)
    real = source_root / "plain.bin"
    real.write_bytes(b"data")
    with pytest.raises(IntakeError) as error:
        copy_and_hash(real, work / "input" / "y.bin", source_root=linked_root, work_root=work,
                      byte_limit=10 ** 6, work_limit=10 ** 9, min_free_bytes=1)
    assert error.value.code == "unsafe_file_path"

    with pytest.raises(IntakeError) as error:
        copy_and_hash(Path("plain.bin"), work / "input" / "z.bin", source_root=source_root, work_root=work,
                      byte_limit=10 ** 6, work_limit=10 ** 9, min_free_bytes=1)
    assert error.value.code == "unsafe_file_path"


def test_copy_and_hash_rejects_target_outside_work_tree(tmp_path):
    work = work_tree(tmp_path)
    source_root = tmp_path / "native"
    source_root.mkdir()
    source = source_root / "media.bin"
    source.write_bytes(b"data")
    with pytest.raises(IntakeError) as error:
        copy_and_hash(source, tmp_path / "escaped.bin", source_root=source_root, work_root=work,
                      byte_limit=10 ** 6, work_limit=10 ** 9, min_free_bytes=1)
    assert error.value.code == "unsafe_file_path"


class FakeBot:
    """Вернуть единственный native-файл, как локальный Bot API."""

    def __init__(self, path):
        self.path = path
        self.calls = []

    async def get_file(self, file_id):
        self.calls.append(file_id)
        return {"file_path": str(self.path)}


FAKE_REPORT = {"schema_version": 1, "phase_id": "fake", "exit_code": 0,
               "cpu_user_seconds": 1.5, "cpu_system_seconds": 0.25,
               "ru_maxrss_kib": 2048, "completeness": "complete"}


class FakeSandbox:
    """Подменить фазы анализа и загрузки, сохранив argv и окружение вызова."""

    def __init__(self, settings):
        self.settings = settings
        self.runs = []
        self.cancelled = []
        self.fail_phase = None
        self.blocked_phase = None

    def _root(self, work_uuid):
        return self.settings.storage_dir / "jobs" / work_uuid

    async def run(self, work_uuid, phase, argv, **kwargs):
        self.runs.append({"work_uuid": work_uuid, "phase": phase, "argv": list(argv), **kwargs})
        if phase == self.blocked_phase:
            self.reached.set()
            await self.gate.wait()
        root = self._root(work_uuid)
        if phase == "youtube":
            extension = ".mkv" if argv[2] == "video" else ".m4a"
            media = root / "tmp" / "youtube-fake" / ("media" + extension)
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"downloaded")
            value = {"ok": True, "relative_path": str(media.relative_to(root)), "duration": 30.0}
        elif argv[3] == "media":
            value = {"ok": True, "duration": 12.5, "pages": None}
        else:
            value = {"ok": True, "duration": None, "pages": 2}
        if phase == self.fail_phase:
            value = {"ok": False, "error": "analysis_failed"}
        out = root / "tmp" / "phase.stdout"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(value), encoding="utf-8")
        return {"exit_code": 0 if phase != self.fail_phase else 1,
                "stdout_path": str(out), "stderr_path": str(out),
                "resource_report": dict(FAKE_REPORT), "binding": {}}

    async def cancel(self, work_uuid):
        self.cancelled.append(work_uuid)


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(admin_ids=(ADMIN,), state_dir=tmp_path / "state",
                        storage_dir=tmp_path / "mount" / "storage", backing_mount=tmp_path / "mount",
                        min_free_bytes=1, quota_jobs=1)
    monkeypatch.setattr(intake_module, "check_storage",
                        lambda value: {"mount": str(value.backing_mount), "writable": True,
                                       "free_bytes": 10 ** 18})

    def create(settings_, work_uuid):
        root = settings_.storage_dir / "jobs" / work_uuid
        for child in ("input", "output", "tmp"):
            (root / child).mkdir(parents=True, exist_ok=True)
        return root

    monkeypatch.setattr(intake_module, "create_work_tree", create)
    monkeypatch.setattr(intake_module, "tr", lambda language, key, **values: key)
    with Store(settings) as store:
        store.allow_user(ADMIN, ALICE)
        store.upsert_user(ALICE, language="en")
        native = settings.storage_dir / "telegram-api-files"
        native.mkdir(parents=True, exist_ok=True)
        native_file = native / "native.m4a"
        native_file.write_bytes(b"payload")
        sandbox = FakeSandbox(settings)
        bot = FakeBot(native_file)
        core = {"media_extensions": [".m4a", ".mp4", ".mkv"],
                "material_extensions": [".pdf", ".jpg"],
                "runtime": {"python_executable": sys.executable, "tool_env": sys.prefix,
                            "python_base_prefix": sys.base_prefix}}
        yield store, Intake(settings, store, bot, core, sandbox), sandbox


def payload(owner, draft_id, message_id=MESSAGE_ID, **fields):
    message = {"message_id": message_id, "chat": {"id": owner}, **fields}
    return {"owner_id": owner, "draft_id": draft_id, "message": message}


def test_handle_imports_media_and_clears_pending(env):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={"pending_import_ids": [MESSAGE_ID]})
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                           document={"file_id": "f1", "file_size": 7, "file_name": "recording.m4a"})))
    assert result["status"] == "imported"
    stored = store.get_draft(draft["id"])
    assert stored["status"] == "ready"
    files = store.files(draft["id"])
    assert len(files) == 1 and files[0]["role"] == "media"
    assert files[0]["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert files[0]["source_message_id"] == MESSAGE_ID
    assert stored["ui_state"]["pending_import_ids"] == []


def test_handle_rejects_second_media_without_touching_state(env):
    store, intake, _ = env
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={"pending_import_ids": [MESSAGE_ID]})
    asyncio.run(intake.handle(payload(ALICE, draft["id"],
                   document={"file_id": "f1", "file_size": 7, "file_name": "recording.m4a"})))
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"], message_id=101,
                           document={"file_id": "f2", "file_size": 7, "file_name": "second.m4a"})))
    assert result == {"status": "rejected", "reason": "media_exists", "work_uuid": draft["id"]}
    assert store.get_draft(draft["id"])["status"] == "ready"
    assert len(store.files(draft["id"])) == 1
    assert any(row["payload"]["kwargs"].get("text") == "intake.error.media_exists"
               for row in store.pending_outbox(limit=100))


def test_handle_propagates_paused_and_keeps_pending(env):
    store, intake, _ = env
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={"pending_import_ids": [MESSAGE_ID]})
    store.set_paused(ADMIN, True)
    with pytest.raises(StoreError) as error:
        asyncio.run(intake.handle(payload(ALICE, draft["id"],
                       document={"file_id": "f1", "file_size": 7, "file_name": "recording.m4a"})))
    assert error.value.code == "paused"
    stored = store.get_draft(draft["id"])
    assert stored["status"] == "collecting"
    assert stored["ui_state"]["pending_import_ids"] == [MESSAGE_ID]
    assert store.files(draft["id"]) == []


def test_handle_youtube_import_keeps_ready_and_records_mode(env):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={
        "pending_import_ids": [MESSAGE_ID],
        "youtube_pending": {"message_id": MESSAGE_ID, "chat": {"id": ALICE}}})
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"], text="https://youtu.be/abcdefghijk",
                                               youtube_mode="audio")))
    assert result["status"] == "imported"
    stored = store.get_draft(draft["id"])
    assert stored["status"] == "ready"
    assert stored["settings"]["youtube_mode"] == "audio"
    files = store.files(draft["id"])
    assert files[0]["original_name"] == "YouTube abcdefghijk.m4a"
    assert stored["ui_state"]["pending_import_ids"] == []
    assert "youtube_pending" not in stored["ui_state"]
    assert [run["phase"] for run in sandbox.runs] == ["youtube", "analyze"]
    assert sandbox.runs[0]["network"] is True
    assert sandbox.runs[0]["selected_key_names"] == ()


def test_later_material_import_survives_private_sandbox_runtime_links(env):
    store, intake, _ = env
    draft = store.create_draft(ALICE)
    first = asyncio.run(intake.handle(payload(ALICE, draft["id"],
        document={"file_id": "media", "file_size": 7, "file_name": "recording.m4a"})))
    assert first["status"] == "imported"
    root = store.settings.storage_dir / "jobs" / draft["id"]
    (root / "tmp" / "cache").mkdir()
    (root / "tmp" / "cache" / "whisper").symlink_to("/models/whisper")
    (root / "tmp" / "uv-tools").mkdir()
    (root / "tmp" / "uv-tools" / "absorbent").symlink_to("/unmounted/tool")
    second = asyncio.run(intake.handle(payload(ALICE, draft["id"], message_id=101,
        document={"file_id": "slides", "file_size": 7, "file_name": "slides.pdf"})))
    assert second["status"] == "imported"
    files = store.files(draft["id"])
    assert [file["role"] for file in files] == ["media", "material"]
    assert files[1]["pages"] == 2
    assert store.get_draft(draft["id"])["status"] == "ready"
    assert store.quota(ALICE, datetime.now(timezone.utc))["used"] == 0


def test_import_persists_measured_preparation_operations(env):
    store, intake, _ = env
    draft = store.create_draft(ALICE)
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                           document={"file_id": "f1", "file_size": 7, "file_name": "recording.m4a"})))
    assert result["status"] == "imported"
    preparation = store.get_draft(draft["id"])["ui_state"]["preparation"]
    copy, analyze = preparation["operations"]
    assert [operation["operation"] for operation in preparation["operations"]] == ["copy", "analyze"]
    assert copy["status"] == "complete" and analyze["status"] == "complete"
    # Копирующий поток измеряет свой CPU, но не присваивает RAM общего процесса.
    assert copy["resources"]["phase_id"] == "input_copy"
    assert copy["resources"]["ru_maxrss_kib"] is None
    assert copy["resources"]["reason"] == "thread_rss_unavailable"
    # Отчёт фазы анализа берётся из фактического ответа sandbox wait4.
    assert analyze["resources"]["phase_id"] == "input_analyze"
    assert analyze["resources"]["cpu_user_seconds"] == 1.5
    assert analyze["resources"]["ru_maxrss_kib"] == 2048
    assert preparation["wall_seconds"] == pytest.approx(copy["wall_seconds"] + analyze["wall_seconds"])
    assert preparation["resources"]["cpu_user_seconds"] == pytest.approx(
        copy["resources"]["cpu_user_seconds"] + 1.5)
    assert preparation["resources"]["ru_maxrss_kib"] == 2048
    # Неизвестная RAM копирования оставляет свод неполным, а не нулевым.
    assert preparation["resources"]["completeness"] == "partial"
    assert preparation["resources"]["incomplete_phase_ids"] == ["input_copy"]


def test_failed_analysis_keeps_measured_preparation_and_clears_pending(env):
    store, intake, sandbox = env
    sandbox.fail_phase = "analyze"
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={"pending_import_ids": [MESSAGE_ID]})
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                           document={"file_id": "f1", "file_size": 7, "file_name": "recording.m4a"})))
    assert result["status"] == "rejected" and result["reason"] == "analysis_failed"
    stored = store.get_draft(draft["id"])
    operations = stored["ui_state"]["preparation"]["operations"]
    assert [operation["operation"] for operation in operations] == ["copy", "analyze"]
    assert operations[1]["status"] == "failed" and operations[1]["reason"] == "analysis_failed"
    assert operations[1]["resources"]["phase_id"] == "input_analyze"
    assert stored["ui_state"]["pending_import_ids"] == []
    assert store.files(draft["id"]) == []


def test_cancelled_import_keeps_measured_preparation(env):
    store, intake, sandbox = env
    sandbox.blocked_phase = "analyze"
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={"pending_import_ids": [MESSAGE_ID]})

    async def scenario():
        sandbox.reached, sandbox.gate = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(intake.handle(payload(ALICE, draft["id"],
                       document={"file_id": "f1", "file_size": 7, "file_name": "recording.m4a"})))
        await sandbox.reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    stored = store.get_draft(draft["id"])
    operations = stored["ui_state"]["preparation"]["operations"]
    assert [operation["operation"] for operation in operations] == ["copy", "analyze"]
    assert operations[1]["status"] == "cancelled" and operations[1]["reason"] == "interrupted"
    assert stored["status"] == "failed" and stored["failure_reason"] == "interrupted"
