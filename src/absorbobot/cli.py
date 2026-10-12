"""Команды проверки, запуска и установки собственных user units."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from absorbobot.config import ConfigError, Settings, check_runtime, check_storage, load_secrets, load_settings


async def check_telegram(settings: Settings, env: dict[str, str]) -> dict:
    """Проверить только getMe выбранного API, не отправляя сообщений."""
    token = env.get("ABSORBOBOT_TOKEN")
    if not token:
        raise ConfigError("Не задана переменная ABSORBOBOT_TOKEN.")
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.utils.token import TokenValidationError

    session = AiohttpSession(api=TelegramAPIServer.from_base(
        settings.bot_api_url, is_local=settings.bot_api_url != "https://api.telegram.org"), timeout=30)
    try:
        bot = Bot(token=token, session=session)
        me = await bot.get_me()
        if (me.username or "").lower() != "absorbobot" or not me.is_bot:
            raise ConfigError("getMe подтвердил не бота @absorbobot.")
        return {"id": me.id, "username": me.username}
    except (TelegramUnauthorizedError, TokenValidationError):
        raise ConfigError("Неверный ABSORBOBOT_TOKEN: Telegram не подтвердил авторизацию.") from None
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("Не удалось выполнить getMe Telegram Bot API.") from None
    finally:
        await session.close()


def _quoted(value: str | Path, *, exec_arg: bool = False) -> str:
    """Экранировать аргумент systemd, а не команду shell."""
    text = str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if exec_arg:
        text = text.replace("$", "$$")
    return '"' + text + '"'


def _exec(argv: list[str | Path]) -> str:
    return " ".join(_quoted(value, exec_arg=True) for value in argv)


# Ограничение частоты перезапусков: сбойный запуск не должен превращаться
# в цикл и вычерпывать лимиты Telegram на публикацию описаний команд.
_START_LIMITS = "StartLimitIntervalSec=60\nStartLimitBurst=10\n"
_SERVICE_TAIL = "UMask=0077\nKillMode=control-group\nTimeoutStopSec=15s\nRestart=on-failure\nRestartSec=5s\n"


def render_units(settings: Settings, *, uv_binary: str | Path | None = None) -> dict[str, str]:
    """Сформировать units по фактическим путям без запуска или установки."""
    uv = uv_binary or shutil.which("uv")
    if uv is None or not Path(uv).is_absolute():
        raise ConfigError("Не найден абсолютный путь исполняемого uv.")
    pre = _exec([uv, "tool", "run", "--offline", "--no-config", "absorbobot", "check-storage", "--config", settings.config_path])
    if settings.bot_api_url == "https://api.telegram.org":
        bot = _exec([uv, "tool", "run", "--offline", "--no-config", "absorbobot", "serve", "--config", settings.config_path])
        return {"absorbobot.service": (
            f"[Unit]\nDescription=Absorbobot\n{_START_LIMITS}\n[Service]\nType=simple\n"
            f"WorkingDirectory={str(settings.state_dir).replace('%', '%%')}\n"
            f"EnvironmentFile={str(settings.secrets_file).replace('%', '%%')}\n"
            f"ExecStartPre={pre}\nExecStart={bot}\n"
            f"{_SERVICE_TAIL}"
            "\n[Install]\nWantedBy=default.target\n"
        )}
    # Каталог моделей динамический; native API не должен получать bot/cloud ключи.
    try:
        raw = yaml.safe_load(settings.absorbent_config.read_text(encoding="utf-8"))
    except Exception:
        raise ConfigError("Не удалось прочитать каталог провайдеров для native unit.") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("providers", {}), dict):
        raise ConfigError("Некорректный каталог провайдеров для native unit.")
    unset = {"ABSORBOBOT_TOKEN"}
    from absorbobot.config import _ENV_NAME
    for provider in raw.get("providers", {}).values():
        if not isinstance(provider, dict):
            raise ConfigError("Некорректный провайдер для native unit.")
        name = provider.get("api_key_env", "")
        if name:
            if not isinstance(name, str) or not _ENV_NAME.fullmatch(name) or name in {"TELEGRAM_API_ID", "TELEGRAM_API_HASH"}:
                raise ConfigError("Некорректное имя cloud API-ключа для native unit.")
            unset.add(name)
    api_state = settings.state_dir / "telegram-api"
    api_workdir = str(api_state).replace("%", "%%")
    bot_workdir = str(settings.state_dir).replace("%", "%%")
    secrets_path = str(settings.secrets_file).replace("%", "%%")
    native = _exec([
        settings.api_binary, "--local", f"--http-ip-address={urlsplit(settings.bot_api_url).hostname}", "--http-port=8081",
        f"--dir={api_state}", f"--files-dir={settings.storage_dir / 'telegram-api-files'}",
        f"--temp-dir={settings.storage_dir / 'telegram-api-tmp'}",
    ])
    bot = _exec([uv, "tool", "run", "--offline", "--no-config", "absorbobot", "serve", "--config", settings.config_path])
    common = (
        f"EnvironmentFile={secrets_path}\n"
        f"ExecStartPre={pre}\n"
        f"{_SERVICE_TAIL}"
    )
    return {
        "absorbobot-api.service": (
            f"[Unit]\nDescription=Absorbobot local Telegram Bot API\n{_START_LIMITS}\n[Service]\nType=simple\n"
            f"WorkingDirectory={api_workdir}\n{common}"
            f"UnsetEnvironment={' '.join(sorted(unset))}\nExecStart={native}\n\n[Install]\nWantedBy=default.target\n"
        ),
        "absorbobot.service": (
            f"[Unit]\nDescription=Absorbobot\nRequires=absorbobot-api.service\nAfter=absorbobot-api.service\n{_START_LIMITS}\n"
            f"[Service]\nType=simple\nWorkingDirectory={bot_workdir}\n{common}"
            f"ExecStart={bot}\n\n[Install]\nWantedBy=default.target\n"
        ),
    }


def unit_directory() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    if not root.is_absolute():
        raise ConfigError("XDG_CONFIG_HOME должен быть абсолютным путём.")
    return root / "systemd" / "user"


def check_unit_paths(settings: Settings) -> None:
    """Обнаружить устаревшие собственные units после изменения настроек."""
    directory = unit_directory()
    existing = [name for name in ("absorbobot-api.service", "absorbobot.service") if (directory / name).exists()]
    if not existing:
        return
    expected = render_units(settings)
    for name in existing:
        try:
            actual = (directory / name).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ConfigError("Не удалось проверить собственные user units.") from None
        if name not in expected or actual != expected[name]:
            raise ConfigError("User units не соответствуют текущим настройкам; выполните install-units.")


def install_units(settings: Settings) -> None:
    """Записать собственные службы выбранного API и выполнить daemon-reload."""
    check_storage(settings)
    units = render_units(settings)
    paths = [settings.state_dir, settings.storage_dir]
    if settings.bot_api_url != "https://api.telegram.org":
        paths.extend((settings.state_dir / "telegram-api", settings.storage_dir / "telegram-api-files",
                      settings.storage_dir / "telegram-api-tmp"))
    for path in paths:
        if path.is_symlink():
            raise ConfigError("Каталог собственной службы является небезопасной ссылкой.")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = unit_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    obsolete = directory / "absorbobot-api.service"
    if settings.bot_api_url == "https://api.telegram.org" and obsolete.exists():
        if obsolete.is_symlink():
            raise ConfigError("Собственный unit является небезопасной ссылкой.")
        try:
            stopped = subprocess.run(["systemctl", "--user", "disable", "--now", "absorbobot-api.service"],
                                     capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ConfigError("Не удалось остановить собственный локальный Bot API.") from None
        if stopped.returncode:
            raise ConfigError("Не удалось отключить собственный локальный Bot API.")
        obsolete.unlink()
    for name, content in units.items():
        target = directory / name
        if target.is_symlink():
            raise ConfigError("Собственный unit является небезопасной ссылкой.")
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError:
            raise ConfigError("Не удалось записать собственные user units.") from None
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise ConfigError("Не найден systemctl для daemon-reload.")
    try:
        run = subprocess.run([systemctl, "--user", "daemon-reload"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigError("Не удалось выполнить systemctl --user daemon-reload.") from None
    if run.returncode:
        raise ConfigError("systemctl --user daemon-reload завершился ошибкой.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="absorbobot", description="Telegram-интерфейс к Absorbent.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "check", "check-storage", "install-units"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Запустить команду; при ошибке вывести только безопасную диагностику."""
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        if args.command == "check-storage":
            print(json.dumps(check_storage(settings), ensure_ascii=False))
            return 0
        if args.command == "install-units":
            install_units(settings)
            print("Собственные user units созданы; daemon-reload выполнен.")
            return 0
        result = check_runtime(settings)
        check_unit_paths(settings)
        env = load_secrets(settings)
        identity = asyncio.run(check_telegram(settings, env))
        if args.command == "check":
            print(json.dumps({"ok": True, "telegram": identity, "version_info": result["core"]["version_info"], "storage": result["storage"]}, ensure_ascii=False))
            return 0
        # Ручной запуск получает то же защищённое окружение, что и systemd.
        os.environ.update(env)
        from absorbobot.store import Store
        with Store(settings):
            pass
        from absorbobot.bot import serve
        value = serve(settings)
        if inspect.isawaitable(value):
            asyncio.run(value)
        return 0
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Absorbobot не смог выполнить команду; запуск остановлен.", file=sys.stderr)
        return 2
