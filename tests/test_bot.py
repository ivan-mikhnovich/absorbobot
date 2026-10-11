import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Update
import pytest

import absorbobot.bot as bot_module
import absorbobot.store as store_module
from absorbobot.admin import Admin
from absorbobot.bot import TelegramOutbox, UpdateRouter
from absorbobot.config import Settings
from absorbobot.engine import Engine
from absorbobot.i18n import tr
from absorbobot.dialog import Dialog
from absorbobot.store import Store, StoreError


NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(admin_ids=(1,), state_dir=tmp_path / "state",
                        storage_dir=tmp_path / "mount" / "data",
                        backing_mount=tmp_path / "mount", min_free_bytes=1)
    core = {"version_info": {"version": "0.1.2", "commit": "a" * 40,
                             "dirty": False}, "models": {"stt": {}, "vision": {}, "llm": {}},
            "defaults": {}, "media_extensions": [".wav"],
            "material_extensions": [".pdf"]}
    with Store(settings) as store:
        yield store, UpdateRouter(store, Dialog(store, settings, core),
                                  Admin(store, settings, core))


def update(id, sender, text="/start", *, callback=None, group=False):
    message = {"message_id": id, "date": int(datetime.now(timezone.utc).timestamp()),
               "from": {"id": sender, "is_bot": False, "first_name": "User",
                        "language_code": "ru-RU"},
               "chat": {"id": sender, "type": "group" if group else "private"},
               "text": text}
    if callback:
        return Update.model_validate({"update_id": id, "callback_query": {
            "id": str(id), "from": message["from"], "message": message,
            "chat_instance": "instance", "data": callback}})
    return Update.model_validate({"update_id": id, "message": message})


def test_unapproved_group_and_forwarded_identity_are_silent(runtime):
    store, router = runtime
    unauthorized = update(1, 2)
    data = unauthorized.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["message"]["forward_origin"] = {
        "type": "user", "date": data["message"]["date"],
        "sender_user": {"id": 1, "is_bot": False, "first_name": "Admin"},
    }
    unauthorized = Update.model_validate(data)
    assert router.handle(unauthorized)
    assert router.handle(update(2, 1, group=True))
    assert store.offset() == 3
    assert not store.pending_outbox()
    assert store.get_user(2) is None
    assert not store.jobs()


def test_update_deduplication_keeps_first_language_and_initial_reply(runtime):
    store, router = runtime
    store.allow_user(1, 2)
    incoming = update(1, 2)
    assert router.handle(incoming)
    assert not router.handle(incoming)
    actions = store.pending_outbox()
    assert store.get_user(2)["language"] == "ru"
    assert sum(row["payload"]["method"] == "send_photo" for row in actions) == 1
    assert store.offset() == 2


def test_authorized_foreign_callback_is_acknowledged_without_consuming_token(runtime):
    store, router = runtime
    store.allow_user(1, 2)
    token = store.issue_token(1, "admin_view", "overview")
    assert router.handle(update(1, 2, callback=token))
    actions = store.pending_outbox()
    assert len(actions) == 1
    assert actions[0]["payload"]["method"] == "answer_callback_query"
    assert store.consume_token(1, token)["action"] == "admin_view"
    store.ban_user(1, 2, datetime.now(timezone.utc))
    assert router.handle(update(2, 2, callback=token))
    assert len(store.pending_outbox()) == 1


def test_domain_failure_rolls_back_offset_user_and_callback_ack(runtime, monkeypatch):
    store, router = runtime
    def fail(message):
        store.create_draft(1)
        raise StoreError("unexpected_boundary")
    monkeypatch.setattr(router.admin, "handle_message", fail)
    with pytest.raises(StoreError):
        router.handle(update(10, 1, text="/new"))
    assert store.offset() == 0
    assert store.get_user(1) is None
    assert store.active_draft(1) is None
    assert not store.pending_outbox()


