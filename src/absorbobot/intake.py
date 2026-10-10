"""Семантическая идентичность входов и выбранной обработки."""

from __future__ import annotations

import hashlib
import json


def input_signature(files: list[dict], settings: dict, models: dict) -> str:
    """Сравнить содержимое и обработку без владельца, путей и версии ядра."""
    active = [file for file in files if file.get("active", True)]
    media = [file for file in active if file["role"] == "media"]
    if len(media) != 1:
        raise ValueError("Exactly one media file is required.")
    parameters = {key: settings[key] for key in (
        "language", "output_language", "fps", "hash_threshold",
        "min_scene_seconds", "slides_dpi", "youtube_mode",
    ) if key in settings}
    selected = {}
    for category, model in sorted(models.items()):
        spec, provider = model["spec"], model.get("provider") or {}
        selected[category] = {
            "backend": spec.get("backend"),
            "whisper_model": spec.get("whisper_model"),
            "api_model": spec.get("api_model"),
            "provider": provider.get("name"),
            "base_url": provider.get("base_url"),
        }
    content = {
        "media": media[0]["sha256"],
        "materials": [
            {"sha256": file["sha256"], "type": file.get("material_type") or
             str(file["relative_path"]).rsplit(".", 1)[-1].lower()}
            for file in active if file["role"] == "material"
        ],
        "settings": parameters,
        "models": selected,
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
