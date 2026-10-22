import asyncio
from copy import deepcopy
import json
import sys

import pytest

import absorbobot.engine as engine_module
from absorbobot.config import Settings
from absorbobot.dialog import Dialog
from absorbobot.engine import Engine
from absorbobot.store import Store, utc_now


@pytest.fixture
def state(tmp_path):
    settings = Settings(admin_ids=(1,), state_dir=tmp_path / 'state',
                        storage_dir=tmp_path / 'mount' / 'data',
                        backing_mount=tmp_path / 'mount', min_free_bytes=1)
    core = {'version_info': {'version': '0.1.2', 'commit': 'a' * 40, 'dirty': False},
            'models': {'stt': {}, 'vision': {}, 'llm': {}}, 'defaults': {}}
    with Store(settings) as store:
        for owner in (1, 2, 3):
            if owner != 1:
                store.allow_user(1, owner)
            store.upsert_user(owner, language='en')
        engine = Engine(settings, store, None, Dialog(store, settings, core), core)
        yield store, engine, core


def preparation_record(wall=2.0, cpu=1.5, system=0.25):
    """Собственные измерения подготовки входа для принятого черновика."""
    stamp = utc_now().isoformat()
    return {
        "schema_version": 1, "wall_seconds": wall, "updated_at": stamp,
        "operations": [{
            "operation": "copy", "status": "complete", "reason": None,
            "started_at": stamp, "ended_at": stamp, "wall_seconds": wall,
            "resources": {"schema_version": 1, "phase_id": "input_copy", "exit_code": None,
                          "cpu_user_seconds": cpu, "cpu_system_seconds": system,
                          "ru_maxrss_kib": None, "completeness": "partial",
                          "reason": "thread_rss_unavailable"}}],
        "resources": {"schema_version": 1, "phases": 1, "cpu_user_seconds": cpu,
                      "cpu_system_seconds": system, "ru_maxrss_kib": None,
                      "host_rss_peak_kib": None, "busy_gpu_seconds": None, "vram_peak_bytes": None,
                      "completeness": "partial", "unknown_phases": 1,
                      "incomplete_phase_ids": ["input_copy"]},
    }


class _ReportPhase:
    """Подменить наблюдение фазы: полный отчёт с причиной GPU и чужими наблюдениями."""

    def __init__(self, settings, store, work_uuid, phase_id):
        self.phase_id = phase_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def finish(self, report=None):
        return {"schema_version": 1, "phase_id": self.phase_id, "completeness": "partial",
                "reason": "missing_report", "exit_code": None,
                "cpu_user_seconds": 1.0, "cpu_system_seconds": 0.5, "ru_maxrss_kib": 1024,
                "host_rss_peak_kib": 2048, "host_rss_samples": 1,
                "busy_gpu_seconds": None, "vram_peak_bytes": None,
                "gpu": {"supported": False, "reason": "nvml_unavailable",
                        "utilization_supported": False, "devices": []},
                "shared_device_observations": [{"index": 0, "uuid": "GPU-1", "gpu_utilization": 70,
                                                "memory_utilization": 20, "offset_seconds": 0.0}],
                "cloud_resources": {"cpu_seconds": None, "gpu_busy_seconds": None,
                                    "reason": "cloud_resources_unknown"}}


def accepted(store, core, owner=2, signature='same', preparation=None):
    draft = store.create_draft(owner, source_message_id=123)
    tree = store.settings.storage_dir / 'jobs' / draft['id']
    (tree / 'input').mkdir(parents=True)
    (tree / 'input' / 'media.wav').write_bytes(b'fixture')
    store.add_file(owner, draft['id'], role='media', original_name='media.wav',
                   relative_path='input/media.wav', sha256='b' * 64, bytes=7,
                   duration=1, source_message_id=123)
    store.update_draft(owner, draft['id'], status='ready', input_signature=signature,
                       snapshot={'version_info': core['version_info'], 'models': {},
                                 'settings': {'language': 'auto', 'output_language': 'en', 'fps': 1.0,
                                              'hash_threshold': 8, 'min_scene_seconds': 2.0,
                                              'slides_dpi': 150}})
    if preparation is not None:
        current = store.get_draft(draft['id'])
        store.update_draft(owner, draft['id'],
                           ui_state={**current['ui_state'], 'preparation': preparation})
    return store.enqueue(owner, draft['id'], utc_now(), force_new=False)


