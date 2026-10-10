from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from absorbobot.config import Settings
from absorbobot.dialog import Dialog
from absorbobot.i18n import tr
from absorbobot.store import Store
import absorbobot.dialog as dialog_module
import absorbobot.store as store_module


NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
ADMIN, ALICE, BOB = 1, 2, 3


@pytest.fixture
def core():
    models = {}
    for category in ('stt', 'vision', 'llm'):
        name = category + '-local'
        models[category] = {name: {'spec': {'name': name, 'backend': 'openai', 'api_model': name, 'whisper_model': '', 'model': name, 'provider': 'local', 'default': True}, 'provider': {'name': 'local', 'base_url': 'http://127.0.0.1:9999/v1', 'api_key_env': ''}, 'local': True}}
    return {'models': models, 'defaults': {'stt': 'stt-local', 'vision': 'vision-local', 'llm': 'llm-local', 'language': 'auto', 'fps': 1.0, 'hash_threshold': 8, 'min_scene_seconds': 2.0, 'slides_dpi': 150}, 'version_info': {'version': '0.1.2', 'commit': 'a' * 40, 'dirty': False}, 'media_extensions': ['.m4a', '.mp4'], 'material_extensions': ['.pdf', '.jpg'], 'installed_editable': False, 'runtime': {}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    settings = Settings(admin_ids=(ADMIN,), state_dir=tmp_path / 'state', storage_dir=tmp_path / 'mount' / 'storage', backing_mount=tmp_path / 'mount', min_free_bytes=1, quota_jobs=1)
    monkeypatch.setattr(dialog_module, 'now_utc', lambda: NOW)
    monkeypatch.setattr(store_module, 'utc_now', lambda: NOW)
    with Store(settings) as value:
        monkeypatch.setattr(dialog_module, 'now_utc', lambda: NOW + timedelta(seconds=len(value.jobs(limit=1000000))))
        for owner in (ALICE, BOB):
            value.allow_user(ADMIN, owner)
            value.upsert_user(owner, language='en')
        value.upsert_user(ADMIN, language='en')
        yield value


@pytest.fixture
def dialog(store, core):
    return Dialog(store, store.settings, core)


def message(owner=ALICE, text=None, message_id=100, **fields):
    result = {'from': {'id': owner, 'language_code': 'en'}, 'chat': {'id': owner, 'type': 'private'}, 'message_id': message_id, **fields}
    if text is not None:
        result['text'] = text
    return result


def callback(token, owner=ALICE):
    return {'id': 'callback-1', 'from': {'id': owner}, 'message': message(owner), 'data': token}


def outbox(store, kind=None):
    return [row for row in store.pending_outbox(limit=10000) if kind is None or row['kind'] == kind]


def texts(store):
    return '\n'.join(row['payload']['kwargs'].get('text', '') for row in outbox(store, 'telegram'))


def tokens(store, action):
    result = []
    for row in outbox(store, 'telegram'):
        keyboard = row['payload']['kwargs'].get('reply_markup', {}).get('inline_keyboard', [])
        for line in keyboard:
            for button in line:
                if button['callback_data'].partition(':')[0] == action:
                    result.append(button['callback_data'])
    return result


def click(dialog, store, action, owner=ALICE, target='user', payload=None):
    token = store.issue_token(owner, action, target, payload=payload)
    assert dialog.handle_callback(callback(token, owner))
    return token


def imported(dialog, store, owner=ALICE, sha='b' * 64):
    dialog.handle_message(message(owner, '/new'))
    dialog.handle_message(message(owner, audio={'file_id': 'f1', 'file_unique_id': 'u1', 'file_size': 3, 'file_name': 'recording.m4a'}))
    draft = store.active_draft(owner)
    tree = store.settings.storage_dir / 'jobs' / draft['id'] / 'input'
    tree.mkdir(parents=True)
    (tree / 'recording.m4a').write_bytes(b'abc')
    store.add_file(owner, draft['id'], role='media', original_name='recording.m4a', relative_path='input/recording.m4a', sha256=sha, bytes=3, duration=3, source_message_id=100)
    # Готовый импорт — долговечное состояние, которое оставляет настоящий intake.
    state = dict(store.get_draft(draft['id'])['ui_state'])
    state['pending_import_ids'] = []
    store.update_draft(owner, draft['id'], ui_state=state)
    return store.get_draft(draft['id'])


def accepted(dialog, store, owner=ALICE, sha='b' * 64):
    draft = imported(dialog, store, owner, sha)
    click(dialog, store, 'summary', owner, draft['id'])
    assert dialog.handle_callback(callback(tokens(store, 'submit')[-1], owner))
    return store.get_job(draft['id'])


def completed(store, job):
    assert store.claim_next(NOW)['id'] == job['id']
    store.update_job(job['id'], status='running')
    output = store.settings.storage_dir / 'jobs' / job['id'] / 'output'
    output.mkdir()
    for name in ('notes.md', 'notes.html', 'notes.pdf', 'notes.zip'):
        (output / name).write_bytes(b'actual result fixture')
    return store.update_job(job['id'], status='completed', completed_at=NOW)


def test_unauthorized_group_and_foreign_tokens_have_no_side_effects(dialog, store):
    before = len(outbox(store))
    assert not dialog.handle_message(message(999, '/start'))
    foreign = store.issue_token(ALICE, 'lang', 'user', payload={'language': 'ru'})
    assert not dialog.handle_callback(callback(foreign, BOB))
    assert not dialog.handle_callback(callback(foreign, 999))
    group = message(ALICE, '/start')
    group['chat'] = {'id': -123, 'type': 'group'}
    assert not dialog.handle_message(group)
    assert store.get_user(ALICE)['language'] == 'en'
    assert len(outbox(store)) == before
    assert store.get_user(999) is None
    assert dialog.handle_callback(callback(foreign))
    assert store.get_user(ALICE)['language'] == 'ru'


def test_revoked_owner_cannot_use_previously_issued_token(dialog, store):
    token = store.issue_token(ALICE, 'nav', 'user', payload={'command': 'new'})
    store.ban_user(ADMIN, ALICE, NOW)
    before = len(outbox(store))
    assert not dialog.handle_callback(callback(token))
    assert len(outbox(store)) == before
    assert store.db.execute('SELECT consumed_at FROM durable_tokens WHERE token=?', (token,)).fetchone()[0] is None


def test_language_override_reset_and_accepted_snapshot_are_independent(dialog, store, core):
    draft = imported(dialog, store)
    click(dialog, store, 'lang', payload={'language': 'ru'})
    click(dialog, store, 'summary', target=draft['id'])
    assert store.get_draft(draft['id'])['snapshot']['settings']['output_language'] == 'ru'
    click(dialog, store, 'set', target=draft['id'], payload={'key': 'output_language', 'value': 'en'})
    click(dialog, store, 'lang', payload={'language': 'en'})
    click(dialog, store, 'lang', payload={'language': 'ru'})
    click(dialog, store, 'summary', target=draft['id'])
    assert store.get_draft(draft['id'])['snapshot']['settings']['output_language'] == 'en'
    click(dialog, store, 'reset', target=draft['id'])
    changed = deepcopy(core)
    changed['defaults']['fps'] = 2.5
    dialog.refresh_core(changed)
    click(dialog, store, 'summary', target=draft['id'])
    assert store.get_draft(draft['id'])['snapshot']['settings']['fps'] == 2.5
    submit = tokens(store, 'submit')[-1]
    dialog.handle_callback(callback(submit))
    snapshot = store.get_job(draft['id'])['snapshot']
    assert snapshot['settings']['output_language'] == 'ru'
    click(dialog, store, 'lang', payload={'language': 'en'})
    click(dialog, store, 'number', payload={'key': 'fps'})
    dialog.handle_message(message(text='4.0'))
    assert store.get_job(draft['id'])['snapshot'] == snapshot
    assert snapshot['source_message_id'] == 100
    assert snapshot['source_chat_id'] == ALICE


@pytest.mark.parametrize('key,value', [('fps', 'nan'), ('fps', 'inf'), ('fps', '-inf'), ('fps', '0'), ('fps', '1e999'), ('hash_threshold', '65'), ('hash_threshold', '-1'), ('hash_threshold', '1.5'), ('min_scene_seconds', '-0.01'), ('slides_dpi', '0'), ('slides_dpi', 'inf'), ('fps', '--output-dir=/etc')])
def test_invalid_numeric_input_never_changes_selected_parameters(dialog, store, key, value):
    draft = imported(dialog, store)
    before = store.get_draft(draft['id'])['settings']
    click(dialog, store, 'number', target=draft['id'], payload={'key': key})
    assert dialog.handle_message(message(text=value))
    assert store.get_draft(draft['id'])['settings'] == before
    assert 'error' not in store.get_draft(draft['id'])['settings']
    assert tr('en', 'error.invalid_number') in texts(store)


def test_unbounded_finite_numeric_input_is_not_clamped(dialog, store):
    draft = imported(dialog, store)
    click(dialog, store, 'number', target=draft['id'], payload={'key': 'fps'})
    dialog.handle_message(message(text='1000000'))
    assert store.get_draft(draft['id'])['settings']['fps'] == 1000000.0
    click(dialog, store, 'number', target=draft['id'], payload={'key': 'min_scene_seconds'})
    dialog.handle_message(message(text='0'))
    assert store.get_draft(draft['id'])['settings']['min_scene_seconds'] == 0


def test_missing_model_is_not_silently_replaced(dialog, store, core):
    draft = imported(dialog, store)
    click(dialog, store, 'set', target=draft['id'], payload={'key': 'llm', 'value': 'llm-local'})
    changed = deepcopy(core)
    changed['models']['llm'] = {'replacement': {**core['models']['llm']['llm-local'], 'spec': {**core['models']['llm']['llm-local']['spec'], 'name': 'replacement'}}}
    changed['defaults']['llm'] = 'replacement'
    dialog.refresh_core(changed)
    click(dialog, store, 'summary', target=draft['id'])
    assert store.get_draft(draft['id'])['settings']['llm'] == 'llm-local'
    assert not store.jobs(ALICE)
    assert 'no longer available' in texts(store)


def test_double_confirmation_accepts_at_most_one_job(dialog, store):
    draft = imported(dialog, store)
    click(dialog, store, 'summary', target=draft['id'])
    first = tokens(store, 'submit')[-1]
    click(dialog, store, 'summary', target=draft['id'])
    second = tokens(store, 'submit')[-1]
    assert dialog.handle_callback(callback(first))
    assert not dialog.handle_callback(callback(first))
    assert dialog.handle_callback(callback(second))
    assert len(store.jobs(ALICE)) == 1
    assert store.quota(ALICE, NOW)['used'] == 1


def test_summary_staleness_rejects_changed_models_and_core(dialog, store, core):
    draft = imported(dialog, store)
    click(dialog, store, 'summary', target=draft['id'])
    token = tokens(store, 'submit')[-1]
    changed = deepcopy(core)
    changed['models']['llm']['llm-local']['provider']['base_url'] = 'http://127.0.0.1:9998/v1'
    changed['version_info']['commit'] = 'c' * 40
    dialog.refresh_core(changed)
    dialog.handle_callback(callback(token))
    assert store.get_job(draft['id']) is None
    assert tr('en', 'error.summary_changed') in texts(store)


def test_reuse_is_available_at_full_quota_without_job_or_source_identity(dialog, store):
    original = completed(store, accepted(dialog, store, BOB))
    accepted(dialog, store, ALICE, sha='d' * 64)
    draft = imported(dialog, store)
    click(dialog, store, 'summary', target=draft['id'])
    dialog.handle_callback(callback(tokens(store, 'submit')[-1]))
    assert store.get_job(draft['id']) is None
    reuse = tokens(store, 'reuse')[-1]
    dialog.handle_callback(callback(reuse))
    assert len(store.jobs(ALICE)) == 1
    assert store.quota(ALICE, NOW)['used'] == 1
    assert store.deliveries(ALICE)[0]['source_job_id'] == original['id']
    assert outbox(store, 'delivery')[-1]['payload']['work_uuid'] == draft['id']
    reuse_text = tr('en', 'dialog.reuse', old_version=original['engine_version'], old_commit=original['engine_commit'], version='0.1.2', commit='a' * 40)
    assert reuse_text in texts(store)
    assert original['id'] not in reuse_text
    assert 'Bob' not in texts(store)


def test_explicit_rerun_obeys_quota_and_does_not_modify_original(dialog, store):
    original = completed(store, accepted(dialog, store, BOB))
    accepted(dialog, store, ALICE, sha='d' * 64)
    draft = imported(dialog, store)
    click(dialog, store, 'summary', target=draft['id'])
    dialog.handle_callback(callback(tokens(store, 'submit')[-1]))
    dialog.handle_callback(callback(tokens(store, 'force_new')[-1]))
    assert store.get_job(draft['id']) is None
    assert store.get_job(original['id'])['status'] == 'completed'
    assert 'quota exhausted' in texts(store)
    assert store.quota(ALICE, NOW)['next_available_at'] in texts(store)


def test_late_reuse_releases_only_own_reserved_quota(dialog, store):
    original = accepted(dialog, store, BOB)
    job = accepted(dialog, store)
    completed(store, original)
    assert store.claim_next(NOW)['id'] == job['id']
    store.await_reuse(job['id'], original['id'], NOW)
    markup = dialog.late_reuse_buttons(ALICE, job['id'])
    token = markup['inline_keyboard'][0][0]['callback_data']
    assert not dialog.handle_callback(callback(token, BOB))
    assert dialog.handle_callback(callback(token))
    assert store.get_job(job['id'])['status'] == 'reused'
    assert store.quota(ALICE, NOW)['used'] == 0
    assert store.get_job(original['id'])['quota_counted'] == 1


def test_own_redelivery_and_statistics_do_not_expose_foreign_money(dialog, store):
    job = completed(store, accepted(dialog, store))
    store.update_job(job['id'], resources={'cpu_user_seconds': 12}, metrics={'processing_seconds': 30})
    store.record_api_call({'run_id': job['id'], 'request_id': 'one', 'timestamp_utc': NOW.isoformat(), 'job_id': job['id']}, {'run_id': job['id'], 'request_id': 'one', 'timestamp_utc': NOW.isoformat(), 'usage': {'prompt_tokens': 12, 'completion_tokens': 5}}, {'amount': '0.55', 'currency': 'USD', 'complete': True})
    dialog.handle_message(message(text='/stats'))
    statistics = texts(store)
    assert '0.55' not in statistics
    assert 'USD' not in statistics
    assert 'estimated_api_cost' not in statistics
    assert 'CPU user seconds' in statistics
    click(dialog, store, 'redeliver', owner=BOB, target=job['id'], payload={'kind': 'job'})
    assert not outbox(store, 'delivery')
    click(dialog, store, 'redeliver', target=job['id'], payload={'kind': 'job'})
    assert outbox(store, 'delivery')[-1]['payload']['owner_id'] == ALICE


def test_second_source_and_pending_material_cannot_be_confirmed(dialog, store):
    draft = imported(dialog, store)
    before = len(outbox(store, 'intake'))
    dialog.handle_message(message(video={'file_id': 'v2', 'file_size': 3}, message_id=101))
    assert len(outbox(store, 'intake')) == before
    assert store.active_draft(ALICE)['id'] == draft['id']
    dialog.handle_message(message(document={'file_id': 'pdf1', 'file_name': 'slides.pdf', 'file_size': 3}, message_id=102))
    assert len(outbox(store, 'intake')) == before + 1
    click(dialog, store, 'summary', target=draft['id'])
    assert not tokens(store, 'submit')
    assert not store.jobs(ALICE)


def test_youtube_preparation_waits_for_explicit_mode_and_keeps_minimal_source(dialog, store):
    dialog.handle_message(message(text='/new'))
    draft = store.active_draft(ALICE)
    dialog.handle_message(message(text='https://youtu.be/abcdefghijk', caption='private caption', forward_origin={'sender_user': {'id': BOB}}))
    assert not outbox(store, 'intake')
    click(dialog, store, 'youtube', target=draft['id'], payload={'mode': 'audio'})
    payload = outbox(store, 'intake')[-1]['payload']
    assert payload['message']['youtube_mode'] == 'audio'
    assert 'caption' not in payload['message']
    assert 'forward_origin' not in payload['message']
    assert payload['message']['chat']['id'] == ALICE


def test_cancel_affects_only_confirmed_own_set_and_preserves_binding(dialog, store):
    own = accepted(dialog, store)
    foreign = accepted(dialog, store, BOB, sha='c' * 64)
    store.claim_next(NOW)
    store.bind_process(own['id'], {'pid': 100000, 'pgid': 100000, 'start_time': 123, 'launcher_pid': 99999})
    dialog.handle_message(message(text='/cancel'))
    dialog.handle_callback(callback(tokens(store, 'cancel_confirm')[-1]))
    assert store.get_job(own['id'])['status'] == 'cancel_requested'
    assert store.get_job(foreign['id'])['status'] == 'queued'
    assert store.process_bindings()[0]['work_uuid'] == own['id']
    assert outbox(store, 'cancel_work')[-1]['payload']['work_uuid'] == own['id']
    assert tr('en', 'dialog.cancelled', id=own['id']) not in texts(store)


def test_admin_commands_are_left_to_admin_handler(dialog, store):
    before = len(outbox(store))
    assert not dialog.handle_message(message(ADMIN, '/queue'))
    assert not dialog.handle_message(message(ADMIN, '/stats 2'))
    assert len(outbox(store)) == before


def test_replacement_requires_confirmation_and_uses_fresh_uuid(dialog, store):
    draft = imported(dialog, store)
    dialog.handle_message(message(text='/new'))
    assert store.active_draft(ALICE)['id'] == draft['id']
    dialog.handle_callback(callback(tokens(store, 'new_confirm')[-1]))
    fresh = store.active_draft(ALICE)
    assert fresh['id'] != draft['id']
    assert store.get_draft(draft['id'])['status'] == 'closed'
    assert store.files(draft['id'])
    assert not store.files(fresh['id'])
    assert (store.settings.storage_dir / 'jobs' / draft['id'] / 'input' / 'recording.m4a').exists()


def test_queued_cancellation_has_individual_source_reply(dialog, store):
    job = accepted(dialog, store)
    dialog.handle_message(message(text='/cancel'))
    dialog.handle_callback(callback(tokens(store, 'cancel_confirm')[-1]))
    assert store.get_job(job['id'])['status'] == 'cancelled'
    notices = [row['payload'] for row in outbox(store, 'telegram') if row['payload'].get('purpose') == 'cancellation']
    assert len(notices) == 1
    assert notices[0]['kwargs']['reply_parameters'] == {'message_id': 100, 'allow_sending_without_reply': True}
    assert job['id'] in notices[0]['kwargs']['text']
    assert not outbox(store, 'cancel_work')
    assert store.quota(ALICE, NOW)['used'] == 1


def test_preparing_draft_cancellation_preserves_binding_until_reap(dialog, store):
    draft = imported(dialog, store)
    store.update_draft(ALICE, draft['id'], status='preparing')
    store.bind_process(draft['id'], {'pid': 100000, 'pgid': 100000, 'start_time': 123})
    dialog.handle_message(message(text='/cancel'))
    dialog.handle_callback(callback(tokens(store, 'cancel_confirm')[-1]))
    current = store.get_draft(draft['id'])
    assert current['status'] == 'preparing'
    assert current['close_requested']
    assert store.process_bindings()[0]['work_uuid'] == draft['id']
    assert outbox(store, 'cancel_work')[-1]['payload']['work_uuid'] == draft['id']
    assert not store.jobs(ALICE)
    assert tr('en', 'dialog.cancelled', id=draft['id']) not in texts(store)


def test_fresh_explicit_rerun_has_new_uuid_and_keeps_original_output(dialog, store):
    original = completed(store, accepted(dialog, store, BOB))
    draft = imported(dialog, store)
    click(dialog, store, 'summary', target=draft['id'])
    dialog.handle_callback(callback(tokens(store, 'submit')[-1]))
    dialog.handle_callback(callback(tokens(store, 'force_new')[-1]))
    job = store.get_job(draft['id'])
    assert job['force_new']
    assert job['id'] != original['id']
    assert store.quota(ALICE, NOW)['used'] == 1
    assert not store.deliveries(ALICE)
    assert (store.settings.storage_dir / 'jobs' / original['id'] / 'output' / 'notes.pdf').read_bytes() == b'actual result fixture'


def test_retained_source_history_does_not_block_ready_summary(dialog, store):
    draft = imported(dialog, store)
    store.update_draft(ALICE, draft['id'], source_message_ids=[12, 55, 100])
    click(dialog, store, 'summary', target=draft['id'])
    assert tokens(store, 'submit')
    assert store.get_draft(draft['id'])['status'] == 'ready'


def test_rejected_source_can_be_replaced_without_implicit_second_source(dialog, store):
    dialog.handle_message(message(text='/new'))
    dialog.handle_message(message(audio={'file_id': 'bad', 'file_size': 3}))
    draft = store.active_draft(ALICE)
    state = dict(draft['ui_state'])
    state['pending_import_ids'] = []
    store.update_draft(ALICE, draft['id'], ui_state=state)
    before = len(outbox(store, 'intake'))
    dialog.handle_message(message(message_id=101, audio={'file_id': 'valid', 'file_size': 3}))
    assert len(outbox(store, 'intake')) == before + 1
    assert store.active_draft(ALICE)['source_message_id'] == 101
