from datetime import datetime, timezone

from aiogram.types import Update
import pytest

from absorbobot.admin import Admin
from absorbobot.bot import UpdateRouter
from absorbobot.config import Settings
from absorbobot.dialog import Dialog
from absorbobot.store import Store, StoreError


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
