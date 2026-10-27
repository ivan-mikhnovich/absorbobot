"""Долговечный пользовательский диалог без сетевых операций в обработчиках."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import PurePath

from .audio import (AudioError, from_snapshot, from_state, normalize_document,
                    speaker_name, state_fields)
from .config import ConfigError, reset_work_tree
from .i18n import tr
from .identity import work_title, work_preview
from .store import RETRYABLE_FAILURES, StoreError, QuotaError
from .workers.media import MediaError, resolve_media_url
from .workers.external import normalize_external_url


COMMANDS = ('start', 'new', 'jobs', 'settings', 'language', 'help', 'cancel', 'stats', 'version')
NUMBERS = ('fps', 'hash_threshold', 'min_scene_seconds', 'slides_dpi')
CATEGORIES = ('stt', 'vision', 'llm')
UNFINISHED = ['queued', 'preparing', 'running', 'awaiting_reuse_choice', 'cancel_requested']
ACTIONS = {'nav', 'lang', 'new_confirm', 'files_added', 'advanced', 'choose', 'set', 'number', 'reset', 'summary', 'submit', 'reuse', 'force_new', 'late_reuse', 'late_force', 'history', 'redeliver', 'cancel_confirm', 'media', 'materials', 'retry', 'audio_toggle', 'audio_speaker', 'audio_clear', 'audio_done'}


def now_utc():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class Dialog:
    def __init__(self, store, settings, core):
        self.store = store
        self.settings = settings
        self.refresh_core(core)

    def refresh_core(self, core: dict) -> None:
        self.core = deepcopy(core)

    @staticmethod
    def command_descriptions(language: str) -> list[dict]:
        return [{'command': name, 'description': tr(language, 'command.' + name)} for name in COMMANDS]

    def _owner(self, update, callback=False):
        sender = update.get('from') or {}
        message = (update.get('message') or {}) if callback else update
        owner = sender.get('id')
        chat = message.get('chat') or {}
        if type(owner) is not int or chat.get('type') != 'private' or chat.get('id') != owner or not self.store.authorized(owner):
            return None
        return owner

    def _language(self, owner):
        return self.store.get_user(owner)['language']

    def _telegram(self, owner, method, purpose=None, **kwargs):
        payload = {'method': method, 'kwargs': kwargs, 'owner_id': owner}
        if purpose is not None:
            payload['purpose'] = purpose
        self.store.add_outbox('telegram', payload)

    def _send(self, owner, text, rows=None, reply=None, purpose=None):
        # Длинная история и сводка не превышают лимит UTF-16 Telegram.
        chunks = [text[index:index + 1800] for index in range(0, len(text), 1800)] or ['']
        for index, chunk in enumerate(chunks):
            kwargs = {'chat_id': owner, 'text': chunk, 'parse_mode': None}
            if rows and index == len(chunks) - 1:
                kwargs['reply_markup'] = {'inline_keyboard': rows}
            if reply is not None:
                kwargs['reply_parameters'] = {'message_id': reply, 'allow_sending_without_reply': True}
            self._telegram(owner, 'send_message', purpose=purpose, **kwargs)

    def _button(self, owner, key, action, target='user', payload=None, literal=False):
        return {'text': key if literal else tr(self._language(owner), key), 'callback_data': self.store.issue_token(owner, action, target, payload=payload)}

    def _menu(self, owner):
        rows = [[self._button(owner, 'button.' + name, 'nav', payload={'command': name})] for name in ('new', 'jobs', 'settings', 'help', 'language')]
        if self.store.is_admin(owner):
            # Административный обработчик владеет своим пространством токенов.
            rows.append([{'text': tr(self._language(owner), 'button.admin'), 'callback_data': self.store.issue_token(owner, 'admin_menu', 'user')}])
        return rows

    def _defaults(self):
        return {key: self.core['defaults'][key] for key in ('language', *NUMBERS, *CATEGORIES) if key in self.core['defaults']}

    def _chosen(self, owner, draft=None):
        user = self.store.get_user(owner)
        chosen = self._defaults()
        explicit = draft['settings'] if draft else user['settings']
        chosen.update({key: value for key, value in explicit.items() if key in (*NUMBERS, *CATEGORIES, 'language', 'media_mode')})
        chosen.setdefault('language', 'auto')
        chosen.setdefault('media_mode', 'video')
        override = explicit.get('output_language_override', user.get('output_language_override'))
        chosen['output_language'] = override or user['language']
        return chosen

    def _target(self, owner, target):
        if target == 'user':
            return None
        draft = self.store.get_draft(target, owner)
        if not draft or draft['status'] not in ('collecting', 'preparing', 'ready') or draft['close_requested']:
            raise StoreError('draft_consumed')
        return draft

    @staticmethod
    def _short(value, limit: int = 32) -> str:
        """Сократить недоверенное название для текста кнопки."""
        return value if len(value) <= limit else value[:limit - 1] + '…'

    def _audio_document(self, draft):
        try:
            return from_state(draft['ui_state'])
        except AudioError:
            raise StoreError('draft_incomplete') from None

    def _audio_ready(self, draft) -> bool:
        """Несколько дорожек требуют явного подмножества, одна выбрана сама."""
        audio = self._audio_document(draft)
        return len(audio['tracks']) <= 1 or bool(audio['selected'])

    def _audio_values(self, document):
        """Проверить выбор дорожек перед сохранением в снимок."""
        try:
            return normalize_document(document, require_selection=True)
        except AudioError as exc:
            raise StoreError(exc.code) from None

    def _set_audio(self, owner, draft_id, document):
        """Сохранить выбор дорожек, отменив устаревшую сводку и подпись."""
        draft = self._target(owner, draft_id)
        try:
            values = normalize_document(document, require_selection=False)
        except AudioError as exc:
            raise StoreError(exc.code) from None
        state = dict(draft['ui_state'])
        state.update(state_fields(values))
        state.pop('audio_prompt', None)
        fields = {'ui_state': state, 'input_signature': None, 'snapshot': {}}
        if draft['status'] == 'ready':
            # Готовая сводка больше не соответствует выбору дорожек.
            fields['status'] = 'collecting'
        self.store.update_draft(owner, draft_id, **fields)
        return self.store.get_draft(draft_id, owner)

    def _audio_title(self, owner, track) -> str:
        return track['title'] or tr(self._language(owner), 'dialog.audio_untitled')

    def _audio_menu(self, owner, draft_id):
        """Показать дорожки записи: выбор подмножества и привязка человека."""
        draft = self._target(owner, draft_id)
        audio = self._audio_document(draft)
        lang = self._language(owner)
        selected = set(audio['selected'])
        lines = [tr(lang, 'dialog.audio_tracks', count=len(audio['tracks']))]
        rows = []
        for track in audio['tracks']:
            index = track['index']
            title = self._audio_title(owner, track)
            lines.append(tr(lang, 'dialog.audio_track', index=index, title=title,
                             track_language=track['language'] or tr(lang, 'unknown'),
                             channels=track['channels'],
                             default=tr(lang, 'dialog.audio_default_yes' if track['default']
                                        else 'dialog.audio_default_no')))
            mark = '✓' if index in selected else '☐'
            rows.append([self._button(owner, f'{mark} {index}: {self._short(title)}',
                                      'audio_toggle', draft_id, {'index': index}, literal=True)])
        for index in sorted(selected):
            speaker = audio['speakers'].get(index)
            label = tr(lang, 'button.audio_speaker')
            if speaker:
                label += ': ' + self._short(speaker)
            rows.append([self._button(owner, label, 'audio_speaker', draft_id,
                                      {'index': index}, literal=True)])
            if speaker:
                rows.append([self._button(owner, 'button.audio_clear', 'audio_clear',
                                          draft_id, {'index': index})])
        rows.append([self._button(owner, 'button.audio_done', 'audio_done', draft_id)])
        self._send(owner, '\n'.join(lines), rows)

    def _audio_prompt(self, owner, draft_id, index):
        """Запросить имя человека для выбранной дорожки в свободном тексте."""
        draft = self._target(owner, draft_id)
        audio = self._audio_document(draft)
        if index not in audio['selected']:
            raise StoreError('audio_selection_invalid')
        state = dict(draft['ui_state'])
        state['audio_prompt'] = index
        self.store.update_draft(owner, draft_id, ui_state=state)
        track = next(track for track in audio['tracks'] if track['index'] == index)
        self._send(owner, tr(self._language(owner), 'dialog.audio_speaker_prompt',
                             index=index, title=self._audio_title(owner, track)))

    def _set_speaker(self, owner, draft, name):
        """Привязать введённое имя к ожидающей дорожке."""
        state = dict(draft['ui_state'])
        index = state.get('audio_prompt')
        audio = self._audio_document(draft)
        if index not in audio['selected']:
            raise StoreError('audio_selection_invalid')
        try:
            value = speaker_name(name)
        except AudioError:
            raise StoreError('invalid_speaker') from None
        audio['speakers'][index] = value
        state.update(state_fields(self._audio_values(audio)))
        state.pop('audio_prompt', None)
        fields = {'ui_state': state, 'input_signature': None, 'snapshot': {}}
        if draft['status'] == 'ready':
            fields['status'] = 'collecting'
        self.store.update_draft(owner, draft['id'], **fields)
        self._send(owner, tr(self._language(owner), 'dialog.audio_speaker_set',
                             index=index, name=value))
        self._audio_menu(owner, draft['id'])

    def _persist(self, owner, draft, key, value):
        user = self.store.get_user(owner)
        explicit = dict(user['settings'])
        explicit.pop('_dialog', None)
        if key == 'output_language_override':
            self.store.update_user(owner, settings=explicit, output_language_override=value)
        else:
            explicit[key] = value
            self.store.update_user(owner, settings=explicit)
        if draft:
            selected = dict(draft['settings'])
            selected[key] = value
            state = dict(draft['ui_state'])
            state.pop('number', None)
            self.store.update_draft(owner, draft['id'], settings=selected, ui_state=state)

    def _models(self, chosen):
        models = {}
        for category in CATEGORIES:
            name = chosen.get(category)
            if name not in self.core['models'][category]:
                raise StoreError('model_missing', category=category, model=name)
            models[category] = {'name': name, **deepcopy(self.core['models'][category][name])}
        return models

    def _preview(self, owner, draft):
        relative = work_preview(draft)
        if relative:
            self.store.add_outbox('telegram', {
                'owner_id': owner, 'method': 'send_photo', 'work_uuid': draft['id'],
                'preview': relative, 'kwargs': {'chat_id': owner, 'caption': work_title(self.store, draft)},
            }, dedupe_key=f"preview:{draft['id']}:{relative}")

    def _main_settings(self, owner, draft=None, advanced=False):
        target = draft['id'] if draft else 'user'
        chosen = self._chosen(owner, draft)
        lang = self._language(owner)
        lines = [tr(lang, 'dialog.settings')]
        rows = []
        keys = (*CATEGORIES, *NUMBERS) if advanced else ('language', 'output_language')
        for key in keys:
            value = chosen.get(key)
            if key in ('language', 'output_language'):
                value = tr(lang, 'value.' + value)
            if key == 'output_language':
                override = (draft['settings'] if draft else {}).get('output_language_override', self.store.get_user(owner).get('output_language_override'))
                value = value if override else tr(lang, 'button.same_language') + ' (' + value + ')'
            label = tr(lang, 'setting.' + key)
            lines.append(f'{label}: {value}')
            rows.append([self._button(owner, f'{label}: {value}', 'number' if key in NUMBERS else 'choose', target, {'key': key}, literal=True)])
        if not advanced:
            rows.append([self._button(owner, 'button.advanced', 'advanced', target)])
        else:
            rows.append([self._button(owner, 'button.back', 'nav', target, {'command': 'settings'})])
        rows.append([self._button(owner, 'button.reset', 'reset', target)])
        if draft:
            rows.append([self._button(owner, 'button.review', 'summary', target), self._button(owner, 'button.materials', 'materials', target)])
        self._send(owner, '\n'.join(lines), rows)

    def _new(self, owner, confirmed=False, old_id=None):
        draft = self.store.active_draft(owner)
        if draft:
            if not confirmed:
                self._send(owner, tr(self._language(owner), 'dialog.existing_draft'), [[self._button(owner, 'button.replace', 'new_confirm', draft['id']), self._button(owner, 'button.back', 'nav', payload={'command': 'settings'})]])
                return
            if draft['id'] != old_id:
                raise StoreError('invalid_token')
            closed = self.store.close_draft(owner, draft['id'], reason='replaced')
            if closed['close_requested']:
                self._cancel_work(owner, closed['id'], 'replaced')
                self._send(owner, tr(self._language(owner), 'dialog.stopping_draft'))
                return
        user = self.store.get_user(owner)
        selected = {key: value for key, value in user['settings'].items() if key != '_dialog'}
        selected['output_language_override'] = user.get('output_language_override')
        self.store.create_draft(owner, settings=selected)
        self._send(owner, tr(self._language(owner), 'dialog.send_source'))

    def _source(self, message):
        result = {'message_id': message.get('message_id'), 'chat': {'id': message['chat']['id']}}
        if message.get('text'):
            result['text'] = message['text']
        for kind in ('document', 'video', 'audio', 'voice', 'video_note', 'photo'):
            if message.get(kind):
                fields = ('file_id', 'file_unique_id', 'file_size', 'file_name', 'mime_type', 'duration', 'width', 'height')
                objects = message[kind] if kind == 'photo' else [message[kind]]
                result[kind] = [{key: item[key] for key in fields if key in item} for item in objects] if kind == 'photo' else {key: objects[0][key] for key in fields if key in objects[0]}
        return result

    def _input(self, owner, message):
        draft = self.store.active_draft(owner)
        if not draft:
            self._send(owner, tr(self._language(owner), 'dialog.need_new'), self._menu(owner))
            return
        if draft['close_requested']:
            self._send(owner, tr(self._language(owner), 'dialog.preparing'))
            return
        platform, external = None, False
        if message.get('text'):
            try:
                platform, _, _ = resolve_media_url(message['text'])
            except MediaError:
                try:
                    normalize_external_url(message['text'])
                    external = True
                except ValueError:
                    raise StoreError('unsupported_input') from None
        state = dict(draft['ui_state'])
        source = self._source(message)
        pending = state.get('pending_import_ids', [])
        has_source = bool(state.get('media_pending') or (state.get('source') or {}).get('message_id') in pending or any(f['role'] == 'media' and not f['removed'] for f in self.store.files(draft['id'])))
        document = message.get('document') or {}
        extension = PurePath(document.get('file_name', '')).suffix.lower()
        material_exts = {str(ext).lower() if str(ext).startswith('.') else '.' + str(ext).lower() for ext in self.core['material_extensions']}
        is_material = bool(message.get('photo') or extension in material_exts)
        if has_source and not is_material and not external:
            self._send(owner, tr(self._language(owner), 'dialog.second_source'), [[self._button(owner, 'button.replace', 'new_confirm', draft['id']), self._button(owner, 'button.files_added', 'files_added', draft['id'])]])
            return
        if not has_source:
            if is_material:
                self._send(owner, tr(self._language(owner), 'dialog.source_first'))
                return
            state['source'] = source
            ids = list(draft['source_message_ids'])
            ids.append(message['message_id'])
            self.store.update_draft(owner, draft['id'], ui_state=state, source_message_id=message['message_id'], source_chat_id=owner, source_message_ids=ids)
            if platform is not None and len(platform.modes) > 1:
                state['media_pending'] = source
                self.store.update_draft(owner, draft['id'], ui_state=state)
                self._send(owner, tr(self._language(owner), 'dialog.media_mode'), [[self._button(owner, 'button.video', 'media', draft['id'], {'mode': 'video'}), self._button(owner, 'button.audio_only', 'media', draft['id'], {'mode': 'audio'})]])
                return
            if platform is not None:
                # Площадка отдаёт записи только одного вида: спрашивать нечего.
                source = {**source, 'media_mode': next(iter(platform.modes))}
        else:
            ids = list(draft['source_message_ids'])
            ids.append(message['message_id'])
            self.store.update_draft(owner, draft['id'], source_message_ids=ids, input_signature=None, snapshot={})
        self._intake(owner, draft['id'], source)

    def _intake(self, owner, draft_id, source):
        draft = self.store.get_draft(draft_id, owner)
        state = dict(draft['ui_state'])
        state['pending_import_ids'] = [*state.get('pending_import_ids', []), source['message_id']]
        self.store.update_draft(owner, draft_id, ui_state=state, input_signature=None, snapshot={})
        self.store.add_outbox('intake', {'owner_id': owner, 'draft_id': draft_id, 'message': source})
        self._send(owner, tr(self._language(owner), 'dialog.add_materials'), [[self._button(owner, 'button.files_added', 'files_added', draft_id)]])

    def _files_added(self, owner, draft_id):
        draft = self.store.get_draft(draft_id, owner)
        if not draft or draft['status'] not in ('collecting', 'preparing', 'ready') or draft['close_requested']:
            job = self.store.get_job(draft_id, owner)
            if job:
                self._send(owner, tr(self._language(owner), 'dialog.action_recorded', status=tr(self._language(owner), 'status.' + job['status'])))
                self._jobs(owner)
                return
            raise StoreError('draft_consumed')
        state = dict(draft['ui_state'])
        if state.get('pending_import_ids') or draft['status'] == 'preparing':
            state['finish_requested'] = True
            self.store.update_draft(owner, draft_id, ui_state=state)
            self._send(owner, tr(self._language(owner), 'dialog.finish_waiting'))
            return
        state.pop('finish_requested', None)
        state.pop('audio_prompt', None)
        self.store.update_draft(owner, draft_id, ui_state=state)
        draft = self.store.get_draft(draft_id, owner)
        if not self._audio_ready(draft):
            # Несколько дорожек без выбора не доходят до сводки и оплаты.
            self._audio_menu(owner, draft_id)
            return
        self._summary(owner, draft_id)

    def import_finished(self, owner, draft_id, result, message_id):
        """Продолжить подтверждённый приём только после завершения всех импортов."""
        if not self.store.authorized(owner):
            return
        draft = self.store.get_draft(draft_id, owner)
        if not draft or draft['status'] not in ('collecting', 'ready') or draft['close_requested']:
            return
        state = draft['ui_state']
        if result.get('status') == 'imported':
            self._preview(owner, draft)
        if state.get('finish_requested') and not state.get('pending_import_ids'):
            try:
                self._files_added(owner, draft_id)
            except StoreError as exc:
                self._error(owner, exc)
        elif result.get('status') == 'imported':
            try:
                audio_ready = self._audio_ready(draft)
            except StoreError:
                audio_ready = True
            if not audio_ready:
                self._audio_menu(owner, draft_id)
                return
            self._send(owner, tr(self._language(owner), 'dialog.input_ready'),
                       [[self._button(owner, 'button.files_added', 'files_added', draft_id)]],
                       reply=message_id)

    def _repeat_callback(self, owner, record):
        action, target = record['action'], record['target']
        if action in ('nav', 'history', 'files_added', 'summary', 'audio_done'):
            self._callback(owner, record)
            return
        if action in ('audio_toggle', 'audio_speaker', 'audio_clear'):
            draft = self.store.get_draft(target, owner)
            if draft and draft['status'] in ('collecting', 'preparing', 'ready') and not draft['close_requested']:
                self._audio_menu(owner, target)
            else:
                self._main_settings(owner)
            return
        draft = self.store.get_draft(target, owner)
        if draft and draft['status'] in ('collecting', 'preparing', 'ready') and not draft['close_requested']:
            if draft['ui_state'].get('pending_import_ids') or draft['status'] == 'preparing':
                self._send(owner, tr(self._language(owner), 'dialog.import_waiting'),
                           [[self._button(owner, 'button.files_added', 'files_added', target)]])
            else:
                self._main_settings(owner, draft)
            return
        job = self.store.get_job(target, owner)
        if job:
            self._send(owner, tr(self._language(owner), 'dialog.action_recorded',
                                status=tr(self._language(owner), 'status.' + job['status'])))
            self._jobs(owner)
        else:
            self._main_settings(owner)

    def _prepare(self, owner, draft_id):
        draft = self._target(owner, draft_id)
        files = [f for f in self.store.files(draft_id) if not f['removed']]
        if len([f for f in files if f['role'] == 'media']) != 1:
            raise StoreError('draft_incomplete')
        # Даже устаревшая кнопка не может обойти ожидающий импорт материала.
        if draft['ui_state'].get('pending_import_ids') or draft['ui_state'].get('media_pending'):
            raise StoreError('draft_incomplete')
        chosen = self._chosen(owner, draft)
        self._validate(chosen)
        models = self._models(chosen)
        try:
            audio = self._audio_values(from_state(draft['ui_state']))
        except AudioError:
            raise StoreError('draft_incomplete') from None
        from .intake import input_signature
        signature = input_signature(files, chosen, models, audio=audio)
        frozen_audio = {**audio, 'speakers': {str(index): name
                                              for index, name in audio['speakers'].items()}}
        snapshot = {**chosen, 'settings': chosen, 'models': models, 'audio': frozen_audio, 'version_info': deepcopy(self.core['version_info']), 'source_message_id': draft['source_message_id'], 'source_chat_id': draft['source_chat_id']}
        snapshot.update(source_title=work_title(self.store, draft), source_preview=work_preview(draft))
        state = dict(draft['ui_state'])
        state.update(state_fields(audio))
        state.pop('audio_prompt', None)
        draft = self.store.update_draft(owner, draft_id, status='ready', ui_state=state,
                                        settings={**draft['settings'], **chosen},
                                        input_signature=signature, snapshot=snapshot)
        fingerprint = _digest({'signature': signature, 'snapshot': snapshot})
        return draft, files, chosen, models, fingerprint

    def _summary(self, owner, draft_id):
        draft, files, chosen, models, fingerprint = self._prepare(owner, draft_id)
        lang = self._language(owner)
        self._preview(owner, draft)
        lines = [tr(lang, 'dialog.summary', id=work_title(self.store, draft))]
        for file in files:
            lines.append(tr(lang, 'dialog.file', name=file['original_name'], bytes=file['bytes'], duration=file['duration'] if file['duration'] is not None else tr(lang, 'unknown'), pages=file['pages'] if file['pages'] is not None else tr(lang, 'unknown')))
        for key in ('language', 'output_language', *NUMBERS, 'media_mode'):
            value = tr(lang, 'value.' + chosen[key]) if key in ('language', 'output_language', 'media_mode') else chosen[key]
            lines.append(f'{tr(lang, "setting." + key)}: {value}')
        for category, model in models.items():
            provider = model['provider']
            lines.append(f'{tr(lang, "setting." + category)}: {model["name"]} — {provider["name"]} ({tr(lang, "local" if model["local"] else "cloud")})')
        audio = from_snapshot(draft['snapshot'])
        if audio['tracks']:
            lines.append(tr(lang, 'dialog.audio_summary', count=len(audio['selected'])))
            for index in audio['selected']:
                track = next(track for track in audio['tracks'] if track['index'] == index)
                lines.append(tr(lang, 'dialog.audio_summary_track', index=index,
                                 title=self._audio_title(owner, track),
                                 track_language=track['language'] or tr(lang, 'unknown'),
                                 channels=track['channels'],
                                 speaker=audio['speakers'].get(index) or tr(lang, 'dialog.audio_unbound')))
        info = self.core['version_info']
        lines.append(tr(lang, 'dialog.summary_privacy', version=info['version'], commit=info['commit']))
        rows = [[self._button(owner, 'button.start', 'submit', draft_id, {'fingerprint': fingerprint})],
                [self._button(owner, 'button.settings', 'nav', draft_id, {'command': 'settings'}),
                 self._button(owner, 'button.materials', 'materials', draft_id)]]
        for index in audio['selected']:
            speaker = audio['speakers'].get(index)
            label = tr(lang, 'button.audio_speaker') + f' {index}'
            if speaker:
                label += ': ' + self._short(speaker)
            rows.append([self._button(owner, label, 'audio_speaker', draft_id,
                                      {'index': index}, literal=True)])
            if speaker:
                rows.append([self._button(owner, 'button.audio_clear', 'audio_clear',
                                          draft_id, {'index': index})])
        self._send(owner, '\n'.join(lines), rows)

    def _validate(self, chosen):
        for key in NUMBERS:
            value = chosen.get(key)
            if type(value) not in (int, float) or type(value) is float and not math.isfinite(value):
                raise StoreError('invalid_number')
            if key in ('hash_threshold', 'slides_dpi') and (type(value) is not int or (not 0 <= value <= 64 if key == 'hash_threshold' else value <= 0)):
                raise StoreError('invalid_number')
            if key == 'fps' and value <= 0 or key == 'min_scene_seconds' and value < 0:
                raise StoreError('invalid_number')
        if chosen.get('language') not in ('auto', 'ru', 'en') or chosen.get('output_language') not in ('ru', 'en') or chosen.get('media_mode') not in ('video', 'audio'):
            raise StoreError('invalid_number')

    def _accepted(self, owner, job):
        self._send(owner, tr(self._language(owner), 'dialog.accepted', id=work_title(self.store, job), position=self.store.queue_position(job['id'])))

    def _reuse_rows(self, owner, target, source, fingerprint=None, late=False):
        payload = {'source_job_id': source['id']}
        if fingerprint is not None:
            payload['fingerprint'] = fingerprint
        return [[self._button(owner, 'button.reuse', 'late_reuse' if late else 'reuse', target, payload), self._button(owner, 'button.force_new', 'late_force' if late else 'force_new', target, payload)], [self._button(owner, 'button.cancel', 'nav', payload={'command': 'cancel'})]]

    def late_reuse_buttons(self, owner_id: int, job_id: str) -> dict:
        job = self.store.get_job(job_id, owner_id)
        if not job or job['status'] != 'awaiting_reuse_choice':
            raise StoreError('choice_consumed')
        source = self.store.find_reuse(job['input_signature'], exclude_job_id=job_id)
        if not source or source['id'] != job['reuse_source_job_id']:
            raise StoreError('reuse_unavailable')
        return {'inline_keyboard': self._reuse_rows(owner_id, job_id, source, late=True)}

    def offer_late_reuse(self, owner_id: int, job_id: str) -> None:
        job = self.store.get_job(job_id, owner_id)
        markup = self.late_reuse_buttons(owner_id, job_id)
        source = self.store.get_job(job['reuse_source_job_id'])
        self._send(owner_id, self._reuse_text(owner_id, source), markup['inline_keyboard'])

    def _reuse_text(self, owner, source):
        info = self.core['version_info']
        return tr(self._language(owner), 'dialog.reuse', old_version=source['engine_version'], old_commit=source['engine_commit'], version=info['version'], commit=info['commit'])

    def _delivery(self, owner, source_job_id, work_uuid):
        self.store.add_outbox('delivery', {'owner_id': owner, 'source_job_id': source_job_id, 'work_uuid': work_uuid})
        self._send(owner, tr(self._language(owner), 'dialog.delivery_pending'))

    def _jobs(self, owner, offset=0):
        lang = self._language(owner)
        jobs = self.store.jobs(owner, offset=offset, limit=10)
        deliveries = self.store.deliveries(owner, offset=offset, limit=10)
        lines = [tr(lang, 'dialog.jobs')]
        rows = []
        for job in jobs:
            title = work_title(self.store, job)
            lines.append(f'{title} — {tr(lang, "status." + job["status"])}; {job["accepted_at"]}')
            if job['status'] == 'awaiting_reuse_choice':
                try:
                    rows.extend(self.late_reuse_buttons(owner, job['id'])['inline_keyboard'])
                except StoreError:
                    rows.append([self._button(owner, 'button.force_new', 'late_force', job['id'])])
            if job['status'] == 'completed' and not job['files_deleted']:
                rows.append([self._button(owner, tr(lang, 'dialog.redeliver', id=title), 'redeliver', job['id'], {'kind': 'job'}, literal=True)])
        for delivery in deliveries:
            work = self.store.get_draft(delivery['work_uuid'], owner) if delivery['work_uuid'] else None
            source = work or self.store.get_job(delivery['source_job_id'])
            title = work_title(self.store, source, viewer=owner) if source else tr(lang, 'work.cached_result')
            lines.append(tr(lang, 'dialog.history_delivery', id=title, status=tr(lang, 'dialog.files_deleted' if delivery['files_deleted'] else 'dialog.result_available')))
            if not delivery['files_deleted']:
                rows.append([self._button(owner, tr(lang, 'dialog.redeliver', id=title), 'redeliver', str(delivery['id']), {'kind': 'delivery'}, literal=True)])
        nav = []
        if offset:
            nav.append(self._button(owner, 'button.previous', 'history', payload={'offset': max(0, offset - 10)}))
        if len(jobs) == 10 or len(deliveries) == 10:
            nav.append(self._button(owner, 'button.next', 'history', payload={'offset': offset + 10}))
        if nav:
            rows.append(nav)
        self._send(owner, '\n'.join(lines), rows)

    def _stats(self, owner):
        lang = self._language(owner)
        stats = self.store.stats(owner, include_cost=False)
        lines = [tr(lang, 'dialog.stats', jobs=stats['jobs'], attempts=stats['api_attempts'], deliveries=stats['deliveries'], reuse=stats['reused_deliveries'], remaining=stats['quota']['remaining'] if stats['quota']['remaining'] is not None else tr(lang, 'dialog.unlimited'))]
        for status, count in stats['statuses'].items():
            lines.append(f'{tr(lang, "status." + status)}: {count}')
        for key in ('cpu_user_seconds', 'cpu_system_seconds', 'busy_gpu_seconds', 'ru_maxrss_kib', 'host_rss_peak_kib', 'vram_peak_bytes', 'queue_wait_seconds', 'preparation_seconds', 'processing_seconds', 'render_seconds', 'delivery_seconds', 'prompt_tokens', 'completion_tokens', 'cache_hit', 'cache_miss', 'reasoning_tokens'):
            metric = stats[key]
            value = metric.get('known_total', metric.get('known_peak'))
            lines.append(tr(lang, 'dialog.metric', name=tr(lang, 'metric.' + key), total=value if value is not None else tr(lang, 'unknown'), unknown=metric.get('unknown_works', metric.get('unknown_calls', metric.get('unknown_deliveries', 0)))))
        self._send(owner, '\n'.join(lines))

    def _cancel_prompt(self, owner):
        placeholders = ','.join('?' for _ in UNFINISHED)
        count = self.store.db.execute(f'SELECT COUNT(*) FROM jobs WHERE owner_id=? AND status IN ({placeholders})', (owner, *UNFINISHED)).fetchone()[0]
        jobs = self.store.jobs(owner, limit=count, statuses=UNFINISHED)
        draft = self.store.active_draft(owner)
        payload = {'job_ids': [job['id'] for job in jobs], 'draft_id': draft['id'] if draft else None}
        self._send(owner, tr(self._language(owner), 'dialog.cancel_prompt', count=len(jobs), drafts=int(draft is not None)), [[self._button(owner, 'button.confirm', 'cancel_confirm', payload=payload), self._button(owner, 'button.back', 'nav', payload={'command': 'jobs'})]])

    def _cancel_work(self, owner, work_uuid, reason):
        self.store.add_outbox('cancel_work', {'work_uuid': work_uuid, 'owner_id': owner, 'actor_id': owner, 'reason': reason}, dedupe_key='cancel:' + work_uuid + ':' + reason)

    def _cancel(self, owner, payload):
        jobs = self.store.request_cancel(owner, payload['job_ids'], reason='user', now_utc=now_utc())
        for job in jobs:
            if job['status'] == 'cancel_requested':
                self._cancel_work(owner, job['id'], 'user')
            else:
                self._send(owner, tr(self._language(owner), 'dialog.cancelled', id=work_title(self.store, job)), reply=job['source_message_id'], purpose='cancellation')
        if payload.get('draft_id'):
            draft = self.store.get_draft(payload['draft_id'], owner)
            if draft and draft['status'] in ('collecting', 'preparing', 'ready'):
                closed = self.store.close_draft(owner, draft['id'], reason='user')
                if closed['close_requested']:
                    self._cancel_work(owner, draft['id'], 'user')
                else:
                    self._send(owner, tr(self._language(owner), 'dialog.cancelled', id=work_title(self.store, draft)), reply=draft['source_message_id'], purpose='cancellation')
        self._send(owner, tr(self._language(owner), 'dialog.cancel_requested'))

    def _command(self, owner, command):
        lang = self._language(owner)
        if command == 'start':
            self._telegram(owner, 'send_photo', chat_id=owner, photo='asset:absorbent.jpg')
            info = self.core['version_info']
            self._send(owner, tr(lang, 'welcome', version=info['version'], commit=info['commit'], max_hours=self.settings.max_media_seconds / 3600, max_media_gb=self.settings.max_media_bytes / 1e9, max_material_mb=self.settings.max_material_bytes / 1e6, max_pages=self.settings.max_material_pages, quota_jobs=self.settings.quota_jobs, quota_hours=self.settings.quota_window_seconds / 3600), self._menu(owner))
        elif command == 'new':
            self._new(owner)
        elif command == 'jobs':
            self._jobs(owner)
        elif command == 'settings':
            draft = self.store.active_draft(owner)
            if draft and draft['status'] != 'preparing':
                state = dict(draft['ui_state'])
                state.pop('number', None)
                self.store.update_draft(owner, draft['id'], ui_state=state)
            else:
                user = self.store.get_user(owner)
                selected = dict(user['settings'])
                selected.pop('_dialog', None)
                self.store.update_user(owner, settings=selected)
            self._main_settings(owner, draft)
        elif command == 'language':
            self._send(owner, tr(lang, 'dialog.language'), [[self._button(owner, 'Русский', 'lang', payload={'language': 'ru'}, literal=True), self._button(owner, 'English', 'lang', payload={'language': 'en'}, literal=True)]])
        elif command == 'help':
            self._send(owner, tr(lang, 'help'), self._menu(owner))
        elif command == 'version':
            from importlib.metadata import version
            self._send(owner, tr(lang, 'version', bot_version=version('absorbobot'), **{key: self.core['version_info'][key] for key in ('version', 'commit')}))
        elif command == 'stats':
            self._stats(owner)
        elif command == 'cancel':
            self._cancel_prompt(owner)

    def _error(self, owner, error):
        lang = self._language(owner)
        if isinstance(error, QuotaError):
            text = tr(lang, 'error.quota', timestamp=error.next_available_at)
        elif error.code == 'model_missing':
            text = tr(lang, 'error.model_missing', **error.details)
        else:
            key = 'error.' + error.code
            from .i18n import catalogues
            text = tr(lang, key if key in catalogues()[lang] else 'error.stale')
        self._send(owner, text)

    def handle_message(self, message: dict) -> bool:
        owner = self._owner(message)
        if owner is None:
            return False
        text = message.get('text') or ''
        command = text.split(maxsplit=1)[0].split('@', 1)[0][1:] if text.startswith('/') else None
        if command:
            if command not in COMMANDS or command == 'stats' and len(text.split()) > 1 and self.store.is_admin(owner):
                return False
            try:
                self._command(owner, command)
            except StoreError as exc:
                self._error(owner, exc)
            return True
        try:
            draft = self.store.active_draft(owner)
            audio_prompt = (draft['ui_state'] if draft else {}).get('audio_prompt')
            pending = (draft['ui_state'] if draft else self.store.get_user(owner)['settings'].get('_dialog', {})).get('number')
            if audio_prompt is not None and text:
                self._set_speaker(owner, draft, text)
            elif pending:
                key = pending
                try:
                    value = int(text) if key in ('hash_threshold', 'slides_dpi') else float(text)
                except (ValueError, OverflowError):
                    raise StoreError('invalid_number') from None
                chosen = self._chosen(owner, draft)
                chosen[key] = value
                self._validate(chosen)
                self._persist(owner, draft, key, value)
                self._main_settings(owner, self.store.get_draft(draft['id'], owner) if draft else None, advanced=True)
            elif any(message.get(kind) for kind in ('document', 'video', 'audio', 'voice', 'video_note', 'photo')) or text:
                self._input(owner, message)
            else:
                return False
        except StoreError as exc:
            self._error(owner, exc)
        return True

    def handle_callback(self, callback: dict) -> bool:
        owner = self._owner(callback, callback=True)
        if owner is None:
            return False
        token = callback.get('data') or ''
        if token.partition(':')[0] not in ACTIONS:
            return False
        try:
            record = self.store.get_token(owner, token)
        except StoreError:
            # Чужой токен не вызывает исходящих действий и не расходуется.
            return False
        try:
            if record['consumed_at'] is not None:
                self._repeat_callback(owner, record)
            else:
                record = self.store.consume_token(owner, token)
                self._callback(owner, record)
        except StoreError as exc:
            self._error(owner, exc)
        return True

    def _callback(self, owner, record):
        action, target, payload = record['action'], record['target'], record['payload']
        if action == 'nav':
            if payload['command'] == 'settings' and target != 'user':
                self._main_settings(owner, self._target(owner, target))
            else:
                self._command(owner, payload['command'])
        elif action == 'lang':
            self.store.update_user(owner, language=payload['language'])
            draft = self.store.active_draft(owner)
            if draft and draft['status'] != 'preparing':
                self.store.update_draft(owner, draft['id'], settings=draft['settings'])
            self._send(owner, tr(self._language(owner), 'dialog.language_changed'), self._menu(owner))
        elif action == 'new_confirm':
            self._new(owner, confirmed=True, old_id=target)
        elif action == 'files_added':
            self._files_added(owner, target)
        elif action == 'advanced':
            self._main_settings(owner, self._target(owner, target), advanced=True)
        elif action == 'choose':
            self._target(owner, target)
            key = payload['key']
            if key in CATEGORIES:
                values = list(self.core['models'][key])
            else:
                values = ['auto', 'ru', 'en'] if key == 'language' else [None, 'ru', 'en']
            rows = [[self._button(owner, 'button.same_language' if value is None else ('value.' + value if key not in CATEGORIES else value), 'set', target, {'key': key, 'value': value}, literal=key in CATEGORIES)] for value in values]
            self._send(owner, tr(self._language(owner), 'setting.' + key), rows)
        elif action == 'set':
            draft = self._target(owner, target)
            key, value = payload['key'], payload['value']
            if key in CATEGORIES and value not in self.core['models'][key]:
                raise StoreError('model_missing', category=key, model=value)
            self._persist(owner, draft, 'output_language_override' if key == 'output_language' else key, value)
            self._main_settings(owner, self.store.get_draft(target, owner) if draft else None, advanced=key in CATEGORIES)
        elif action == 'number':
            draft = self._target(owner, target)
            if draft:
                self.store.update_draft(owner, target, ui_state={**draft['ui_state'], 'number': payload['key']})
            else:
                user = self.store.get_user(owner)
                self.store.update_user(owner, settings={**user['settings'], '_dialog': {'number': payload['key']}})
            self._send(owner, tr(self._language(owner), 'dialog.number', name=tr(self._language(owner), 'setting.' + payload['key'])))
        elif action == 'reset':
            draft = self._target(owner, target)
            self.store.update_user(owner, settings={}, output_language_override=None)
            if draft:
                state = dict(draft['ui_state'])
                state.pop('number', None)
                selected = {'output_language_override': None}
                if 'media_mode' in draft['settings']:
                    selected['media_mode'] = draft['settings']['media_mode']
                self.store.update_draft(owner, target, settings=selected, ui_state=state)
            self._main_settings(owner, self.store.get_draft(target, owner) if draft else None)
        elif action == 'materials':
            draft = self._target(owner, target)
            state = dict(draft['ui_state'])
            state.pop('finish_requested', None)
            fields = {'ui_state': state, 'input_signature': None, 'snapshot': {}}
            if draft['status'] != 'preparing':
                fields['status'] = 'collecting'
            self.store.update_draft(owner, target, **fields)
            self._send(owner, tr(self._language(owner), 'dialog.add_materials'), [[self._button(owner, 'button.files_added', 'files_added', target)]])
        elif action == 'audio_toggle':
            draft = self._target(owner, target)
            audio = self._audio_document(draft)
            index = payload.get('index')
            if type(index) is not int or index not in {track['index'] for track in audio['tracks']}:
                raise StoreError('audio_selection_invalid')
            selected = set(audio['selected'])
            selected.symmetric_difference_update({index})
            speakers = {key: value for key, value in audio['speakers'].items()
                        if key in selected}
            self._set_audio(owner, target, {'tracks': audio['tracks'],
                                            'selected': sorted(selected),
                                            'speakers': speakers})
            self._audio_menu(owner, target)
        elif action == 'audio_speaker':
            self._audio_prompt(owner, target, payload.get('index'))
        elif action == 'audio_clear':
            draft = self._target(owner, target)
            audio = self._audio_document(draft)
            index = payload.get('index')
            if index not in audio['selected']:
                raise StoreError('audio_selection_invalid')
            audio['speakers'].pop(index, None)
            self._set_audio(owner, target, audio)
            self._audio_menu(owner, target)
        elif action == 'audio_done':
            draft = self._target(owner, target)
            self._audio_values(self._audio_document(draft))
            self._files_added(owner, target)
        elif action == 'media':
            draft = self._target(owner, target)
            source = draft['ui_state'].get('media_pending')
            if not source:
                raise StoreError('choice_consumed')
            mode = payload['mode']
            self._persist(owner, draft, 'media_mode', mode)
            state = dict(draft['ui_state'])
            state.pop('media_pending', None)
            self.store.update_draft(owner, target, ui_state=state)
            self._intake(owner, target, {**source, 'media_mode': mode})
        elif action == 'summary':
            self._files_added(owner, target)
        elif action in ('submit', 'reuse', 'force_new'):
            draft, _, _, _, fingerprint = self._prepare(owner, target)
            if fingerprint != payload['fingerprint']:
                raise StoreError('summary_changed')
            if action == 'submit':
                source = self.store.find_reuse(draft['input_signature'])
                if source:
                    self._send(owner, self._reuse_text(owner, source), self._reuse_rows(owner, target, source, fingerprint))
                    return
            if action == 'reuse':
                self.store.reuse_draft(owner, target, payload['source_job_id'], now_utc())
                self._delivery(owner, payload['source_job_id'], target)
            else:
                self._accepted(owner, self.store.enqueue(owner, target, now_utc(), force_new=action == 'force_new'))
        elif action in ('late_reuse', 'late_force'):
            job = self.store.get_job(target, owner)
            if not job:
                raise StoreError('not_found')
            if action == 'late_force':
                frozen = job['snapshot']['models']
                chosen = {category: model['name'] for category, model in frozen.items()}
                if self._models(chosen) != frozen or job['snapshot']['version_info'] != self.core['version_info']:
                    raise StoreError('summary_changed')
            job = self.store.confirm_late_reuse(owner, target, 'reuse' if action == 'late_reuse' else 'force_new', now_utc())
            if action == 'late_reuse':
                self._delivery(owner, job['reuse_source_job_id'], target)
            else:
                self._accepted(owner, job)
        elif action == 'history':
            self._jobs(owner, payload['offset'])
        elif action == 'redeliver':
            if payload['kind'] == 'job':
                job = self.store.get_job(target, owner)
                if not job or job['status'] != 'completed':
                    raise StoreError('reuse_unavailable')
                source, work = job['id'], job['id']
            else:
                delivery = self.store.db.execute('SELECT * FROM deliveries WHERE id=? AND owner_id=?', (target, owner)).fetchone()
                if not delivery:
                    raise StoreError('not_owner')
                source, work = delivery['source_job_id'], delivery['work_uuid']
                if work:
                    self.store.get_draft(work, owner)
            self.store.record_delivery(owner, source, work_uuid=work, now_utc=now_utc())
            self._delivery(owner, source, work)
        elif action == 'retry':
            job = self.store.get_job(target, owner)
            if not job or job['status'] != 'failed' or job['failure_reason'] not in RETRYABLE_FAILURES:
                raise StoreError('retry_unavailable')
            try:
                reset_work_tree(self.settings, job['id'])
            except (ConfigError, OSError):
                raise StoreError('retry_unavailable') from None
            job = self.store.retry_job(owner, job['id'], now_utc())
            self._accepted(owner, job)
        elif action == 'cancel_confirm':
            self._cancel(owner, payload)
