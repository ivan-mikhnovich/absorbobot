"""Границы безопасного импорта: подпись входа, разбор сообщения, копирование, жизненный цикл."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
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
from absorbobot.workers import progress as progress_module


ADMIN, ALICE = 1, 2
MESSAGE_ID = 100
MEDIA = "a" * 64
DOC_ONE = "b" * 64
DOC_TWO = "c" * 64

SETTINGS = {"language": "ru", "output_language": "ru", "fps": 1.0, "hash_threshold": 8,
            "min_scene_seconds": 2.0, "slides_dpi": 150, "media_mode": "video"}
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


def test_signature_changes_with_audio_selection_and_binding():
    files = [entry("media", MEDIA, "input/a.m4a")]
    tracks = [
        {"index": 1, "title": "Зал", "language": "ru", "default": True,
         "channels": 2, "start_time": 0.0, "offset": 0.0},
        {"index": 4, "title": "Перевод", "language": "en", "default": False,
         "channels": 1, "start_time": 1.0, "offset": 1.0},
    ]
    base = input_signature(files, SETTINGS, MODELS, audio={
        "tracks": tracks, "selected": [1], "speakers": {}})
    assert base != input_signature(files, SETTINGS, MODELS, audio={
        "tracks": tracks, "selected": [4], "speakers": {}})
    assert base != input_signature(files, SETTINGS, MODELS, audio={
        "tracks": tracks, "selected": [1], "speakers": {1: "Алиса"}})
    # Запись без обнаруженных дорожек сохраняет прежнюю подпись и готовый reuse.
    assert input_signature(files, SETTINGS, MODELS) == input_signature(
        files, SETTINGS, MODELS, audio={"tracks": [], "selected": [], "speakers": {}})


@pytest.mark.parametrize("change", [
    {"language": "en"},
    {"output_language": "en"},
    {"fps": 2.0},
    {"hash_threshold": 9},
    {"min_scene_seconds": 3.0},
    {"slides_dpi": 200},
    {"media_mode": "audio"},
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
    ([entry("media", MEDIA, "input/a.m4a")], {**SETTINGS, "media_mode": "both"}),
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
        self.fail_code = "analysis_failed"
        self.blocked_phase = None
        self.audio_tracks = None

    def _root(self, work_uuid):
        return self.settings.storage_dir / "jobs" / work_uuid

    async def run(self, work_uuid, phase, argv, **kwargs):
        self.runs.append({"work_uuid": work_uuid, "phase": phase, "argv": list(argv), **kwargs})
        if phase == self.blocked_phase:
            self.reached.set()
            await self.gate.wait()
        root = self._root(work_uuid)
        if phase == "media":
            extension = ".mkv" if argv[3] == "video" else ".m4a"
            media = root / "tmp" / "media-fake" / ("media" + extension)
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"downloaded")
            value = {"ok": True, "relative_path": str(media.relative_to(root)), "duration": 30.0}
        elif len(argv) > 3 and argv[3] == "media":
            value = {"ok": True, "duration": 12.5, "pages": None}
            if self.audio_tracks is not None:
                value["audio_tracks"] = self.audio_tracks
        else:
            value = {"ok": True, "duration": None, "pages": 2}
        if phase == self.fail_phase:
            value = {"ok": False, "error": self.fail_code}
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
                        min_free_bytes=1, quota_jobs=1, bot_api_url="http://127.0.0.1:8081")
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


def test_handle_persists_discovered_tracks_without_preselecting_all(env):
    store, intake, sandbox = env
    sandbox.audio_tracks = [
        {"index": 1, "title": "Зал", "language": "ru", "default": True,
         "channels": 2, "start_time": 0.5, "offset": 0.0},
        {"index": 2, "title": "Синхронный перевод", "language": "en", "default": False,
         "channels": 1, "start_time": 0.5, "offset": 0.0},
    ]
    draft = store.create_draft(ALICE)
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                           document={"file_id": "f1", "file_size": 7, "file_name": "recording.mkv"})))
    assert result["status"] == "imported"
    state = store.get_draft(draft["id"])["ui_state"]
    assert [track["index"] for track in state["audio_tracks"]] == [1, 2]
    assert state["audio_tracks"][0]["title"] == "Зал"
    # Несколько дорожек не выбираются молча: выбор делает пользователь.
    assert state["audio_selection"] == []
    assert state["audio_speakers"] == {}


def test_handle_auto_selects_the_only_audio_track(env):
    store, intake, sandbox = env
    sandbox.audio_tracks = [
        {"index": 0, "title": None, "language": None, "default": True,
         "channels": 1, "start_time": 0.0, "offset": 0.0},
    ]
    draft = store.create_draft(ALICE)
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                           document={"file_id": "f1", "file_size": 7, "file_name": "recording.m4a"})))
    assert result["status"] == "imported"
    state = store.get_draft(draft["id"])["ui_state"]
    assert state["audio_selection"] == [0]
    assert state["audio_speakers"] == {}


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


@pytest.mark.parametrize("link,mode,canonical,name", [
    ("https://youtu.be/abcdefghijk", "audio", "https://www.youtube.com/watch?v=abcdefghijk",
     "YouTube abcdefghijk.m4a"),
    ("https://vkvideo.ru/video-77521_162222515", "video", "https://vk.com/video-77521_162222515",
     "VK Video -77521_162222515.mkv"),
])
def test_handle_media_import_keeps_ready_and_records_mode(env, link, mode, canonical, name):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={
        "pending_import_ids": [MESSAGE_ID],
        "media_pending": {"message_id": MESSAGE_ID, "chat": {"id": ALICE}}})
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"], text=link, media_mode=mode)))
    assert result["status"] == "imported"
    stored = store.get_draft(draft["id"])
    assert stored["status"] == "ready"
    assert stored["settings"]["media_mode"] == mode
    files = store.files(draft["id"])
    assert files[0]["original_name"] == name
    assert stored["ui_state"]["pending_import_ids"] == []
    assert "media_pending" not in stored["ui_state"]
    assert [run["phase"] for run in sandbox.runs] == ["media", "analyze"]
    assert sandbox.runs[0]["network"] is True
    assert sandbox.runs[0]["selected_key_names"] == ()
    # Worker получает канонический URL площадки, а не текст сообщения.
    assert sandbox.runs[0]["argv"][2] == canonical
    assert sandbox.runs[0]["argv"][3] == mode


def test_handle_audio_only_platform_forces_audio_mode(env):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    result = asyncio.run(intake.handle(payload(
        ALICE, draft["id"], text="https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy",
        media_mode="video")))
    assert result["status"] == "imported"
    files = store.files(draft["id"])
    # Площадка отдаёт только звук, поэтому режим видео к ней не применяется.
    assert files[0]["original_name"] == "SoundCloud ethmusic/lostin-powers-she-so-heavy.m4a"
    assert sandbox.runs[0]["argv"][2] == "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy"
    assert sandbox.runs[0]["argv"][3] == "audio"


PROXY = "http://127.0.0.1:1081"


def test_platform_worker_receives_configured_proxy(env):
    store, intake, sandbox = env
    settings = replace(store.settings, download_proxy=PROXY)
    with Store(settings) as other:
        other.allow_user(ADMIN, ALICE)
        other.upsert_user(ALICE, language="en")
        instance = Intake(settings, other, intake.bot, intake.core, sandbox)
        draft = other.create_draft(ALICE)
        result = asyncio.run(instance.handle(payload(ALICE, draft["id"],
                                                     text="https://youtu.be/abcdefghijk")))
    assert result["status"] == "imported"
    # Прокси доезжает до worker площадок последним аргументом.
    assert sandbox.runs[0]["argv"][-1] == PROXY


def test_external_request_carries_configured_proxy(env):
    store, intake, sandbox = env
    settings = replace(store.settings, download_proxy=PROXY)
    seen = {}

    async def run(work_uuid, phase, argv, **kwargs):
        if phase == "external":
            root = settings.storage_dir / "jobs" / work_uuid
            request_path = root / "tmp" / Path(argv[2]).name
            seen["proxy"] = json.loads(request_path.read_text(encoding="utf-8"))["proxy"]
            media = root / "tmp" / (request_path.stem + ".m4a")
            media.write_bytes(b"downloaded")
            out = root / "tmp" / "phase-external.stdout"
            out.write_text(json.dumps({"ok": True, "relative_path": str(media.relative_to(root)),
                                       "role": "media", "extension": ".m4a", "bytes": 10,
                                       "original_name": "lecture.m4a", "duration": 12.0}),
                           encoding="utf-8")
            return {"exit_code": 0, "stdout_path": str(out), "stderr_path": str(out),
                    "resource_report": dict(FAKE_REPORT), "binding": {}}
        return await original_run(work_uuid, phase, argv, **kwargs)

    original_run = sandbox.run
    sandbox.run = run
    with Store(settings) as other:
        other.allow_user(ADMIN, ALICE)
        other.upsert_user(ALICE, language="en")
        instance = Intake(settings, other, intake.bot, intake.core, sandbox)
        draft = other.create_draft(ALICE)
        result = asyncio.run(instance.handle(payload(
            ALICE, draft["id"], text="https://files.example.org/lecture.m4a")))
    assert result["status"] == "imported"
    assert seen["proxy"] == PROXY


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


def test_external_signin_page_reaches_the_user_as_signin_not_size(env):
    """Закрытая ссылка Google: причина отказа — вход, а не размер файла."""
    store, intake, sandbox = env
    sandbox.fail_phase, sandbox.fail_code = "external", "link_requires_signin"
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={"pending_import_ids": [MESSAGE_ID]})
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                         text="https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWx/view")))
    assert result["status"] == "rejected" and result["reason"] == "link_requires_signin"
    stored = store.get_draft(draft["id"])
    assert stored["failure_reason"] == "link_requires_signin"
    assert stored["status"] == "collecting"
    assert any(row["payload"]["kwargs"].get("text") == "intake.error.link_requires_signin"
               for row in store.pending_outbox(limit=100))


def test_worker_safe_codes_are_known_to_intake():
    from absorbobot.workers.external import _SAFE_CODES
    assert _SAFE_CODES <= intake_module._ERROR_CODES


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


def test_cloud_oversized_attachment_rejected_before_getfile(env):
    from dataclasses import replace
    store, intake, sandbox = env
    intake.settings = replace(intake.settings, bot_api_url="https://api.telegram.org")
    draft = store.create_draft(ALICE)
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                         document={"file_id": "large", "file_size": 20000001, "file_name": "lecture.mp4"})))
    assert result["reason"] == "telegram_file_too_large"
    assert intake.bot.calls == [] and sandbox.runs == []
    assert store.files(draft["id"]) == []


def test_cloud_download_enforces_actual_bytes_and_removes_partial_file(env):
    from dataclasses import replace
    from types import SimpleNamespace
    store, intake, _ = env
    intake.settings = replace(intake.settings, bot_api_url="https://api.telegram.org")
    draft = store.create_draft(ALICE)
    root = intake_module.create_work_tree(intake.settings, draft["id"])
    target = root / "tmp" / "download.m4a"

    async def stream(*args, **kwargs):
        yield b"payload"
        yield b"overflow"

    intake.bot.session = SimpleNamespace(api=SimpleNamespace(file_url=lambda token, path: "https://api.telegram.org/file"),
                                         stream_content=stream)
    intake.bot.token = "fixture"
    with pytest.raises(IntakeError) as error:
        asyncio.run(intake._download_telegram("documents/file.m4a", target, root, 7))
    assert error.value.code == "input_too_large"
    assert not target.exists()


def test_cloud_download_relative_path_imports_content(env):
    from dataclasses import replace
    from types import SimpleNamespace
    store, intake, _ = env
    intake.settings = replace(intake.settings, bot_api_url="https://api.telegram.org")
    draft = store.create_draft(ALICE)

    async def get_file(file_id):
        return {"file_path": "documents/file.m4a", "file_size": 7}

    async def stream(*args, **kwargs):
        yield b"pay"
        yield b"load"

    intake.bot.get_file = get_file
    intake.bot.session = SimpleNamespace(api=SimpleNamespace(file_url=lambda token, path: "https://api.telegram.org/file"),
                                         stream_content=stream)
    intake.bot.token = "fixture"
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                         document={"file_id": "small", "file_size": 7, "file_name": "lecture.m4a"})))
    assert result["status"] == "imported"
    file = store.files(draft["id"])[0]
    assert file["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert file["bytes"] == 7 and file["duration"] == 12.5
    root = intake.settings.storage_dir / "jobs" / draft["id"]
    assert (root / file["relative_path"]).read_bytes() == b"payload"
    assert not list((root / "tmp").glob("*.m4a"))


def test_oversized_cloud_material_preserves_already_imported_recording(env):
    from dataclasses import replace
    store, intake, _ = env
    draft = store.create_draft(ALICE)
    first = asyncio.run(intake.handle(payload(ALICE, draft["id"],
                        document={"file_id": "media", "file_size": 7, "file_name": "recording.m4a"})))
    assert first["status"] == "imported"
    before = store.files(draft["id"])
    intake.settings = replace(intake.settings, bot_api_url="https://api.telegram.org")
    rejected = asyncio.run(intake.handle(payload(ALICE, draft["id"], message_id=102,
                           document={"file_id": "slides", "file_size": 20000001, "file_name": "slides.pdf"})))
    assert rejected["reason"] == "telegram_file_too_large"
    assert store.files(draft["id"]) == before
    assert store.get_draft(draft["id"])["status"] == "ready"
    assert intake.bot.calls == ["media"]


def test_cancelled_external_worker_partial_is_removed_without_worker_result(env):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)

    async def scenario():
        reached = asyncio.Event()
        paths = []

        async def run(work_uuid, phase, argv, **kwargs):
            root = sandbox._root(work_uuid)
            request = root / "tmp" / Path(argv[2]).name
            data = json.loads(request.read_text())
            partial = root / "tmp" / (data["download_id"] + ".m4a")
            partial.write_bytes(b"unfinished")
            neighbor = root / "tmp" / "unrelated.m4a"
            neighbor.write_bytes(b"preserve")
            paths.extend((request, partial, neighbor))
            reached.set()
            await asyncio.Event().wait()

        sandbox.run = run
        task = asyncio.create_task(intake.handle(payload(ALICE, draft["id"],
                                   text="https://files.example.org/recording.m4a")))
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not paths[0].exists() and not paths[1].exists()
        assert paths[2].read_bytes() == b"preserve"
        assert store.files(draft["id"]) == []
        assert store.get_draft(draft["id"])["status"] == "failed"

    asyncio.run(scenario())


def _media_then_analyze(sandbox, *, media_value, analyze_value):
    """Собрать подмену sandbox: фаза загрузки и фаза разбора из одного словаря."""

    async def run(work_uuid, phase, argv, **kwargs):
        root = sandbox._root(work_uuid)
        if phase == "media":
            media = root / "tmp" / "media-fake" / "media.mkv"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"downloaded")
            value = {"relative_path": str(media.relative_to(root)), **media_value}
        else:
            value = dict(analyze_value)
            preview = value.get("preview_relative_path")
            if isinstance(preview, str) and not preview.startswith(("/", "..")):
                target = root / preview
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\xff\xd8\xff" + b"x" * 64)
        out = root / "tmp" / ("phase-" + phase + ".stdout")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"ok": True, **value}), encoding="utf-8")
        return {"exit_code": 0, "stdout_path": str(out), "stderr_path": str(out),
                "resource_report": dict(FAKE_REPORT), "binding": {}}

    sandbox.run = run


def test_media_progress_and_title_reach_draft_during_await(env):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={"pending_import_ids": [MESSAGE_ID]})

    async def scenario():
        reached, gate = asyncio.Event(), asyncio.Event()

        async def run(work_uuid, phase, argv, **kwargs):
            root = sandbox._root(work_uuid)
            if phase == "media":
                progress_module.write_progress(root, "media", completed=1234, total=4096, unit="bytes")
                reached.set()
                await gate.wait()
                media = root / "tmp" / "media-fake" / "media.mkv"
                media.parent.mkdir(parents=True, exist_ok=True)
                media.write_bytes(b"downloaded")
                value = {"relative_path": "tmp/media-fake/media.mkv", "duration": 30.0,
                         "title": "Lecture One"}
            else:
                value = {"duration": 30.0, "pages": None}
            out = root / "tmp" / ("phase-" + phase + ".stdout")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"ok": True, **value}), encoding="utf-8")
            return {"exit_code": 0, "stdout_path": str(out), "stderr_path": str(out),
                    "resource_report": dict(FAKE_REPORT), "binding": {}}

        sandbox.run = run
        task = asyncio.create_task(intake.handle(payload(ALICE, draft["id"],
                       text="https://youtu.be/abcdefghijk")))
        await asyncio.wait_for(reached.wait(), 2)
        observed = None
        for _ in range(60):
            observed = store.get_draft(draft["id"])["ui_state"].get("import_progress")
            if observed and observed.get("completed") == 1234:
                break
            await asyncio.sleep(0.05)
        assert observed["stage"] == "media" and observed["unit"] == "bytes"
        assert observed["completed"] == 1234 and observed["total"] == 4096
        gate.set()
        return await task

    result = asyncio.run(scenario())
    assert result["status"] == "imported"
    state = store.get_draft(draft["id"])["ui_state"]
    # Фактический заголовок из метаданных, а не идентификатор видео.
    assert state["title"] == "Lecture One"
    assert store.files(draft["id"])[0]["original_name"] == "Lecture One.mkv"
    assert state["import_progress"]["stage"] == "analyze"
    assert not (store.settings.storage_dir / "jobs" / draft["id"] / "tmp" / "progress.media.json").exists()


def test_cancelled_import_leaves_no_success_progress(env):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    store.update_draft(ALICE, draft["id"], ui_state={"pending_import_ids": [MESSAGE_ID]})

    async def scenario():
        reached = asyncio.Event()

        async def run(work_uuid, phase, argv, **kwargs):
            progress_module.write_progress(sandbox._root(work_uuid), "media",
                                           completed=10, total=100, unit="bytes")
            reached.set()
            await asyncio.Event().wait()

        sandbox.run = run
        task = asyncio.create_task(intake.handle(payload(ALICE, draft["id"],
                       text="https://youtu.be/abcdefghijk")))
        await asyncio.wait_for(reached.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    stored = store.get_draft(draft["id"])
    assert stored["status"] == "failed"
    assert "import_progress" not in stored["ui_state"]


def test_analyze_preview_path_is_stored_for_accepted_media(env):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    _media_then_analyze(
        sandbox,
        media_value={"duration": 30.0, "title": "Резервная копия"},
        analyze_value={"duration": 30.0, "pages": None,
                       "preview_relative_path": "tmp/analyze-x/preview.jpg",
                       "preview_width": 320, "preview_height": 240})
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"], text="https://youtu.be/abcdefghijk")))
    assert result["status"] == "imported"
    state = store.get_draft(draft["id"])["ui_state"]
    assert state["preview_relative_path"] == "tmp/analyze-x/preview.jpg"
    assert state["title"] == "Резервная копия"


@pytest.mark.parametrize("preview", ["../escape.jpg", "/tmp/escape.jpg", "input/escape.jpg"])
def test_preview_path_outside_tmp_is_ignored(env, preview):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    _media_then_analyze(
        sandbox,
        media_value={"duration": 30.0, "title": "T"},
        analyze_value={"duration": 30.0, "pages": None,
                       "preview_relative_path": preview,
                       "preview_width": 320, "preview_height": 240})
    result = asyncio.run(intake.handle(payload(ALICE, draft["id"], text="https://youtu.be/abcdefghijk")))
    assert result["status"] == "imported"
    assert "preview_relative_path" not in store.get_draft(draft["id"])["ui_state"]


def test_lookup_media_metadata_stores_title_without_download(env):
    store, intake, sandbox = env
    draft = store.create_draft(ALICE)
    calls = []

    async def run(work_uuid, phase, argv, **kwargs):
        calls.append((phase, list(argv)))
        root = sandbox._root(work_uuid)
        out = root / "tmp" / "meta.stdout"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"ok": True, "duration": 42.0, "title": "Recovered"}))
        return {"exit_code": 0, "stdout_path": str(out), "stderr_path": str(out),
                "resource_report": dict(FAKE_REPORT), "binding": {}}

    sandbox.run = run
    result = asyncio.run(intake.lookup_media_metadata(ALICE, draft["id"], "https://youtu.be/abcdefghijk"))
    assert result == {"duration": 42.0, "title": "Recovered"}
    assert store.get_draft(draft["id"])["ui_state"]["title"] == "Recovered"
    assert calls[0][0] == "media"
    assert calls[0][1][2] == "--metadata"
    # Вспомогательная фаза получает канонический URL площадки.
    assert calls[0][1][3] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert store.files(draft["id"]) == []


def test_material_import_preserves_recording_title_and_preview(env):
    store, intake, _ = env
    draft = store.create_draft(ALICE)
    asyncio.run(intake.handle(payload(ALICE, draft["id"],
        document={"file_id": "recording", "file_size": 7, "file_name": "recording.m4a"})))
    state = dict(store.get_draft(draft["id"])["ui_state"])
    state.update(title="Consensus algorithms", preview_relative_path="tmp/source-preview.jpg")
    store.update_draft(ALICE, draft["id"], ui_state=state)

    result = asyncio.run(intake.handle(payload(ALICE, draft["id"], message_id=101,
        document={"file_id": "slides", "file_size": 7, "file_name": "slides.pdf"})))

    assert result["status"] == "imported"
    current = store.get_draft(draft["id"])["ui_state"]
    assert current["title"] == "Consensus algorithms"
    assert current["preview_relative_path"] == "tmp/source-preview.jpg"
