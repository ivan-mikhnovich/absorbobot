"""Границы разбора ссылок площадок, метаданных и фактического потока загрузки."""

from __future__ import annotations

import os

import pytest

import absorbobot.workers.media as media_module
from absorbobot.workers.media import (
    DownloadBudget,
    MediaError,
    clean_title,
    download,
    normalize_media_url,
    placeholder_name,
    placeholder_platform,
    platform_label,
    resolve_media_url,
    tree_bytes,
    validate_metadata,
)


CANONICAL = "https://www.youtube.com/watch?v=abcdefghijk"
VIDEO = "abcdefghijk"
VK_CANONICAL = "https://vk.com/video-77521_162222515"
VK_ID = "-77521_162222515"
RUTUBE_ID = "3eac3b4561676c17df9132a9a1e62e3e"
RUTUBE_CANONICAL = "https://rutube.ru/video/" + RUTUBE_ID + "/"
DZEN_ID = "6002240ff8b1af50bb2da5e3"
DZEN_CANONICAL = "https://dzen.ru/video/watch/" + DZEN_ID
ZOOM_TOKEN = "VjE-5kW3xmgbEYqR5KzRgZ1OFZvtMtiXk5HyRJo5kK4m5PYE6RF4rF_oiiO_9qaM.UTAg1MI7JSnF3ZjX"
ZOOM_CANONICAL = "https://cityofdetroit.zoom.us/rec/share/" + ZOOM_TOKEN
PANOPTO_ID_FULL = "26b3ae9e-4a48-4dcc-96ba-0befba08a0fb"
PANOPTO_CANONICAL = "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=" + PANOPTO_ID_FULL
KALTURA_CANONICAL = "kaltura:691292:0_c076mna6"


def platform_of(url: str):
    """Правила площадки, которой принадлежит канонический URL."""
    return resolve_media_url(url)[0]


YOUTUBE = platform_of(CANONICAL)
VK = platform_of(VK_CANONICAL)


def metadata(**fields):
    base = {"id": VIDEO, "_type": "video", "live_status": "not_live",
            "duration": 100, "availability": "public", "extractor_key": "Youtube",
            "formats": [{"format_id": "v"}]}
    base.update(fields)
    return base


def vk_metadata(**fields):
    # Метаданные VKIE: идентификатор из URL, is_live вместо live_status и availability.
    base = {"id": VK_ID, "duration": 195, "extractor_key": "VK", "is_live": False,
            "formats": [{"format_id": "url720"}]}
    base.update(fields)
    return base


@pytest.mark.parametrize("value", [
    "https://youtube.com/watch?v=abcdefghijk",
    "https://www.youtube.com/watch?v=abcdefghijk",
    "https://www.youtube.com/watch?v=abcdefghijk&t=30",
    "https://m.youtube.com/watch?v=abcdefghijk&start=10",
    "https://youtu.be/abcdefghijk",
    "https://youtu.be/abcdefghijk?si=share",
    "https://www.youtube.com/shorts/abcdefghijk",
    "https://www.youtube.com/embed/abcdefghijk",
])
def test_normalize_accepts_only_canonical_https_video_forms(value):
    assert normalize_media_url(value) == CANONICAL


