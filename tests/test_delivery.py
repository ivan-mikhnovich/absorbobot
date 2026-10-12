import asyncio
from datetime import datetime, timezone

import pytest

import absorbobot.delivery as delivery_module
from absorbobot.config import Settings
from absorbobot.delivery import Delivery, FORMATS, package_result
from absorbobot.store import Store, StoreError


@pytest.fixture
def store(tmp_path):
    settings = Settings(admin_ids=(1,), state_dir=tmp_path / "state",
                        storage_dir=tmp_path / "mount" / "data",
                        backing_mount=tmp_path / "mount", min_free_bytes=1)
    with Store(settings) as value:
        for user in (2, 3):
            value.allow_user(1, user)
            value.upsert_user(user, language="en")
        yield value


def write_preparation(store, owner, draft_id, *, wall=2.0, cpu=1.5, system=0.25):
    """Записать собственные измерения подготовки входа в состояние черновика."""
    draft = store.get_draft(draft_id)
    state = dict(draft["ui_state"])
    state["preparation"] = {
        "schema_version": 1, "wall_seconds": wall, "updated_at": datetime.now(timezone.utc).isoformat(),
        "operations": [{
            "operation": "copy", "status": "complete", "reason": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(), "wall_seconds": wall,
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
    store.update_draft(owner, draft_id, ui_state=state)


def drafted(store, owner=2, signature="same", prepared=None):
    draft = store.create_draft(owner, source_message_id=123)
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    (tree / "input").mkdir(parents=True)
    (tree / "input" / "audio.wav").write_bytes(b"audio")
    store.add_file(owner, draft["id"], role="media", original_name="audio.wav",
                   relative_path="input/audio.wav", bytes=5, sha256="a" * 64,
                   duration=1, source_message_id=123)
    snapshot = {"version_info": {"version": "0.1.2", "commit": "b" * 40,
                                 "dirty": False}, "models": {}}
    store.update_draft(owner, draft["id"], status="ready", input_signature=signature,
                       snapshot=snapshot)
    if prepared is not None:
        write_preparation(store, owner, draft["id"], **prepared)
    return store.get_draft(draft["id"])


def completed(store, owner=2, signature="same", prepared=None):
    draft = drafted(store, owner, signature, prepared)
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    job = store.enqueue(owner, draft["id"], datetime.now(timezone.utc), force_new=True)
    store.claim_next(datetime.now(timezone.utc))
    store.update_job(job["id"], status="running")
    output = tree / "output"
    (output / ".absorbent").mkdir(parents=True)
    for name in ("notes.md", "notes.html", "notes.pdf"):
        (output / name).write_bytes(b"lecture notes")
    (output / ".absorbent" / "transcript.json").write_text("[]")
    package_result(tree)
    return store.update_job(job["id"], status="completed")


def plan(store, job, request=50):
    delivery = Delivery(store.settings, store)
    payload = {"owner_id": job["owner_id"], "source_job_id": job["id"],
               "work_uuid": job["id"]}
    files = asyncio.run(delivery.prepare_files(payload))
    return delivery, payload, files, delivery.enqueue(payload, request, files)


def test_completed_processing_survives_partial_delivery_and_restart(store, monkeypatch):
    monkeypatch.setattr(delivery_module, "ALBUM_LIMIT", 2)
    job = completed(store)
    delivery, payload, files, record = plan(store, job)
    actions = store.pending_outbox(100)
    assert len(actions) == 2
    store.outbox_sent(actions[0]["id"], {"message_id": 11})
    delivery.reconcile()
    assert store.deliveries(2)[0]["status"] == "pending"
    with Store(store.settings) as restarted:
        again = Delivery(store.settings, restarted).enqueue(payload, 50, files)
        assert again["id"] == record["id"] and again["already_planned"]
        assert restarted.get_job(job["id"])["status"] == "completed"
        for action in restarted.pending_outbox(100):
            restarted.outbox_sent(action["id"], {"message_id": action["id"]})
        Delivery(store.settings, restarted).reconcile()
        report = restarted.deliveries(2)[0]
        assert report["status"] == "sent"
        assert report["metrics"]["delivery_seconds"] >= 0
        stats = restarted.stats(2)
        assert stats["delivery_seconds"] == {
            "known_total": report["metrics"]["delivery_seconds"], "unknown_deliveries": 0}
        assert restarted.stats(3)["delivery_seconds"] == {
            "known_total": None, "unknown_deliveries": 0}
        assert restarted.quota(2, datetime.now(timezone.utc))["used"] == 1


def test_missing_source_cannot_be_replaced_by_other_matching_completed_result(store):
    first = completed(store)
    delivery, payload, files, record = plan(store, first)
    second = completed(store, owner=3)
    assert second["input_signature"] == first["input_signature"]
    store.purge(1, first["id"])
    with pytest.raises(StoreError) as exc:
        asyncio.run(delivery.prepare_files(payload))
    assert exc.value.code == "reuse_unavailable"
    assert store.get_job(first["id"])["status"] == "completed"


def test_unowned_history_delivery_and_banned_source_are_rejected(store):
    job = completed(store)
    delivery = Delivery(store.settings, store)
    payload = {"owner_id": 3, "source_job_id": job["id"], "work_uuid": None}
    files = asyncio.run(delivery.prepare_files(payload))
    with pytest.raises(StoreError) as exc:
        delivery.enqueue(payload, 100, files)
    assert exc.value.code == "not_owner"
    store.ban_user(1, 2, datetime.now(timezone.utc))
    with pytest.raises(StoreError) as exc:
        asyncio.run(delivery.prepare_files(payload))
    assert exc.value.code == "reuse_unavailable"


def test_packaging_rejects_symlinked_output_without_touching_foreign_files(tmp_path):
    tree, foreign = tmp_path / "work", tmp_path / "foreign"
    tree.mkdir()
    foreign.mkdir()
    (foreign / "notes.md").write_text("private")
    (tree / "output").symlink_to(foreign, target_is_directory=True)
    with pytest.raises(StoreError):
        package_result(tree)
    assert (foreign / "notes.md").read_text() == "private"
    assert not (foreign / "notes.zip").exists()


def test_delivery_preserves_preparation_and_redelivery_does_not_repeat_it(store):
    job = completed(store, prepared={"wall": 2.0, "cpu": 1.5, "system": 0.25})
    delivery, payload, files, record = plan(store, job)
    assert store.deliveries(2)[0]["metrics"]["preparation"]["wall_seconds"] == 2.0
    again = Delivery(store.settings, store).enqueue(payload, 51, files)
    assert again["id"] != record["id"]
    second = store.deliveries(2)[0]["metrics"]
    assert second["preparation"]["wall_seconds"] == 2.0
    assert second["request_outbox_id"] == 51
    # Доставки не переносят подготовку в свод повторно: работа считается однажды.
    assert store.stats(2)["preparation_seconds"]["known_total"] == 2.0
    assert store.stats(2)["preparation_seconds"]["unknown_works"] == 0


def test_reuse_delivery_carries_recipient_preparation_not_source_work(store):
    source = completed(store, owner=3, prepared={"wall": 9.0, "cpu": 8.5, "system": 2.0})
    recipient = drafted(store, owner=2, prepared={"wall": 1.25, "cpu": 0.5, "system": 0.1})
    store.reuse_draft(2, recipient["id"], source["id"], datetime.now(timezone.utc))
    payload = {"owner_id": 2, "source_job_id": source["id"], "work_uuid": recipient["id"]}
    delivery = Delivery(store.settings, store)
    files = asyncio.run(delivery.prepare_files(payload))
    delivery.enqueue(payload, 60, files)
    metrics = store.deliveries(2)[0]["metrics"]
    assert metrics["preparation"]["wall_seconds"] == 1.25
    assert metrics["preparation"]["resources"]["cpu_user_seconds"] == 0.5
    # Оплаченная работа первоисточника остаётся в его задании, не в выдаче получателя.
    assert "processing_seconds" not in metrics and "cpu_user_seconds" not in metrics
    assert store.stats(2)["cpu_user_seconds"]["known_total"] == 0.5
    assert store.stats(3)["cpu_user_seconds"]["known_total"] == 8.5


def test_album_chunks_avoid_a_singleton_tail():
    assert [len(chunk) for chunk in delivery_module.album_chunks(list(range(11)))] == [9, 2]
    assert [len(chunk) for chunk in delivery_module.album_chunks(list(range(20)))] == [10, 10]
    assert [len(chunk) for chunk in delivery_module.album_chunks(list(range(4)))] == [4]


def test_delivery_attaches_every_format_to_one_album(store):
    job = completed(store)
    delivery, payload, files, record = plan(store, job)
    actions = store.pending_outbox(100)
    assert len(actions) == 1
    data = actions[0]["payload"]
    assert data["method"] == "send_media_group"
    assert [attachment["filename"] for attachment in data["files"]] == list(FORMATS)
    assert data["documents"] == len(FORMATS)
    assert data["caption"].startswith("Lecture notes")
    assert record["documents"] == len(FORMATS)


def test_delivery_splits_many_attachments_into_several_albums(store, monkeypatch):
    monkeypatch.setattr(delivery_module, "ALBUM_LIMIT", 3)
    job = completed(store)
    delivery, payload, files, record = plan(store, job)
    actions = store.pending_outbox(100)
    assert [action["payload"]["documents"] for action in actions] == [2, 2]
    assert [action["payload"]["method"] for action in actions] == \
        ["send_media_group", "send_media_group"]
