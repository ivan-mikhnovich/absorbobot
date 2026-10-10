from datetime import datetime, timedelta, timezone

import pytest

from absorbobot.admin import Admin
from absorbobot.config import Settings
from absorbobot.i18n import tr
from absorbobot.store import Store


NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
ADMIN = 1
OTHER_ADMIN = 4
ALICE = 2
BOB = 3
CORE = {"version_info": {"version": "0.1.2", "commit": "a" * 40, "dirty": False}}
SNAPSHOT = {**CORE, "models": {"llm": {"name": "local-model", "spec": {"backend": "openai", "api_model": "local-model"}, "provider": {"name": "local", "base_url": "http://127.0.0.1:9999/v1", "api_key_env": ""}, "local": True}}}


@pytest.fixture
def store(tmp_path):
    settings = Settings(admin_ids=(ADMIN, OTHER_ADMIN), state_dir=tmp_path / "ssd", storage_dir=tmp_path / "mount" / "storage", backing_mount=tmp_path / "mount", min_free_bytes=1)
    with Store(settings) as value:
        value.upsert_user(ADMIN, language="en")
        value.upsert_user(OTHER_ADMIN, language="ru")
        value.allow_user(ADMIN, ALICE)
        value.upsert_user(ALICE, language="ru")
        value.allow_user(ADMIN, BOB)
        value.upsert_user(BOB, language="en")
        yield value


@pytest.fixture
def admin(store):
    return Admin(store, store.settings, CORE)


def message(text, actor=ADMIN, *, chat_type="private", chat_id=None):
    return {"message_id": 90, "text": text, "from": {"id": actor}, "chat": {"id": actor if chat_id is None else chat_id, "type": chat_type}}


def callback(token, actor=ADMIN, *, chat_type="private"):
    return {"id": "callback-id", "data": token, "from": {"id": actor}, "message": {"message_id": 90, "chat": {"id": actor, "type": chat_type}}}


def token(store, action, target=None):
    row = store.db.execute("SELECT token FROM durable_tokens WHERE action=?" + (" AND target=?" if target is not None else "") + " ORDER BY rowid DESC LIMIT 1", (action, target) if target is not None else (action,)).fetchone()
    assert row is not None
    return row["token"]


def outgoing(store, owner=None, *, purpose=None):
    rows = store.pending_outbox(1000)
    return [row for row in rows if row["kind"] == "telegram" and (owner is None or row["payload"]["owner_id"] == owner) and (purpose is None or row["payload"].get("purpose") == purpose)]


def ready(store, owner=ALICE, *, source=123, signature="same"):
    draft = store.create_draft(owner, source_message_id=source)
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    (tree / "input").mkdir(parents=True)
    (tree / "input" / "recording.m4a").write_bytes(b"lecture")
    store.add_file(owner, draft["id"], role="media", original_name="lecture.m4a", relative_path="input/recording.m4a", sha256="b" * 64, bytes=7, duration=3, source_message_id=source)
    return store.update_draft(owner, draft["id"], status="ready", input_signature=signature, snapshot=SNAPSHOT)


def enqueue(store, owner=ALICE, *, at=NOW, source=123, signature="same"):
    draft = ready(store, owner, source=source, signature=signature)
    return store.enqueue(owner, draft["id"], at, force_new=False)


def complete(store, job):
    assert store.claim_next(NOW)["id"] == job["id"]
    store.update_job(job["id"], status="running")
    output = store.settings.storage_dir / "jobs" / job["id"] / "output"
    output.mkdir()
    for name in ("notes.md", "notes.html", "notes.pdf", "notes.zip"):
        (output / name).write_bytes(b"result")
    return store.update_job(job["id"], status="completed", completed_at=NOW)


@pytest.mark.parametrize("command", ["/admin", "/queue", "/users", "/storage", "/allow 8", "/ban 3", "/purge 123", "/job 123", "/pause", "/resume", "/stats 3"])
def test_nonadmin_cannot_inspect_or_mutate(admin, store, command):
    update = message(command, ALICE, chat_id=ADMIN)
    update["forward_origin"] = {"sender_user": {"id": ADMIN}}
    admin.handle_message(update)
    assert not outgoing(store)
    assert not store.paused()
    assert store.authorized(BOB)
    assert store.get_user(8) is None


def test_group_commands_and_callbacks_are_silent(admin, store):
    assert admin.handle_message(message("/pause", chat_type="group"))
    data = store.issue_token(ADMIN, "admin_view", "pause")
    assert admin.handle_callback(callback(data, chat_type="supergroup"))
    assert not store.paused()
    assert not outgoing(store)
    assert store.db.execute("SELECT consumed_at FROM durable_tokens WHERE token=?", (data,)).fetchone()[0] is None


