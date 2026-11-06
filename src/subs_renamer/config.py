"""Build-time credentials and source-only dotenv loading; no credentials in GUI settings."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from .paths import resource_dir

EMBEDDED_CONFIG = "embedded-config.json"


def development_key(env_file: Path | None = None) -> str:
    path = env_file if env_file is not None else resource_dir() / ".env"
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path.exists() else {}
    key = (os.environ.get("GEMINI_API_KEY") or values.get("GEMINI_API_KEY") or "").strip()
    if not key or any(c in key for c in "\r\n"):
        raise ValueError("Задайте GEMINI_API_KEY в .env перед запуском или сборкой приложения.")
    return key


def api_key() -> str:
    if getattr(sys, "frozen", False):
        try:
            key = json.loads((resource_dir() / EMBEDDED_CONFIG).read_text(encoding="utf-8"))["gemini_api_key"]
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError(
                "В EXE отсутствует встроенный ключ Gemini. Пересоберите приложение с .env."
            ) from None
        if not isinstance(key, str) or not key.strip() or any(c in key for c in "\r\n"):
            raise RuntimeError("В EXE некорректный ключ Gemini. Пересоберите приложение с .env.")
        return key.strip()
    return development_key()
