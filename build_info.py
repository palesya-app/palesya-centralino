"""Versione incorporata nei bundle Palesya durante la pipeline di release."""
import json
import os
from pathlib import Path
import sys


def runtime_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def current_version(default="0.0.0"):
    configured = os.getenv("PALESYA_VERSION", "").strip()
    if configured:
        return configured
    path = runtime_root() / "packaging" / "build-info.json"
    try:
        value = str(json.loads(path.read_text(encoding="utf-8")).get("version") or "").strip()
    except (OSError, ValueError, TypeError):
        value = ""
    return value or default
