"""Скачать ровно одно YouTube-видео без пользовательской конфигурации."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4


class YouTubeError(ValueError):
    """Отказ с безопасным кодом без URL потоков и ответов сервера."""


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def normalize_youtube_url(value: str) -> str:
    """Разобрать только разрешённые HTTPS-формы и построить собственный URL."""
    try:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
                or parsed.port not in (None, 443)):
            raise ValueError
        # Запретить двусмысленные escaped paths и любые playlist-параметры.
        if "%" in parsed.path or "\\" in parsed.netloc or "\\" in parsed.path:
            raise ValueError
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        keys = [key for key, _ in query]
        if len(keys) != len(set(keys)) or any(key in {"list", "index", "playlist"} for key in keys):
            raise ValueError
        if any(key not in {"v", "t", "start", "si", "feature"} for key in keys):
            raise ValueError
        parts = parsed.path.split("/")
        if parsed.hostname == "youtu.be":
            if len(parts) != 2 or "v" in keys:
                raise ValueError
            video_id = parts[1]
        elif parsed.path == "/watch":
            if keys.count("v") != 1:
                raise ValueError
            video_id = dict(query)["v"]
        elif len(parts) == 3 and parts[1] in {"shorts", "embed"} and "v" not in keys:
            video_id = parts[2]
        else:
            raise ValueError
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError
        return "https://www.youtube.com/watch?v=" + video_id
    except (ValueError, TypeError, AttributeError):
        raise YouTubeError("invalid_youtube_url") from None


_TITLE_LIMIT = 300


def clean_title(value) -> str | None:
    """Оставить человекочитаемый заголовок без управляющих знаков или ``None``."""
    if not isinstance(value, str):
        return None
    title = "".join(char for char in value if char >= " " and char != "\x7f").strip()
    if not title:
        return None
    return title[:_TITLE_LIMIT]


def validate_metadata(info: dict, video_id: str, max_seconds: float) -> dict:
    """Не начинать загрузку эфира, плейлиста или записи неизвестной длины."""
    if (not isinstance(info, dict) or info.get("_type", "video") != "video"
            or info.get("id") != video_id or info.get("entries") is not None
            or info.get("extractor_key") not in (None, "Youtube")):
        raise YouTubeError("invalid_youtube_metadata")
    if (info.get("is_live") or info.get("is_upcoming")
            or info.get("live_status") not in ("not_live", "was_live")):
        raise YouTubeError("youtube_live")
    if info.get("availability") not in (None, "public", "unlisted"):
        raise YouTubeError("youtube_unavailable")
    duration = info.get("duration")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise YouTubeError("invalid_duration")
    if duration > max_seconds:
        raise YouTubeError("media_too_long")
    result = {"duration": duration}
    title = clean_title(info.get("title"))
    if title is not None:
        # Заголовок берём из фактических метаданных, а не из идентификатора видео.
        result["title"] = title
    return result


def tree_bytes(root: Path) -> int:
    """Считать реальные файлы, не следуя ссылкам из недоверенного дерева."""
    total, pending = 0, [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    raise YouTubeError("unsafe_file_path")
    return total


class DownloadBudget:
    """Ограничить фактический поток, включая HTTP без Content-Length."""

    def __init__(self, directory: Path, limit: int, report=None):
        self.directory, self.limit = directory.resolve(), limit
        self.report = report
        self.downloaded: dict[str, int] = {}

    def __call__(self, progress: dict) -> None:
        filename = progress.get("filename") or progress.get("tmpfilename")
        if filename:
            path = Path(filename).resolve()
            if not path.is_relative_to(self.directory):
                raise YouTubeError("unsafe_file_path")
            value = progress.get("downloaded_bytes")
            if type(value) is int and value >= 0:
                self.downloaded[str(path)] = max(self.downloaded.get(str(path), 0), value)
        if self.report is not None:
            # Форматы видео и аудио приходят отдельно; их общий размер заранее неизвестен.
            self.report(sum(self.downloaded.values()), None)
        if sum(self.downloaded.values()) > self.limit or tree_bytes(self.directory) > 2 * self.limit:
            raise YouTubeError("input_too_large")


class _QuietLogger:
    # Подписанные stream URLs и внешние ошибки не попадают в stdout/stderr бота.
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def _progress_module():
    """Загрузить модуль прогресса и в пакете, и при запуске как скрипта."""
    try:
        from . import progress as module
    except ImportError:
        import progress as module  # type: ignore[no-redef]
    return module


def _download(video_id: str, mode: str, byte_limit: int, max_seconds: float,
              directory: Path) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.globals import all_plugins_loaded, plugin_dirs
    # Library API не читает CLI config; глобальная загрузка plugins отключена явно.
    plugin_dirs.value = []
    all_plugins_loaded.value = True
    from yt_dlp.extractor.youtube import YoutubeIE

    reporter = _progress_module().ProgressWriter(directory.parent.parent, "youtube", "bytes",
                                                 min_interval=0.5)
    budget = DownloadBudget(directory, byte_limit, report=reporter.update)
    options = {
        "quiet": True, "no_warnings": True, "logger": _QuietLogger(),
        "noplaylist": True, "cachedir": False, "cookiefile": None,
        "cookiesfrombrowser": None, "enable_file_urls": False,
        "js_runtimes": {"bun": {"path": "/usr/bin/bun"}}, "remote_components": [],
        "outtmpl": str(directory / "media.%(ext)s"), "restrictfilenames": True,
        "paths": {"home": str(directory), "temp": str(directory)},
        "progress_hooks": [budget], "max_filesize": byte_limit,
        "concurrent_fragment_downloads": 1, "socket_timeout": 30,
        "retries": 3, "fragment_retries": 3, "overwrites": False,
        "format": "bv[height<=1080]+ba/b[height<=1080]" if mode == "video" else "bestaudio",
        "merge_output_format": "mkv",
        "postprocessors": ([{"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}]
                           if mode == "video" else
                           [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]),
    }
    with YoutubeDL(options, auto_init=False) as downloader:
        downloader.add_info_extractor(YoutubeIE())
        info = downloader.extract_info("https://www.youtube.com/watch?v=" + video_id,
                                       download=False, process=False)
        metadata = validate_metadata(info, video_id, max_seconds)
        # Обработка только уже проверенного результата YoutubeIE, без повторного arbitrary URL.
        result = downloader.process_ie_result(info, download=True)
        if mode == "video" and (type(result.get("height")) not in (int, float)
                                 or not 0 < result["height"] <= 1080):
            raise YouTubeError("invalid_youtube_metadata")
    output = directory / ("media.mkv" if mode == "video" else "media.m4a")
    if output.is_symlink() or not output.is_file() or output.stat().st_size <= 0:
        raise YouTubeError("youtube_failed")
    if output.stat().st_size > byte_limit:
        raise YouTubeError("input_too_large")
    budget({})
    return {"relative_path": str(output.relative_to(directory.parent.parent)), **metadata}


def lookup(video_id: str, max_seconds: float) -> dict:
    """Прочитать метаданные записи без загрузки файла для восстановления подписи."""
    if not VIDEO_ID.fullmatch(video_id):
        raise YouTubeError("invalid_youtube_url")
    from yt_dlp import YoutubeDL
    from yt_dlp.globals import all_plugins_loaded, plugin_dirs
    plugin_dirs.value = []
    all_plugins_loaded.value = True
    from yt_dlp.extractor.youtube import YoutubeIE

    options = {
        "quiet": True, "no_warnings": True, "logger": _QuietLogger(),
        "noplaylist": True, "cachedir": False, "cookiefile": None,
        "cookiesfrombrowser": None, "enable_file_urls": False,
        "js_runtimes": {"bun": {"path": "/usr/bin/bun"}}, "remote_components": [],
        "socket_timeout": 30, "retries": 3,
    }
    with YoutubeDL(options, auto_init=False) as downloader:
        downloader.add_info_extractor(YoutubeIE())
        info = downloader.extract_info("https://www.youtube.com/watch?v=" + video_id,
                                       download=False, process=False)
        return validate_metadata(info, video_id, max_seconds)


def _stop_child(process: subprocess.Popen) -> None:
    """Завершить downloader и ffmpeg; дождаться child до возврата worker."""
    import psutil
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    _, alive = psutil.wait_procs(children, timeout=0)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=10)


def download(video_id: str, mode: str, byte_limit: int, max_seconds: float,
             max_work_bytes: int, min_free_bytes: int, *, work_root: Path = Path("/work")) -> dict:
    if not VIDEO_ID.fullmatch(video_id) or mode not in ("video", "audio"):
        raise YouTubeError("invalid_youtube_url")
    root = work_root.resolve(strict=True)
    directory = root / "tmp" / ("youtube-" + uuid4().hex)
    directory.mkdir(mode=0o700)
    result_path = directory / "result.json"
    command = [sys.executable, str(Path(__file__).absolute()), "--child", video_id,
               mode, str(byte_limit), str(max_seconds), str(directory)]
    with (directory / "downloader.stdout.log").open("xb") as stdout, (directory / "downloader.stderr.log").open("xb") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr)
    try:
        while True:
            if tree_bytes(directory) > 2 * byte_limit:
                raise YouTubeError("input_too_large")
            if tree_bytes(root) > max_work_bytes:
                raise YouTubeError("work_limit")
            usage = os.statvfs(root)
            if usage.f_bavail * usage.f_frsize < min_free_bytes:
                raise YouTubeError("storage_unavailable")
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if not result_path.is_file() or result_path.stat().st_size > 16384:
            raise YouTubeError("youtube_failed")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if process.returncode or not result.get("ok"):
            raise YouTubeError(result.get("error", "youtube_failed"))
        output = (root / result["relative_path"]).resolve(strict=True)
        if not output.is_relative_to(directory) or output.stat().st_size > byte_limit:
            raise YouTubeError("input_too_large")
        return result
    except BaseException:
        # Неудачная загрузка не оставляет progress-файл стадии в дереве задания.
        _progress_module().discard_progress(root, "youtube")
        raise
    finally:
        # Не оставлять дочернюю extraction/merge после отказа guard или сигнала.
        _stop_child(process)


def _result_error(exc: Exception) -> dict:
    code = str(exc) if isinstance(exc, YouTubeError) else "youtube_failed"
    return {"ok": False, "error": code}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--metadata":
        try:
            if len(args) != 3:
                raise YouTubeError("youtube_failed")
            result = {"ok": True, **lookup(args[1], float(args[2]))}
        except Exception as exc:
            result = _result_error(exc)
        print(json.dumps(result, allow_nan=False))
        return 0 if result["ok"] else 2
    if args and args[0] == "--child":
        directory = Path(args[-1])
        try:
            if len(args) != 6:
                raise YouTubeError("youtube_failed")
            result = {"ok": True, **_download(args[1], args[2], int(args[3]), float(args[4]), directory)}
        except Exception as exc:
            detail = re.sub(r"(?<=://)[^\s/]+@", "<redacted>@", str(exc))
            print(f"{type(exc).__name__}: {detail}", file=sys.stderr)
            result = _result_error(exc)
        temporary = directory / "result.tmp"
        temporary.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        temporary.replace(directory / "result.json")
        return 0 if result["ok"] else 2
    try:
        if len(args) != 6:
            raise YouTubeError("youtube_failed")
        result = download(args[0], args[1], int(args[2]), float(args[3]), int(args[4]), int(args[5]))
        print(json.dumps(result, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps(_result_error(exc), allow_nan=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
