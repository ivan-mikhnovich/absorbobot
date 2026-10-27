from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
import threading

import pytest

from absorbobot.config import ConfigError, Settings
from absorbobot.store import QuotaError, Store, StoreError
import absorbobot.store as store_module


NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
ADMIN = 1
ALICE = 2
BOB = 3
SNAPSHOT = {
    "version_info": {"version": "0.1.2", "commit": "a" * 40, "dirty": False},
    "models": {"llm": {"spec": {"backend": "openai", "api_model": "local-model"}, "provider": {"name": "local", "base_url": "http://127.0.0.1:9999/v1", "api_key_env": ""}, "local": True}},
}


@pytest.fixture
def settings(tmp_path):
    return Settings(admin_ids=(ADMIN,), state_dir=tmp_path / "ssd", storage_dir=tmp_path / "mount" / "storage", backing_mount=tmp_path / "mount", min_free_bytes=1)


@pytest.fixture
def store(settings, monkeypatch):
    monkeypatch.setattr(store_module, "utc_now", lambda: NOW)
    with Store(settings) as value:
        value.allow_user(ADMIN, ALICE, "Alice")
        value.allow_user(ADMIN, BOB, "Bob")
        yield value


def ready(store, owner=ALICE, signature="same", snapshot=None, source=123):
    draft = store.create_draft(owner, source_message_id=source, settings={"language": "auto", "output_language": "en", "fps": 1.0}, now_utc=NOW)
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    (tree / "input").mkdir(parents=True)
    (tree / "input" / "recording.m4a").write_bytes(b"test recording")
    store.add_file(owner, draft["id"], role="media", original_name="Original recording.m4a", relative_path="input/recording.m4a", sha256="b" * 64, bytes=14, duration=3, source_message_id=source)
    return store.update_draft(owner, draft["id"], status="ready", input_signature=signature, snapshot=SNAPSHOT if snapshot is None else snapshot)


def enqueue(store, owner=ALICE, at=NOW, signature="same", force=False):
    draft = ready(store, owner, signature)
    return store.enqueue(owner, draft["id"], at, force_new=force)


def complete(store, job, at=NOW):
    if job["status"] == "queued":
        assert store.claim_next(at)["id"] == job["id"]
    store.update_job(job["id"], status="running")
    output = store.settings.storage_dir / "jobs" / job["id"] / "output"
    output.mkdir()
    for name in ("notes.md", "notes.html", "notes.pdf", "notes.zip"):
        (output / name).write_bytes(name.encode())
    return store.update_job(job["id"], status="completed", completed_at=at)


def with_preparation(store, draft, wall=2.0, cpu=1.5, system=0.25):
    """Записать собственные измерения подготовки входа в состояние черновика."""
    state = dict(draft["ui_state"])
    state["preparation"] = {
        "schema_version": 1, "wall_seconds": wall, "updated_at": NOW.isoformat(),
        "operations": [{
            "operation": "copy", "status": "complete", "reason": None,
            "started_at": NOW.isoformat(), "ended_at": NOW.isoformat(), "wall_seconds": wall,
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
    return store.update_draft(draft["owner_id"], draft["id"], ui_state=state)


def error_code(code):
    return pytest.raises(StoreError, match=f"^{code}$")


def test_sqlite_wal_foreign_keys_and_ssd(settings, store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("INSERT INTO process_bindings(work_uuid,binding,created_at) VALUES ('missing','{}','now')")
    invalid_state = settings.storage_dir / "state"
    with pytest.raises(ConfigError):
        Store(replace(settings, state_dir=invalid_state))
    assert not invalid_state.exists()


def test_one_active_draft_and_new_uuid_after_close(store):
    first = ready(store)
    with error_code("active_draft_exists"):
        store.create_draft(ALICE)
    store.close_draft(ALICE, first["id"])
    second = ready(store)
    assert second["id"] != first["id"]
    assert len(store.files(first["id"])) == 1
    assert (store.settings.storage_dir / "jobs" / first["id"] / "input" / "recording.m4a").is_file()


def test_quota_eleventh_rejected_without_insert_and_exact_expiry(store):
    for i in range(10):
        enqueue(store, at=NOW + timedelta(seconds=i))
    draft = ready(store)
    at = NOW + timedelta(seconds=10)
    with pytest.raises(QuotaError) as failure:
        store.enqueue(ALICE, draft["id"], at, force_new=False)
    assert datetime.fromisoformat(failure.value.next_available_at) == NOW + timedelta(days=1)
    assert store.get_job(draft["id"]) is None
    assert store.get_draft(draft["id"])["status"] == "ready"
    assert store.quota(ALICE, NOW + timedelta(days=1) - timedelta(microseconds=1))["remaining"] == 0
    job = store.enqueue(ALICE, draft["id"], NOW + timedelta(days=1), force_new=False)
    assert job["id"] == draft["id"]
    assert store.quota(ALICE, NOW + timedelta(days=1))["used"] == 10


def test_errors_cancels_and_queued_still_spend_quota(store):
    first = enqueue(store)
    store.claim_next(NOW)
    store.update_job(first["id"], status="failed", failure_reason="processing_failed")
    second = enqueue(store)
    store.request_cancel(ALICE, [second["id"]], now_utc=NOW)
    third = enqueue(store)
    assert store.get_job(third["id"])["status"] == "queued"
    assert store.quota(ALICE, NOW)["used"] == 3


def test_last_slot_arbitrated_between_connections(store, settings):
    for i in range(9):
        enqueue(store, signature=str(i))
    barrier = threading.Barrier(2)

    def accept():
        with Store(settings) as connection:
            barrier.wait()
            try:
                with connection.transaction():
                    return enqueue(connection, signature="last")["id"]
            except QuotaError:
                return "quota_exceeded"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: accept(), range(2)))
    assert results.count("quota_exceeded") == 1
    assert len(store.jobs(ALICE, limit=100)) == 10
    assert store.quota(ALICE, NOW)["used"] == 10


def test_double_callback_is_atomic_and_does_not_spend_twice(store, settings):
    draft = ready(store)
    barrier = threading.Barrier(2)

    def click():
        with Store(settings) as connection:
            barrier.wait()
            try:
                return connection.enqueue(ALICE, draft["id"], NOW, force_new=False)["status"]
            except StoreError as failure:
                return failure.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: click(), range(2)))
    assert sorted(results) == ["draft_consumed", "queued"]
    assert store.quota(ALICE, NOW)["used"] == 1