class FakeBot:
    """Записать вызовы Telegram и вернуть правдоподобные идентификаторы сообщений."""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.next_message_id = 1000

    async def send_message(self, **kwargs):
        return self._record("send_message", kwargs)

    async def edit_message_text(self, **kwargs):
        return self._record("edit_message_text", kwargs)

    def _record(self, method, kwargs):
        if self.fail is not None:
            failure, self.fail = self.fail, None
            raise failure
        self.calls.append((method, kwargs))
        if kwargs.get("message_id"):
            return SimpleNamespace(message_id=kwargs["message_id"])
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)


@pytest.fixture
def clock(monkeypatch):
    """Управляемое время: продюсер, хранилище и outbox видят одни и те же часы."""
    state = {"now": NOW}
    monkeypatch.setattr(bot_module, "utc_now", lambda: state["now"])
    monkeypatch.setattr(store_module, "utc_now", lambda: state["now"])
    return state


def make_settings(tmp_path):
    return Settings(admin_ids=(1,), state_dir=tmp_path / "state",
                    storage_dir=tmp_path / "mount" / "data",
                    backing_mount=tmp_path / "mount", min_free_bytes=1)


def make_job(store, owner=2, signature="same"):
    draft = store.create_draft(owner, source_message_id=123,
                               settings={"language": "auto", "output_language": "en"},
                               now_utc=NOW)
    tree = store.settings.storage_dir / "jobs" / draft["id"]
    (tree / "input").mkdir(parents=True)
    (tree / "input" / "media.wav").write_bytes(b"fixture")
    store.add_file(owner, draft["id"], role="media", original_name="media.wav",
                   relative_path="input/media.wav", sha256="b" * 64, bytes=7,
                   duration=1, source_message_id=123)
    store.update_draft(owner, draft["id"], status="ready", input_signature=signature,
                       snapshot={"version_info": {"version": "0.1.2", "commit": "a" * 40,
                                                  "dirty": False},
                                 "models": {},
                                 "settings": {"language": "auto", "output_language": "en"}})
    return store.enqueue(owner, draft["id"], NOW, force_new=False)


def progress_row(store, job, text, *, method="send_message", owner=None, message_id=None):
    owner = job["owner_id"] if owner is None else owner
    kwargs = {"chat_id": owner, "text": text}
    if method == "send_message":
        kwargs["reply_parameters"] = {"message_id": job["source_message_id"],
                                      "allow_sending_without_reply": True}
    else:
        kwargs["message_id"] = message_id
    return store.add_outbox("telegram", {"owner_id": owner, "method": method,
                                         "purpose": "progress", "work_uuid": job["id"],
                                         "kwargs": kwargs})


def outbox_row(store, item_id):
    return dict(store.db.execute("SELECT * FROM outbox WHERE id=?", (item_id,)).fetchone())


def outbox_result(store, item_id):
    return json.loads(outbox_row(store, item_id)["result"])


def rewrite_text(store, item_id, text):
    """Изменить текст ещё не отправленного действия так же, как это делает продюсер."""
    payload = outbox_row(store, item_id)["payload"]
    payload = json.loads(payload)
    payload["kwargs"]["text"] = text
    store.db.execute("UPDATE outbox SET payload=? WHERE id=?",
                     (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                      item_id))


def dispatch(store, outbox, item_id):
    item = next(row for row in store.pending_outbox(100) if row["id"] == item_id)
    asyncio.run(outbox.send(item))