def test_foreign_stale_and_role_revoked_tokens_are_silent(admin, store):
    data = store.issue_token(ADMIN, "admin_view", "pause")
    assert admin.handle_callback(callback(data, OTHER_ADMIN))
    assert not store.paused()
    assert not outgoing(store)
    assert admin.handle_callback(callback(data))
    count = len(outgoing(store))
    assert store.paused()
    assert admin.handle_callback(callback(data))
    assert len(outgoing(store)) == count
    nonadmin_token = store.issue_token(ALICE, "admin_view", "resume")
    assert admin.handle_callback(callback(nonadmin_token, ALICE))
    assert len(outgoing(store)) == count
    assert store.paused()


def test_allow_unseen_user_initial_language_and_ban_confirmation(admin, store):
    assert admin.handle_message(message("/allow 7 A label"))
    assert store.authorized(7)
    assert not store.get_user(7)["language_initialized"]
    store.upsert_user(7, language="ru")
    assert store.get_user(7)["language"] == "ru"
    assert admin.handle_message(message("/ban 7"))
    assert store.authorized(7)
    data = token(store, "admin_confirm", "ban")
    assert admin.handle_callback(callback(data, OTHER_ADMIN))
    assert store.authorized(7)
    assert admin.handle_callback(callback(data))
    assert not store.authorized(7)
    assert admin.handle_message(message("/allow 7"))
    assert store.authorized(7)
    assert store.get_user(7)["language"] == "ru"


@pytest.mark.parametrize("target", [ADMIN, OTHER_ADMIN])
def test_configured_administrator_cannot_be_banned(admin, store, target):
    admin.handle_message(message(f"/ban {target}"))
    assert store.authorized(target)
    assert store.db.execute("SELECT COUNT(*) FROM durable_tokens WHERE action='admin_confirm'").fetchone()[0] == 0
    assert outgoing(store, ADMIN)


@pytest.mark.parametrize("argument", ["@username", "-3", "0", "9223372036854775808", "3 extra"])
def test_ban_needs_numeric_id_without_resolution(admin, store, argument):
    admin.handle_message(message("/ban " + argument))
    assert store.authorized(BOB)
    assert store.db.execute("SELECT COUNT(*) FROM durable_tokens").fetchone()[0] == 0


def test_ban_notifies_each_queued_job_once_with_source_and_reason(admin, store):
    first = enqueue(store, source=100)
    second = enqueue(store, at=NOW + timedelta(seconds=1), source=101)
    foreign = enqueue(store, BOB, at=NOW + timedelta(seconds=2), source=102)
    before_quota = store.quota(ALICE, NOW + timedelta(seconds=3))["used"]
    admin.handle_message(message(f"/ban {ALICE}"))
    data = token(store, "admin_confirm", "ban")
    admin.handle_callback(callback(data))
    assert not store.authorized(ALICE)
    assert store.get_job(foreign["id"])["status"] == "queued"
    notices = outgoing(store, ALICE, purpose="cancellation")
    assert len(notices) == 2
    assert {row["payload"]["kwargs"]["reply_parameters"]["message_id"] for row in notices} == {100, 101}
    for job, source in ((first, 100), (second, 101)):
        changed = store.get_job(job["id"])
        assert changed["status"] == "cancelled"
        assert changed["cancellation_actor_id"] == ADMIN
        assert changed["cancellation_reason"] == "administrator"
        matching = [row for row in notices if job["id"] in row["payload"]["kwargs"]["text"]]
        assert len(matching) == 1
        assert matching[0]["payload"]["kwargs"]["text"] == tr("ru", "admin_cancelled", target=job["id"], kind=tr("ru", "admin_job"))
        assert matching[0]["payload"]["kwargs"]["reply_parameters"]["allow_sending_without_reply"] is True
    assert store.quota(ALICE, NOW + timedelta(seconds=3), actor_id=ADMIN)["used"] == before_quota
    admin.handle_callback(callback(data))
    assert len(outgoing(store, ALICE, purpose="cancellation")) == 2
    assert not [row for row in store.pending_outbox(1000) if row["kind"] == "cancel_work"]


def test_active_job_ban_retains_binding_and_defers_notification_until_reap(admin, store):
    job = enqueue(store)
    store.claim_next(NOW)
    store.update_job(job["id"], status="running")
    store.bind_process(job["id"], {"host_pid": 111, "host_pgid": 111, "start_time": 1})
    admin.handle_message(message(f"/ban {ALICE}"))
    admin.handle_callback(callback(token(store, "admin_confirm", "ban")))
    assert store.get_job(job["id"])["status"] == "cancel_requested"
    assert store.process_bindings()[0]["work_uuid"] == job["id"]
    intents = [row for row in store.pending_outbox(1000) if row["kind"] == "cancel_work"]
    assert len(intents) == 1
    assert intents[0]["payload"] == {"work_uuid": job["id"], "owner_id": ALICE, "actor_id": ADMIN, "reason": "administrator"}
    assert not outgoing(store, ALICE)


