from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if value.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or value.get("text") or "").strip()
    if isinstance(value, list):
        return ", ".join(filter(None, (text(item) for item in value)))
    return str(value).strip()


def get_single_select_value(value: Any) -> str:
    """Return the visible value from a Baserow single-select response."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("value") or "").strip()
    return str(value).strip()


def row_readiness(row: dict[str, Any], source_status: str) -> tuple[bool, str]:
    if get_single_select_value(row.get("Generation Status")) != source_status:
        return False, "not_source_status"
    if not text(row.get("Product Code")):
        return False, "missing_product_code"
    if not text(row.get("Product Title")):
        return False, "missing_product_title"
    return True, ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def setup_logging(root: Path) -> logging.Logger:
    (root / "logs").mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("shopify_sync")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), logging.FileHandler(root / "logs" / "shopify_sync.log", encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger
