"""Границы разбора ссылок, метаданных и фактического потока загрузки YouTube."""

from __future__ import annotations

import os

import pytest

from absorbobot.workers.youtube import (
    DownloadBudget,
    YouTubeError,
    download,
    normalize_youtube_url,
    tree_bytes,
    validate_metadata,
)


CANONICAL = "https://www.youtube.com/watch?v=abcdefghijk"
VIDEO = "abcdefghijk"


def metadata(**fields):
    base = {"id": VIDEO, "_type": "video", "live_status": "not_live",
            "duration": 100, "availability": "public", "extractor_key": "Youtube"}
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
    assert normalize_youtube_url(value) == CANONICAL


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
    with pytest.raises(YouTubeError) as error:
        normalize_youtube_url(value)
    assert str(error.value) == "invalid_youtube_url"


def test_validate_metadata_accepts_known_finite_not_live_video():
    # Метаданные yt-dlp дают числовую длительность, а не строку.
    assert validate_metadata(metadata(duration=100), VIDEO, 14400) == {"duration": 100}
    assert validate_metadata(metadata(duration=14400.0, live_status="was_live"), VIDEO, 14400) == {"duration": 14400.0}


@pytest.mark.parametrize("fields,code", [
    ({"live_status": "is_live"}, "youtube_live"),
    ({"live_status": "is_upcoming"}, "youtube_live"),
    ({"live_status": "post_live"}, "youtube_live"),
    ({"is_live": True}, "youtube_live"),
    ({"is_upcoming": True}, "youtube_live"),
    ({"live_status": None}, "youtube_live"),
    ({"availability": "private"}, "youtube_unavailable"),
    ({"availability": "premium_only"}, "youtube_unavailable"),
    ({"duration": None}, "invalid_duration"),
    ({"duration": 0}, "invalid_duration"),
    ({"duration": -5}, "invalid_duration"),
    ({"duration": "abc"}, "invalid_duration"),
    ({"duration": float("nan")}, "invalid_duration"),
    ({"duration": float("inf")}, "invalid_duration"),
    ({"duration": True}, "invalid_duration"),
    ({"duration": 14401}, "media_too_long"),
    ({"id": "zzzzzzzzzzz"}, "invalid_youtube_metadata"),
    ({"_type": "playlist"}, "invalid_youtube_metadata"),
    ({"extractor_key": "Generic"}, "invalid_youtube_metadata"),
    ({"entries": [{}]}, "invalid_youtube_metadata"),
])
def test_validate_metadata_rejects_live_private_and_unknown(fields, code):
    with pytest.raises(YouTubeError) as error:
        validate_metadata(metadata(**fields), VIDEO, 14400)
    assert str(error.value) == code


def test_download_budget_stops_stream_without_content_length(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    inside = directory / "media.f137.mp4"
    inside.write_bytes(b"x" * 100)
    budget = DownloadBudget(directory, 1000)
    budget({"filename": str(inside), "downloaded_bytes": 100})
    with pytest.raises(YouTubeError) as error:
        budget({"filename": str(inside), "downloaded_bytes": 1001})
    assert str(error.value) == "input_too_large"


def test_download_budget_checks_real_tree_growth(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    (directory / "media.f137.mp4").write_bytes(b"x" * 3000)
    budget = DownloadBudget(directory, 1000)
    with pytest.raises(YouTubeError) as error:
        budget({"downloaded_bytes": 10})
    assert str(error.value) == "input_too_large"


def test_download_budget_rejects_progress_outside_tmp(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    outside = tmp_path / "escape.mp4"
    outside.write_bytes(b"x")
    budget = DownloadBudget(directory, 1000)
    with pytest.raises(YouTubeError) as error:
        budget({"filename": str(outside), "downloaded_bytes": 1})
    assert str(error.value) == "unsafe_file_path"


def test_tree_bytes_counts_files_and_rejects_specials(tmp_path):
    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "a").write_bytes(b"12345")
    (root / "nested" / "b").write_bytes(b"67")
    assert tree_bytes(root) == 7

    os.mkfifo(root / "pipe")
    with pytest.raises(YouTubeError) as error:
        tree_bytes(root)
    assert str(error.value) == "unsafe_file_path"


def test_download_refuses_unknown_video_id_and_mode_before_filesystem():
    with pytest.raises(YouTubeError) as error:
        download("short", "video", 10, 10, 10, 10)
    assert str(error.value) == "invalid_youtube_url"
    with pytest.raises(YouTubeError) as error:
        download(VIDEO, "both", 10, 10, 10, 10)
    assert str(error.value) == "invalid_youtube_url"