def test_preparing_draft_ban_retains_tree_until_reap(admin, store):
    draft = store.create_draft(ALICE, source_message_id=123)
    store.update_draft(ALICE, draft["id"], status="preparing")
    store.bind_process(draft["id"], {"host_pid": 111, "host_pgid": 111, "start_time": 1})
    admin.handle_message(message(f"/ban {ALICE}"))
    admin.handle_callback(callback(token(store, "admin_confirm", "ban")))
    changed = store.get_draft(draft["id"])
    assert changed["status"] == "preparing"
    assert changed["close_requested"]
    assert changed["cancellation_reason"] == "administrator"
    assert store.process_bindings()[0]["work_uuid"] == draft["id"]
    assert not outgoing(store, ALICE)
    assert any(row["kind"] == "cancel_work" and row["payload"]["work_uuid"] == draft["id"] for row in store.pending_outbox(1000))


def test_confirmed_cancel_does_not_change_completed_race(admin, store):
    job = enqueue(store)
    admin.handle_message(message("/job " + job["id"]))
    admin.handle_callback(callback(token(store, "admin_prompt", "cancel")))
    data = token(store, "admin_confirm", "cancel")
    complete(store, job)
    admin.handle_callback(callback(data))
    assert store.get_job(job["id"])["status"] == "completed"
    assert not outgoing(store, ALICE)
    assert not [row for row in store.pending_outbox(1000) if row["kind"] == "cancel_work"]


def test_purge_requires_confirmation_and_retains_metadata(admin, store):
    draft = ready(store)
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    admin.handle_message(message("/purge " + draft["id"]))
    assert tree.exists()
    data = token(store, "admin_confirm", "purge")
    admin.handle_callback(callback(data, OTHER_ADMIN))
    assert tree.exists()
    admin.handle_callback(callback(data))
    assert not tree.exists()
    assert store.get_draft(draft["id"])["files_deleted"]
    assert len(store.files(draft["id"])) == 1


def test_purging_reused_child_keeps_original_and_dependent_history(admin, store):
    original = complete(store, enqueue(store))
    child = enqueue(store, BOB)
    assert store.claim_next(NOW)["id"] == child["id"]
    store.await_reuse(child["id"], original["id"], NOW)
    store.confirm_late_reuse(BOB, child["id"], "reuse", NOW)
    original_tree = store.settings.storage_dir / "jobs" / original["id"]
    admin.handle_message(message("/purge " + child["id"]))
    admin.handle_callback(callback(token(store, "admin_confirm", "purge")))
    assert original_tree.exists()
    assert not store.get_job(original["id"])["files_deleted"]
    assert store.deliveries(BOB)[0]["source_job_id"] == original["id"]
    assert store.find_reuse("same")["id"] == original["id"]
    admin.handle_message(message("/purge " + original["id"]))
    assert outgoing(store, ADMIN)[-1]["payload"]["kwargs"]["text"] == tr("en", "admin_confirm_purge", target=original["id"], count=1)
    admin.handle_callback(callback(token(store, "admin_confirm", "purge")))
    assert store.deliveries(BOB)[0]["files_deleted"]
    assert store.find_reuse("same") is None
    assert store.get_job(original["id"])["quota_counted"]


def test_purge_rechecks_active_state_on_confirmation(admin, store):
    draft = ready(store)
    admin.handle_message(message("/purge " + draft["id"]))
    data = token(store, "admin_confirm", "purge")
    store.enqueue(ALICE, draft["id"], NOW, force_new=False)
    admin.handle_callback(callback(data))
    assert (store.settings.storage_dir / "jobs" / draft["id"]).exists()
    assert not store.get_draft(draft["id"])["files_deleted"]


def test_pause_persists_and_resume_preserves_fifo(admin, store):
    first = enqueue(store, at=NOW)
    second = enqueue(store, BOB, at=NOW + timedelta(seconds=1))
    admin.handle_message(message("/pause"))
    assert store.claim_next(NOW) is None
    with Store(store.settings) as reopened:
        assert reopened.paused()
        Admin(reopened, reopened.settings, CORE).handle_message(message("/resume"))
        assert reopened.claim_next(NOW)["id"] == first["id"]
        assert reopened.get_job(second["id"])["status"] == "queued"