def test_admin_bypasses_only_quota_and_fifo_is_claimed_exclusively(store, settings):
    normal = enqueue(store, at=NOW)
    admin_jobs = [enqueue(store, ADMIN, NOW + timedelta(seconds=i + 1), signature=str(i)) for i in range(11)]
    assert store.quota(ADMIN, NOW + timedelta(seconds=20))["remaining"] is None
    assert store.queue_position(normal["id"]) == 1
    assert store.claim_next(NOW)["id"] == normal["id"]
    with Store(settings) as other:
        assert other.claim_next(NOW) is None
    store.update_job(normal["id"], status="failed")
    assert store.claim_next(NOW)["id"] == admin_jobs[0]["id"]


def test_fifo_ties_use_uuid_and_persisted_pause(store, settings):
    first = enqueue(store)
    second = enqueue(store, BOB)
    ordered = sorted([first["id"], second["id"]])
    store.set_paused(ADMIN, True)
    with Store(settings) as other:
        assert other.paused()
        assert other.claim_next(NOW) is None
    with error_code("admin_required"):
        store.set_paused(ALICE, False)
    store.set_paused(ADMIN, False)
    assert store.claim_next(NOW)["id"] == ordered[0]


def test_missing_selected_env_rejected_atomically(store, monkeypatch):
    monkeypatch.delenv("ABSORBOBOT_UNIT_TEST_MISSING_KEY", raising=False)
    snapshot = {**SNAPSHOT, "models": {"llm": {"spec": {}, "provider": {"name": "cloud", "api_key_env": "ABSORBOBOT_UNIT_TEST_MISSING_KEY"}}}}
    draft = ready(store, snapshot=snapshot)
    with error_code("model_keys_unavailable") as failure:
        store.enqueue(ALICE, draft["id"], NOW, force_new=False)
    assert failure.value.details["key_names"] == ["ABSORBOBOT_UNIT_TEST_MISSING_KEY"]
    assert store.get_job(draft["id"]) is None
    assert store.quota(ALICE, NOW)["used"] == 0


def test_multitrack_audio_requires_explicit_selection_before_enqueue(store):
    tracks = [
        {"index": 1, "title": "Зал", "language": "ru", "default": True,
         "channels": 2, "start_time": 0.0, "offset": 0.0},
        {"index": 2, "title": "Перевод", "language": "en", "default": False,
         "channels": 1, "start_time": 0.0, "offset": 0.0},
    ]
    draft = ready(store, snapshot={**SNAPSHOT, "audio": {
        "tracks": tracks, "selected": [], "speakers": {}}})
    with error_code("audio_selection_required"):
        store.enqueue(ALICE, draft["id"], NOW, force_new=False)
    assert store.get_job(draft["id"]) is None
    store.update_draft(ALICE, draft["id"], snapshot={**SNAPSHOT, "audio": {
        "tracks": tracks, "selected": [2], "speakers": {"2": "Боб"}}})
    job = store.enqueue(ALICE, draft["id"], NOW, force_new=False)
    assert job["snapshot"]["audio"]["selected"] == [2]
    assert job["snapshot"]["audio"]["speakers"] == {"2": "Боб"}


def test_snapshot_is_independent_from_draft_and_last_user_preferences(store):
    job = enqueue(store)
    store.update_user(ALICE, settings={"fps": 99}, language="ru", output_language_override="en")
    assert store.get_job(job["id"])["settings"]["fps"] == 1.0
    assert store.get_job(job["id"])["snapshot"] == SNAPSHOT
    with error_code("draft_consumed"):
        store.update_draft(ALICE, job["id"], settings={"fps": 0.5})
    assert store.get_user(ALICE)["output_language_override"] == "en"
    store.update_user(ALICE, language="en")
    assert store.get_user(ALICE)["output_language_override"] == "en"
    store.update_user(ALICE, output_language_override=None)
    assert store.get_user(ALICE)["output_language_override"] is None


def test_incoming_material_invalidates_ready_signature_and_preserves_removed_metadata(store):
    draft = ready(store)
    material = store.add_file(ALICE, draft["id"], role="material", original_name="Slides.pdf", relative_path="input/slides.pdf", sha256="c" * 64, bytes=5, pages=2, source_message_id=124)
    changed = store.get_draft(draft["id"])
    assert changed["status"] == "collecting"
    assert changed["input_signature"] is None
    assert changed["source_message_ids"] == [123, 124]
    store.remove_file(ALICE, draft["id"], material["id"])
    assert store.files(draft["id"])[1]["removed"]
    assert store.files(draft["id"])[1]["bytes"] == 5
    with error_code("replace_requires_new_draft"):
        store.remove_file(ALICE, draft["id"], store.files(draft["id"])[0]["id"])


def test_authorization_owner_and_terminal_guards(store):
    with error_code("not_authorized"):
        store.upsert_user(999, username="stranger")
    assert store.get_user(999) is None
    job = enqueue(store)
    with error_code("not_owner"):
        store.get_job(job["id"], BOB)
    with error_code("not_owner"):
        store.request_cancel(BOB, [job["id"]])
    complete(store, job)
    assert store.request_cancel(ALICE, [job["id"]]) == []
    assert store.get_job(job["id"])["status"] == "completed"
    with error_code("invalid_transition"):
        store.update_job(job["id"], status="cancelled")
    with error_code("invalid_field"):
        store.update_job(job["id"], quota_counted=False)