@pytest.mark.parametrize("value", [
    None,
    42,
    "",
    "http://www.youtube.com/watch?v=abcdefghijk",
    "ftp://youtube.com/watch?v=abcdefghijk",
    "https://user@www.youtube.com/watch?v=abcdefghijk",
    "https://user:pass@youtu.be/abcdefghijk",
    "https://www.youtube.com:8443/watch?v=abcdefghijk",
    "https://www.youtube.com/watch?v=abcdefghijk#t=30",
    "https://www.youtube.com/watch?v=abcdefghijk&list=PLabc",
    "https://www.youtube.com/playlist?list=PLabc",
    "https://www.youtube.com/watch?v=abcdefghijk&index=2",
    "https://www.youtube.com/watch?v=abcdefghijk&x=1",
    "https://www.youtube.com/watch?v=abcdefghijk&v=zzzzzzzzzzz",
    "https://www.youtube.com/watch?v=abcdefghij",
    "https://www.youtube.com/watch?v=abcdefghijkl",
    "https://www.youtube.com/watch?v=abcd!fghijk",
    "https://evil.example/watch?v=abcdefghijk",
    "https://youtu.be/abcdefghijk/extra",
    "https://youtu.be/abcdefghijk?v=abcdefghijk",
    "https://www.youtube.com/watch",
    "https://www.youtube.com/watch?",
    "https://www.youtube.com",
    "https://www.youtube.com/watch?v=abcdefghijk ",
    "https://www.youtube.com/%77atch?v=abcdefghijk",
])
def test_normalize_rejects_userinfo_ports_playlists_and_fake_hosts(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


@pytest.mark.parametrize("value,expected", [
    ("https://vk.com/video-77521_162222515", VK_CANONICAL),
    ("https://m.vk.com/video-77521_162222515", VK_CANONICAL),
    ("https://new.vk.com/video-77521_162222515", VK_CANONICAL),
    ("https://vk.ru/video-77521_162222515", VK_CANONICAL),
    ("https://vkvideo.ru/video-77521_162222515", VK_CANONICAL),
    ("https://m.vkvideo.ru/video-77521_162222515", VK_CANONICAL),
    ("https://vk.com/video-77521_162222515/", VK_CANONICAL),
    # Идентификатор без знака владельца — запись личной страницы.
    ("https://vk.com/video205387401_165548505", "https://vk.com/video205387401_165548505"),
])
def test_normalize_accepts_canonical_vk_video_forms(value, expected):
    assert normalize_media_url(value) == expected


@pytest.mark.parametrize("value", [
    None,
    42,
    "",
    "http://vk.com/video-77521_162222515",
    "ftp://vk.com/video-77521_162222515",
    "https://user@vk.com/video-77521_162222515",
    "https://user:pass@vkvideo.ru/video-77521_162222515",
    "https://vk.com:8443/video-77521_162222515",
    "https://vk.com/video-77521_162222515#t=30",
    # Плейлист, альбом и метка времени выбирают не одну запись.
    "https://vk.com/video-77521_162222515?list=ln-cBjJ7S4jYYx3ADnmDT",
    "https://vk.com/videos-77521?z=video-77521_162222515",
    "https://vk.com/video-77521_162222515?t=30",
    # Встраиваемая ссылка требует hash и не является публичной формой записи.
    "https://vk.com/video_ext.php?oid=-77521&id=162222515&hash=87b046504ccd8bfa",
    # Клип — не запись лекции, а контейнер ссылки отдаёт чужой результат.
    "https://vk.com/clip-77521_162222515",
    "https://vk.com/video-77521",
    "https://vk.com/video-77521_",
    "https://vk.com/video-77521_162222515/extra",
    "https://vk.com/",
    "https://evil.example/video-77521_162222515",
    "https://vk.com.evil.example/video-77521_162222515",
    "https://vk.com/video-77521_162222515 ",
    "https://vk.com/%76ideo-77521_162222515",
    # Формы чужой площадки на узле VK не разбирает ни один парсер.
    "https://vk.com/watch?v=abcdefghijk",
])
def test_normalize_rejects_vk_ambiguous_and_foreign_links(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


@pytest.mark.parametrize("value", [
    "https://rutube.ru/video/" + RUTUBE_ID,
    "https://rutube.ru/video/" + RUTUBE_ID + "/",
    "https://www.rutube.ru/video/" + RUTUBE_ID + "/",
    "https://m.rutube.ru/video/" + RUTUBE_ID,
])
def test_normalize_accepts_rutube_single_video_forms(value):
    # Мобильный и www-узел приводим к каноническому: экстрактор знает только rutube.ru.
    assert normalize_media_url(value) == RUTUBE_CANONICAL


@pytest.mark.parametrize("value", [
    None,
    "",
    "http://rutube.ru/video/" + RUTUBE_ID,
    "https://rutube.ru:8443/video/" + RUTUBE_ID,
    "https://user@rutube.ru/video/" + RUTUBE_ID,
    # Приватная ссылка несёт токен ?p=, а эфир и вставка — иная форма записи.
    "https://rutube.ru/video/" + RUTUBE_ID + "/?p=token",
    "https://rutube.ru/video/private/" + RUTUBE_ID + "/?p=token",
    "https://rutube.ru/live/video/" + RUTUBE_ID + "/",
    "https://rutube.ru/embed/" + RUTUBE_ID,
    "https://rutube.ru/play/embed/" + RUTUBE_ID,
    "https://rutube.ru/video/" + RUTUBE_ID + "/?pl_id=4252",
    # Идентификатор — строго 32 строчных шестнадцатеричных знака.
    "https://rutube.ru/video/" + RUTUBE_ID.upper(),
    "https://rutube.ru/video/3eac3b4561676c17df9132a9a1e62e3",
    "https://rutube.ru/channel/25902603/",
    "https://evil.example/video/" + RUTUBE_ID,
])
def test_normalize_rejects_rutube_private_live_and_foreign_links(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


@pytest.mark.parametrize("value", [
    "https://dzen.ru/video/watch/" + DZEN_ID,
    "https://dzen.ru/video/watch/" + DZEN_ID + "/",
    "https://www.dzen.ru/video/watch/" + DZEN_ID,
    "https://zen.yandex.ru/video/watch/" + DZEN_ID,
])
def test_normalize_accepts_dzen_single_video_forms(value):
    assert normalize_media_url(value) == DZEN_CANONICAL


@pytest.mark.parametrize("value", [
    None,
    "",
    "http://dzen.ru/video/watch/" + DZEN_ID,
    "https://dzen.ru/video/watch/" + DZEN_ID + "?from=feed",
    "https://dzen.ru/video/watch/" + DZEN_ID.upper(),
    # Статьи, каналы и профили — не запись.
    "https://dzen.ru/media/" + DZEN_ID,
    "https://dzen.ru/media/id/channel/slug-" + DZEN_ID,
    "https://dzen.ru/id/1234567890",
    "https://evil.example/video/watch/" + DZEN_ID,
])
def test_normalize_rejects_dzen_articles_channels_and_foreign_links(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


VKPLAY_ID_FULL = "f5e6e3b5-dc52-4d14-965d-0680dd2882da"
VKPLAY_CANONICAL = "https://vkplay.live/zitsmann/record/" + VKPLAY_ID_FULL
TWITCH_CANONICAL = "https://www.twitch.tv/videos/6528877"
SOUNDCLOUD_CANONICAL = "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy"


@pytest.mark.parametrize("value", [
    VKPLAY_CANONICAL,
    VKPLAY_CANONICAL + "/",
    "https://www.vkplay.live/zitsmann/record/" + VKPLAY_ID_FULL,
])
def test_normalize_accepts_vkplay_record_forms(value):
    assert normalize_media_url(value) == VKPLAY_CANONICAL


@pytest.mark.parametrize("value", [
    None,
    "",
    "http://vkplay.live/zitsmann/record/" + VKPLAY_ID_FULL,
    "https://vkplay.live/zitsmann/record/" + VKPLAY_ID_FULL + "/records",
    # Канал — прямой эфир, клипы и усечённый идентификатор обслуживает не запись.
    "https://vkplay.live/zitsmann",
    "https://vkplay.live/zitsmann/clips",
    "https://vkplay.live/zitsmann/record/f5e6e3b5",
    "https://vkplay.live/zitsmann/record/123",
    "https://evil.example/zitsmann/record/" + VKPLAY_ID_FULL,
])
def test_normalize_rejects_vkplay_channels_and_foreign_links(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


@pytest.mark.parametrize("value", [
    TWITCH_CANONICAL,
    "https://twitch.tv/videos/6528877",
    "https://www.twitch.tv/videos/6528877/",
])
def test_normalize_accepts_twitch_vod_forms(value):
    assert normalize_media_url(value) == TWITCH_CANONICAL


@pytest.mark.parametrize("value", [
    None,
    "",
    "http://www.twitch.tv/videos/6528877",
    # Канал со списком записей, клип и эфир — не одна запись.
    "https://www.twitch.tv/riotgames/videos",
    "https://www.twitch.tv/riotgames",
    "https://www.twitch.tv/riotgames/clip/SomeClip",
    "https://www.twitch.tv/videos/",
    "https://www.twitch.tv/videos/6528877abcd",
    "https://evil.example/videos/6528877",
])
def test_normalize_rejects_twitch_channels_clips_and_foreign_links(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


@pytest.mark.parametrize("value", [
    SOUNDCLOUD_CANONICAL,
    "https://www.soundcloud.com/ethmusic/lostin-powers-she-so-heavy",
    "https://m.soundcloud.com/ethmusic/lostin-powers-she-so-heavy",
    "https://soundcloud.com/ethmusic/Lostin-Powers-She-so-Heavy",
])
def test_normalize_accepts_soundcloud_track_forms(value):
    assert normalize_media_url(value).lower() == SOUNDCLOUD_CANONICAL


@pytest.mark.parametrize("value", [
    None,
    "",
    "http://soundcloud.com/ethmusic/lostin-powers-she-so-heavy",
    # Секретные ссылки, API, плейлисты и страницы пользователей не принимаются.
    "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy/s-8Pjrp",
    "https://api.soundcloud.com/tracks/123998367",
    "https://soundcloud.com/ethmusic/sets/playlist",
    "https://soundcloud.com/ethmusic",
    "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy?secret_token=s-8Pjrp",
    "https://evil.example/ethmusic/lostin-powers-she-so-heavy",
])
def test_normalize_rejects_soundcloud_private_playlists_and_foreign_links(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


def test_soundcloud_serves_only_audio_mode():
    # У SoundCloud нет видеозаписей, поэтому вопрос о режиме не задаётся.
    assert list(platform_of(SOUNDCLOUD_CANONICAL).modes) == ["audio"]
    assert list(platform_of(VKPLAY_CANONICAL).modes) == ["video", "audio"]


def test_soundcloud_identity_is_checked_by_permalink_not_numeric_id():
    platform = platform_of(SOUNDCLOUD_CANONICAL)
    track = "ethmusic/lostin-powers-she-so-heavy"
    matching = {"id": "62986583", "duration": 143.0, "extractor_key": "Soundcloud",
                "webpage_url": SOUNDCLOUD_CANONICAL, "formats": [{"format_id": "http_mp3_0_0"}]}
    assert validate_metadata(matching, track, 14400, platform) == {"duration": 143.0}
    assert platform.identify(matching, track) is True
    # Пермалинк другой записи или его отсутствие тождества не подтверждают.
    assert platform.identify({**matching, "webpage_url": "https://soundcloud.com/other/track"}, track) is False
    assert platform.identify({"id": "62986583"}, track) is False
    with pytest.raises(MediaError) as error:
        validate_metadata({**matching, "webpage_url": "https://soundcloud.com/other/track", "extractor_key": "Soundcloud"},
                          track, 14400, platform)
    assert str(error.value) == "invalid_media_metadata"


@pytest.mark.parametrize("value,expected", [
    (ZOOM_CANONICAL, ZOOM_CANONICAL),
    # Пароль и метка времени отбрасываются: бот не принимает ни того, ни другого.
    (ZOOM_CANONICAL + "?pwd=AbCdEf&startTime=1615000000000", ZOOM_CANONICAL),
    ("https://us02web.zoom.us/recording/play/" + ZOOM_TOKEN,
     "https://us02web.zoom.us/recording/play/" + ZOOM_TOKEN),
    ("https://zoom.us/rec/play/" + ZOOM_TOKEN, "https://zoom.us/rec/play/" + ZOOM_TOKEN),
])
def test_normalize_accepts_zoom_recording_forms(value, expected):
    assert normalize_media_url(value) == expected


@pytest.mark.parametrize("value", [
    None,
    "",
    "http://us02web.zoom.us/rec/play/" + ZOOM_TOKEN,
    "https://user@us02web.zoom.us/rec/play/" + ZOOM_TOKEN,
    # Поддомен ровно один, а хост верхнего уровня — только zoom.us.
    "https://foo.bar.zoom.us/rec/share/" + ZOOM_TOKEN,
    "https://zoom.com/rec/share/" + ZOOM_TOKEN,
    # Комната, скачивание и лишние сегменты — не запись.
    "https://us02web.zoom.us/j/123456789",
    "https://us02web.zoom.us/rec/download/" + ZOOM_TOKEN,
    "https://us02web.zoom.us/rec/play/" + ZOOM_TOKEN + "/extra",
    "https://us02web.zoom.us/rec/play/",
    "https://us02web.zoom.us/rec/play/short",
])
def test_normalize_rejects_zoom_rooms_and_foreign_links(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


@pytest.mark.parametrize("value,expected", [
    (PANOPTO_CANONICAL, PANOPTO_CANONICAL),
    (PANOPTO_CANONICAL + "&mode=Viewer", PANOPTO_CANONICAL),
    ("https://demo.hosted.panopto.com/Panopto/Pages/Embed.aspx?id=" + PANOPTO_ID_FULL,
     "https://demo.hosted.panopto.com/Panopto/Pages/Embed.aspx?id=" + PANOPTO_ID_FULL),
    ("https://uni.panopto.eu/Panopto/Pages/Viewer.aspx?id=" + PANOPTO_ID_FULL,
     "https://uni.panopto.eu/Panopto/Pages/Viewer.aspx?id=" + PANOPTO_ID_FULL),
])
def test_normalize_accepts_panopto_session_forms(value, expected):
    assert normalize_media_url(value) == expected


@pytest.mark.parametrize("value", [
    None,
    "",
    "http://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=" + PANOPTO_ID_FULL,
    # Нужен поддомен учебной организации и литерал /Panopto в пути.
    "https://panopto.com/Panopto/Pages/Viewer.aspx?id=" + PANOPTO_ID_FULL,
    "https://demo.hosted.panopto.com/foo/Pages/Viewer.aspx?id=" + PANOPTO_ID_FULL,
    # View.aspx?vid= экстрактор не обслуживает, pid= — это плейлист.
    "https://demo.hosted.panopto.com/Panopto/Pages/View.aspx?vid=" + PANOPTO_ID_FULL,
    PANOPTO_CANONICAL + "&pid=other",
    # Идентификатор обязан быть полным и в нижнем регистре.
    "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=" + PANOPTO_ID_FULL.upper(),
    "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=26",
    "https://demo.hosted.panopto.com/Panopto/Pages/Sessions/List.aspx",
    "https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx",
])
def test_normalize_rejects_panopto_playlists_and_wrong_ids(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


@pytest.mark.parametrize("value,expected", [
    ("https://cdnapisec.kaltura.com/index.php/kwidget/wid/_691292/entry_id/0_c076mna6",
     KALTURA_CANONICAL),
    ("https://cdnapi.kaltura.com/index.php/kwidget/p/691292/entry_id/0_c076mna6", KALTURA_CANONICAL),
    ("https://www.kaltura.com/index.php/extwidget/preview/partner_id/691292/entry_id/0_c076mna6",
     KALTURA_CANONICAL),
    ("https://cdnapisec.kaltura.com/html5/html5lib/v2.101/mwEmbedFrame.php/wid/_691292/entry_id/0_c076mna6",
     KALTURA_CANONICAL),
])
def test_normalize_accepts_kaltura_widget_forms(value, expected):
    assert normalize_media_url(value) == expected


@pytest.mark.parametrize("value", [
    None,
    "",
    # Собственная форма yt-dlp входом не считается: собираем её сами.
    "kaltura:691292:0_c076mna6",
    "http://cdnapisec.kaltura.com/index.php/kwidget/wid/_691292/entry_id/0_c076mna6",
    # Учебные порталы и playManifest экстрактор не разбирает.
    "https://mediaspace.university.edu/media/Title/1_abc123",
    "https://cdnapi.kaltura.com/p/691292/sp/69129200/playManifest/entryId/0_c076mna6",
    # Нужны и партнёр, и запись в пути.
    "https://cdnapisec.kaltura.com/index.php/kwidget/wid/_691292",
    "https://cdnapisec.kaltura.com/index.php/kwidget/entry_id/0_c076mna6",
    "https://cdnapisec.kaltura.com/index.php/kwidget/wid/_691292/entry_id/0_C076MNA6",
    "https://evil.example/index.php/kwidget/wid/_691292/entry_id/0_c076mna6",
])
def test_normalize_rejects_kaltura_portals_and_incomplete_forms(value):
    with pytest.raises(MediaError) as error:
        normalize_media_url(value)
    assert str(error.value) == "invalid_media_url"


def test_zoom_video_mode_does_not_cap_screen_layout_height():
    # Экранная раскладка Zoom выше 1080, и предел кадра её бы отбросил.
    zoom = platform_of(ZOOM_CANONICAL)
    assert zoom.modes["video"].max_height is None
    assert zoom.modes["video"].selector == "b"
    assert platform_of(PANOPTO_CANONICAL).modes["video"].max_height == 1080


def test_validate_metadata_accepts_rutube_and_dzen_without_live_status():
    # Rutube отдаёт is_live, Дзен не отдаёт ни одного признака прямого эфира.
    formats = [{"format_id": "hls"}]
    rutube = {"id": RUTUBE_ID, "duration": 81, "extractor_key": "Rutube", "is_live": False,
              "formats": formats}
    assert validate_metadata(rutube, RUTUBE_ID, 14400, platform_of(RUTUBE_CANONICAL)) == {"duration": 81}
    dzen = {"id": DZEN_ID, "duration": 243, "extractor_key": "ZenYandex", "formats": formats}
    assert validate_metadata(dzen, DZEN_ID, 14400, platform_of(DZEN_CANONICAL)) == {"duration": 243}


def test_validate_metadata_rejects_recording_without_formats():
    # Пустой список форматов означает закрытую, удалённую или требующую входа запись.
    closed = {"id": DZEN_ID, "duration": 243, "extractor_key": "ZenYandex", "formats": []}
    with pytest.raises(MediaError) as error:
        validate_metadata(closed, DZEN_ID, 14400, platform_of(DZEN_CANONICAL))
    assert str(error.value) == "media_unavailable"


@pytest.mark.parametrize("fields,code", [
    ({"is_live": True}, "media_live"),
    ({"live_status": "is_live"}, "media_live"),
    # Незавершённый эфир приходит без длины.
    ({"duration": 0}, "invalid_duration"),
    ({"duration": None}, "invalid_duration"),
])
def test_validate_rutube_metadata_rejects_live_and_unfinished(fields, code):
    info = {"id": RUTUBE_ID, "duration": 81, "extractor_key": "Rutube", "is_live": False,
            "formats": [{"format_id": "hls"}], **fields}
    with pytest.raises(MediaError) as error:
        validate_metadata(info, RUTUBE_ID, 14400, platform_of(RUTUBE_CANONICAL))
    assert str(error.value) == code


def test_resolve_reports_platform_canonical_url_and_identity():
    platform, url, media_id = resolve_media_url("https://m.vk.com/video-77521_162222515?")
    assert platform.name == "vk"
    assert (url, media_id) == (VK_CANONICAL, VK_ID)
    platform, url, media_id = resolve_media_url("https://youtu.be/abcdefghijk")
    assert platform.name == "youtube"
    assert (url, media_id) == (CANONICAL, VIDEO)


def test_placeholder_names_carry_platform_and_stay_recognizable():
    assert placeholder_name("https://youtu.be/abcdefghijk") == "YouTube abcdefghijk"
    assert placeholder_name(VK_CANONICAL) == "VK Video -77521_162222515"
    assert placeholder_platform("YouTube abcdefghijk") == "youtube"
    assert placeholder_platform("VK Video -77521_162222515") == "vk"
    assert placeholder_platform("Lecture one") is None
    assert placeholder_platform("YouTube") is None
    assert platform_label("vk") == "work.vk"
    assert platform_label("youtube") == "work.youtube"
    assert platform_label(None) == "work.lecture"


def test_validate_metadata_accepts_known_finite_not_live_video():
    # Метаданные yt-dlp дают числовую длительность, а не строку.
    assert validate_metadata(metadata(duration=100), VIDEO, 14400, YOUTUBE) == {"duration": 100}
    assert validate_metadata(metadata(duration=14400.0, live_status="was_live"), VIDEO, 14400,
                             YOUTUBE) == {"duration": 14400.0}


@pytest.mark.parametrize("fields,code", [
    ({"live_status": "is_live"}, "media_live"),
    ({"live_status": "is_upcoming"}, "media_live"),
    ({"live_status": "post_live"}, "media_live"),
    ({"is_live": True}, "media_live"),
    ({"is_upcoming": True}, "media_live"),
    ({"live_status": None}, "media_live"),
    ({"availability": "private"}, "media_unavailable"),
    ({"availability": "premium_only"}, "media_unavailable"),
    ({"duration": None}, "invalid_duration"),
    ({"duration": 0}, "invalid_duration"),
    ({"duration": -5}, "invalid_duration"),
    ({"duration": "abc"}, "invalid_duration"),
    ({"duration": float("nan")}, "invalid_duration"),
    ({"duration": float("inf")}, "invalid_duration"),
    ({"duration": True}, "invalid_duration"),
    ({"duration": 14401}, "media_too_long"),
    ({"id": "zzzzzzzzzzz"}, "invalid_media_metadata"),
    ({"_type": "playlist"}, "invalid_media_metadata"),
    ({"extractor_key": "VK"}, "invalid_media_metadata"),
    ({"extractor_key": "Generic"}, "invalid_media_metadata"),
    ({"entries": [{}]}, "invalid_media_metadata"),
])
def test_validate_metadata_rejects_live_private_and_unknown(fields, code):
    with pytest.raises(MediaError) as error:
        validate_metadata(metadata(**fields), VIDEO, 14400, YOUTUBE)
    assert str(error.value) == code


def test_validate_metadata_accepts_vk_video_without_live_status():
    # VKIE не сообщает live_status и availability: проверка опирается на is_live.
    assert validate_metadata(vk_metadata(duration=195, title="Lecture"),
                             VK_ID, 14400, VK) == {"duration": 195, "title": "Lecture"}


@pytest.mark.parametrize("fields,code", [
    ({"is_live": True}, "media_live"),
    ({"live_status": "is_live"}, "media_live"),
    ({"id": "205387401_165548505"}, "invalid_media_metadata"),
    ({"extractor_key": "Youtube"}, "invalid_media_metadata"),
    ({"_type": "url"}, "invalid_media_metadata"),
    ({"entries": [{}]}, "invalid_media_metadata"),
    ({"duration": None}, "invalid_duration"),
    ({"duration": 14401}, "media_too_long"),
])
def test_validate_vk_metadata_rejects_live_foreign_and_unknown(fields, code):
    with pytest.raises(MediaError) as error:
        validate_metadata(vk_metadata(**fields), VK_ID, 14400, VK)
    assert str(error.value) == code


PROXY = "http://127.0.0.1:1081"


def work_root(tmp_path):
    root = tmp_path / "work"
    (root / "tmp").mkdir(parents=True)
    return root


def test_download_changes_route_once_after_network_failure(tmp_path, monkeypatch):
    calls = []

    def fake_attempt(root, directory, proxy, url, mode, byte_limit, max_seconds, max_work, min_free):
        calls.append(proxy)
        if proxy is not None:
            raise MediaError("media_failed")
        return {"relative_path": "tmp/media-x/media.mkv", "duration": 1.0}

    monkeypatch.setattr(media_module, "_attempt", fake_attempt)
    result = media_module.download(CANONICAL, "video", 1000, 10, 10 ** 9, 0,
                                   proxy=PROXY, work_root=work_root(tmp_path))
    assert result["duration"] == 1.0
    # Прокси попробован первым, затем маршрут сменился на прямое подключение.
    assert calls == [PROXY, None]


def test_download_does_not_repeat_metadata_failure_on_second_route(tmp_path, monkeypatch):
    calls = []

    def fake_attempt(root, directory, proxy, url, mode, byte_limit, max_seconds, max_work, min_free):
        calls.append(proxy)
        raise MediaError("invalid_media_metadata")

    monkeypatch.setattr(media_module, "_attempt", fake_attempt)
    with pytest.raises(MediaError) as error:
        media_module.download(CANONICAL, "video", 1000, 10, 10 ** 9, 0,
                              proxy=PROXY, work_root=work_root(tmp_path))
    assert str(error.value) == "invalid_media_metadata"
    assert calls == [PROXY]


def test_download_without_proxy_uses_single_direct_route(tmp_path, monkeypatch):
    calls = []

    def fake_attempt(root, directory, proxy, url, mode, byte_limit, max_seconds, max_work, min_free):
        calls.append(proxy)
        raise MediaError("media_failed")

    monkeypatch.setattr(media_module, "_attempt", fake_attempt)
    with pytest.raises(MediaError):
        media_module.download(CANONICAL, "video", 1000, 10, 10 ** 9, 0, work_root=work_root(tmp_path))
    assert calls == [None]


def test_download_rejects_malformed_proxy_before_filesystem():
    with pytest.raises(MediaError) as error:
        media_module.download(CANONICAL, "video", 10, 10, 10, 10, proxy="socks5://127.0.0.1:1080")
    assert str(error.value) == "invalid_media_url"


def test_main_forwards_proxy_argument(monkeypatch):
    captured = {}

    def fake_download(url, mode, byte_limit, max_seconds, max_work, min_free, *, proxy=None, work_root=None):
        captured.update({"url": url, "mode": mode, "proxy": proxy})
        return {"ok": True}

    monkeypatch.setattr(media_module, "download", fake_download)
    assert media_module.main([CANONICAL, "video", "10", "10", "10", "10", PROXY]) == 0
    assert captured == {"url": CANONICAL, "mode": "video", "proxy": PROXY}
    assert media_module.main([CANONICAL, "video", "10", "10", "10", "10"]) == 0
    assert captured["proxy"] is None


def test_download_budget_stops_stream_without_content_length(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    inside = directory / "media.f137.mp4"
    inside.write_bytes(b"x" * 100)
    budget = DownloadBudget(directory, 1000)
    budget({"filename": str(inside), "downloaded_bytes": 100})
    with pytest.raises(MediaError) as error:
        budget({"filename": str(inside), "downloaded_bytes": 1001})
    assert str(error.value) == "input_too_large"


def test_download_budget_checks_real_tree_growth(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    (directory / "media.f137.mp4").write_bytes(b"x" * 3000)
    budget = DownloadBudget(directory, 1000)
    with pytest.raises(MediaError) as error:
        budget({"downloaded_bytes": 10})
    assert str(error.value) == "input_too_large"


def test_download_budget_rejects_progress_outside_tmp(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    outside = tmp_path / "escape.mp4"
    outside.write_bytes(b"x")
    budget = DownloadBudget(directory, 1000)
    with pytest.raises(MediaError) as error:
        budget({"filename": str(outside), "downloaded_bytes": 1})
    assert str(error.value) == "unsafe_file_path"


def test_tree_bytes_counts_files_and_rejects_specials(tmp_path):
    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "a").write_bytes(b"12345")
    (root / "nested" / "b").write_bytes(b"67")
    assert tree_bytes(root) == 7

    os.mkfifo(root / "pipe")
    with pytest.raises(MediaError) as error:
        tree_bytes(root)
    assert str(error.value) == "unsafe_file_path"


@pytest.mark.parametrize("url", ["short", "https://youtu.be/abcdefghij", "https://vk.com/video-1",
                                 "https://vk.com/clip-77521_162222515"])
def test_download_refuses_unknown_url_before_filesystem(url):
    with pytest.raises(MediaError) as error:
        download(url, "video", 10, 10, 10, 10)
    assert str(error.value) == "invalid_media_url"


@pytest.mark.parametrize("url", [CANONICAL, VK_CANONICAL])
def test_download_refuses_unknown_mode_before_filesystem(url):
    with pytest.raises(MediaError) as error:
        download(url, "both", 10, 10, 10, 10)
    assert str(error.value) == "invalid_media_url"


def test_validate_metadata_reports_actual_title_but_omits_absent_one():
    # Заголовок приходит только из фактических метаданных, не из идентификатора.
    assert validate_metadata(metadata(duration=100, title="Lecture 1"), VIDEO, 14400, YOUTUBE) == {
        "duration": 100, "title": "Lecture 1"}
    assert validate_metadata(metadata(duration=100), VIDEO, 14400, YOUTUBE) == {"duration": 100}


def test_clean_title_strips_controls_trims_and_bounds():
    assert clean_title("  Line\x00 one  ") == "Line one"
    assert clean_title("x" * 500) == "x" * 300
    assert clean_title(None) is None
    assert clean_title("   ") is None
    assert clean_title(42) is None


def test_split_video_stream_progress_never_uses_one_stream_total(tmp_path):
    from absorbobot.workers.progress import ProgressWriter, read_progress

    root = tmp_path / "work"
    directory = root / "tmp" / "media"
    directory.mkdir(parents=True)
    video, audio = directory / "video.mp4", directory / "audio.m4a"
    video.write_bytes(b"x" * 60)
    audio.write_bytes(b"x" * 10)
    writer = ProgressWriter(root, "media", "bytes", min_interval=0)
    budget = DownloadBudget(directory, 1000, report=writer.update)
    budget({"filename": str(video), "downloaded_bytes": 60, "total_bytes": 60})
    budget({"filename": str(audio), "downloaded_bytes": 10, "total_bytes": 20})
    current = read_progress(root, "media", "bytes")
    assert current["completed"] == 70
    assert current["total"] is None