def test_progress_initial_send_reads_fresh_text_and_stays_singular(tmp_path, clock):
    settings = make_settings(tmp_path)
    with Store(settings) as store:
        store.allow_user(1, 2)
        job = make_job(store)
        root = progress_row(store, job, "Job X\nstage.one")
        snapshot = store.pending_outbox(100)[0]
        rewrite_text(store, root, "Job X\nstatus.completed")
        bot = FakeBot()
        outbox = TelegramOutbox(bot, store)
        asyncio.run(outbox.send(snapshot))
        assert [call[0] for call in bot.calls] == ["send_message"]
        assert bot.calls[0][1]["text"] == "Job X\nstatus.completed"
        assert bot.calls[0][1]["reply_parameters"]["message_id"] == job["source_message_id"]
        result = outbox_result(store, root)
        assert result["progress"] is True and result["message_id"]

        # Продюсер после перезапуска снова предлагает корневую отправку: второе сообщение не создаётся.
        duplicate = progress_row(store, job, "Job X\nstatus.failed")
        clock["now"] = NOW + timedelta(seconds=6)
        dispatch(store, outbox, duplicate)
        assert [call[0] for call in bot.calls] == ["send_message", "edit_message_text"]
        assert bot.calls[1][1]["message_id"] == result["message_id"]
        assert bot.calls[1][1]["text"] == "Job X\nstatus.failed"
        assert outbox_result(store, duplicate)["progress"] is True


def test_progress_backlog_coalesces_to_newest_edit(tmp_path, clock):
    settings = make_settings(tmp_path)
    with Store(settings) as store:
        store.allow_user(1, 2)
        job = make_job(store)
        root = progress_row(store, job, "Job X\nstatus.running")
        bot = FakeBot()
        outbox = TelegramOutbox(bot, store)
        dispatch(store, outbox, root)
        message_id = outbox_result(store, root)["message_id"]
        older = progress_row(store, job, "Job X\nstage.one", method="edit_message_text",
                             message_id=message_id)
        newer = progress_row(store, job, "Job X\nstage.two", method="edit_message_text",
                             message_id=message_id)
        clock["now"] = NOW + timedelta(seconds=6)
        for item in store.pending_outbox(100):
            asyncio.run(outbox.send(item))
        assert [call[0] for call in bot.calls] == ["send_message", "edit_message_text"]
        assert bot.calls[1][1]["text"] == "Job X\nstage.two"
        assert outbox_result(store, older) == {"skipped": "superseded", "superseded_by": newer}
        assert outbox_result(store, newer)["progress"] is True
        assert outbox_row(store, older)["status"] == "sent"


def test_progress_spacing_survives_restart_without_spending_attempts(tmp_path, clock):
    settings = make_settings(tmp_path)
    with Store(settings) as store:
        store.allow_user(1, 2)
        job = make_job(store)
        root = progress_row(store, job, "Job X\nstatus.running")
        bot = FakeBot()
        dispatch(store, TelegramOutbox(bot, store), root)
        message_id = outbox_result(store, root)["message_id"]
        edit = progress_row(store, job, "Job X\nstage.two", method="edit_message_text",
                            message_id=message_id)
        clock["now"] = NOW + timedelta(seconds=1)
        dispatch(store, TelegramOutbox(bot, store), edit)
        assert [call[0] for call in bot.calls] == ["send_message"]
        deferred = outbox_row(store, edit)
        assert deferred["status"] == "pending" and deferred["attempts"] == 0
        assert datetime.fromisoformat(deferred["available_at"]) == NOW + timedelta(seconds=5)
    clock["now"] = NOW + timedelta(seconds=4)
    with Store(settings) as restarted:
        assert not restarted.pending_outbox(100)
        clock["now"] = NOW + timedelta(seconds=5)
        assert [row["id"] for row in restarted.pending_outbox(100)] == [edit]
        dispatch(restarted, TelegramOutbox(bot, restarted), edit)
    assert [call[0] for call in bot.calls] == ["send_message", "edit_message_text"]
    assert bot.calls[1][1]["text"] == "Job X\nstage.two"