def test_transaction_nested_failure_can_be_caught_without_leaking_side_effects(store):
    with store.transaction():
        store.update_user(ALICE, language="ru")
        try:
            with store.transaction():
                store.update_user(ALICE, language="en")
                store.add_outbox("message", {"owner_id": ALICE})
                raise RuntimeError("rollback nested")
        except RuntimeError:
            pass
        assert store.get_user(ALICE)["language"] == "ru"
    assert store.pending_outbox() == []


def test_update_domain_outbox_and_offset_commit_together(store, settings):
    def failed():
        store.update_user(ALICE, language="ru")
        store.add_outbox("message", {"owner_id": ALICE}, dedupe_key="reply-10")
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError):
        store.process_update(10, failed)
    assert store.offset() == 0
    assert store.get_user(ALICE)["language"] == "en"
    assert store.pending_outbox() == []
    count = []

    def accepted():
        count.append(1)
        store.update_user(ALICE, language="ru")
        store.add_outbox("message", {"owner_id": ALICE}, dedupe_key="reply-10")

    assert store.process_update(10, accepted)
    assert not store.process_update(10, accepted)
    assert count == [1]
    assert store.offset() == 11
    assert store.process_update(8, lambda: None)
    assert store.offset() == 11
    with Store(settings) as other:
        assert other.offset() == 11
        assert other.pending_outbox()[0]["payload"] == {"owner_id": ALICE}
        assert other.get_user(ALICE)["language"] == "ru"


def test_unknown_update_retains_only_update_id(store):
    assert store.process_update(987, lambda: None)
    assert store.offset() == 988
    assert store.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    assert store.pending_outbox() == []


def test_outbox_dedupe_retry_after_and_sent_survive_restart(store, settings, monkeypatch):
    message = store.add_outbox("send_document", {"delivery_id": 1}, dedupe_key="document-1")
    assert store.add_outbox("send_document", {"delivery_id": 1}, dedupe_key="document-1") == message
    store.outbox_retry(message, NOW + timedelta(seconds=30), "telegram_retry_after")
    assert store.pending_outbox() == []
    monkeypatch.setattr(store_module, "utc_now", lambda: NOW + timedelta(seconds=30))
    with Store(settings) as other:
        pending = other.pending_outbox()
        assert pending[0]["attempts"] == 1
        other.outbox_sent(message, {"message_id": 42})
        other.outbox_retry(message, NOW + timedelta(hours=1), "network_error")
    assert store.pending_outbox() == []
    with error_code("invalid_error_code"):
        store.outbox_retry(message, NOW, "raw exception with private path")


def test_tokens_once_only_owner_auth_and_transactional_consumption(store, settings):
    token = store.issue_token(ALICE, "start", "work", payload={"force_new": True})
    assert len(token.encode()) <= 64
    with error_code("invalid_token"):
        store.consume_token(BOB, token)
    with error_code("invalid_token"):
        store.consume_token(ALICE, token, "purge")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.consume_token(ALICE, token, "start")
            raise RuntimeError("failed action")
    with Store(settings) as other:
        assert other.consume_token(ALICE, token, "start")["payload"] == {"force_new": True}
    with error_code("token_consumed"):
        store.consume_token(ALICE, token)
    another = store.issue_token(ALICE, "cancel", "work")
    store.ban_user(ADMIN, ALICE, NOW)
    with error_code("not_authorized"):
        store.consume_token(ALICE, another)


def test_secret_and_nonfinite_json_are_never_persisted(store):
    for invalid, code in (({"api_key": "private"}, "secret_in_record"), ({"ABSORBOBOT_TOKEN": "private"}, "secret_in_record"), ({"fps": float("nan")}, "invalid_json")):
        with error_code(code):
            store.update_user(ALICE, settings=invalid)
    assert store.get_user(ALICE)["settings"] == {}
    store.update_user(ALICE, settings={"decimal": Decimal("1.250")})
    assert store.get_user(ALICE)["settings"] == {"decimal": "1.250"}
    with error_code("invalid_timestamp"):
        store.enqueue(ALICE, ready(store)["id"], NOW.replace(tzinfo=None), force_new=False)
    assert store.quota(ALICE, NOW)["used"] == 0


def test_allow_unseen_user_then_first_contact_initializes_language_once(store):
    assert not store.get_user(ALICE)["language_initialized"]
    first = store.upsert_user(ALICE, username="alice", language="ru")
    assert first["language"] == "ru"
    assert first["language_initialized"]
    assert store.upsert_user(ALICE, language="en")["language"] == "ru"
    store.update_user(ALICE, language="en")
    assert store.upsert_user(ALICE, language="ru")["language"] == "en"
    store.upsert_user(ADMIN)
    store.db.execute("UPDATE users SET approved=0 WHERE id=?", (ADMIN,))
    assert store.authorized(ADMIN)


def test_direct_reuse_is_free_at_full_quota_and_keeps_source_private(store):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    for i in range(10):
        enqueue(store, signature=f"distinct-{i}")
    draft = ready(store)
    delivery = store.reuse_draft(ALICE, draft["id"], source["id"], NOW)
    assert delivery["owner_id"] == ALICE
    assert delivery["source_job_id"] == source["id"]
    assert delivery["reused"]
    assert store.get_job(draft["id"]) is None
    assert store.quota(ALICE, NOW)["used"] == 10
    assert store.get_draft(draft["id"])["close_reason"] == "reused"
    assert store.deliveries(ALICE)[0]["engine_commit"] == "a" * 40
    with error_code("draft_not_ready"):
        store.reuse_draft(ALICE, draft["id"], source["id"], NOW)
    assert len(store.deliveries(ALICE)) == 1


@pytest.mark.parametrize("signature", ["material-order-ba", "different-language", "different-model-endpoint"])
def test_distinct_supplied_signatures_cannot_reuse(store, signature):
    source = complete(store, enqueue(store, BOB, signature="material-order-ab"))
    draft = ready(store, signature=signature)
    assert store.find_reuse(signature) is None
    with error_code("reuse_unavailable"):
        store.reuse_draft(ALICE, draft["id"], source["id"], NOW)
    assert store.get_draft(draft["id"])["status"] == "ready"
    assert store.deliveries(ALICE) == []


