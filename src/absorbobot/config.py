"""Настройки, защищённое окружение и проверки установленного ядра."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import psutil
import yaml


class ConfigError(Exception):
    """Безопасная диагностика без содержимого секретов или ответов API."""


@dataclass(frozen=True)
class Settings:
    """Пути и согласованные ограничения; секреты здесь не хранятся."""

    admin_ids: tuple[int, ...] = (581047912,)
    storage_dir: Path = Path("/media/storage/absorbobot")
    state_dir: Path = Path("~/.local/state/absorbobot")
    absorbent_config: Path = Path("~/.config/absorbent/config.yaml")
    bot_api_url: str = "https://api.telegram.org"
    quota_jobs: int = 10
    quota_window_seconds: int = 86400
    max_media_bytes: int = 2000000000
    max_media_seconds: int = 14400
    max_material_bytes: int = 100000000
    max_material_pages: int = 100
    telegram_part_bytes: int = 49000000
    min_free_bytes: int = 50000000000
    max_work_bytes: int = 100000000000
    max_processing_seconds: int = 21600
    metrics_interval_seconds: float = 1
    download_proxy: str | None = None
    backing_mount: Path = Path("/media/storage")
    secrets_file: Path = Path("~/.config/absorbobot/secrets.env")
    api_binary: Path = Path("~/.local/libexec/absorbobot/bin/telegram-bot-api")
    config_path: Path = Path("~/.config/absorbobot/config.yaml")

    def __post_init__(self) -> None:
        for name in _PATH_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ConfigError(f"Некорректный путь: {name}.")
            path = Path(value).expanduser().absolute()
            if any(c in str(path) for c in ("\n", "\r", "\x00")):
                raise ConfigError(f"Некорректный путь: {name}.")
            object.__setattr__(self, name, path)
        if not isinstance(self.admin_ids, (list, tuple)) or not self.admin_ids:
            raise ConfigError("admin_ids должен содержать хотя бы одного администратора.")
        if any(type(value) is not int or value <= 0 for value in self.admin_ids):
            raise ConfigError("admin_ids должен содержать положительные целые ID.")
        object.__setattr__(self, "admin_ids", tuple(dict.fromkeys(self.admin_ids)))
        for name in _INTEGER_FIELDS:
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ConfigError(f"{name} должен быть положительным целым числом.")
        interval = self.metrics_interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, (float, int)) or not math.isfinite(interval) or interval <= 0:
            raise ConfigError("metrics_interval_seconds должен быть конечным положительным числом.")
        _validate_api_url(self.bot_api_url)
        object.__setattr__(self, "download_proxy", _validate_download_proxy(self.download_proxy))
        mount = self.backing_mount.resolve()
        storage = self.storage_dir.resolve()
        if mount == Path("/") or not storage.is_relative_to(mount) or storage == mount:
            raise ConfigError("storage_dir должен находиться внутри явно указанного backing_mount, не корневого диска.")
        if self.state_dir.resolve().is_relative_to(mount):
            raise ConfigError("state_dir должен находиться на SSD вне backing_mount.")
        if self.secrets_file.resolve().is_relative_to(mount):
            raise ConfigError("secrets_file должен находиться на SSD вне backing_mount.")
        maximum = 2000000000 if self.bot_api_url != "https://api.telegram.org" else 50000000
        if self.telegram_part_bytes > maximum:
            raise ConfigError("telegram_part_bytes превышает предел выбранного Bot API.")


_PATH_FIELDS = ("storage_dir", "state_dir", "absorbent_config", "backing_mount", "secrets_file", "api_binary", "config_path")
_INTEGER_FIELDS = ("quota_jobs", "quota_window_seconds", "max_media_bytes", "max_media_seconds", "max_material_bytes", "max_material_pages", "telegram_part_bytes", "min_free_bytes", "max_work_bytes", "max_processing_seconds")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REQUIRED_TOOLS = ("uv", "bwrap", "ffmpeg", "ffprobe", "pdfinfo", "pdftoppm", "soffice", "pandoc", "lualatex", "kpsewhich", "bun")


def _positive_int(value: object) -> bool:
    """Отличить положительное целое от bool и остальных типов."""
    return type(value) is int and value > 0


def _validate_api_url(value: str) -> None:
    if value == "https://api.telegram.org":
        return
    try:
        if not isinstance(value, str) or any(c in value for c in ("\n", "\r", "\t", "\x00")):
            raise ValueError
        url = urlsplit(value)
        valid = (url.scheme == "http" and url.hostname is not None
                 and ipaddress.ip_address(url.hostname).is_loopback and url.port == 8081
                 and url.username is None and url.password is None
                 and url.path in ("", "/") and not url.query and not url.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ConfigError("bot_api_url должен быть https://api.telegram.org или HTTP loopback адресом на порту 8081 без credentials, пути или параметров.")


def _validate_download_proxy(value: object) -> str | None:
    """Проверить необязательный прокси загрузок: только http(s) без credentials.

    Прокси применяется и к публичным ссылкам, и к загрузкам площадок, поэтому
    поддерживается ровно то, что умеют оба транспорта. Учётные данные
    запрещены: значение попадает в аргументы дочернего процесса и видно
    в списке процессов.
    """
    if value is None:
        return None
    valid = False
    if isinstance(value, str) and value and value.strip() == value \
            and not any(c in value for c in ("\n", "\r", "\t", "\x00")):
        try:
            url = urlsplit(value)
            valid = (url.scheme in ("http", "https") and url.hostname is not None
                     and url.port is not None and url.username is None and url.password is None
                     and url.path in ("", "/") and not url.query and not url.fragment)
        except ValueError:
            valid = False
    if not valid:
        raise ConfigError("download_proxy должен быть адресом прокси вида http://host:port без credentials, пути и параметров.")
    return value


def load_settings(path: str | Path) -> Settings:
    """Прочитать YAML без неявного поиска другой конфигурации."""
    chosen = Path(path).expanduser().absolute()
    try:
        raw = yaml.safe_load(chosen.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("Не удалось прочитать YAML настроек Absorbobot.") from None
    if not isinstance(raw, dict):
        raise ConfigError("Настройки должны быть YAML-словарём.")
    allowed = {field.name for field in fields(Settings)} - {"config_path"}
    if any(not isinstance(key, str) or key not in allowed for key in raw):
        raise ConfigError("В настройках есть неизвестные поля.")
    if "admin_ids" not in raw:
        raise ConfigError("Не задан обязательный admin_ids.")
    return Settings(**raw, config_path=chosen)


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_storage(settings: Settings) -> dict:
    """Проверить настоящий mount до создания файлов и без чтения секретов."""
    mount = settings.backing_mount.resolve()
    storage = settings.storage_dir.resolve()
    if not mount.is_dir() or not os.path.ismount(mount):
        raise ConfigError("backing_mount не смонтирован; приём и обработка остановлены.")
    if not storage.is_relative_to(mount) or storage == mount:
        raise ConfigError("storage_dir вышел за backing_mount.")
    existing = _existing_parent(storage)
    partitions = psutil.disk_partitions(all=True)
    candidates = [p for p in partitions if existing.is_relative_to(Path(p.mountpoint).resolve())]
    if not candidates:
        raise ConfigError("Не удалось определить файловую систему storage_dir.")
    actual = max(candidates, key=lambda p: len(Path(p.mountpoint).resolve().parts))
    if Path(actual.mountpoint).resolve() != mount:
        raise ConfigError("storage_dir находится на другом backing mount.")
    if not existing.is_dir() or not os.access(existing, os.W_OK | os.X_OK):
        raise ConfigError("storage_dir недоступен для записи.")
    try:
        info = os.statvfs(existing)
        free = info.f_bavail * info.f_frsize
    except OSError:
        raise ConfigError("Не удалось проверить свободное место storage_dir.") from None
    if info.f_flag & os.ST_RDONLY:
        raise ConfigError("Файловая система storage_dir доступна только для чтения.")
    if free < settings.min_free_bytes:
        raise ConfigError("На storage_dir недостаточно свободного места.")
    return {"mount": str(mount), "writable": True, "free_bytes": free}


def create_work_tree(settings: Settings, work_uuid: str) -> Path:
    """Создать только собственное UUID-дерево после проверки диска."""
    try:
        valid = str(UUID(work_uuid)) == work_uuid
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ConfigError("Некорректный UUID рабочего каталога.")
    check_storage(settings)
    root = settings.storage_dir / "jobs" / work_uuid
    if root.parent.is_symlink() or not root.resolve().is_relative_to(settings.storage_dir.resolve()) or root.is_symlink():
        raise ConfigError("Рабочий каталог вышел за storage_dir.")
    for child in ("input", "output", "tmp"):
        target = root / child
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise ConfigError("Рабочий подкаталог содержит небезопасную ссылку.")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _parse_envfile(text: str) -> dict[str, str]:
    """Разобрать EnvironmentFile без shell, интерполяции или исполнения команд."""
    result: dict[str, str] = {}
    lines = iter(text.splitlines(keepends=True))
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if "=" not in line:
            raise ConfigError("Некорректное присваивание в secrets_file.")
        name, raw = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ConfigError("Некорректное имя переменной в secrets_file.")
        raw = raw.lstrip(" \t\r")
        quote = raw[0] if raw.startswith(("'", '"')) else ""
        index = 1 if quote else 0
        chars: list[str] = []
        closed = not quote
        while index < len(raw):
            char = raw[index]
            if quote and char == quote:
                if raw[index + 1:].strip():
                    raise ConfigError("Лишние символы после кавычек в secrets_file.")
                closed = True
                break
            if char == "\\" and quote != "'":
                if index + 1 == len(raw):
                    raise ConfigError("Незавершённое экранирование в secrets_file.")
                following = raw[index + 1]
                if following == "\n":
                    try:
                        raw += next(lines)
                    except StopIteration:
                        pass
                elif not quote or following in ('"', "\\", "$", "`"):
                    chars.append(following)
                else:
                    chars.extend(("\\", following))
                index += 2
                continue
            if char == "\n":
                if not quote:
                    break
                try:
                    raw += next(lines)
                except StopIteration:
                    raise ConfigError("Незакрытые кавычки в secrets_file.") from None
            chars.append(char)
            index += 1
        if not closed:
            raise ConfigError("Незакрытые кавычки в secrets_file.")
        value = "".join(chars)
        if "\x00" in value:
            raise ConfigError("Недопустимый символ в secrets_file.")
        result[name] = value if quote else value.rstrip(" \t\r")
    return result


def load_secrets(settings: Settings) -> dict[str, str]:
    """Прочитать принадлежащий владельцу файл 0600; окружение имеет приоритет."""
    try:
        fd = os.open(settings.secrets_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise ConfigError("Не удалось открыть защищённый secrets_file.") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigError("secrets_file должен быть обычным файлом владельца с правами 0600.")
        if info.st_size > 1048576:
            raise ConfigError("secrets_file имеет недопустимый размер.")
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1
            parsed = _parse_envfile(handle.read())
    except (OSError, UnicodeError):
        raise ConfigError("Не удалось прочитать secrets_file.") from None
    finally:
        if fd != -1:
            os.close(fd)
    return {**parsed, **os.environ}


def require_key_names(snapshot: Mapping, env: Mapping[str, str] | None = None) -> None:
    """Проверить имена ключей выбранных моделей, не принимая значения в snapshot."""
    environment = os.environ if env is None else env
    models = snapshot.get("models", snapshot)
    names: set[str] = set()
    if not isinstance(models, Mapping):
        raise ConfigError("Некорректный snapshot моделей.")
    for category in ("stt", "vision", "llm"):
        model = models.get(category)
        if not isinstance(model, Mapping):
            continue
        provider = model.get("provider", {})
        if not isinstance(provider, Mapping):
            raise ConfigError("Некорректный snapshot провайдера.")
        name = provider.get("api_key_env", "")
        if name:
            if not isinstance(name, str) or not _ENV_NAME.fullmatch(name) or name in {"ABSORBOBOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"}:
                raise ConfigError("Некорректное имя переменной API-ключа.")
            names.add(name)
    for name in sorted(names):
        if not environment.get(name):
            raise ConfigError(f"Не задана переменная {name}.")


def _metadata_env() -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR", "XDG_DATA_HOME", "XDG_CACHE_HOME"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _run_metadata(argv: list[str], *, timeout: int = 60) -> str:
    try:
        run = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_metadata_env(), check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ConfigError("Не удалось выполнить проверку установленного инструмента.") from None
    if run.returncode:
        raise ConfigError("Проверка установленного инструмента завершилась ошибкой.")
    return run.stdout.strip()


def _receipt_env() -> Path:
    uv = shutil.which("uv")
    if not uv:
        raise ConfigError("Не установлен необходимый инструмент uv.")
    directory = _run_metadata([uv, "tool", "dir"])
    if not directory or not Path(directory).is_absolute():
        raise ConfigError("uv вернул некорректный каталог tools.")
    tool_env = Path(directory) / "absorbent"
    try:
        receipt = tomllib.loads((tool_env / "uv-receipt.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError("Absorbent не установлен как uv tool: отсутствует корректный receipt.") from None
    tool = receipt.get("tool", {})
    requirements = tool.get("requirements", []) if isinstance(tool, dict) else []
    requirement = next((r for r in requirements if isinstance(r, dict) and r.get("name") == "absorbent"), None)
    if requirement is None:
        raise ConfigError("Receipt не подтверждает установку Absorbent.")
    if requirement.get("editable"):
        raise ConfigError("Editable установка Absorbent запрещена.")
    return tool_env.resolve()


def validate_probe(data: dict, tool_env: Path) -> dict:
    """Проверить JSON-контракт и происхождение runtime без доверия к uvx-кэшу."""
    required = {"version_info", "installed_editable", "models", "defaults", "media_extensions", "material_extensions", "runtime"}
    if not isinstance(data, dict) or set(data) != required:
        raise ConfigError("Некорректная схема ответа probe.")
    info = data["version_info"]
    if not isinstance(info, dict) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", str(info.get("version", ""))):
        raise ConfigError("Неизвестная версия установленного Absorbent.")
    if not isinstance(info.get("commit"), str) or not _SHA.fullmatch(info["commit"]):
        raise ConfigError("Установленный Absorbent не содержит полного commit SHA.")
    if info.get("dirty") is not False:
        raise ConfigError("Установка Absorbent изменена или имеет неизвестный dirty status.")
    if data["installed_editable"] is not False:
        raise ConfigError("Editable установка Absorbent запрещена.")
    runtime = data["runtime"]
    if not isinstance(runtime, dict):
        raise ConfigError("Некорректный runtime probe.")
    for key in ("tool_env", "python_executable", "python_base_prefix"):
        value = runtime.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ConfigError("Некорректные пути runtime probe.")
    roots = runtime.get("tex_roots", [])
    if not isinstance(roots, list) or any(not isinstance(root, str) or not Path(root).is_absolute() for root in roots):
        raise ConfigError("Некорректные корни TeX в probe.")
    if Path(runtime["tool_env"]).resolve() != tool_env.resolve():
        raise ConfigError("Probe запущен не из установленного uv tool Absorbent.")
    # Исполняемый путь в venv может быть symlink на базовый Python.
    if not Path(runtime["python_executable"]).absolute().is_relative_to(Path(runtime["tool_env"]).absolute()):
        raise ConfigError("Python probe не принадлежит установленному tool.")
    if not isinstance(data["defaults"], dict) or not isinstance(data["models"], dict) or set(data["models"]) != {"stt", "vision", "llm"}:
        raise ConfigError("Некорректный каталог моделей probe.")
    for category in ("stt", "vision", "llm"):
        table = data["models"][category]
        if not isinstance(table, dict) or not table or data["defaults"].get(category) not in table:
            raise ConfigError("В категории моделей отсутствует доступная модель по умолчанию.")
        for name, model in table.items():
            if not isinstance(name, str) or not isinstance(model, dict) or set(model) != {"spec", "provider", "local"}:
                raise ConfigError("Некорректная модель probe.")
            spec, provider = model["spec"], model["provider"]
            if not isinstance(spec, dict) or not {"name", "provider", "backend", "whisper_model", "model", "default", "api_model"} <= set(spec):
                raise ConfigError("Некорректная спецификация модели probe.")
            if spec["name"] != name or type(spec["default"]) is not bool or any(not isinstance(spec[key], str) for key in ("name", "provider", "backend", "whisper_model", "model", "api_model")):
                raise ConfigError("Некорректные поля спецификации модели probe.")
            # Новое ядро добавляет необязательный размер батча: None означает размер у провайдера.
            if "batch_size" in spec and spec["batch_size"] is not None and not _positive_int(spec["batch_size"]):
                raise ConfigError("Некорректная спецификация модели probe.")
            # Провайдер принимает прежние три ключа и необязательный размер батча.
            if (not isinstance(provider, dict) or not {"name", "base_url", "api_key_env"} <= set(provider)
                    or set(provider) - {"name", "base_url", "api_key_env", "batch_size"}
                    or any(key != "batch_size" and not isinstance(value, str) for key, value in provider.items())
                    or type(model["local"]) is not bool):
                raise ConfigError("Некорректный провайдер probe.")
            if "batch_size" in provider and not _positive_int(provider["batch_size"]):
                raise ConfigError("Некорректный провайдер probe.")
            if provider["api_key_env"] and (not _ENV_NAME.fullmatch(provider["api_key_env"]) or provider["api_key_env"] in {"ABSORBOBOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"}):
                raise ConfigError("Некорректное имя переменной ключа в probe.")
    for key in ("media_extensions", "material_extensions"):
        extensions = data[key]
        if not isinstance(extensions, list) or not extensions or any(not isinstance(ext, str) or not re.fullmatch(r"\.[a-z0-9]+", ext) for ext in extensions):
            raise ConfigError("Некорректные расширения файлов в probe.")
    return data


def probe_core(settings: Settings) -> dict:
    """Вызвать только установленное ядро, без ключей и без обработки лекции."""
    tool_env = _receipt_env()
    script = Path(__file__).parent / "workers" / "probe.py"
    raw = _run_metadata([shutil.which("uv") or "uv", "tool", "run", "--offline", "--no-config", "--from", "absorbent", "python", str(script), str(settings.absorbent_config)])
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ConfigError("Установленное ядро вернуло некорректный JSON probe.") from None
    return validate_probe(data, tool_env)


def check_runtime(settings: Settings) -> dict:
    """Проверить диск, зависимости, неизменное ядро и защищённое окружение."""
    storage = check_storage(settings)
    for tool in _REQUIRED_TOOLS:
        if not shutil.which(tool):
            raise ConfigError(f"Не установлен необходимый инструмент {tool}.")
    local_api = settings.bot_api_url != "https://api.telegram.org"
    if local_api and (not settings.api_binary.is_file() or not os.access(settings.api_binary, os.X_OK)):
        raise ConfigError("Не установлен исполняемый telegram-bot-api по api_binary.")
    state = settings.state_dir.resolve()
    existing = _existing_parent(state)
    if not existing.is_dir() or not os.access(existing, os.W_OK | os.X_OK):
        raise ConfigError("state_dir недоступен для записи.")
    candidates = [p for p in psutil.disk_partitions(all=True) if existing.is_relative_to(Path(p.mountpoint).resolve())]
    if not candidates or max(candidates, key=lambda p: len(Path(p.mountpoint).parts)).fstype.lower() in {"ntfs", "ntfs3", "fuseblk"}:
        raise ConfigError("SQLite state_dir должен находиться на поддерживаемой SSD файловой системе, не NTFS.")
    core = probe_core(settings)
    env = load_secrets(settings)
    required = ("ABSORBOBOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH") if local_api else ("ABSORBOBOT_TOKEN",)
    for name in required:
        if not env.get(name):
            raise ConfigError(f"Не задана переменная {name}.")
    if local_api and (not env["TELEGRAM_API_ID"].isdigit() or int(env["TELEGRAM_API_ID"]) <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", env["TELEGRAM_API_HASH"])):
        raise ConfigError("Некорректные application credentials Telegram.")
    selected = {category: core["models"][category][core["defaults"][category]] for category in ("stt", "vision", "llm")}
    require_key_names(selected, env)
    if not core["runtime"].get("tex_roots"):
        raise ConfigError("Не обнаружены необходимые корни TeX для LuaLaTeX.")
    return {"storage": storage, "core": core}