def test_progress_retry_after_is_never_shortened_by_spacing(tmp_path, clock):
    settings = make_settings(tmp_path)
    with Store(settings) as store:
        store.allow_user(1, 2)
        job = make_job(store)
        root = progress_row(store, job, "Job X\nstatus.running")
        bot = FakeBot()
        dispatch(store, TelegramOutbox(bot, store), root)
        message_id = outbox_result(store, root)["message_id"]
        edit = progress_row(store, job, "Job X\nstage.two", method="edit_message_text",
                            message_id=message_id)
        clock["now"] = NOW + timedelta(seconds=6)
        bot.fail = TelegramRetryAfter(SimpleNamespace(chat_id=2), "flood control", 30)
        dispatch(store, TelegramOutbox(bot, store), edit)
        assert [call[0] for call in bot.calls] == ["send_message"]
        deferred = outbox_row(store, edit)
        assert deferred["error_code"] == "telegram_rate_limit" and deferred["attempts"] == 1
        assert datetime.fromisoformat(deferred["available_at"]) == NOW + timedelta(seconds=36)
        clock["now"] = NOW + timedelta(seconds=35)
        assert not store.pending_outbox(100)
        clock["now"] = NOW + timedelta(seconds=36)
        dispatch(store, TelegramOutbox(bot, store), edit)
    assert [call[0] for call in bot.calls] == ["send_message", "edit_message_text"]


def test_progress_rejects_foreign_owner_and_revoked_access(tmp_path, clock):
    settings = make_settings(tmp_path)
    with Store(settings) as store:
        store.allow_user(1, 2)
        store.allow_user(1, 3)
        job = make_job(store, owner=2)
        bot = FakeBot()
        outbox = TelegramOutbox(bot, store)
        foreign = progress_row(store, job, "Job X\nstage.one", owner=3)
        dispatch(store, outbox, foreign)
        assert not bot.calls
        assert outbox_result(store, foreign) == {"skipped": "owner_mismatch"}

        store.ban_user(1, 2, NOW)
        revoked = progress_row(store, job, "Job X\nstage.two", owner=2)
        dispatch(store, outbox, revoked)
        assert not bot.calls
        assert outbox_result(store, revoked) == {"skipped": "access_revoked"}


def test_non_progress_notices_are_neither_gated_nor_coalesced(tmp_path, clock):
    settings = make_settings(tmp_path)
    with Store(settings) as store:
        store.allow_user(1, 2)
        job = make_job(store)
        root = progress_row(store, job, "Job X\nstatus.running")
        bot = FakeBot()
        outbox = TelegramOutbox(bot, store)
        dispatch(store, outbox, root)
        notices = [store.add_outbox("telegram", {"owner_id": 2, "method": "send_message",
                                                 "kwargs": {"chat_id": 2, "text": f"notice {index}"}})
                   for index in range(2)]
        for item in store.pending_outbox(100):
            asyncio.run(outbox.send(item))
        assert [call[0] for call in bot.calls] == ["send_message"] * 3
        assert [call[1]["text"] for call in bot.calls[1:]] == ["notice 0", "notice 1"]
        for item_id in notices:
            result = outbox_result(store, item_id)
            assert result.get("message_id") and "progress" not in result


def test_actual_engine_progress_before_first_send_delivers_latest_stage(tmp_path, clock):
    settings = make_settings(tmp_path)
    with Store(settings) as store:
        store.allow_user(1, 2)
        job = make_job(store)
        engine = Engine(settings, store, None, None, {})
        engine._progress_action(job, "status.running", final=True)
        snapshot = store.pending_outbox(100)[0]
        engine._progress_action(job, "stage.stt", final=True)
        engine._progress_action(job, "stage.render", final=True)
        bot = FakeBot()
        asyncio.run(TelegramOutbox(bot, store).send(snapshot))
        assert [call[0] for call in bot.calls] == ["send_message"]
        user = store.get_user(job["owner_id"])
        assert bot.calls[0][1]["text"] == tr(
            user["language"], "engine.progress", id=job["id"],
            stage=tr(user["language"], "stage.render"))
        assert not store.pending_outbox(100)
