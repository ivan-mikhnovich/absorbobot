"""Read-only probe установленного Absorbent; запускать только через uv tool."""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from dataclasses import asdict
from importlib.metadata import distribution
from pathlib import Path
from urllib.parse import urlsplit


def _local(spec: dict, provider: dict) -> bool:
    if spec["backend"] == "whisper":
        return True
    host = urlsplit(provider["base_url"]).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def _tex_roots() -> list[str]:
    """Обнаружить format именно LuaLaTeX и системные корни его конфигурации."""
    commands = [
        ["kpsewhich", "-engine=luahbtex", "-progname=lualatex", "-format=fmt", "lualatex.fmt"],
        ["kpsewhich", "-engine=luahbtex", "-progname=lualatex", "-var-value=TEXMFROOT"],
        ["kpsewhich", "-engine=luahbtex", "-progname=lualatex", "-var-value=TEXMFSYSCONFIG"],
        ["kpsewhich", "-engine=luahbtex", "-progname=lualatex", "-var-value=TEXMFSYSVAR"],
    ]
    roots: set[str] = set()
    for index, command in enumerate(commands):
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return []
        value = run.stdout.strip()
        if run.returncode or not value or not Path(value).is_absolute():
            if index == 0:
                return []
            continue
        path = Path(value)
        if index == 0:
            if not path.is_file():
                return []
            path = path.parent
        if path.is_dir():
            roots.add(str(path.resolve()))
    for path in (Path("/etc/texmf"), Path("/var/lib/texmf")):
        if path.is_dir():
            roots.add(str(path.resolve()))
    return sorted(roots)


def collect(config_path: str) -> dict:
    """Получить каталог из dataclasses ядра без чтения API-ключей."""
    from absorbent.cli import MEDIA_EXTENSIONS, build_parser
    from absorbent.config import load_config
    from absorbent.slides import MATERIAL_EXTENSIONS
    from absorbent.version import get_version_info

    cfg = load_config(config_path)
    defaults = vars(build_parser().parse_args([]))
    models: dict = {}
    for category in ("stt", "vision", "llm"):
        models[category] = {}
        for name in getattr(cfg, category):
            provider, spec = cfg.resolve(category, name)
            spec_data = {**asdict(spec), "api_model": spec.api_model}
            provider_data = asdict(provider)
            models[category][name] = {"spec": spec_data, "provider": provider_data, "local": _local(spec_data, provider_data)}
        default = cfg.default_name(category)
        cfg.resolve(category, default)
        defaults[category] = default
    dist = distribution("absorbent")
    direct_url = dist.read_text("direct_url.json")
    editable = False
    if direct_url:
        editable = json.loads(direct_url).get("dir_info", {}).get("editable", False)
    return {
        "version_info": get_version_info(),
        "installed_editable": editable,
        "models": models,
        "defaults": defaults,
        "media_extensions": sorted(MEDIA_EXTENSIONS),
        "material_extensions": sorted(MATERIAL_EXTENSIONS),
        "runtime": {
            "tool_env": str(Path(sys.prefix).absolute()),
            "python_executable": str(Path(sys.executable).absolute()),
            "python_base_prefix": str(Path(sys.base_prefix).absolute()),
            "tex_roots": _tex_roots(),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Probe требует один путь конфигурации.", file=sys.stderr)
        return 2
    try:
        result = collect(args[0])
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception:
        # Ошибки сторонних библиотек могут содержать пути или секреты конфигурации.
        print("Проверка установленного Absorbent завершилась ошибкой.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