def test_changed_core_fails_before_any_api_or_outputs(state, monkeypatch):
    store, engine, core = state
    job = accepted(store, core)
    store.claim_next(utc_now())
    changed = deepcopy(core)
    changed['version_info']['commit'] = 'c' * 40
    monkeypatch.setattr('absorbobot.engine.probe_core', lambda settings: changed)
    asyncio.run(engine._process(job))
    failed = store.get_job(job['id'])
    assert failed['status'] == 'failed' and failed['failure_reason'] == 'core_changed'
    assert store.stats(2)['api_attempts'] == 0
    assert store.quota(2, utc_now())['used'] == 1
    assert not (store.settings.storage_dir / 'jobs' / job['id'] / 'config.yaml').exists()


def test_failed_job_removes_live_report_before_failure_notice(state, monkeypatch):
    """Упавшее задание не оставляет таблицу после сообщения об ошибке."""
    store, engine, core = state
    job = accepted(store, core)
    store.claim_next(utc_now())
    engine._progress_action(job, "stage.render")
    sent = next(row for row in store.pending_outbox(100)
                if row["payload"].get("purpose") == "progress")
    store.outbox_sent(sent["id"], {"message_id": 700, "progress": True})
    engine._progress[job["id"]]["last"] -= 10
    engine._progress_action(job, "stage.render")

    async def failed_run(work_uuid, phase, argv, **kwargs):
        raise engine_module.SandboxError("storage_unavailable")

    monkeypatch.setattr('absorbobot.engine.probe_core', lambda settings: core)
    monkeypatch.setattr(engine.sandbox, "run", failed_run)
    asyncio.run(engine._process(job))
    assert store.get_job(job["id"])["failure_reason"] == "storage_unavailable"
    rows = [row["payload"] for row in store.pending_outbox(100)]
    assert not [row for row in rows if row.get("purpose") == "progress"]
    drops = [row for row in rows if row.get("method") == "delete_message"]
    assert [row["kwargs"]["message_id"] for row in drops] == [700]
    assert any(row.get("method") == "send_message" for row in rows)


def test_late_completed_reuse_releases_executor_not_reserved_quota(state, monkeypatch):
    store, engine, core = state
    original = accepted(store, core, owner=3)
    store.claim_next(utc_now())
    store.update_job(original['id'], status='running')
    output = store.settings.storage_dir / 'jobs' / original['id'] / 'output'
    output.mkdir()
    for name in ('notes.md', 'notes.html', 'notes.pdf', 'notes.zip'):
        (output / name).write_bytes(b'complete result')
    store.update_job(original['id'], status='completed')
    job = accepted(store, core)
    store.claim_next(utc_now())
    monkeypatch.setattr('absorbobot.engine.probe_core', lambda settings: core)
    asyncio.run(engine._process(job))
    waiting = store.get_job(job['id'])
    assert waiting['status'] == 'awaiting_reuse_choice'
    assert waiting['reuse_source_job_id'] == original['id']
    assert store.quota(2, utc_now())['used'] == 1
    assert store.stats(2)['api_attempts'] == 0
    assert not store.process_bindings()
    following = accepted(store, core, owner=1, signature='different')
    assert store.claim_next(utc_now())['id'] == following['id']


def test_cancelled_active_task_does_not_stop_scheduler_or_release_before_cleanup(state):
    store, engine, core = state
    first = accepted(store, core)
    store.claim_next(utc_now())
    store.request_cancel(2, [first['id']])
    following = accepted(store, core, owner=3, signature='other')
    events = []
    async def scenario():
        ready = asyncio.Event()
        async def active():
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append('reap')
                store.update_job(first['id'], status='cancelled')
        engine._active = asyncio.create_task(active())
        engine._active_uuid = first['id']
        await ready.wait()
        await engine._cancel({'work_uuid': first['id']})
        events.append('cancel-returned')
        assert engine._active is None
        assert store.claim_next(utc_now())['id'] == following['id']
    asyncio.run(scenario())
    assert events == ['reap', 'cancel-returned']
    assert store.get_job(first['id'])['status'] == 'cancelled'
    assert store.quota(2, utc_now())['used'] == 1