def test_reuse_requires_four_actual_contained_files_and_source_authorization(store, tmp_path):
    source = complete(store, enqueue(store, BOB))
    assert store.find_reuse("same")["id"] == source["id"]
    output = store.settings.storage_dir / "jobs" / source["id"] / "output"
    (output / "notes.pdf").unlink()
    assert store.find_reuse("same") is None
    external = tmp_path / "external.pdf"
    external.write_bytes(b"outside")
    (output / "notes.pdf").symlink_to(external)
    assert store.find_reuse("same") is None
    (output / "notes.pdf").unlink()
    (output / "notes.pdf").write_bytes(b"restored")
    store.ban_user(ADMIN, BOB, NOW)
    assert store.find_reuse("same") is None
    store.allow_user(ADMIN, BOB)
    assert store.find_reuse("same")["id"] == source["id"]
    assert store.find_reuse("same", exclude_job_id=source["id"]) is None


def test_reuse_chooses_latest_complete_result_and_skips_missing_latest(store):
    first = complete(store, enqueue(store, BOB, NOW - timedelta(hours=2)), NOW - timedelta(hours=1))
    second = complete(store, enqueue(store, BOB, NOW - timedelta(minutes=30)), NOW)
    assert store.find_reuse("same")["id"] == second["id"]
    (store.settings.storage_dir / "jobs" / second["id"] / "output" / "notes.zip").unlink()
    assert store.find_reuse("same")["id"] == first["id"]


def test_late_completion_choice_refunds_once_and_does_not_block_fifo(store):
    source = enqueue(store, BOB, NOW - timedelta(seconds=10))
    waiting = enqueue(store, at=NOW)
    following = enqueue(store, ADMIN, NOW + timedelta(seconds=1), signature="other")
    complete(store, source, NOW + timedelta(seconds=2))
    assert store.claim_next(NOW + timedelta(seconds=3))["id"] == waiting["id"]
    awaiting = store.await_reuse(waiting["id"], source["id"], NOW)
    assert awaiting["quota_counted"]
    assert store.quota(ALICE, NOW)["used"] == 1
    assert store.claim_next(NOW + timedelta(seconds=4))["id"] == following["id"]
    reused = store.confirm_late_reuse(ALICE, waiting["id"], "reuse", NOW)
    assert reused["status"] == "reused"
    assert not reused["quota_counted"]
    assert store.quota(ALICE, NOW)["used"] == 0
    assert len(store.deliveries(ALICE)) == 1
    with error_code("choice_consumed"):
        store.confirm_late_reuse(ALICE, waiting["id"], "reuse", NOW)
    assert len(store.deliveries(ALICE)) == 1


def test_late_force_confirmation_excludes_own_slot_and_moves_to_tail(store):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    waiting = enqueue(store)
    following = enqueue(store, ADMIN, NOW + timedelta(seconds=1), signature="other")
    for i in range(9):
        enqueue(store, at=NOW + timedelta(seconds=i + 2), signature=f"distinct-{i}")
    assert store.claim_next(NOW)["id"] == waiting["id"]
    store.await_reuse(waiting["id"], source["id"], NOW)
    initial = store.get_job(waiting["id"])["initial_accepted_at"]
    forced = store.confirm_late_reuse(ALICE, waiting["id"], "force_new", NOW + timedelta(minutes=1))
    assert forced["status"] == "queued"
    assert forced["force_new"] and forced["quota_counted"]
    assert forced["initial_accepted_at"] == initial
    assert datetime.fromisoformat(forced["accepted_at"]) == NOW + timedelta(minutes=1)
    assert store.quota(ALICE, NOW + timedelta(minutes=1))["used"] == 10
    assert store.queue_position(waiting["id"]) == 11
    assert store.claim_next(NOW)["id"] == following["id"]


def test_expired_reservation_cannot_force_when_quota_filled_but_can_reuse(store):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    waiting = enqueue(store)
    store.claim_next(NOW)
    store.await_reuse(waiting["id"], source["id"], NOW)
    tomorrow = NOW + timedelta(days=1)
    for i in range(10):
        enqueue(store, at=tomorrow, signature=f"tomorrow-{i}")
    with pytest.raises(QuotaError):
        store.confirm_late_reuse(ALICE, waiting["id"], "force_new", tomorrow)
    assert store.get_job(waiting["id"])["status"] == "awaiting_reuse_choice"
    assert store.confirm_late_reuse(ALICE, waiting["id"], "reuse", tomorrow)["status"] == "reused"
    assert store.quota(ALICE, tomorrow)["used"] == 10


def test_missing_late_source_leaves_choice_without_automatic_processing(store):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    waiting = enqueue(store)
    store.claim_next(NOW)
    store.await_reuse(waiting["id"], source["id"], NOW)
    store.purge(ADMIN, source["id"])
    with error_code("reuse_unavailable"):
        store.confirm_late_reuse(ALICE, waiting["id"], "reuse", NOW)
    assert store.get_job(waiting["id"])["status"] == "awaiting_reuse_choice"
    assert store.deliveries(ALICE) == []
    forced = store.confirm_late_reuse(ALICE, waiting["id"], "force_new", NOW)
    assert forced["status"] == "queued"
    assert forced["force_new"]


def test_explicit_force_cannot_be_substituted_with_late_reuse(store):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    forced = enqueue(store, force=True)
    store.claim_next(NOW)
    with error_code("invalid_transition"):
        store.await_reuse(forced["id"], source["id"], NOW)
    assert store.get_job(forced["id"])["status"] == "preparing"


def test_restart_preserves_awaiting_choice_queued_completed_and_pending_delivery(store, settings):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    waiting = enqueue(store)
    following = enqueue(store, ADMIN, NOW + timedelta(seconds=1), signature="other")
    store.claim_next(NOW)
    store.await_reuse(waiting["id"], source["id"], NOW)
    store.add_outbox("delivery", {"source_job_id": source["id"]})
    with Store(settings) as restarted:
        assert restarted.recover_interrupted(NOW) == []
        assert restarted.get_job(waiting["id"])["status"] == "awaiting_reuse_choice"
        assert restarted.get_job(source["id"])["status"] == "completed"
        assert restarted.pending_outbox()[0]["kind"] == "delivery"
        assert restarted.claim_next(NOW)["id"] == following["id"]
        assert restarted.confirm_late_reuse(ALICE, waiting["id"], "reuse", NOW)["status"] == "reused"