def test_queue_is_paginated_in_fifo_order_and_contains_all_users(admin, store):
    jobs = [enqueue(store, ADMIN if index % 2 else OTHER_ADMIN, at=NOW + timedelta(seconds=index)) for index in range(12)]
    admin.handle_message(message("/queue"))
    page = outgoing(store, ADMIN)[-1]["payload"]["kwargs"]
    text = page["text"]
    assert all(job["id"] in text for job in jobs[:10])
    assert all(job["id"] not in text for job in jobs[10:])
    assert text.index(jobs[0]["id"]) < text.index(jobs[9]["id"])
    data = next(button["callback_data"] for row in page["reply_markup"]["inline_keyboard"] for button in row if button["text"] == tr("en", "admin_next"))
    admin.handle_callback(callback(data))
    text = outgoing(store, ADMIN)[-1]["payload"]["kwargs"]["text"]
    assert all(job["id"] in text for job in jobs[10:])
    assert all(job["id"] not in text for job in jobs[:10])


def test_storage_includes_closed_unused_draft_and_keeps_native_cache(admin, store):
    draft = ready(store)
    store.close_draft(ALICE, draft["id"])
    native = store.settings.storage_dir / "telegram-api-files"
    native.mkdir()
    opaque = native / "opaque-cache"
    opaque.write_bytes(b"opaque")
    admin.handle_message(message("/storage"))
    output = "\n".join(row["payload"]["kwargs"]["text"] for row in outgoing(store, ADMIN))
    assert draft["id"] in output
    assert tr("en", "admin_native_notice") in output
    admin.handle_message(message("/purge " + draft["id"]))
    admin.handle_callback(callback(token(store, "admin_confirm", "purge")))
    assert opaque.read_bytes() == b"opaque"
    assert store.get_draft(draft["id"])["files_deleted"]


def test_admin_can_read_banned_history_with_unknown_resources_and_estimated_cost(admin, store):
    job = complete(store, enqueue(store))
    store.record_api_call({"job_id": job["id"], "run_id": "run", "request_id": "attempt", "timestamp_utc": NOW.isoformat(), "model": "local-model"}, {"run_id": "run", "request_id": "attempt", "timestamp_utc": NOW.isoformat(), "usage": {"prompt_tokens": 10, "completion_tokens": 2}}, {"amount": "0.001", "currency": "USD", "complete": False})
    store.ban_user(ADMIN, ALICE, NOW)
    admin.handle_message(message(f"/stats {ALICE}"))
    text = "\n".join(row["payload"]["kwargs"]["text"] for row in outgoing(store, ADMIN))
    assert "0.001 USD" in text
    assert tr("en", "admin_unknown") in text
    assert not outgoing(store, ALICE)


def test_update_transaction_deduplicates_administration_action(admin, store):
    update = message("/allow 8")
    assert store.process_update(700, lambda: admin.handle_message(update))
    assert not store.process_update(700, lambda: admin.handle_message(update))
    assert len(outgoing(store, ADMIN)) == 1
    assert store.offset() == 701


def test_overview_shows_installed_full_commit_and_user_buttons_change_access(admin, store):
    admin.handle_message(message("/admin"))
    overview = outgoing(store, ADMIN)[-1]["payload"]["kwargs"]
    assert CORE["version_info"]["commit"] in overview["text"]
    assert CORE["version_info"]["version"] in overview["text"]
    store.ban_user(ADMIN, BOB, NOW)
    admin.handle_message(message("/users"))
    allow_token = token(store, "admin_allow", str(BOB))
    admin.handle_callback(callback(allow_token))
    assert store.authorized(BOB)
    admin.handle_message(message("/users"))
    ban_token = token(store, "admin_prompt", "ban")
    admin.handle_callback(callback(ban_token))
    assert store.authorized(BOB)
    admin.handle_callback(callback(token(store, "admin_confirm", "ban")))
    assert not store.authorized(BOB)


def test_pause_does_not_cancel_or_signal_current_work(admin, store):
    job = enqueue(store)
    store.claim_next(NOW)
    store.update_job(job["id"], status="running")
    store.bind_process(job["id"], {"host_pid": 111, "host_pgid": 111, "start_time": 1})
    admin.handle_message(message("/pause"))
    assert store.paused()
    assert store.get_job(job["id"])["status"] == "running"
    assert store.process_bindings()[0]["work_uuid"] == job["id"]
    assert not [row for row in store.pending_outbox(1000) if row["kind"] == "cancel_work"]


def test_dialog_administration_menu_token_requires_configured_admin(admin, store):
    data = store.issue_token(ADMIN, "admin_menu", "user")
    assert admin.handle_callback(callback(data))
    assert CORE["version_info"]["commit"] in outgoing(store, ADMIN)[-1]["payload"]["kwargs"]["text"]
    count = len(outgoing(store))
    other = store.issue_token(ALICE, "admin_menu", "user")
    assert admin.handle_callback(callback(other, ALICE))
    assert len(outgoing(store)) == count