def test_late_reuse_choice_retains_own_preparation_metadata(state, monkeypatch):
    store, engine, core = state
    original = accepted(store, core, owner=3)
    store.claim_next(utc_now())
    store.update_job(original['id'], status='running')
    output = store.settings.storage_dir / 'jobs' / original['id'] / 'output'
    output.mkdir()
    for name in ('notes.md', 'notes.html', 'notes.pdf', 'notes.zip'):
        (output / name).write_bytes(b'complete result')
    store.update_job(original['id'], status='completed')
    job = accepted(store, core, preparation=preparation_record())
    store.claim_next(utc_now())
    monkeypatch.setattr('absorbobot.engine.probe_core', lambda settings: core)
    asyncio.run(engine._process(job))
    waiting = store.get_job(job['id'])
    # Ранний выход на выбор повторного использования не теряет собственную подготовку.
    assert waiting['status'] == 'awaiting_reuse_choice'
    assert waiting['metrics']['preparation_seconds'] == 2.0
    assert waiting['metrics']['preparation']['wall_seconds'] == 2.0
    assert waiting['resources']['cpu_user_seconds'] == 1.5
    assert waiting['resources']['phase_reports'][0]['phase_id'] == 'input_copy'


def test_engine_adds_input_preparation_and_persists_full_phase_reports(state, monkeypatch):
    store, engine, core = state
    core['runtime'] = {'python_executable': sys.executable, 'tool_env': sys.prefix,
                       'python_base_prefix': sys.base_prefix}
    job = accepted(store, core, preparation=preparation_record())
    store.claim_next(utc_now())
    monkeypatch.setattr('absorbobot.engine.probe_core', lambda settings: core)
    monkeypatch.setattr(engine_module, 'PhaseMetrics', _ReportPhase)
    monkeypatch.setattr(engine_module, 'package_result', lambda tree: None)
    monkeypatch.setattr(engine_module.shutil, 'which', lambda name: '/fake/uv')

    def fake_tree(settings, work_uuid):
        root = settings.storage_dir / 'jobs' / work_uuid
        for child in ('input', 'output', 'tmp'):
            (root / child).mkdir(parents=True, exist_ok=True)
        return root

    monkeypatch.setattr(engine_module, 'create_work_tree', fake_tree)

    async def fake_run(work_uuid, phase, argv, **kwargs):
        return {'exit_code': 0, 'stdout_path': '', 'stderr_path': '',
                'resource_report': None, 'binding': {}}

    monkeypatch.setattr(engine.sandbox, 'run', fake_run)
    asyncio.run(engine._process(job))
    stored = store.get_job(job['id'])
    assert stored['status'] == 'completed'
    # Подготовка входа добавлена к собственной подготовке движка, а не перезаписана.
    assert stored['metrics']['preparation_seconds'] >= 2.0
    assert stored['metrics']['preparation']['wall_seconds'] == 2.0
    reports = stored['resources']['phase_reports']
    assert [report['phase_id'] for report in reports] == ['input_copy', 'synthesize', 'render']
    assert stored['resources']['cpu_user_seconds'] == 3.5
    # Причина недоступности GPU и наблюдения чужих устройств переживают хранение.
    assert reports[1]['gpu']['reason'] == 'nvml_unavailable'
    assert reports[1]['shared_device_observations'][0]['gpu_utilization'] == 70


def report_text(payload):
    """Плоский текст rich-таблицы: ячейки, подписи и запасные глифы эмодзи."""
    def walk(value):
        if isinstance(value, str):
            return value + " "
        if isinstance(value, list):
            return "".join(walk(item) for item in value)
        if isinstance(value, dict):
            return "".join(walk(item) for item in value.values())
        return ""
    blocks = payload["kwargs"]["rich_message"]["blocks"]
    return walk(blocks)