def test_cancel_only_own_jobs_including_choice_and_completed_race(store):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    waiting = enqueue(store)
    store.claim_next(NOW)
    store.await_reuse(waiting["id"], source["id"], NOW)
    queued = enqueue(store, signature="queued")
    foreign = enqueue(store, BOB, signature="foreign")
    with error_code("not_owner"):
        store.request_cancel(ALICE, [queued["id"], foreign["id"]])
    assert store.get_job(queued["id"])["status"] == "queued"
    cancelled = store.request_cancel(ALICE, now_utc=NOW)
    assert {job["id"] for job in cancelled} == {waiting["id"], queued["id"]}
    assert all(job["status"] == "cancelled" for job in cancelled)
    assert store.get_job(foreign["id"])["status"] == "queued"
    assert store.get_job(source["id"])["status"] == "completed"
    assert store.quota(ALICE, NOW)["used"] == 2


def test_ban_cancels_target_active_queue_draft_not_other_users(store):
    active = enqueue(store)
    queued = enqueue(store, at=NOW + timedelta(seconds=1), signature="queued")
    foreign = enqueue(store, BOB, NOW + timedelta(seconds=2), signature="foreign")
    draft = ready(store)
    store.claim_next(NOW)
    store.update_job(active["id"], status="running")
    actions = store.ban_user(ADMIN, ALICE, NOW)
    assert not store.authorized(ALICE)
    assert store.get_job(active["id"])["status"] == "cancel_requested"
    assert store.get_job(queued["id"])["status"] == "cancelled"
    assert store.get_draft(draft["id"])["status"] == "closed"
    assert store.get_job(foreign["id"])["status"] == "queued"
    assert {row["id"] for row in actions} == {active["id"], queued["id"], draft["id"]}
    assert all(row["cancellation_actor_id"] == ADMIN for row in actions)
    with error_code("not_authorized"):
        store.enqueue(ALICE, draft["id"], NOW, force_new=False)
    with error_code("cannot_ban_admin"):
        store.ban_user(ADMIN, ADMIN, NOW)
    store.allow_user(ADMIN, ALICE)
    assert store.quota(ALICE, NOW)["used"] == 2


def test_preparation_pause_lock_binding_cancel_and_reap(store):
    draft = ready(store)
    other = ready(store, BOB)
    store.set_paused(ADMIN, True)
    with error_code("paused"):
        store.update_draft(ALICE, draft["id"], status="preparing")
    store.set_paused(ADMIN, False)
    store.update_draft(ALICE, draft["id"], status="preparing")
    with error_code("execution_busy"):
        store.update_draft(BOB, other["id"], status="preparing")
    queued = enqueue(store, ADMIN)
    assert store.claim_next(NOW) is None
    store.bind_process(draft["id"], {"host_pid": 1234, "host_pgid": 1234, "start_time": 123.0})
    with error_code("process_bound"):
        store.update_draft(ALICE, draft["id"], status="ready")
    closing = store.close_draft(ALICE, draft["id"])
    assert closing["status"] == "preparing" and closing["close_requested"]
    assert store.claim_next(NOW) is None
    store.bind_process(draft["id"], None)
    assert store.get_draft(draft["id"])["status"] == "closed"
    assert store.claim_next(NOW)["id"] == queued["id"]


@pytest.mark.parametrize("active_status", ["preparing", "running", "cancel_requested"])
def test_restart_fails_active_and_retains_identity_until_cleanup(store, settings, active_status):
    active = enqueue(store)
    following = enqueue(store, BOB, NOW + timedelta(seconds=1), signature="other")
    store.claim_next(NOW)
    if active_status != "preparing":
        store.update_job(active["id"], status="running")
    store.bind_process(active["id"], {"host_pid": 1234, "host_pgid": 1234, "start_time": 123.0})
    if active_status == "cancel_requested":
        store.request_cancel(ALICE, [active["id"]])
    with Store(settings) as restarted:
        recovered = restarted.recover_interrupted(NOW)
        assert recovered[0]["id"] == active["id"]
        assert recovered[0]["failure_reason"] == "interrupted"
        assert recovered[0]["resources"] == {"completeness": "partial"}
        assert restarted.process_bindings()[0]["binding"]["host_pid"] == 1234
        assert restarted.claim_next(NOW) is None
        restarted.bind_process(active["id"], None)
        assert restarted.claim_next(NOW)["id"] == following["id"]
        assert restarted.quota(ALICE, NOW)["used"] == 1


def test_preparing_draft_restart_fails_without_job_or_automatic_resume(store, settings):
    draft = ready(store)
    store.update_draft(ALICE, draft["id"], status="preparing")
    store.bind_process(draft["id"], {"host_pid": 1234, "start_time": 123.0})
    with Store(settings) as restarted:
        recovered = restarted.recover_interrupted(NOW)
        assert recovered[0]["kind"] == "draft"
        assert recovered[0]["status"] == "failed"
        assert recovered[0]["failure_reason"] == "interrupted"
        assert restarted.get_job(draft["id"]) is None
        assert restarted.process_bindings()
        assert restarted.active_draft(ALICE) is None
        restarted.bind_process(draft["id"], None)
        assert restarted.quota(ALICE, NOW)["used"] == 0


def test_active_cancel_requires_reap_before_terminal_and_next_claim(store):
    active = enqueue(store)
    following = enqueue(store, BOB, NOW + timedelta(seconds=1))
    store.claim_next(NOW)
    store.bind_process(active["id"], {"host_pid": 1234})
    assert store.request_cancel(ALICE)[0]["status"] == "cancel_requested"
    assert store.request_cancel(ALICE) == []
    with error_code("process_bound"):
        store.update_job(active["id"], status="cancelled")
    assert store.claim_next(NOW) is None
    store.bind_process(active["id"], None)
    store.update_job(active["id"], status="cancelled")
    assert store.claim_next(NOW)["id"] == following["id"]


