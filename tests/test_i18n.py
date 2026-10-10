"""Язык конспекта следует интерфейсу только без явного переопределения."""

import pytest

from absorbobot.i18n import language_for, notes_language, tr


@pytest.mark.parametrize("code, expected", [(None, "en"), ("ru", "ru"), ("ru-RU", "ru"),
                                            ("RU", "ru"), ("en-US", "en"), ("fr", "en")])
def test_initial_language_uses_supported_telegram_prefix(code, expected):
    assert language_for(code) == expected


def test_interface_switch_does_not_overwrite_explicit_output_language():
    user = {"language": "ru", "output_language_override": None}
    assert notes_language(user) == "ru"
    user["language"] = "en"
    assert notes_language(user) == "en"
    user["output_language_override"] = "ru"
    assert notes_language(user) == "ru"
    user["language"] = "ru"
    user["output_language_override"] = "en"
    assert notes_language(user) == "en"
    user["output_language_override"] = None
    assert notes_language(user) == "ru"


@pytest.mark.parametrize("language", ["ru", "en"])
def test_full_installed_revision_is_not_truncated_in_version_message(language):
    revision = "f0123456789abcdef0123456789abcdef01234567"
    text = tr(language, "version", version="0.1.2", commit=revision, bot_version="0.1.0")
    assert revision in text
    assert "0.1.2" in text