def test_import_report_is_a_line_and_removed_when_work_finishes(state):
    """Приём файла идёт отдельной строкой, а по завершении её убирают вместе с правкой."""
    store, engine, _ = state
    draft = store.create_draft(2, source_message_id=123)
    engine._progress_action(draft, "input.external",
                            detail={"completed": 13_700_000, "total": 51_000_000, "unit": "bytes"})
    actions = [row for row in store.pending_outbox(100)
               if row["payload"].get("purpose") == "progress"]
    assert len(actions) == 1
    payload = actions[0]["payload"]
    assert payload["method"] == "send_rich_message"
    blocks = payload["kwargs"]["rich_message"]["blocks"]
    assert len(blocks) == 1 and blocks[0]["type"] == "paragraph"
    text = report_text(payload)
    assert "6096080291347042802" in json.dumps(payload, ensure_ascii=False)
    assert "▮▮▯▯▯▯▯▯" in text and "27 %" in text and "13.7 / 51.0 MB" in text
    assert "Fetching the file" in text
    assert payload["text"].startswith("📥 Fetching the file")

    store.outbox_sent(actions[0]["id"], {"message_id": 900, "progress": True})
    engine._progress_action(draft, "input.ready", final=True)
    rows = [row["payload"] for row in store.pending_outbox(100)]
    drops = [row for row in rows if row.get("method") == "delete_message"]
    assert [row["kwargs"]["message_id"] for row in drops] == [900]
    stored = dict(store.db.execute("SELECT status,result FROM outbox WHERE id=?", (actions[0]["id"],)).fetchone())
    assert stored["status"] == "sent"
    # Отправленная строка остаётся историей, но целью правок быть перестаёт.
    assert json.loads(stored["result"]) == {"progress": True, "skipped": "report_removed"}
    assert store.progress_message_id(draft["id"]) is None


def test_live_report_rewrites_the_pending_action(state):
    """Частые правки одного состояния не плодят сообщений: продюсер правит уже поставленную строку."""
    store, engine, _ = state
    draft = store.create_draft(2, source_message_id=123)
    engine._progress_action(draft, "stage.stt",
                            detail={"completed": 100.0, "total": 400.0, "unit": "seconds"})
    # Продюсер правит ту же строку не чаще раза в пять секунд: сдвинуть собственный счётчик.
    engine._progress[draft["id"]]["last"] -= 10
    engine._progress_action(draft, "stage.stt",
                            detail={"completed": 300.0, "total": 400.0, "unit": "seconds"})
    actions = [row for row in store.pending_outbox(100)
               if row["payload"].get("purpose") == "progress"]
    assert len(actions) == 1
    text = report_text(actions[0]["payload"])
    assert "75 %" in text and "▮▮▮▮▮▮▯▯" in text
    assert "300 / 400 s" in text


def test_live_report_restarts_after_its_message_is_dropped(state):
    """Удалённый или сдвинутый отчёт не правится в пустоту: ставится новый.

    Прежде движок запоминал идентификатор действия и после переноса отчёта
    продолжал править уже отправленную строку без ``message_id``, поэтому
    подробности загрузки замирали на первом обновлении.
    """
    store, engine, _ = state
    draft = store.create_draft(2, source_message_id=123)
    detail = {"completed": 8_000_000, "total": None, "unit": "bytes"}
    engine._progress_action(draft, "input.media", detail=detail)
    first = next(row for row in store.pending_outbox(100)
                 if row["payload"].get("purpose") == "progress")
    store.outbox_sent(first["id"], {"message_id": 900, "progress": True})
    # Отчёт убран из чата: живого сообщения больше нет.
    store.forget_progress_message(draft["id"], 900, "report_moved")
    engine._progress[draft["id"]]["last"] -= 10
    engine._progress_action(draft, "input.media",
                            detail={**detail, "completed": 16_000_000})
    actions = [row for row in store.pending_outbox(100)
               if row["payload"].get("purpose") == "progress"]
    assert len(actions) == 1
    payload = actions[0]["payload"]
    assert payload["method"] == "send_rich_message"
    assert "16.0 MB" in report_text(payload)