def test_purge_reused_own_tree_never_source_and_retains_quota_usage(store):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    waiting = enqueue(store)
    store.claim_next(NOW)
    store.await_reuse(waiting["id"], source["id"], NOW)
    store.confirm_late_reuse(ALICE, waiting["id"], "reuse", NOW)
    result = store.purge(ADMIN, waiting["id"])
    assert result["files_deleted"]
    assert not (store.settings.storage_dir / "jobs" / waiting["id"]).exists()
    assert (store.settings.storage_dir / "jobs" / source["id"] / "output" / "notes.pdf").exists()
    assert store.find_reuse("same")["id"] == source["id"]
    assert store.get_job(waiting["id"])["status"] == "reused"
    assert store.get_job(waiting["id"])["reuse_source_job_id"] == source["id"]
    assert store.files(waiting["id"])[0]["bytes"] == 14
    result = store.purge(ADMIN, source["id"])
    assert result["dependent_deliveries"] == 1
    assert store.find_reuse("same") is None
    assert store.deliveries(ALICE)[0]["files_deleted"]
    assert store.quota(BOB, NOW)["used"] == 1


def test_purge_closed_and_failed_drafts_and_ready_without_job_only(store):
    for status in ("closed", "failed", "ready"):
        draft = ready(store)
        if status == "closed":
            store.close_draft(ALICE, draft["id"])
        elif status == "failed":
            store.update_draft(ALICE, draft["id"], status="failed", failure_reason="input_rejected")
        rows = store.work_storage()
        assert any(row["id"] == draft["id"] and row["job_status"] is None and row["storage_bytes"] == 14 for row in rows)
        store.purge(ADMIN, draft["id"])
        assert store.get_draft(draft["id"])["status"] == "deleted"
        assert store.files(draft["id"])[0]["bytes"] == 14
    assert store.jobs(ALICE) == []


def test_purge_requires_admin_terminal_and_path_containment_never_native_cache(store, tmp_path):
    native = store.settings.storage_dir / "telegram-api-files"
    native.mkdir(parents=True)
    native_file = native / "opaque"
    native_file.write_bytes(b"do not delete")
    draft = ready(store)
    with error_code("admin_required"):
        store.purge(ALICE, draft["id"])
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    outside = tmp_path / "personal"
    outside.mkdir()
    external = outside / "private"
    external.write_bytes(b"personal")
    (tree / "external-link").symlink_to(outside, target_is_directory=True)
    store.purge(ADMIN, draft["id"])
    assert external.read_bytes() == b"personal"
    assert native_file.read_bytes() == b"do not delete"
    with error_code("invalid_work_uuid"):
        store.purge(ADMIN, "../telegram-api-files")
    active = enqueue(store)
    with error_code("work_active"):
        store.purge(ADMIN, active["id"])


def test_purge_rejects_symlinked_uuid_tree(store, tmp_path):
    draft = store.create_draft(ALICE)
    store.close_draft(ALICE, draft["id"])
    jobs = store.settings.storage_dir / "jobs"
    jobs.mkdir(parents=True)
    external = tmp_path / "personal"
    external.mkdir()
    (jobs / draft["id"]).symlink_to(external, target_is_directory=True)
    with error_code("unsafe_path"):
        store.purge(ADMIN, draft["id"])
    assert external.is_dir()
    assert not store.get_draft(draft["id"])["files_deleted"]


def api_events(job, request="attempt-1", usage=None, run_id=None):
    start = {"schema_version": 1, "event": "api_request_start", "run_id": run_id or job["id"], "job_id": job["id"], "request_id": request, "timestamp_utc": NOW.isoformat(), "provider": "cloud", "model": "model"}
    end = {"schema_version": 1, "event": "api_request_end", "run_id": start["run_id"], "request_id": request, "timestamp_utc": (NOW + timedelta(seconds=1)).isoformat(), "usage": usage}
    return start, end


def test_immutable_tariffs_attempt_ingest_decimal_costs_and_user_stats_redaction(store):
    job = enqueue(store)
    tariff = {"observed_at": NOW.isoformat(), "rates": {"hit": Decimal("0.006")}}
    digest = store.save_tariff_snapshot(tariff)
    assert store.save_tariff_snapshot(tariff) == digest
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE tariff_snapshots SET document='{}' WHERE digest=?", (digest,))
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("DELETE FROM tariff_snapshots WHERE digest=?", (digest,))
    start, end = api_events(job, usage={"prompt_tokens": 10, "completion_tokens": 3, "reasoning_tokens": 1}, run_id="independent-core-run")
    cost = {"amount": Decimal("0.000012"), "currency": "USD", "complete": True, "tariff_digest": digest}
    store.record_api_call(start, end, cost)
    store.record_api_call(start, end, cost)
    unknown_start, unknown_end = api_events(job, request="attempt-2")
    store.record_api_call(unknown_start, unknown_end, {"amount": None, "reason": "usage_missing"})
    admin = store.stats(ALICE, include_cost=True, actor_id=ADMIN)
    assert admin["api_attempts"] == 2
    assert admin["estimated_api_cost"] == {"known_totals": {"USD": "0.000012"}, "incomplete_calls": 1}
    assert admin["completion_tokens"] == {"known_total": 3, "unknown_calls": 1}
    assert admin["cpu_user_seconds"] == {"known_total": None, "unknown_works": 1}
    user = store.stats(ALICE)
    assert "estimated_api_cost" not in user
    assert not any(word in str(user).lower() for word in ("usd", "tariff", "price", "cost", "amount"))
    newer = store.save_tariff_snapshot({"rates": {"hit": "0.009"}})
    assert newer != digest
    assert store.stats(ALICE, include_cost=True, actor_id=ADMIN)["estimated_api_cost"] == admin["estimated_api_cost"]
    with error_code("api_attempt_conflict"):
        store.record_api_call(start, end, {**cost, "amount": "9"})


