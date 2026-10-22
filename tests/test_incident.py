import asyncio

import pytest

from absorbobot import incident
from absorbobot.config import Settings, reset_work_tree
from absorbobot.store import Store, utc_now


ADMIN = 1
ALICE = 2


@pytest.fixture
def settings(tmp_path):
    return Settings(admin_ids=(ADMIN,), state_dir=tmp_path / "state",
                    storage_dir=tmp_path / "mount" / "storage",
                    backing_mount=tmp_path / "mount", min_free_bytes=1)


def failed_job(store):
    draft = store.create_draft(ALICE, source_message_id=123,
                               settings={"language": "auto", "output_language": "ru"})
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    (tree / "input").mkdir(parents=True)
    (tree / "input" / "recording.m4a").write_bytes(b"input")
    store.add_file(ALICE, draft["id"], role="media", original_name="recording.m4a",
                   relative_path="input/recording.m4a", sha256="b" * 64, bytes=5,
                   duration=1, source_message_id=123)
    store.update_draft(ALICE, draft["id"], status="ready", input_signature="sig",
                       snapshot={"version_info": {"version": "0.1", "commit": "a" * 40,
                                                   "dirty": False},
                                 "models": {},
                                 "settings": {"output_language": "ru"}})
    job = store.enqueue(ALICE, draft["id"], utc_now(), force_new=False)
    store.claim_next(utc_now())
    store.update_job(job["id"], status="running")
    store.update_job(job["id"], status="failed", failure_reason="processing_failed")
    return store.get_job(job["id"])


def test_incident_dedup_and_prompt(settings, monkeypatch):
    starts = []
    monkeypatch.setattr(incident.IncidentManager, "_start_unit",
                        staticmethod(lambda incident_id: starts.append(incident_id)))
    with Store(settings) as store:
        store.allow_user(ADMIN, ALICE, "Alice")
        job = failed_job(store)
        manager = incident.IncidentManager(settings, store)
        first = asyncio.run(manager.job_failure(job, "processing_failed",
                                                context={"code": "processing_failed"}))
        second = asyncio.run(manager.job_failure(job, "processing_failed",
                                                 context={"code": "processing_failed"}))
        assert first["id"] == second["id"]
        assert starts == [first["id"]]
        assert first["status"] == "queued" and first["model"] == incident.MODEL
        prompt = (settings.state_dir / "incidents" / first["id"] / "prompt.md").read_text(encoding="utf-8")
        assert first["id"] in prompt
        assert "deepseek/deepseek-flash" in prompt
        assert "secrets.env" in prompt


def test_retry_resets_only_generated_tree(settings):
    with Store(settings) as store:
        store.allow_user(ADMIN, ALICE, "Alice")
        job = failed_job(store)
        tree = settings.storage_dir / "jobs" / job["id"]
        (tree / "output").mkdir()
        (tree / "output" / "notes.md").write_text("old")
        (tree / "tmp").mkdir(exist_ok=True)
        (tree / "config.yaml").write_text("old")
        (tree / "events.jsonl").write_text("old")
        reset_work_tree(settings, job["id"])
        assert (tree / "input" / "recording.m4a").read_bytes() == b"input"
        assert not (tree / "output" / "notes.md").exists()
        assert not (tree / "config.yaml").exists()
        assert not (tree / "events.jsonl").exists()
        assert (tree / "output").is_dir() and (tree / "tmp").is_dir()
        retried = store.retry_job(ALICE, job["id"], utc_now())
        assert retried["status"] == "queued"
        assert retried["failure_reason"] is None
        assert retried["metrics"] == {} and retried["resources"] == {}


def test_incident_record_rejects_unknown_kind(settings):
    with Store(settings) as store:
        with pytest.raises(Exception):
            store.add_incident("unknown", "fingerprint")


def test_omp_runner_prefers_configured_path(monkeypatch):
    monkeypatch.setattr(incident.shutil, "which", lambda name: "/usr/bin/omp" if name == "omp" else None)
    assert incident._find_omp().as_posix() == "/usr/bin/omp"


def test_retryable_codes_cover_engine_failures():
    assert {"processing_failed", "render_failed", "storage_unavailable", "stalled"} <= incident.RETRYABLE_FAILURES


def test_runner_reports_and_offers_retry(settings, monkeypatch, tmp_path):
    with Store(settings) as store:
        store.allow_user(ADMIN, ALICE, "Alice")
        job = failed_job(store)
        manager = incident.IncidentManager(settings, store)
        created, _ = store.add_incident("job", "processing_failed", job_id=job["id"])
        manager._prepare(created, {"code": "processing_failed"})
        incident_id = created["id"]

    fake_omp = tmp_path / "omp"
    fake_omp.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, re, sys\n"
        "match = re.search(r'Каталог инцидента: `([^`]+)`', sys.argv[-1])\n"
        "directory = pathlib.Path(match.group(1))\n"
        "(directory / 'report.md').write_text('# report\\nfixed', encoding='utf-8')\n"
        "(directory / 'result.json').write_text(json.dumps({'status': 'fixed', 'summary': 'fixed'}), encoding='utf-8')\n",
        encoding="utf-8")
    fake_omp.chmod(0o755)
    monkeypatch.setattr(incident, "_find_omp", lambda: fake_omp)

    sent = []
    class Notifier:
        def __init__(self, value):
            pass
        async def text(self, chat_id, text, keyboard=None):
            sent.append(("text", chat_id, text, keyboard))
        async def document(self, chat_id, path, caption):
            sent.append(("document", chat_id, str(path), caption))
        async def close(self):
            pass
    monkeypatch.setattr(incident, "TelegramNotifier", Notifier)

    asyncio.run(incident._run(settings, incident_id))
    with Store(settings) as store:
        finished = store.get_incident(incident_id)
        assert finished["status"] == "fixed" and finished["retry_offered"]
        assert store.get_job(job["id"])["status"] == "failed"
        tokens = store.db.execute("SELECT action,target FROM durable_tokens WHERE owner_id=?", (ALICE,)).fetchall()
        assert [(row[0], row[1]) for row in tokens] == [("retry", job["id"])]
    assert any(item[0] == "document" and item[2].endswith("report.md") for item in sent)
    assert any(item[0] == "text" and item[3] and item[1] == ALICE for item in sent)