def test_report_splits_frame_extraction_and_description(state):
    """Нарезка кадров и их описание — соседние строки с общим знаком."""
    store, engine, _ = state
    draft = store.create_draft(2, source_message_id=123)
    engine._progress_action(draft, "stage.describe_keyframes",
                            detail={"completed": 2, "total": 5, "unit": "images"})
    payload = next(row for row in store.pending_outbox(100)
                   if row["payload"].get("purpose") == "progress")["payload"]
    blocks = payload["kwargs"]["rich_message"]["blocks"]
    assert blocks[1]["type"] == "table" and len(blocks[1]["cells"]) == 9
    text = report_text(payload)
    assert "Frame extraction" in text
    assert "Frame description" in text
    assert "40 %" in text
    # Приём файла в таблицу обработки не входит.
    assert "Fetching the file" not in text
    engine._progress[draft["id"]]["last"] -= 10
    engine._progress_action(draft, "stage.extract_frames", ended="ok")
    payload = next(row for row in store.pending_outbox(100)
                   if row["payload"].get("purpose") == "progress")["payload"]
    text = report_text(payload)
    # Нарезка завершена, а описание всё ещё идёт своей полосой.
    assert "Frame extraction" in text and "100 %" in text


def test_delivery_report_shows_finished_pipeline(state):
    """Перед выдачей готовые этапы не выглядят ожидающими."""
    store, engine, _ = state
    draft = store.create_draft(2, source_message_id=123)
    engine._progress_action(draft, "stage.delivery",
                            detail={"completed": 1, "total": 4, "unit": "items"})
    payload = next(row for row in store.pending_outbox(100)
                   if row["payload"].get("purpose") == "progress")["payload"]
    text = report_text(payload)
    assert "Telegram upload" in text and "25 %" in text
    assert text.count("100 %") >= 7


def test_report_keeps_parallel_stage_bars(state):
    """Параллельные этапы видны одновременно и обновляются независимо."""
    store, engine, _ = state
    draft = store.create_draft(2, source_message_id=123)
    engine._progress_action(draft, "stage.stt",
                            detail={"completed": 100.0, "total": 400.0, "unit": "seconds"})
    engine._progress[draft["id"]]["last"] -= 10
    engine._progress_action(draft, "stage.extract_frames",
                            detail={"completed": 3, "total": 12, "unit": "frames"})
    payload = next(row for row in store.pending_outbox(100)
                   if row["payload"].get("purpose") == "progress")["payload"]
    text = report_text(payload)
    assert "Speech recognition" in text and "25 %" in text
    assert "Frame extraction" in text and "25 %" in text
    assert "3 / 12 frames" in text
    assert "Fetching the file" not in text


def test_pending_intake_does_not_preempt_its_notice(state):
    """Ожидающий импорт не ставит отчёт раньше уведомления «Принял запись».

    Прежде цикл движка сразу показывал «Приём файла · 0 с», а уведомление о
    приёме ложилось следом, и отчёт приходилось удалять и переносить вниз.
    """
    store, engine, _ = state
    draft = store.create_draft(2, source_message_id=123)
    store.add_outbox("intake", {"owner_id": 2, "draft_id": draft["id"],
                                "message": {"message_id": 123, "chat": {"id": 2}}})
    # Пауза не даёт движку начать импорт: отчёта до уведомления быть не должно.
    store.set_paused(1, True)
    stop = asyncio.Event()

    def progress_actions():
        return [row for row in store.pending_outbox(limit=50)
                if row["kind"] == "telegram" and row["payload"].get("purpose") == "progress"]

    async def scenario():
        task = asyncio.create_task(engine.run(stop))
        for _ in range(20):
            await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(scenario())
    assert progress_actions() == []


def test_core_render_skip_does_not_mark_bot_render(state):
    """Пропуск render ядром не делает строку «Не потребовалось»: рендерит бот."""
    store, engine, _ = state
    draft = store.create_draft(2, source_message_id=123)
    engine._progress_action(draft, "stage.render")
    payload = next(row for row in store.pending_outbox(100)
                   if row["payload"].get("purpose") == "progress")["payload"]
    assert "HTML and PDF" in report_text(payload)
    engine._apply_event(draft, {"event": "stage_end", "stage": "render", "status": "skipped"})
    assert "Not needed" not in report_text(payload)
    # Собственная фаза рендера завершает строку успешно.
    engine._progress[draft["id"]]["last"] -= 10
    engine._progress_action(draft, "stage.render", ended="ok")
    payload = next(row for row in store.pending_outbox(100)
                   if row["payload"].get("purpose") == "progress")["payload"]
    assert "100 %" in report_text(payload)