def test_incomplete_attempt_can_close_once_and_retries_are_separate(store):
    job = enqueue(store)
    start, end = api_events(job)
    store.record_api_call(start, None, {"amount": None, "reason": "pending"})
    assert store.stats(ALICE)["prompt_tokens"]["known_total"] is None
    store.record_api_call(start, end, {"amount": None, "reason": "transport_error"})
    second_start, second_end = api_events(job, "attempt-2", {"completion_tokens": 0})
    store.record_api_call(second_start, second_end, {"amount": "0", "currency": "USD", "complete": True})
    assert store.stats(ALICE)["api_attempts"] == 2
    assert store.stats(ALICE)["completion_tokens"] == {"known_total": 0, "unknown_calls": 1}
    assert store.stats(ALICE, include_cost=True, actor_id=ADMIN)["estimated_api_cost"]["incomplete_calls"] == 1


def test_reuse_does_not_transfer_original_api_or_resources_to_recipient(store):
    source = complete(store, enqueue(store, BOB))
    store.update_job(source["id"], resources={"cpu_user_seconds": 8.5, "cpu_system_seconds": 2, "busy_gpu_seconds": None})
    start, end = api_events(source, usage={"prompt_tokens": 10})
    store.record_api_call(start, end, {"amount": "1.2", "currency": "USD", "complete": True})
    draft = ready(store)
    store.reuse_draft(ALICE, draft["id"], source["id"], NOW)
    recipient = store.stats(ALICE, include_cost=True, actor_id=ADMIN)
    assert recipient["jobs"] == 0 and recipient["api_attempts"] == 0
    assert recipient["cpu_user_seconds"]["known_total"] is None
    assert recipient["estimated_api_cost"]["known_totals"] == {}
    assert store.stats(BOB, include_cost=True, actor_id=ADMIN)["estimated_api_cost"]["known_totals"] == {"USD": "1.2"}
    assert store.stats(BOB)["cpu_user_seconds"]["known_total"] == 8.5
    store.purge(ADMIN, source["id"])
    assert store.stats(BOB, include_cost=True, actor_id=ADMIN)["estimated_api_cost"]["known_totals"] == {"USD": "1.2"}


def test_enqueue_freezes_own_preparation_into_job_metrics_and_resources(store):
    draft = with_preparation(store, ready(store))
    job = store.enqueue(ALICE, draft["id"], NOW, force_new=True)
    # Собственная подготовка входа заморожена вместе со снимком, а не пересчитана.
    assert job["metrics"]["preparation_seconds"] == 2.0
    assert job["metrics"]["preparation"]["wall_seconds"] == 2.0
    assert job["resources"]["cpu_user_seconds"] == 1.5
    assert job["resources"]["cpu_system_seconds"] == 0.25
    assert job["resources"]["phase_reports"] == [
        job["metrics"]["preparation"]["operations"][0]["resources"]]


def test_preparation_is_counted_once_whether_in_job_or_standalone_draft(store):
    draft = with_preparation(store, ready(store))
    store.enqueue(ALICE, draft["id"], NOW, force_new=True)
    stats = store.stats(ALICE)
    assert stats["jobs"] == 1
    assert stats["cpu_user_seconds"]["known_total"] == 1.5
    assert stats["cpu_system_seconds"]["known_total"] == 0.25
    assert stats["preparation_seconds"]["known_total"] == 2.0
    assert stats["cpu_user_seconds"]["unknown_works"] == 0


def test_free_reuse_with_full_quota_keeps_only_recipient_preparation(store):
    source_draft = with_preparation(store, ready(store, BOB), wall=9.0, cpu=8.5, system=2.0)
    source = complete(store, store.enqueue(BOB, source_draft["id"], NOW, force_new=False))
    for index in range(10):
        enqueue(store, at=NOW + timedelta(seconds=index + 1), signature=str(index))
    recipient = with_preparation(store, ready(store, signature="same"), wall=2.0, cpu=1.5, system=0.25)
    store.reuse_draft(ALICE, recipient["id"], source["id"], NOW)
    stats = store.stats(ALICE, include_cost=True, actor_id=ADMIN)
    assert stats["jobs"] == 10 and stats["api_attempts"] == 0
    # Получатель бесплатного повторного использования платит только за свою подготовку.
    assert stats["estimated_api_cost"]["known_totals"] == {}
    assert stats["cpu_user_seconds"]["known_total"] == 1.5
    assert stats["preparation_seconds"]["known_total"] == 2.0
    assert store.stats(BOB, include_cost=True, actor_id=ADMIN)["cpu_user_seconds"]["known_total"] == 8.5


def test_simultaneous_workers_claim_one_execution_even_before_process_binding(store, settings):
    first = enqueue(store)
    second = enqueue(store, BOB, NOW + timedelta(seconds=1))
    barrier = threading.Barrier(2)

    def claim():
        with Store(settings) as connection:
            barrier.wait()
            job = connection.claim_next(NOW)
            return job["id"] if job else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert results.count(first["id"]) == 1
    assert results.count(None) == 1
    assert store.get_job(second["id"])["status"] == "queued"
    with error_code("dedicated_transition_required"):
        store.update_job(second["id"], status="preparing")


def test_concurrent_late_choice_is_one_delivery_and_one_refund(store, settings):
    source = complete(store, enqueue(store, BOB, NOW - timedelta(hours=1)))
    waiting = enqueue(store)
    store.claim_next(NOW)
    store.await_reuse(waiting["id"], source["id"], NOW)
    barrier = threading.Barrier(2)

    def confirm():
        with Store(settings) as connection:
            barrier.wait()
            try:
                return connection.confirm_late_reuse(ALICE, waiting["id"], "reuse", NOW)["status"]
            except StoreError as failure:
                return failure.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert sorted(results) == ["choice_consumed", "reused"]
    assert len(store.deliveries(ALICE)) == 1
    assert store.quota(ALICE, NOW)["used"] == 0


