"""Выбор звуковых дорожек: подпись входа, argv ядра и недоверенные метаданные."""

from __future__ import annotations

import pytest

from absorbobot.audio import (AudioError, core_arguments, from_state, normalize_document,
                              signature_part, speaker_name, state_fields)


TRACKS = [
    {"index": 1, "title": "Зал", "language": "ru", "default": True,
     "channels": 2, "start_time": 0.5, "offset": 0.0},
    {"index": 4, "title": "Перевод", "language": "en", "default": False,
     "channels": 1, "start_time": 1.5, "offset": 1.0},
]


def test_one_track_is_selected_automatically_but_many_need_a_choice():
    single = normalize_document({"tracks": TRACKS[:1], "selected": []})
    assert single["selected"] == [1]
    with pytest.raises(AudioError, match="^audio_selection_required$"):
        normalize_document({"tracks": TRACKS, "selected": []})
    chosen = normalize_document({"tracks": TRACKS, "selected": [4]})
    assert chosen["selected"] == [4]
    assert chosen["speakers"] == {}


@pytest.mark.parametrize("selection", [[9], [1, 1], ["1"], [True]])
def test_selection_must_reference_existing_unique_stream_indexes(selection):
    with pytest.raises(AudioError, match="^audio_selection_invalid$"):
        normalize_document({"tracks": TRACKS, "selected": selection})


def test_core_arguments_repeat_absolute_indexes_and_verified_names():
    document = normalize_document({"tracks": TRACKS, "selected": [4, 1],
                                   "speakers": {1: "Алиса", 4: "Боб"}})
    assert core_arguments(document) == [
        "--audio-track", "1", "--audio-track", "4",
        "--track-speaker", "1=Алиса", "--track-speaker", "4=Боб",
    ]
    no_names = normalize_document({"tracks": TRACKS, "selected": [1]})
    assert core_arguments(no_names) == ["--audio-track", "1"]


def test_signature_part_includes_selection_and_bindings_but_not_titles():
    base = normalize_document({"tracks": TRACKS, "selected": [1]})
    renamed = normalize_document({"tracks": [
        {**TRACKS[0], "title": "Другое название"}, TRACKS[1]], "selected": [1]})
    assert signature_part(base) == signature_part(renamed)
    other_track = normalize_document({"tracks": TRACKS, "selected": [4]})
    assert signature_part(base) != signature_part(other_track)
    bound = normalize_document({"tracks": TRACKS, "selected": [1],
                                "speakers": {1: "Алиса"}})
    assert signature_part(base) != signature_part(bound)
    # Смещение и каналы остаются частью снимка, хотя и не подписывают выбор.
    assert base["tracks"][1]["offset"] == 1.0


def test_binding_must_target_a_selected_track():
    with pytest.raises(AudioError, match="^invalid_track_speaker$"):
        normalize_document({"tracks": TRACKS, "selected": [1],
                            "speakers": {4: "Боб"}})


def test_state_fields_round_trip_through_json_shaped_mapping():
    document = normalize_document({"tracks": TRACKS, "selected": [1, 4],
                                   "speakers": {1: "Алиса"}})
    state = state_fields(document)
    assert state == {"audio_tracks": TRACKS, "audio_selection": [1, 4],
                     "audio_speakers": {"1": "Алиса"}}
    assert from_state(state) == document


@pytest.mark.parametrize("value, expected", [
    ("  Алиса  ", "Алиса"),
    ("Алиса\nБоб", "Алиса Боб"),
    ("\x00\x07", None),
    ("", None),
    ("а" * 200, None),
])
def test_speaker_name_is_displayed_text_not_a_command(value, expected):
    if expected is None:
        with pytest.raises(AudioError, match="^invalid_speaker$"):
            speaker_name(value)
    else:
        assert speaker_name(value) == expected
