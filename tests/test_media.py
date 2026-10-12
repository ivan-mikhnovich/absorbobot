"""Границы разбора ссылок площадок, метаданных и фактического потока загрузки."""

from __future__ import annotations

import os

import pytest

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


def platform_of(url: str):
    """Правила площадки, которой принадлежит канонический URL."""
    return resolve_media_url(url)[0]


YOUTUBE = platform_of(CANONICAL)
VK = platform_of(VK_CANONICAL)


def metadata(**fields):
    base = {"id": VIDEO, "_type": "video", "live_status": "not_live",
            "duration": 100, "availability": "public", "extractor_key": "Youtube"}
    base.update(fields)
    return base


def vk_metadata(**fields):
    # Метаданные VKIE: идентификатор из URL, is_live вместо live_status и availability.
    base = {"id": VK_ID, "duration": 195, "extractor_key": "VK", "is_live": False}
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
    "https://vk.com/video-77521_162222515/",
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