def test_owned_lookup_missing_is_none_and_unknown_usage_is_not_zero(store):
    assert store.get_job("missing", ALICE) is None
    assert store.get_draft("missing", ALICE) is None
    job = enqueue(store)
    start, end = api_events(job)
    store.record_api_call(start, end, {"amount": None, "reason": "unknown_usage"})
    stats = store.stats(ALICE)
    assert stats["prompt_tokens"] == {"known_total": None, "unknown_calls": 1}
    assert stats["cache_hit"] == {"known_total": None, "unknown_calls": 1}
    assert stats["busy_gpu_seconds"] == {"known_total": None, "unknown_works": 1}


def test_admin_retains_banned_user_history_and_quota(store):
    job = enqueue(store)
    store.ban_user(ADMIN, ALICE, NOW)
    assert store.get_job(job["id"])["status"] == "cancelled"
    report = store.stats(ALICE, include_cost=True, actor_id=ADMIN)
    assert report["jobs"] == 1
    assert report["quota"]["used"] == 1
    assert report["statuses"] == {"cancelled": 1}
    with error_code("not_authorized"):
        store.stats(ALICE)
    with error_code("admin_required"):
        store.stats(ALICE, include_cost=True, actor_id=BOB)
    with error_code("admin_required"):
        store.quota(ALICE, NOW, actor_id=BOB)


def test_only_admin_can_obtain_money_or_global_statistics(store):
    enqueue(store)
    with error_code("admin_required"):
        store.stats(ALICE, include_cost=True)
    with error_code("admin_required"):
        store.stats()
    assert "estimated_api_cost" not in store.stats(ALICE)
    assert store.stats(include_cost=True, actor_id=ADMIN)["jobs"] == 1


def test_binding_enrichment_retains_identity_even_after_ban(store):
    job = enqueue(store)
    store.claim_next(NOW)
    binding = {"launcher_pid": 12000, "launcher_start_time": 17.5, "phase_id": "probe"}
    store.bind_process(job["id"], binding)
    store.ban_user(ADMIN, ALICE, NOW)
    enriched = {**binding, "namespace_pid": 12001, "namespace_start_time": 17.6, "pgid": 12001}
    store.bind_process(job["id"], enriched)
    assert store.process_bindings()[0]["binding"] == enriched
    assert store.claim_next(NOW) is None
    for field, changed in (("launcher_pid", 13000), ("launcher_start_time", 19),
                           ("phase_id", "different"), ("pgid", 1)):
        with error_code("invalid_binding"):
            store.bind_process(job["id"], {**enriched, field: changed})
    with error_code("invalid_binding"):
        store.bind_process(job["id"], binding)
    store.bind_process(job["id"], None)
    assert not store.process_bindings()


def test_memory_statistics_take_peak_and_keep_partially_unknown_work_visible(store):
    first = complete(store, enqueue(store, signature="first"))
    store.update_job(first["id"], resources={
        "cpu_user_seconds": 3, "ru_maxrss_kib": 4, "vram_peak_bytes": 10,
        "phase_reports": [{"cpu_user_seconds": 3, "ru_maxrss_kib": 4, "vram_peak_bytes": 10},
                          {"cpu_user_seconds": None, "ru_maxrss_kib": None, "vram_peak_bytes": None}]})
    second = complete(store, enqueue(store, at=NOW + timedelta(seconds=1), signature="second"))
    store.update_job(second["id"], resources={
        "cpu_user_seconds": 5, "ru_maxrss_kib": 6, "vram_peak_bytes": 20,
        "phase_reports": [{"cpu_user_seconds": 5, "ru_maxrss_kib": 6, "vram_peak_bytes": 20}]})
    foreign = complete(store, enqueue(store, BOB, NOW + timedelta(seconds=2), signature="foreign"))
    store.update_job(foreign["id"], resources={"ru_maxrss_kib": 99, "vram_peak_bytes": 99})
    stats = store.stats(ALICE)
    assert stats["cpu_user_seconds"] == {"known_total": 8, "unknown_works": 1}
    assert stats["ru_maxrss_kib"] == {"known_peak": 6, "unknown_works": 1}
    assert stats["vram_peak_bytes"] == {"known_peak": 20, "unknown_works": 1}
    assert store.stats(actor_id=ADMIN)["ru_maxrss_kib"]["known_peak"] == 99


def test_preparation_only_draft_does_not_become_unknown_processing_work(store):
    with_preparation(store, ready(store))
    stats = store.stats(ALICE)
    assert stats["preparation_seconds"]["known_total"] == 2
    assert stats["processing_seconds"] == {"known_total": None, "unknown_works": 0}
    assert stats["render_seconds"] == {"known_total": None, "unknown_works": 0}


def test_request_access_resolves_known_username_and_queues_unknown(store):
    store.upsert_user(ALICE, username="Alice_W", language="ru")
    resolved = store.request_access(ADMIN, "alice_w", "Trusted")
    assert resolved == {"id": ALICE, "username": "alice_w", "queued": False}
    assert store.get_user(ALICE)["label"] == "Trusted"
    queued = store.request_access(ADMIN, "Ghost_1")
    assert queued["queued"] and queued["id"] is None
    assert store.db.execute("SELECT username FROM access_requests").fetchone()[0] == "Ghost_1"


def test_claim_access_grants_once_case_insensitively_and_needs_a_username(store):
    store.request_access(ADMIN, "Newcomer", "Note")
    assert store.claim_access(9, None) is None
    assert store.claim_access(9, "someone_else") is None
    granted = store.claim_access(9, "newcomer")
    assert granted == {"actor_id": ADMIN, "username": "Newcomer", "label": "Note"}
    assert store.authorized(9)
    assert store.get_user(9)["label"] == "Note"
    assert store.claim_access(9, "Newcomer") is None
    assert store.db.execute("SELECT COUNT(*) FROM access_requests").fetchone()[0] == 0


def test_request_access_requires_an_administrator(store):
    with pytest.raises(StoreError) as error:
        store.request_access(ALICE, "Ghost_1")
    assert error.value.code == "admin_required"
