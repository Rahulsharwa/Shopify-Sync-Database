from __future__ import annotations

import csv
import html
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from baserow_client import BaserowClient
from shopify_client import ShopifyClient, ShopifyError


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "men_accessories_product_create_config.json"
OUTPUT = ROOT / "output"
LOGS = ROOT / "logs"
SUPPORTED_SHOPIFY_IMAGE_MIME_TYPES = {
    "image/gif",
    "image/heic",
    "image/heif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


class PartialProductError(RuntimeError):
    def __init__(self, product_id: str, message: str) -> None:
        super().__init__(message)
        self.product_id = product_id


def load_openrouter_class():
    path = ROOT / "utils" / "openrouter_client.py"
    spec = importlib.util.spec_from_file_location("men_accessories_openrouter_client", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OpenRouterClient


def load_image_prepare():
    path = ROOT / "utils" / "image_prepare.py"
    spec = importlib.util.spec_from_file_location("men_accessories_image_prepare", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.strip().casefold() in {"true", "1", "yes", "on"}:
        return True
    if raw.strip().casefold() in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def visible(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("value") or value.get("name") or value.get("text") or "").strip()
    if isinstance(value, list):
        return ", ".join(part for part in (visible(item) for item in value) if part)
    return str(value).strip()


def status(value: Any) -> str:
    return visible(value).casefold()


def normalize_status(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("value") or ""
    value = str(value).replace("\u00a0", " ")
    value = " ".join(value.split())
    return value.strip().lower()


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return value[:240] or "tie"


def gql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def original_file_urls(value: Any) -> list[str]:
    files = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
    urls: list[str] = []
    for item in files:
        url = item.get("url") if isinstance(item, dict) else None
        if url and "/thumbnails/" in str(url).casefold():
            raise ValueError("selected thumbnail url instead of original")
        if url and url not in urls:
            urls.append(str(url))
    return urls


def get_baserow_original_file(file_list: list, field_name: str) -> dict:
    if not file_list:
        raise ValueError(f"{field_name} has no file")
    file_obj = file_list[0]
    if not isinstance(file_obj, dict):
        raise ValueError(f"{field_name} file metadata is invalid")
    url = file_obj.get("url")
    if not url:
        raise ValueError(f"{field_name} original url missing")
    if "/thumbnails/" in str(url).casefold():
        raise ValueError(
            f"{field_name} selected thumbnail url instead of original"
        )
    return file_obj


def get_baserow_original_file_url(file_list: list, field_name: str) -> str:
    file_obj = get_baserow_original_file(file_list, field_name)
    return str(file_obj["url"])


def get_baserow_source_file(row: dict, field_name: str) -> dict | None:
    files = row.get(field_name) or []
    if isinstance(files, dict):
        files = [files]
    if not files:
        return None
    file_obj = files[0]
    if not isinstance(file_obj, dict):
        raise ValueError(f"{field_name}: file metadata is invalid")
    source_url = str(file_obj.get("url") or "").strip()
    if not source_url:
        raise ValueError(f"{field_name}: original Baserow URL missing")
    if "/thumbnails/" in source_url.lower():
        raise ValueError(f"{field_name}: thumbnail URL blocked")
    return {
        "field_name": field_name,
        "url": source_url,
        "name": file_obj.get("name"),
        "visible_name": file_obj.get("visible_name"),
        "size": file_obj.get("size"),
        "mime_type": file_obj.get("mime_type"),
        "image_width": file_obj.get("image_width"),
        "image_height": file_obj.get("image_height"),
        "is_image": file_obj.get("is_image"),
    }


def download_exact_source_file(source_url: str, destination_path: Path) -> None:
    with requests.get(source_url, stream=True, timeout=(20, 180)) as response:
        response.raise_for_status()
        with destination_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def money(value: Any) -> str:
    source = visible(value)
    raw = re.sub(r"[^0-9.\-]", "", source.replace(",", ""))
    if not raw or raw.count(".") > 1 or "-" in raw[1:]:
        raise ValueError(f"invalid Price: {source!r}")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid Price: {source!r}") from exc
    if amount < 0:
        raise ValueError("Price cannot be negative")
    return f"{amount:.2f}"


def setup_logger() -> logging.Logger:
    LOGS.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("men_accessories_product_create")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (
        logging.StreamHandler(),
        logging.FileHandler(LOGS / "men_accessories_product_create.log", encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


@dataclass
class RunState:
    rows_loaded: int = 0
    approved_rows: int = 0
    eligible: int = 0
    attempted: int = 0
    created: int = 0
    processed_success: int = 0
    existing_products_matched: int = 0
    duplicate_products_prevented: int = 0
    images_found: int = 0
    images_already_present: int = 0
    images_newly_uploaded: int = 0
    duplicate_media_prevented: int = 0
    duplicates: int = 0
    missing_fields: int = 0
    failed: int = 0
    baserow_updated: int = 0
    source_duplicate_skus: int = 0
    existing_shopify_skus: int = 0
    existing_shopify_handles: int = 0
    existing_shopify_titles: int = 0
    ai_success: int = 0
    ai_failed: int = 0
    collection_success: int = 0
    collection_failed: int = 0
    primary_collections_assigned: int = 0
    secondary_collections_assigned: int = 0
    new_arrivals_assigned: int = 0
    new_arrivals_already_assigned: int = 0
    new_arrivals_assignment_failures: int = 0
    image_urls_checked: int = 0
    image_urls_valid: int = 0
    image_urls_invalid: int = 0
    products_active: int = 0
    products_online_store_published: int = 0
    online_store_publish_failures: int = 0
    inventory_set_to_quantity: int = 0
    inventory_set_failures: int = 0
    location_not_found_failures: int = 0
    total_tags_written: int = 0
    products_with_more_than_max_tags: int = 0
    preview: list[dict[str, Any]] = field(default_factory=list)
    report: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    duplicate_rows: list[dict[str, Any]] = field(default_factory=list)
    missing_rows: list[dict[str, Any]] = field(default_factory=list)
    ai_rows: list[dict[str, Any]] = field(default_factory=list)
    image_failures: list[dict[str, Any]] = field(default_factory=list)
    image_quality: list[dict[str, Any]] = field(default_factory=list)
    resolution_validation: list[dict[str, Any]] = field(default_factory=list)
    persistence_checks: list[dict[str, Any]] = field(default_factory=list)
    theme_audit: list[dict[str, Any]] = field(default_factory=list)
    compressed_images: list[dict[str, Any]] = field(default_factory=list)


class MenAccessoriesCreator:
    def __init__(self) -> None:
        explicit_max_products = os.environ.get("MAX_PRODUCTS")
        load_dotenv(ROOT / ".env")
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.logger = setup_logger()
        self.state = RunState()
        self.check_baserow_access = env_bool(
            "CHECK_MEN_ACCESSORIES_BASEROW_ACCESS", False
        )
        self.fix_created_products = env_bool(
            "FIX_MEN_ACCESSORIES_CREATED_PRODUCTS", False
        )
        self.fix_direct_product = env_bool(
            "FIX_MEN_ACCESSORIES_PRODUCT_PUBLISHING", False
        )
        self.repair_media_quality = env_bool(
            "REPAIR_MEN_ACCESSORIES_LOW_QUALITY_MEDIA", False
        ) or env_bool("REPAIR_MEN_ACCESSORIES_MEDIA_QUALITY", False)
        self.repair_resized_media = env_bool(
            "REPAIR_MEN_ACCESSORIES_RESIZED_MEDIA", False
        )
        self.audit_image_quality = env_bool(
            "AUDIT_MEN_ACCESSORIES_IMAGE_UPLOAD_SOURCE", False
        ) or env_bool("AUDIT_MEN_ACCESSORIES_IMAGE_QUALITY", False)
        self.reconcile_shopify_sync = env_bool(
            "RECONCILE_MEN_ACCESSORIES_SHOPIFY_SYNC", False
        )
        if self.reconcile_shopify_sync:
            reconciliation_log = LOGS / "men_accessories_shopify_sync_reconciliation.log"
            handler = logging.FileHandler(reconciliation_log, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            self.logger.addHandler(handler)
        self.dry_run = env_bool("DRY_RUN", True)
        self.enabled = env_bool("CREATE_PRODUCTS_FROM_MEN_ACCESSORIES", False)
        self.write_comments = env_bool("WRITE_MEN_ACCESSORIES_BASEROW_COMMENTS", True)
        self.require_shopify_notes_approved = env_bool(
            "MEN_ACCESSORIES_REQUIRE_SHOPIFY_NOTES_APPROVED", True
        )
        self.use_image_input = env_bool("USE_OPENROUTER_IMAGE_INPUT", True)
        # MAX_PRODUCTS is a run-control switch, not a persisted application
        # setting. Ignoring a value left in .env makes "Remove-Item Env:"
        # reliably mean an unlimited approved run.
        max_raw = (
            explicit_max_products.strip()
            if explicit_max_products is not None
            else ""
        )
        self.max_products = int(max_raw) if max_raw else None
        shared_table_id = os.getenv("BASEROW_TABLE_ID", "").strip()
        self.table_id = str(
            os.getenv("MEN_ACCESSORIES_BASEROW_TABLE_ID", self.config["table_id"])
        ).strip()
        if int(self.table_id) != int(self.config["table_id"]):
            raise ValueError(
                f"MEN_ACCESSORIES_BASEROW_TABLE_ID must be {self.config['table_id']}"
            )
        self.baserow_token_env, baserow_token = self.select_baserow_token()
        self.baserow = BaserowClient(
            os.getenv("BASEROW_API_BASE", "https://api.baserow.io").strip()
            or "https://api.baserow.io",
            baserow_token,
            self.table_id,
            timeout=45,
        )
        self.fields: dict[str, dict[str, Any]] = {}
        self.status_option_id: int | None = None
        self.approved_status_option_id: int | None = None
        self.collection_cache: dict[str, dict[str, Any] | None] = {}
        self.publication_cache: dict[str, str] = {}
        self.location_cache: dict[str, dict[str, Any]] = {}
        self.reference_audit: dict[str, Any] = {}
        self.reference_template_suffix = ""
        self.reference_taxonomy_gid = ""
        self.reference_collection_gid = ""
        self.reference_collection_title = ""
        self.current_partial_product_id = ""
        self.shopify: ShopifyClient | None = None
        self.openrouter = None
        if shared_table_id and int(shared_table_id) != int(self.table_id):
            self.logger.info(
                "Ignoring shared BASEROW_TABLE_ID=%s; Men Accessories creator is pinned to %s",
                shared_table_id,
                self.table_id,
            )
        if self.check_baserow_access:
            self.logger.info(
                "Baserow-only diagnostic mode enabled; Shopify and OpenRouter are disabled"
            )
            return
        self.load_operational_settings()
        if not baserow_token:
            raise ValueError("Missing BASEROW_TOKEN or BASEROW_API_TOKEN")
        self.shopify = ShopifyClient(
            self.required("SHOPIFY_STORE_DOMAIN"),
            self.required("SHOPIFY_ADMIN_ACCESS_TOKEN"),
            os.getenv("SHOPIFY_API_VERSION", "2026-04"),
            timeout=60,
        )
        self.openrouter = None
        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if key:
            OpenRouterClient = load_openrouter_class()
            self.openrouter = OpenRouterClient(
                key, os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
            )
        elif not self.dry_run:
            if (
                not self.fix_created_products
                and not self.fix_direct_product
                and not self.repair_media_quality
                and not self.repair_resized_media
            ):
                raise ValueError("OPENROUTER_API_KEY is required in live create mode")

    @staticmethod
    def select_baserow_token() -> tuple[str, str]:
        for name in ("BASEROW_TOKEN", "BASEROW_API_TOKEN"):
            value = os.getenv(name, "").strip()
            if value:
                return name, value
        return "none", ""

    def load_operational_settings(self) -> None:
        self.product_status = os.getenv(
            "MEN_ACCESSORIES_PRODUCT_STATUS",
            os.getenv("UPLOAD_SAREE_PRODUCT_STATUS", "ACTIVE"),
        ).strip().upper()
        if self.product_status not in {"ACTIVE", "DRAFT"}:
            raise ValueError("MEN_ACCESSORIES_PRODUCT_STATUS must be ACTIVE or DRAFT")
        self.primary_collection_title = os.getenv(
            "MEN_ACCESSORIES_COLLECTION_NAME",
            os.getenv(
                "MEN_ACCESSORIES_PRIMARY_COLLECTION_TITLE",
                self.config.get("primary_collection_title", "Men's Ties"),
            ),
        ).strip()
        self.secondary_collection_title = os.getenv(
            "MEN_ACCESSORIES_SECONDARY_COLLECTION_TITLE",
            self.config.get("secondary_collection_title", ""),
        ).strip()
        self.secondary_collection_id = os.getenv(
            "MEN_ACCESSORIES_SECONDARY_COLLECTION_ID",
            self.config.get("secondary_collection_id", ""),
        ).strip()
        self.media_completion_existing = env_bool(
            "MEN_ACCESSORIES_MEDIA_COMPLETION_EXISTING", True
        )
        self.never_duplicate_media = env_bool(
            "MEN_ACCESSORIES_NEVER_DUPLICATE_MEDIA", True
        )
        self.fix_existing_collection = env_bool(
            "MEN_ACCESSORIES_FIX_EXISTING_COLLECTION", False
        )
        self.copy_reference_template = env_bool(
            "MEN_ACCESSORIES_COPY_REFERENCE_TEMPLATE", True
        )
        self.copy_reference_taxonomy = env_bool(
            "MEN_ACCESSORIES_COPY_REFERENCE_TAXONOMY", True
        )
        self.copy_promotional_collections = env_bool(
            "MEN_ACCESSORIES_COPY_PROMOTIONAL_COLLECTIONS", False
        )
        self.publish_online_store = env_bool(
            "MEN_ACCESSORIES_PUBLISH_ONLINE_STORE",
            env_bool("UPLOAD_SAREE_PUBLISH_ONLINE_STORE", True),
        )
        self.publication_name = os.getenv(
            "SHOPIFY_ONLINE_STORE_PUBLICATION_NAME", "Online Store"
        ).strip()
        if self.publish_online_store and not self.publication_name:
            raise ValueError("SHOPIFY_ONLINE_STORE_PUBLICATION_NAME cannot be empty")
        self.verify_publication = env_bool(
            "VERIFY_PRODUCT_ONLINE_STORE_PUBLISHED", True
        )
        self.add_new_arrivals = env_bool(
            "ADD_NEW_PRODUCTS_TO_NEW_ARRIVALS", True
        )
        self.new_arrivals_collection_name = os.getenv(
            "SHOPIFY_NEW_ARRIVALS_COLLECTION_NAME", "New Arrivals"
        ).strip()
        if self.add_new_arrivals and not self.new_arrivals_collection_name:
            raise ValueError("SHOPIFY_NEW_ARRIVALS_COLLECTION_NAME cannot be empty")
        self.inventory_tracked = env_bool(
            "MEN_ACCESSORIES_INVENTORY_TRACKED",
            env_bool("UPLOAD_SAREE_INVENTORY_TRACKED", True),
        )
        quantity_raw = os.getenv(
            "MEN_ACCESSORIES_INVENTORY_QUANTITY",
            os.getenv("UPLOAD_SAREE_INVENTORY_QUANTITY", "1"),
        ).strip()
        try:
            self.inventory_quantity = int(quantity_raw)
        except ValueError as exc:
            raise ValueError(
                "MEN_ACCESSORIES_INVENTORY_QUANTITY must be a non-negative integer"
            ) from exc
        if self.inventory_quantity < 0:
            raise ValueError(
                "MEN_ACCESSORIES_INVENTORY_QUANTITY must be a non-negative integer"
            )
        self.inventory_policy = os.getenv(
            "MEN_ACCESSORIES_INVENTORY_POLICY",
            os.getenv("UPLOAD_SAREE_INVENTORY_POLICY", "DENY"),
        ).strip().upper()
        if self.inventory_policy not in {"DENY", "CONTINUE"}:
            raise ValueError(
                "MEN_ACCESSORIES_INVENTORY_POLICY must be DENY or CONTINUE"
            )
        self.location_name = os.getenv(
            "MEN_ACCESSORIES_LOCATION_NAME",
            os.getenv("SHOPIFY_LOCATION_NAME", "Janardhana Silk House JC Road"),
        ).strip()
        if self.inventory_tracked and not self.location_name:
            raise ValueError("SHOPIFY_LOCATION_NAME cannot be empty")
        self.allow_location_fallback = env_bool(
            "MEN_ACCESSORIES_ALLOW_LOCATION_FALLBACK", False
        )
        self.taxable = env_bool("MEN_ACCESSORIES_TAXABLE", True)
        max_tags_raw = os.getenv("MEN_ACCESSORIES_MAX_TAGS", "6").strip()
        try:
            self.max_clean_tags = int(max_tags_raw)
        except ValueError as exc:
            raise ValueError("MEN_ACCESSORIES_MAX_TAGS must be an integer from 1 to 6") from exc
        if not 1 <= self.max_clean_tags <= 6:
            raise ValueError("MEN_ACCESSORIES_MAX_TAGS must be an integer from 1 to 6")
        self.tag_style = os.getenv("MEN_ACCESSORIES_TAG_STYLE", "clean").strip().casefold()
        if self.tag_style != "clean":
            raise ValueError("MEN_ACCESSORIES_TAG_STYLE must be clean")
        self.fix_inventory = env_bool("FIX_INVENTORY", False)
        self.fix_tags = env_bool("FIX_TAGS", False)
        self.fix_legacy_id = os.getenv("FIX_SHOPIFY_PRODUCT_LEGACY_ID", "").strip()
        self.image_source = os.getenv(
            "MEN_ACCESSORIES_IMAGE_SOURCE", "baserow_file_url"
        ).strip().casefold()
        if self.image_source != "baserow_file_url":
            raise ValueError("MEN_ACCESSORIES_IMAGE_SOURCE must be baserow_file_url")
        self.exact_file_fallback = env_bool(
            "MEN_ACCESSORIES_EXACT_FILE_FALLBACK", True
        )
        self.verify_source_metadata = env_bool(
            "MEN_ACCESSORIES_VERIFY_SOURCE_METADATA", True
        )
        self.verify_source_hash = env_bool("MEN_ACCESSORIES_VERIFY_SOURCE_HASH", True)
        self.fail_on_source_mismatch = env_bool(
            "MEN_ACCESSORIES_FAIL_ON_SOURCE_MISMATCH", True
        )
        self.block_thumbnail_urls = env_bool(
            "MEN_ACCESSORIES_BLOCK_THUMBNAIL_URLS", True
        )
        self.resize_before_shopify = env_bool(
            "MEN_ACCESSORIES_RESIZE_BEFORE_SHOPIFY", False
        )
        try:
            self.resize_target_width = int(
                os.getenv("MEN_ACCESSORIES_TARGET_WIDTH", "2304")
            )
            self.resize_target_height = int(
                os.getenv("MEN_ACCESSORIES_TARGET_HEIGHT", "4096")
            )
            self.jpeg_export_quality = int(
                os.getenv("MEN_ACCESSORIES_JPEG_EXPORT_QUALITY", "95")
            )
        except ValueError as exc:
            raise ValueError(
                "Men Accessories resize dimensions and JPEG quality must be integers"
            ) from exc
        if self.resize_target_width <= 0 or self.resize_target_height <= 0:
            raise ValueError("Men Accessories resize dimensions must be positive")
        if not 1 <= self.jpeg_export_quality <= 100:
            raise ValueError("MEN_ACCESSORIES_JPEG_EXPORT_QUALITY must be 1..100")
        self.resize_filter = os.getenv(
            "MEN_ACCESSORIES_RESIZE_FILTER", "LANCZOS"
        ).strip().upper()
        if self.resize_filter != "LANCZOS":
            raise ValueError("MEN_ACCESSORIES_RESIZE_FILTER must be LANCZOS")
        self.resize_format = os.getenv(
            "MEN_ACCESSORIES_RESIZE_FORMAT", "preserve"
        ).strip().casefold()
        if self.resize_format != "preserve":
            raise ValueError("MEN_ACCESSORIES_RESIZE_FORMAT must be preserve")
        self.require_target_dimensions = env_bool(
            "MEN_ACCESSORIES_REQUIRE_TARGET_DIMENSIONS", True
        )
        self.resize_fields = {
            value.strip()
            for value in os.getenv(
                "MEN_ACCESSORIES_RESIZE_FIELDS",
                ",".join(
                    label
                    for label in self.config["image_order"]
                    if label != "Tie Image"
                ),
            ).split(",")
            if value.strip()
        }
        if self.resize_before_shopify and not self.resize_fields:
            raise ValueError(
                "MEN_ACCESSORIES_RESIZE_FIELDS cannot be empty when resizing"
            )
        self.keep_failed_temp_files = env_bool(
            "MEN_ACCESSORIES_KEEP_FAILED_TEMP_FILES", False
        )
        self.resize_temp_root = ROOT / "temp" / "men_accessories_shopify"
        min_front_width_raw = os.getenv(
            "MEN_ACCESSORIES_MIN_FRONT_WIDTH", "0"
        ).strip()
        recommended_front_width_raw = os.getenv(
            "MEN_ACCESSORIES_RECOMMENDED_FRONT_WIDTH", "1152"
        ).strip()
        try:
            self.min_front_width = int(min_front_width_raw)
            self.recommended_front_width = int(recommended_front_width_raw)
        except ValueError as exc:
            raise ValueError(
                "MEN_ACCESSORIES_MIN_FRONT_WIDTH and "
                "MEN_ACCESSORIES_RECOMMENDED_FRONT_WIDTH must be positive integers"
            ) from exc
        if self.min_front_width < 0 or self.recommended_front_width <= 0:
            raise ValueError(
                "MEN_ACCESSORIES_MIN_FRONT_WIDTH and "
                "MEN_ACCESSORIES_RECOMMENDED_FRONT_WIDTH must be non-negative "
                "and positive integers respectively"
            )
        if self.recommended_front_width < self.min_front_width:
            raise ValueError(
                "MEN_ACCESSORIES_RECOMMENDED_FRONT_WIDTH cannot be below "
                "MEN_ACCESSORIES_MIN_FRONT_WIDTH"
            )
        self.require_min_front_width = env_bool(
            "MEN_ACCESSORIES_REQUIRE_MIN_FRONT_WIDTH", False
        )
        if self.require_min_front_width and self.min_front_width <= 0:
            raise ValueError(
                "MEN_ACCESSORIES_MIN_FRONT_WIDTH must be above zero when "
                "MEN_ACCESSORIES_REQUIRE_MIN_FRONT_WIDTH=true"
            )
        self.confirm_full_sync = env_bool(
            "CONFIRM_MEN_ACCESSORIES_FULL_SYNC", False
        )
        self.acknowledge_external_deletion = env_bool(
            "ACKNOWLEDGE_MEN_ACCESSORIES_EXTERNAL_DELETION", False
        )
        self.image_upload_mode = os.getenv(
            "MEN_ACCESSORIES_IMAGE_UPLOAD_MODE", "original_only"
        ).strip().casefold()
        if self.image_upload_mode != "original_only":
            raise ValueError(
                "MEN_ACCESSORIES_IMAGE_UPLOAD_MODE must be original_only"
            )
        self.disable_compression = env_bool(
            "MEN_ACCESSORIES_DISABLE_COMPRESSION", True
        )
        self.disable_resize = env_bool("MEN_ACCESSORIES_DISABLE_RESIZE", True)
        self.disable_reencode = env_bool(
            "MEN_ACCESSORIES_DISABLE_REENCODE", True
        )
        self.keep_exact_original = env_bool(
            "MEN_ACCESSORIES_KEEP_EXACT_ORIGINAL", True
        )
        self.fail_if_original_upload_fails = env_bool(
            "MEN_ACCESSORIES_FAIL_IF_ORIGINAL_UPLOAD_FAILS", True
        )
        self.allow_compression_fallback = env_bool(
            "MEN_ACCESSORIES_COMPRESS_OVER_LIMIT",
            env_bool("MEN_ACCESSORIES_ALLOW_COMPRESSION_FALLBACK", True),
        )
        if not self.resize_before_shopify and not all(
            (
                self.disable_compression,
                self.disable_resize,
                self.disable_reencode,
                self.keep_exact_original,
            )
        ):
            raise ValueError(
                "Men Accessories original-only mode requires compression, resize, "
                "and re-encode disabled with exact original preservation enabled"
            )
        compress_mb_raw = os.getenv(
            "MEN_ACCESSORIES_IMAGE_MAX_MB",
            os.getenv("MEN_ACCESSORIES_MAX_ORIGINAL_UPLOAD_MB", "20"),
        ).strip()
        try:
            self.compress_only_over_mb = Decimal(compress_mb_raw)
        except InvalidOperation as exc:
            raise ValueError(
                "MEN_ACCESSORIES_MAX_ORIGINAL_UPLOAD_MB must be a positive number"
            ) from exc
        if self.compress_only_over_mb <= 0:
            raise ValueError(
                "MEN_ACCESSORIES_MAX_ORIGINAL_UPLOAD_MB must be a positive number"
            )
        self.image_max_bytes = int(
            self.compress_only_over_mb * Decimal(1024 * 1024)
        )
        if self.fix_direct_product and (
            not self.fix_legacy_id or not self.fix_legacy_id.isdigit()
        ):
            raise ValueError(
                "FIX_SHOPIFY_PRODUCT_LEGACY_ID must be a numeric Shopify product ID"
            )
        if (
            self.audit_image_quality
            or self.repair_media_quality
            or self.repair_resized_media
            or self.reconcile_shopify_sync
        ):
            # These explicit modes take precedence over a persisted
            # CREATE_PRODUCTS_FROM_MEN_ACCESSORIES=true in .env.
            self.enabled = False
        enabled_modes = sum(
            bool(value)
            for value in (
                self.enabled,
                self.fix_created_products,
                self.fix_direct_product,
                self.repair_media_quality,
                self.repair_resized_media,
                self.audit_image_quality,
                self.reconcile_shopify_sync,
            )
        )
        if enabled_modes > 1:
            raise ValueError(
                "Enable only one of CREATE_PRODUCTS_FROM_MEN_ACCESSORIES, "
                "FIX_MEN_ACCESSORIES_CREATED_PRODUCTS, or "
                "FIX_MEN_ACCESSORIES_PRODUCT_PUBLISHING, "
                "REPAIR_MEN_ACCESSORIES_MEDIA_QUALITY, or "
                "REPAIR_MEN_ACCESSORIES_RESIZED_MEDIA, or "
                "AUDIT_MEN_ACCESSORIES_IMAGE_QUALITY"
            )

    @staticmethod
    def required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise ValueError(f"Missing required environment variable: {name}")
        return value

    def fetch_and_validate_fields(self) -> None:
        url = f"{self.baserow.base_url}/api/database/fields/table/{self.table_id}/"
        response = self.baserow.session.get(url, timeout=45)
        response.raise_for_status()
        metadata = response.json()
        by_name = {str(field["name"]).strip().casefold(): field for field in metadata}
        by_id = {
            int(field["id"]): field
            for field in metadata
            if field.get("id") is not None
        }
        aliases: dict[str, set[str]] = {
            "Generated Front View": {
                "generated front view",
                "front view",
                "full-length front flat-lay view",
                "field_8133053",
                "field_9684129",
            },
            "Rolled Tie Hero View": {"rolled tie hero view", "field_9684125"},
            "Diagonal Close-Up Detail View": {
                "diagonal close-up detail view",
                "diagonal close up detail view",
                "field_9684126",
            },
            "Opened Back Construction View": {
                "opened back construction view",
                "field_9684127",
            },
            "Full-Length Front Flat-Lay View": {
                "full-length front flat-lay view",
                "front view",
                "field_9684129",
            },
            "Comment": {"comment", "comment / notes", "comments", "error notes"},
            "Descriptions": {"descriptions", "description"},
            "Price": {"price", "price (inr)"},
        }
        discovered: dict[str, Any] = {}
        for configured_name, configured_id in self.config["field_ids"].items():
            found = by_id.get(int(configured_id)) if configured_id else None
            candidate_names = aliases.get(configured_name, {configured_name.casefold()})
            if found is None:
                matches = [
                    field
                    for key, field in by_name.items()
                    if key in candidate_names
                ]
                if len(matches) == 1:
                    found = matches[0]
                elif len(matches) > 1:
                    raise RuntimeError(
                        f"Field {configured_name} resolved to multiple Baserow fields"
                    )
            if found is None:
                if configured_name in self.config.get("required_field_names", []):
                    raise RuntimeError(
                        f"Required field {configured_name} could not be uniquely resolved"
                    )
                discovered[configured_name] = {"configured_id": configured_id, "resolved": False}
                continue
            actual_name = str(found["name"]).strip()
            if configured_id and int(found["id"]) != int(configured_id):
                raise RuntimeError(
                    f"Field {configured_name} expected ID {configured_id}, found {found['id']}"
                )
            if actual_name.casefold() not in candidate_names:
                if configured_name in self.config.get("required_field_names", []):
                    raise RuntimeError(
                        f"Field ID {configured_id} expected {configured_name}, found {found['name']}"
                    )
            self.fields[configured_name] = found
            discovered[configured_name] = {
                "configured_id": configured_id,
                "resolved_id": found.get("id"),
                "resolved_name": actual_name,
                "field_type": found.get("type", ""),
                "resolved": True,
            }
        generation = self.fields["Generation Status"]
        for option in generation.get("select_options", []):
            option_name = normalize_status(option)
            if option_name == "shopify-sync":
                self.status_option_id = int(option["id"])
            elif option_name == "approved":
                self.approved_status_option_id = int(option["id"])
        if self.status_option_id is None:
            raise RuntimeError("Generation Status is missing the Shopify-sync option")
        if self.approved_status_option_id is None:
            raise RuntimeError("Generation Status is missing the Approved option")
        self.write_field_audit(metadata, discovered)

    def write_field_audit(
        self, metadata: list[dict[str, Any]], discovered: dict[str, Any]
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        report = {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "database_id": self.config["database_id"],
            "table_id": int(self.table_id),
            "table_name": self.config["table_name"],
            "fields": discovered,
            "all_fields": [
                {
                    "id": field.get("id"),
                    "name": field.get("name"),
                    "type": field.get("type"),
                }
                for field in metadata
            ],
        }
        (OUTPUT / "men_accessories_baserow_field_audit.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        lines = [
            "Men Accessories Baserow Field Audit",
            f"Checked UTC: {report['checked_at_utc']}",
            f"Table: {self.config['table_name']} ({self.table_id})",
            "",
            "Resolved fields:",
        ]
        for name, item in discovered.items():
            if item.get("resolved"):
                lines.append(
                    f"- {name}: {item.get('resolved_name')} "
                    f"({item.get('resolved_id')}) type={item.get('field_type')}"
                )
            else:
                lines.append(f"- {name}: not resolved")
        (OUTPUT / "men_accessories_baserow_field_audit.txt").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def row_value(self, row: dict[str, Any], name: str) -> Any:
        if name not in self.fields:
            return ""
        actual = self.fields[name]["name"]
        return row.get(actual)

    def images(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for label in self.config["image_order"]:
            files = self.row_value(row, label) or []
            if isinstance(files, dict):
                files = [files]
            for index, raw_file in enumerate(files):
                file_obj = get_baserow_source_file({label: [raw_file]}, label)
                if not file_obj:
                    continue
                url = str(file_obj["url"])
                normalized = url.split("?", 1)[0]
                if normalized in seen:
                    continue
                seen.add(normalized)
                result.append(
                    {
                        "label": label,
                        "field_index": index,
                        "url": url,
                        "size": int(file_obj.get("size") or 0),
                        "mime_type": visible(file_obj.get("mime_type")),
                        "image_width": int(file_obj.get("image_width") or 0),
                        "image_height": int(file_obj.get("image_height") or 0),
                        "is_image": file_obj.get("is_image"),
                        "name": visible(file_obj.get("name"))
                        or Path(url.split("?", 1)[0]).name
                        or "image",
                        "visible_name": visible(file_obj.get("visible_name")),
                    }
                )
        return result

    def featured_image_label(self, images: list[dict[str, Any]]) -> str:
        available = {image.get("label") for image in images}
        for label in self.config.get("featured_image_fallback_order", []):
            if label in available:
                return label
        return str(images[0]["label"]) if images else ""

    def move_featured_image_first(
        self, images: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        featured_label = self.featured_image_label(images)
        if not featured_label:
            return images
        featured: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []
        moved = False
        for image in images:
            if not moved and image.get("label") == featured_label:
                featured.append(image)
                moved = True
            else:
                others.append(image)
        return featured + others

    def prepare_images_before_create(
        self,
        images: list[dict[str, Any]],
        sku: str,
        row_id: Any,
    ) -> tuple[list[dict[str, Any]], Path | None]:
        if not self.resize_before_shopify:
            return images, None
        image_tools = load_image_prepare()
        self.resize_temp_root.mkdir(parents=True, exist_ok=True)
        product_dir = self.resize_temp_root / (
            f"{row_id}-{slugify(sku)}-{uuid.uuid4().hex[:8]}"
        )
        product_dir.mkdir(parents=True, exist_ok=False)
        prepared_images: list[dict[str, Any]] = []
        try:
            for image in images:
                prepared = dict(image)
                if image["label"] not in self.resize_fields:
                    prepared_images.append(prepared)
                    continue
                source_url = str(image["url"])
                if "/thumbnails/" in source_url.casefold():
                    raise ValueError("thumbnail_url_blocked")
                if "/user_files/" not in source_url.casefold():
                    raise ValueError("baserow_user_files_url_required")
                source_name = Path(
                    visible(image.get("name"))
                    or source_url.split("?", 1)[0]
                ).name
                source_path = product_dir / source_name
                image_tools.download_baserow_source_file(
                    source_url, source_path
                )
                source_suffix = source_path.suffix.casefold()
                if source_suffix not in {".png", ".jpg", ".jpeg"}:
                    raise ValueError(
                        f"unsupported_resize_extension:{source_suffix}"
                    )
                output_name = (
                    f"{source_path.stem}-shopify-"
                    f"{self.resize_target_width}x{self.resize_target_height}"
                    f"{source_path.suffix}"
                )
                output_path = product_dir / output_name
                try:
                    resize = image_tools.resize_for_shopify(
                        source_path,
                        output_path,
                        self.resize_target_width,
                        self.resize_target_height,
                        jpeg_quality=self.jpeg_export_quality,
                    )
                except Exception as exc:
                    if "front_view_aspect_ratio_invalid" in str(exc):
                        raise ValueError("generated_front_aspect_ratio_invalid") from exc
                    raise
                if self.require_target_dimensions and (
                    resize["output_width"] != self.resize_target_width
                    or resize["output_height"] != self.resize_target_height
                ):
                    raise ValueError("resized_output_dimension_mismatch")
                if resize["output_size_bytes"] > self.image_max_bytes:
                    raise ValueError("resized_output_exceeds_shopify_media_limit")
                prepared.update(
                    {
                        "resize_enabled": True,
                        "source_path": str(source_path),
                        "source_sha256": sha256_file(source_path),
                        "local_path": str(output_path),
                        "output_filename": output_name,
                        "output_width": resize["output_width"],
                        "output_height": resize["output_height"],
                        "output_size": resize["output_size_bytes"],
                        "output_mime": resize["output_mime"],
                        "output_sha256": sha256_file(output_path),
                        "resize_filter": resize["resize_filter"],
                    }
                )
                prepared_images.append(prepared)
            return prepared_images, product_dir
        except Exception:
            if not self.keep_failed_temp_files:
                shutil.rmtree(product_dir, ignore_errors=True)
            raise

    @staticmethod
    def cleanup_prepared_images(product_dir: Path | None) -> None:
        if product_dir:
            shutil.rmtree(product_dir, ignore_errors=True)

    def validate_generated_front_image(
        self,
        image: dict[str, Any],
        *,
        row_id: Any = "",
        sku: str = "",
    ) -> dict[str, Any]:
        """Validate Baserow's original Generated Front View before any Shopify mutation."""
        source_url = str(image.get("url") or "").strip()
        source_width = int(image.get("image_width") or 0)
        source_height = int(image.get("image_height") or 0)
        source_size = int(image.get("size") or 0)
        source_mime = visible(image.get("mime_type")).split(";", 1)[0].casefold()
        errors: list[str] = []

        if not source_url:
            errors.append("generated_front_original_url_missing")
        if source_url and "/user_files/" not in source_url.casefold():
            errors.append("generated_front_not_baserow_user_files_url")
        if "/thumbnails/" in source_url.casefold():
            errors.append("generated_front_thumbnail_url_blocked")
        if image.get("is_image") is not True:
            errors.append("generated_front_is_image_not_true")
        if source_mime not in SUPPORTED_SHOPIFY_IMAGE_MIME_TYPES:
            errors.append("generated_front_unsupported_mime_type")
        if source_width <= 0 or source_height <= 0:
            errors.append("generated_front_dimensions_missing")
        if self.require_min_front_width and source_width < self.min_front_width:
            errors.append("generated_front_resolution_too_low")
        if source_size <= 0:
            errors.append("generated_front_size_missing")
        elif source_size > self.image_max_bytes and not self.allow_compression_fallback:
            errors.append("generated_front_exceeds_shopify_media_size_limit")

        eligible = not errors
        if not eligible:
            result = (
                "resolution_gate_rejected"
                if "generated_front_resolution_too_low" in errors
                else errors[0]
            )
        elif self.require_min_front_width:
            result = "resolution_gate_passed"
        elif source_width < self.recommended_front_width:
            result = "resolution_advisory"
        else:
            result = "resolution_gate_disabled"
        error = "; ".join(errors)
        if "generated_front_resolution_too_low" in errors:
            error = (
                f"generated_front_resolution_too_low: actual "
                f"{source_width} x {source_height}; configured minimum width "
                f"{self.min_front_width}"
            )
        return {
            "Baserow Row ID": row_id,
            "Product Code": sku,
            "Image Field": "Generated Front View",
            "Source URL": source_url,
            "Baserow Source URL": source_url,
            "Source URL Type": "file_obj.url",
            "Source Width": source_width or "",
            "Source Height": source_height or "",
            "Source Size": source_size or "",
            "Source MIME": source_mime,
            "Minimum Width Required": self.min_front_width,
            "Recommended Width": self.recommended_front_width,
            "Resolution Gate Enabled": (
                "yes" if self.require_min_front_width else "no"
            ),
            "Resolution Result": result,
            "Resolution Eligible": "yes" if eligible else "no",
            "Shopify Media ID": "",
            "Shopify Media Status": "",
            "Shopify Width": "",
            "Shopify Height": "",
            "Metadata Match": "",
            "Generated Front View First": "",
            "Result": result,
            "Error": error,
        }

    def missing_required(self, row: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        if normalize_status(self.row_value(row, "Generation Status")) != "approved":
            missing.append("Generation Status not Approved")
        if (
            self.require_shopify_notes_approved
            and normalize_status(self.row_value(row, "SHOPIFY Notes")) != "approved"
        ):
            missing.append("SHOPIFY Notes not Approved")
        for name in ("Product Title", "Product Code", "Price"):
            if not visible(self.row_value(row, name)):
                missing.append(f"{name} missing")
        if not self.images(row):
            missing.append("Men Accessories image missing")
        return missing

    def approval_ok(self, row: dict[str, Any]) -> bool:
        primary_ok = normalize_status(self.row_value(row, "Generation Status")) == "approved"
        shopify_notes = normalize_status(self.row_value(row, "SHOPIFY Notes"))
        secondary_ok = not (
            self.require_shopify_notes_approved
            and shopify_notes
            and shopify_notes != "approved"
        )
        return primary_ok and secondary_ok

    def validate_run_scope(self) -> None:
        if self.max_products is None and not self.confirm_full_sync:
            raise RuntimeError(
                "Full Men Accessories sync is blocked. Set "
                "CONFIRM_MEN_ACCESSORIES_FULL_SYNC=true explicitly."
            )

    def run_cap_reached(self) -> bool:
        if self.max_products is None:
            return False
        completed = (
            len(self.state.preview)
            if self.dry_run
            else (
                self.state.processed_success
                if isinstance(self.state.processed_success, int)
                else self.state.created
            )
        )
        return completed >= self.max_products

    def missing_required_fields(self, row: dict[str, Any]) -> list[str]:
        missing = [
            f"{name} missing"
            for name in ("Product Title", "Product Code", "Price")
            if not visible(self.row_value(row, name))
        ]
        if not self.images(row):
            missing.append("Men Accessories image missing")
        return missing

    def validate_image_urls(self, images: list[dict[str, str]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for image in images:
            self.state.image_urls_checked += 1
            result = {
                "label": image["label"],
                "url": image["url"],
                "valid": False,
                "status_code": None,
                "content_type": "",
                "error": "",
            }
            try:
                response = requests.head(
                    image["url"], allow_redirects=True, timeout=30
                )
                if response.status_code in {403, 405}:
                    response = requests.get(
                        image["url"], stream=True, timeout=45
                    )
                content_type = response.headers.get("Content-Type", "")
                result.update(
                    {
                        "status_code": response.status_code,
                        "content_type": content_type,
                        "valid": response.status_code == 200
                        and content_type.casefold().startswith("image/"),
                    }
                )
                response.close()
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            if result["valid"]:
                self.state.image_urls_valid += 1
            else:
                self.state.image_urls_invalid += 1
            results.append(result)
        return results

    def resolve_collection_for_plan(
        self, row: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        title = self.collection_title(row)
        if not title:
            raise ValueError(
                "No collection mapping for catalog or Category"
            )
        if title not in self.collection_cache:
            self.collection_cache[title] = self.shopify.find_collection(title)
        collection = self.collection_cache[title]
        if not collection:
            raise ValueError(f"Mapped Shopify collection not found: {title}")
        return title, collection

    def get_collection_id_by_title(self, title: str) -> str:
        collection_id = self.shopify.get_collection_id_by_title(title)
        if not collection_id:
            if title.casefold() == "men's ties":
                raise ShopifyError("mens_ties_collection_not_found")
            if title.casefold() == self.new_arrivals_collection_name.casefold():
                raise ShopifyError("new_arrivals_collection_not_found")
            raise ShopifyError(f"collection_not_found:{title}")
        return collection_id

    def assign_and_verify_required_collections(
        self, product_id: str, row: dict[str, Any] | None
    ) -> dict[str, Any]:
        primary_title = self.collection_title(row) if row is not None else ""
        if not primary_title:
            raise ShopifyError("primary_collection_mapping_missing")
        primary_id = self.get_collection_id_by_title(primary_title)
        primary_assigned, _ = self.shopify.ensure_product_in_collection(
            product_id, primary_id
        )
        secondary_id = ""
        secondary_assigned = False
        secondary_already = False
        if self.secondary_collection_title:
            secondary_id = self.get_collection_id_by_title(
                self.secondary_collection_title
            )
            if (
                self.secondary_collection_id
                and secondary_id != self.secondary_collection_id
            ):
                raise ShopifyError(
                    "secondary_collection_id_mismatch:"
                    f"expected={self.secondary_collection_id}:actual={secondary_id}"
                )
            secondary_assigned, secondary_already = (
                self.shopify.ensure_product_in_collection(product_id, secondary_id)
            )
        memberships = self.shopify.product_collection_ids(product_id)
        if primary_id not in memberships:
            raise ShopifyError("primary_collection_membership_missing")
        if secondary_id and secondary_id not in memberships:
            raise ShopifyError("secondary_collection_membership_missing")
        details: dict[str, Any] = {
            "Primary Collection": primary_title,
            "Primary Collection ID": primary_id,
            "Primary Collection Assigned": "yes" if primary_assigned else "no",
            "Secondary Collection": self.secondary_collection_title,
            "Secondary Collection ID": secondary_id,
            "Secondary Collection Assigned": "yes" if secondary_assigned else "no",
            "Secondary Collection Already Assigned": (
                "yes" if secondary_already else "no"
            ),
            "New Arrivals Collection ID": "",
            "New Arrivals Assignment Attempted": "no",
            "New Arrivals Assigned": "disabled",
            "New Arrivals Already Assigned": "no",
            "New Arrivals Error": "",
        }
        if not self.add_new_arrivals:
            return details
        details["New Arrivals Assignment Attempted"] = "yes"
        try:
            new_arrivals_id = self.get_collection_id_by_title(
                self.new_arrivals_collection_name
            )
            assigned, already = self.shopify.ensure_product_in_collection(
                product_id, new_arrivals_id
            )
            memberships = self.shopify.product_collection_ids(product_id)
            if new_arrivals_id not in memberships:
                raise ShopifyError("new_arrivals_membership_missing")
            details.update(
                {
                    "New Arrivals Collection ID": new_arrivals_id,
                    "New Arrivals Assigned": "yes" if assigned else "no",
                    "New Arrivals Already Assigned": "yes" if already else "no",
                }
            )
            return details
        except Exception as exc:
            details["New Arrivals Error"] = f"{type(exc).__name__}: {exc}"
            self.state.new_arrivals_assignment_failures += 1
            raise ShopifyError(
                f"new_arrivals_assignment_failed: {exc}"
            ) from exc

    def audit_reference_product(self) -> None:
        reference_gid = self.config["reference_product_gid"]
        query = """query($id:ID!){
          product(id:$id){
            id legacyResourceId title handle descriptionHtml vendor productType
            status templateSuffix tags
            seo{title description}
            category{id name fullName}
            collections(first:50){nodes{id title handle}}
            variants(first:10){
              nodes{
                id sku price compareAtPrice taxable inventoryPolicy
                inventoryItem{id tracked}
              }
            }
            media(first:50){
              nodes{
                id alt status mediaContentType
                ... on MediaImage{image{width height url}}
              }
            }
          }
        }"""
        product = self.shopify.graphql(query, {"id": reference_gid}).get("product")
        if not product:
            raise RuntimeError("men_accessories_reference_product_not_found")
        collections = product.get("collections", {}).get("nodes", [])
        primary = next(
            (
                collection
                for collection in collections
                if visible(collection.get("title")).casefold()
                == self.primary_collection_title.casefold()
            ),
            None,
        )
        if not primary:
            found = self.shopify.find_collection(self.primary_collection_title)
            if not found:
                raise RuntimeError("men_accessories_primary_collection_not_found")
            primary = found
        category = product.get("category") or {}
        self.reference_audit = {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "reference_product_url": self.config["reference_product_url"],
            "product": product,
            "primary_collection": primary,
            "copy_reference_template": self.copy_reference_template,
            "copy_reference_taxonomy": self.copy_reference_taxonomy,
            "copy_promotional_collections": self.copy_promotional_collections,
        }
        self.reference_collection_gid = primary["id"]
        self.reference_collection_title = visible(primary.get("title"))
        self.reference_template_suffix = visible(product.get("templateSuffix"))
        self.reference_taxonomy_gid = visible(category.get("id"))
        self.write_reference_product_audit()

    def write_reference_product_audit(self) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / "men_accessories_reference_product_audit.json").write_text(
            json.dumps(self.reference_audit, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        product = self.reference_audit.get("product", {})
        category = product.get("category") or {}
        variants = product.get("variants", {}).get("nodes", [])
        media = product.get("media", {}).get("nodes", [])
        lines = [
            "Men Accessories Reference Product Audit",
            f"Checked UTC: {self.reference_audit.get('checked_at_utc')}",
            f"Product ID: {product.get('id', '')}",
            f"Legacy ID: {product.get('legacyResourceId', '')}",
            f"Title: {product.get('title', '')}",
            f"Handle: {product.get('handle', '')}",
            f"Vendor: {product.get('vendor', '')}",
            f"Product type: {product.get('productType', '')}",
            f"Status: {product.get('status', '')}",
            f"Template suffix: {product.get('templateSuffix') or ''}",
            f"Taxonomy: {category.get('fullName') or category.get('name') or ''}",
            f"Taxonomy ID: {category.get('id', '')}",
            f"Primary collection: {self.reference_collection_title} ({self.reference_collection_gid})",
            f"Variants: {len(variants)}",
            f"Media items: {len(media)}",
            f"Tags: {', '.join(product.get('tags') or [])}",
        ]
        (OUTPUT / "men_accessories_reference_product_audit.txt").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def count_shopify_duplicate(self, reason: str) -> None:
        if reason == "existing_sku":
            self.state.existing_shopify_skus += 1
        elif reason == "existing_handle":
            self.state.existing_shopify_handles += 1
        elif reason == "possible_duplicate_title":
            self.state.existing_shopify_titles += 1

    def find_duplicates(self, sku: str, handle: str, title: str) -> tuple[str, dict[str, Any] | None]:
        q = """query($sku:String!,$handle:String!,$title:String!){
          variants:productVariants(first:10,query:$sku){nodes{sku product{id title handle}}}
          handles:products(first:10,query:$handle){nodes{id title handle}}
          titles:products(first:20,query:$title){nodes{id title handle}}
        }"""
        data = self.shopify.graphql(
            q,
            {
                "sku": f'sku:"{gql_escape(sku)}"',
                "handle": f'handle:"{gql_escape(handle)}"',
                "title": f'title:"{gql_escape(title)}"',
            },
        )
        sku_matches = [node for node in data["variants"]["nodes"] if visible(node.get("sku")) == sku]
        if sku_matches:
            return "existing_sku", sku_matches[0]["product"]
        handle_matches = [
            node for node in data["handles"]["nodes"] if visible(node.get("handle")).casefold() == handle.casefold()
        ]
        if handle_matches:
            return "existing_handle", handle_matches[0]
        title_matches = [
            node for node in data["titles"]["nodes"] if visible(node.get("title")).casefold() == title.casefold()
        ]
        if title_matches:
            return "possible_duplicate_title", title_matches[0]
        return "", None

    @staticmethod
    def normalized_sku_for_search(value: Any) -> str:
        return re.sub(r"\s+", "", visible(value)).casefold()

    def find_products_by_sku(self, sku: str) -> list[dict[str, Any]]:
        query = """query($query:String!){
          productVariants(first:50,query:$query){
            nodes{sku product{id legacyResourceId title handle status}}
          }
        }"""
        nodes = self.shopify.graphql(
            query, {"query": f'sku:"{gql_escape(sku)}"'}
        )["productVariants"]["nodes"]
        expected = self.normalized_sku_for_search(sku)
        products: dict[str, dict[str, Any]] = {}
        for node in nodes:
            if self.normalized_sku_for_search(node.get("sku")) != expected:
                continue
            product = node.get("product") or {}
            if product.get("id"):
                products[str(product["id"])] = product
        return list(products.values())

    @staticmethod
    def shopify_admin_product_url(product: dict[str, Any]) -> str:
        legacy_id = visible(product.get("legacyResourceId"))
        if not legacy_id:
            legacy_id = visible(product.get("id")).rsplit("/", 1)[-1]
        return (
            "https://admin.shopify.com/store/janardhana-silk-house-2/products/"
            + legacy_id
        )

    def deterministic_ai(self, row: dict[str, Any], sku: str) -> dict[str, Any]:
        source_title = visible(self.row_value(row, "Product Title"))
        title = source_title if sku.casefold() in source_title.casefold() else f"{source_title} {sku}"
        description = visible(self.row_value(row, "Descriptions"))
        intro = description or "A men's tie from Janardhana Silk House."
        html = (
            f"<h2>Introduction</h2><p>{self.html_escape(intro)}</p>"
            "<h2>Fabric &amp; Craftsmanship</h2><p>Refer to the supplied product details and imagery for material and construction information.</p>"
            "<h2>Styling &amp; Occasion</h2><p>Pair this tie according to your preferred outfit and occasion.</p>"
            "<h2>Product Highlights</h2><ul>"
            f"<li><strong>Product Code:</strong> {self.html_escape(sku)}</li></ul>"
            "<h2>Material &amp; Wash Care</h2><p>Follow the care information supplied with the product.</p>"
            "<h2>Note</h2><p>Colours may vary slightly due to lighting and screen settings.</p>"
        )
        return {
            "title": title[:255],
            "description_html": html,
            "seo_title": title[:70],
            "seo_description": re.sub("<[^>]+>", " ", html)[:320],
            "image_alt_text": f"{title} - Generated Front View",
            "tags": [],
            "product_highlights": {
                "material": "",
                "colour": "",
                "pattern": "",
                "finish": "",
                "occasion": "",
                "product_code": sku,
            },
            "warnings": ["OpenRouter was not called; deterministic preview copy used."],
        }

    @staticmethod
    def html_escape(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def generate_ai(self, row: dict[str, Any], sku: str, images: list[dict[str, str]]) -> dict[str, Any]:
        facts = {
            key: visible(self.row_value(row, key))
            for key in (
                "Product Title",
                "Product Code",
                "Category",
                "Price",
                "Descriptions",
                "Generation Prompt",
            )
        }
        if self.openrouter is None:
            result = self.deterministic_ai(row, sku)
        else:
            result = self.generate_men_accessories_copy(
                facts, [image["url"] for image in images]
            )
        return self.normalize_ai(result, row, sku)

    def generate_men_accessories_copy(
        self, facts: dict[str, Any], image_urls: list[str]
    ) -> dict[str, Any]:
        schema = {
            "title": "",
            "description_html": "",
            "seo_title": "",
            "seo_description": "",
            "image_alt_text": "",
            "tags": [],
            "product_highlights": {
                "material": "",
                "colour": "",
                "pattern": "",
                "finish": "",
                "occasion": "",
                "product_code": "",
            },
            "warnings": [],
        }
        system = (
            "You write accurate premium ecommerce copy for Janardhana Silk House "
            "men's ties. Return one strict JSON object only, without markdown. "
            "Do not invent pure silk, hand finishing, exact width, standard length, "
            "handmade production, natural dyes, pocket squares, or gift packaging "
            "unless supported by source facts or clear image evidence. Never expose "
            "warnings inside customer-facing HTML. The description_html section order "
            "must be: Introduction; Fabric & Craftsmanship; Styling & Occasion; "
            "Product Highlights; Material & Wash Care; Note."
        )
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "Source facts:\n"
                + json.dumps(facts, ensure_ascii=False)
                + "\nRequired JSON shape:\n"
                + json.dumps(schema),
            }
        ]
        if self.use_image_input:
            content.extend(
                {"type": "image_url", "image_url": {"url": url}}
                for url in image_urls[:3]
                if url
            )
        response = self.openrouter.session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json={
                "model": self.openrouter.model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
            },
            timeout=self.openrouter.timeout,
        )
        response.raise_for_status()
        result = json.loads(response.json()["choices"][0]["message"]["content"])
        self.validate_ai_result(result)
        return result

    @staticmethod
    def validate_ai_result(result: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            raise ValueError("OpenRouter result is not an object")
        for key in (
            "title",
            "description_html",
            "seo_title",
            "seo_description",
            "image_alt_text",
        ):
            if not isinstance(result.get(key), str):
                raise ValueError(f"OpenRouter field {key} must be a string")
        if not isinstance(result.get("tags"), list):
            raise ValueError("OpenRouter tags must be an array")
        if not isinstance(result.get("product_highlights"), dict):
            raise ValueError("OpenRouter product_highlights must be an object")
        if not isinstance(result.get("warnings", []), list):
            raise ValueError("OpenRouter warnings must be an array")

    def normalize_ai(self, result: dict[str, Any], row: dict[str, Any], sku: str) -> dict[str, Any]:
        title = visible(self.row_value(row, "Product Title")) or visible(result.get("title"))
        if sku.casefold() not in title.casefold():
            title = f"{title} {sku}"
        result["title"] = title[:255]
        html = visible(result.get("description_html"))
        required_sections = (
            "Introduction",
            "Fabric & Craftsmanship",
            "Styling & Occasion",
            "Product Highlights",
            "Material & Wash Care",
            "Note",
        )
        check_html = html.replace("&amp;", "&")
        positions = [check_html.find(section) for section in required_sections]
        if any(position < 0 for position in positions) or positions != sorted(positions):
            fallback = self.deterministic_ai(row, sku)
            intro_source = re.sub("<[^>]+>", " ", html).strip()
            if intro_source:
                fallback["description_html"] = (
                    f"<h2>Introduction</h2><p>{self.html_escape(intro_source[:800])}</p>"
                    "<h2>Fabric &amp; Craftsmanship</h2><p>Refer to the supplied product details and imagery for material and construction information.</p>"
                    "<h2>Styling &amp; Occasion</h2><p>Pair this tie according to your preferred outfit and occasion.</p>"
                    "<h2>Product Highlights</h2><ul>"
                    f"<li><strong>Product Code:</strong> {self.html_escape(sku)}</li></ul>"
                    "<h2>Material &amp; Wash Care</h2><p>Follow the care information supplied with the product.</p>"
                    "<h2>Note</h2><p>Colours may vary slightly due to lighting and screen settings.</p>"
                )
            result["description_html"] = fallback["description_html"]
            warnings = list(result.get("warnings") or [])
            warnings.append(
                "AI description headings were normalized to the required six-section format."
            )
            result["warnings"] = warnings
            html = result["description_html"]
        if sku.casefold() not in html.casefold():
            raise ValueError("AI description_html does not contain Product Code")
        return result

    def clean_tags(self, row: dict[str, Any] | None, product_title: str = "") -> list[str]:
        source = product_title
        if row is not None:
            source = " ".join(
                [
                    visible(self.row_value(row, "Product Title")),
                    visible(self.row_value(row, "Category")),
                    visible(self.row_value(row, "Descriptions")),
                ]
            )
        normalized = source.casefold()
        tags = ["Men's Ties", "Tie", "Men Accessories"]
        if "silk" in normalized:
            tags.append("Silk Tie")
        if "print" in normalized:
            tags.append("Printed Tie")
        if any(value in normalized for value in ("formal", "business", "office")):
            tags.append("Formal")
        if "wedding" in normalized:
            tags.append("Wedding")
        if any(value in normalized for value in ("festive", "festival", "occasion")):
            tags.append("Traditional")
        clean = [
            tag
            for tag in dict.fromkeys(tags)
            if ":" not in tag
            and tag.casefold()
            not in {"source-table", "product-code", "vendor", "category", "catalog", "automation"}
        ]
        return clean[: self.max_clean_tags]

    def create_product(
        self,
        row: dict[str, Any],
        sku: str,
        handle: str,
        price: str,
        ai: dict[str, Any],
        images: list[dict[str, str]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        tags = self.clean_tags(row)
        category_name = visible(self.row_value(row, "Category"))
        product_input: dict[str, Any] = {
            "title": ai["title"][:255],
            "handle": handle,
            "descriptionHtml": ai["description_html"],
            "vendor": self.config["vendor"],
            "productType": self.config["product_type"],
            "status": "DRAFT",
            "tags": tags,
            "seo": {
                "title": ai["seo_title"][:70],
                "description": ai["seo_description"][:320],
            },
        }
        if self.copy_reference_template and self.reference_template_suffix:
            product_input["templateSuffix"] = self.reference_template_suffix
        category = self.shopify.find_taxonomy_category(category_name) if category_name else None
        category_gid = ""
        if self.copy_reference_taxonomy and self.reference_taxonomy_gid:
            category_gid = self.reference_taxonomy_gid
        elif category:
            category_gid = category["id"]
        else:
            category_gid = self.config.get("default_shopify_taxonomy", {}).get("id", "")
        if category_gid:
            product_input["category"] = category_gid
        collection_title = self.collection_title(row)
        if collection_title:
            if collection_title not in self.collection_cache:
                self.collection_cache[collection_title] = self.shopify.find_collection(collection_title)
            collection = self.collection_cache[collection_title]
            if not collection:
                raise ShopifyError(f"Configured Shopify collection not found: {collection_title}")
            product_input["collectionsToJoin"] = [collection["id"]]
        featured_label = self.featured_image_label(images)
        featured_count = sum(
            1 for image in images if image.get("label") == featured_label
        )
        alt_base = (
            f"{sku} - {featured_label or 'Men Accessories Image'}"
            + (" 1" if featured_count > 1 else "")
        )
        mutation = """mutation($product:ProductCreateInput!,$media:[CreateMediaInput!]){
          productCreate(product:$product,media:$media){
            product{id title handle status variants(first:5){nodes{id sku price}} media(first:100){nodes{id alt status}}}
            userErrors{field message}
          }
        }"""
        # Create the product shell first, then add media one at a time. This makes
        # URL rejection recoverable without recreating the product or re-encoding
        # every source image.
        result = self.shopify.graphql(
            mutation, {"product": product_input, "media": []}
        )["productCreate"]
        if result["userErrors"]:
            raise ShopifyError(str(result["userErrors"]))
        product = result["product"]
        self.current_partial_product_id = product["id"]
        variants = product["variants"]["nodes"]
        if len(variants) != 1:
            raise ShopifyError(f"Expected one initial variant, found {len(variants)}")
        variant_mutation = """mutation($productId:ID!,$variants:[ProductVariantsBulkInput!]!){
          productVariantsBulkUpdate(productId:$productId,variants:$variants){
            productVariants{id sku price inventoryItem{tracked}}
            userErrors{field message}
          }
        }"""
        variant_result = self.shopify.graphql(
            variant_mutation,
            {
                "productId": product["id"],
                "variants": [
                    {
                        "id": variants[0]["id"],
                        "price": price,
                        "inventoryItem": {
                            "sku": sku,
                            "tracked": self.inventory_tracked,
                        },
                        "inventoryPolicy": self.inventory_policy,
                        "taxable": self.taxable,
                    }
                ],
            },
        )["productVariantsBulkUpdate"]
        if variant_result["userErrors"]:
            raise ShopifyError(str(variant_result["userErrors"]))
        upload_images = self.move_featured_image_first(images)
        quality_rows = self.upload_product_images(
            product["id"],
            sku,
            ai["title"],
            alt_base,
            upload_images,
            baserow_row_id=row.get("id", ""),
        )
        self.set_front_first(product["id"], alt_base)
        media_nodes = self.get_product_media(product["id"])
        front_id = media_nodes[0]["id"] if media_nodes else ""
        for record in quality_rows:
            front_first = (
                "yes"
                if record["Image field"] == featured_label
                and record["Shopify media ID"] == front_id
                else "not applicable"
            )
            record["Featured Image main yes/no"] = front_first
            record["Generated Front View main yes/no"] = front_first
            record["Generated Front View First"] = front_first
            self.state.image_quality.append(record)
            if record["Compressed yes/no"] == "yes":
                self.state.compressed_images.append(record.copy())
        front_quality = next(
            (
                quality
                for quality in quality_rows
                if quality["Image field"] == featured_label
            ),
            {},
        )
        if front_quality.get("Media READY yes/no") != "yes":
            raise ShopifyError("Featured Men Accessories media is not READY")
        if front_quality.get("Selected Source Type") != "file_obj.url":
            raise ShopifyError("Featured Men Accessories source was not file_obj.url")
        if front_quality.get("Source Metadata Verified") != "yes":
            raise ShopifyError("Featured Men Accessories source metadata verification failed")
        if front_quality.get("Generated Front View First") != "yes":
            raise ShopifyError("Featured Men Accessories image is not first/main media")
        refreshed = self.get_product_for_fix(product["id"])
        self.current_inventory_sku = sku
        details = self.apply_operational_setup(
            refreshed,
            row,
            configure_inventory=self.inventory_tracked,
            configure_tags=True,
        )
        details.update(front_quality)
        return refreshed, details

    def prepare_media_source(
        self,
        image: dict[str, Any],
        force_stage: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Use Baserow's original file metadata; never select a thumbnail."""
        image_url = str(image["url"])
        image_field = str(image["label"])
        if self.block_thumbnail_urls and "/thumbnails/" in image_url.casefold():
            raise ValueError(
                f"{image_field} selected thumbnail url instead of original"
            )
        metadata_size = int(image.get("size") or 0)
        original_dimensions = (
            f"{image.get('image_width')}x{image.get('image_height')}"
            if image.get("image_width") and image.get("image_height")
            else ""
        )
        record = {
            "Image field": image_field,
            "Image Field": image_field,
            "Source URL": image_url,
            "Source Width": image.get("image_width") or "",
            "Source Height": image.get("image_height") or "",
            "Source Size": metadata_size or "",
            "Source MIME": image.get("mime_type", ""),
            "Source Size Bytes": metadata_size or "",
            "Resize Enabled": (
                "yes" if image.get("resize_enabled") else "no"
            ),
            "Target Width": (
                self.resize_target_width if image.get("resize_enabled") else ""
            ),
            "Target Height": (
                self.resize_target_height if image.get("resize_enabled") else ""
            ),
            "Resize Filter": image.get("resize_filter", ""),
            "Output Filename": image.get("output_filename", ""),
            "Output Width": image.get("output_width", ""),
            "Output Height": image.get("output_height", ""),
            "Output Size Bytes": image.get("output_size", ""),
            "Output MIME": image.get("output_mime", ""),
            "Minimum Width Required": (
                self.min_front_width if image_field == "Generated Front View" else ""
            ),
            "Resolution Eligible": "yes",
            "Baserow Source URL": image_url,
            "Selected Source Type": "file_obj.url",
            "Source Filename": visible(image.get("name"))
            or Path(image_url.split("?", 1)[0]).name
            or "image",
            "Baserow Original URL": image_url,
            "Baserow Original Size Bytes": metadata_size or "",
            "Baserow Size Bytes": metadata_size or "",
            "Baserow Original MIME Type": image.get("mime_type", ""),
            "Baserow MIME Type": image.get("mime_type", ""),
            "Baserow Original Width": image.get("image_width") or "",
            "Baserow Width": image.get("image_width") or "",
            "Baserow Original Height": image.get("image_height") or "",
            "Baserow Height": image.get("image_height") or "",
            "Baserow SHA256": "",
            "Selected Upload URL": image_url,
            "Selected URL Type": "original",
            "Selected URL Contains Thumbnail yes/no": "no",
            "Original file size MB": (
                round(metadata_size / (1024 * 1024), 3)
                if metadata_size
                else ""
            ),
            "Original dimensions": original_dimensions,
            "Upload mode": "external_original_url",
            "Compressed yes/no": "no",
            "Resized yes/no": "no",
            "Reencoded yes/no": "no",
            "Compression reason": "",
            "Final file size MB": (
                round(metadata_size / (1024 * 1024), 3)
                if metadata_size
                else ""
            ),
            "Final dimensions": original_dimensions,
            "JPEG quality used": "",
            "Shopify media ID": "",
            "Shopify Media ID": "",
            "Media READY yes/no": "no",
            "Shopify Media Status": "",
            "Shopify Original Source Size": "",
            "Shopify image width": "",
            "Shopify image height": "",
            "Shopify Width": "",
            "Shopify Height": "",
            "Shopify image URL": "",
            "Shopify Original Source SHA256": "",
            "Exact Bytes Verified": "",
            "Source Metadata Verified": "",
            "Metadata Match": "",
            "Verification Failed": "",
            "Target Dimensions Verified": "",
            "Transfer Method": "direct_original_url",
            "Generated Front View main yes/no": "no",
            "Generated Front View First": "",
            "Warning": "",
            "Error": "",
        }
        if image.get("local_path"):
            output_path = Path(str(image["local_path"]))
            if not output_path.is_file():
                raise ValueError(
                    f"resized_output_missing:{output_path}"
                )
            record.update(
                {
                    "Baserow SHA256": image.get("source_sha256", ""),
                    "Expected Shopify Size Bytes": image.get(
                        "output_size", ""
                    ),
                    "Expected Shopify Width": image.get("output_width", ""),
                    "Expected Shopify Height": image.get("output_height", ""),
                    "Expected Shopify SHA256": image.get(
                        "output_sha256", ""
                    ),
                    "Selected Upload URL": str(output_path),
                    "Selected URL Type": "staged_resized_file",
                    "Upload mode": "staged_resized_file",
                    "Resized yes/no": "yes",
                    "Reencoded yes/no": "yes",
                    "Final file size MB": round(
                        int(image.get("output_size") or 0) / (1024 * 1024),
                        3,
                    ),
                    "Final dimensions": (
                        f"{image.get('output_width')}x"
                        f"{image.get('output_height')}"
                    ),
                    "Transfer Method": "staged_resized_file",
                }
            )
            return self.stage_file(
                output_path,
                str(image.get("output_mime") or "application/octet-stream"),
            ), record
        if self.verify_source_hash or force_stage:
            with tempfile.TemporaryDirectory(prefix="men-accessories-source-") as temp:
                source_path = Path(temp) / record["Source Filename"]
                download_exact_source_file(image_url, source_path)
                actual_size = source_path.stat().st_size
                record["Baserow SHA256"] = sha256_file(source_path)
                if not metadata_size:
                    record["Baserow Original Size Bytes"] = actual_size
                    record["Baserow Size Bytes"] = actual_size
                    record["Original file size MB"] = round(
                        actual_size / (1024 * 1024), 3
                    )
                if force_stage:
                    if actual_size > self.image_max_bytes:
                        if not self.allow_compression_fallback:
                            raise ValueError(
                                "original_over_20mb_compression_fallback_disabled"
                            )
                        image_tools = load_image_prepare()
                        prepared = image_tools.prepare_image_for_shopify(
                            image_url,
                            temp,
                            self.image_max_bytes,
                            force_stage=True,
                        )
                        record.update(
                            {
                                "Upload mode": prepared.upload_mode,
                                "Compressed yes/no": (
                                    "yes" if prepared.compressed else "no"
                                ),
                                "Resized yes/no": (
                                    "yes"
                                    if prepared.original_dimensions
                                    != prepared.final_dimensions
                                    else "no"
                                ),
                                "Reencoded yes/no": (
                                    "yes" if prepared.compressed else "no"
                                ),
                                "Compression reason": prepared.compression_reason,
                                "Final file size MB": round(
                                    prepared.final_size_bytes / (1024 * 1024), 3
                                ),
                                "Final dimensions": prepared.final_dimensions,
                                "JPEG quality used": prepared.quality_used or "",
                                "Transfer Method": prepared.upload_mode,
                            }
                        )
                        self.apply_prepared_expectations(record, prepared)
                        return self.stage_prepared_image(prepared), record
                    record.update(
                        {
                            "Upload mode": "staged_original_file",
                            "Transfer Method": "staged_original_file",
                            "Baserow Original Size Bytes": metadata_size or actual_size,
                            "Baserow Size Bytes": metadata_size or actual_size,
                            "Original file size MB": round(
                                (metadata_size or actual_size) / (1024 * 1024), 3
                            ),
                            "Final file size MB": round(
                                actual_size / (1024 * 1024), 3
                            ),
                        }
                    )
                    mime_type = (
                        str(image.get("mime_type") or "")
                        or "application/octet-stream"
                    )
                    return self.stage_file(source_path, mime_type), record
        if metadata_size > self.image_max_bytes:
            if not self.allow_compression_fallback:
                raise ValueError(
                    "original_over_20mb_compression_fallback_disabled"
                )
            image_tools = load_image_prepare()
            with tempfile.TemporaryDirectory(prefix="men-accessories-") as temp:
                prepared = image_tools.prepare_image_for_shopify(
                    image_url,
                    temp,
                    self.image_max_bytes,
                    force_stage=True,
                )
                record.update(
                    {
                        "Upload mode": prepared.upload_mode,
                        "Compressed yes/no": (
                            "yes" if prepared.compressed else "no"
                        ),
                        "Resized yes/no": (
                            "yes"
                            if prepared.original_dimensions
                            and prepared.final_dimensions
                            and prepared.original_dimensions
                            != prepared.final_dimensions
                            else "no"
                        ),
                        "Reencoded yes/no": (
                            "yes" if prepared.compressed else "no"
                        ),
                        "Compression reason": prepared.compression_reason,
                        "Final file size MB": round(
                            prepared.final_size_bytes / (1024 * 1024), 3
                        ),
                        "Final dimensions": prepared.final_dimensions,
                        "JPEG quality used": prepared.quality_used or "",
                        "Warning": prepared.warning,
                    }
                )
                self.apply_prepared_expectations(record, prepared)
                return self.stage_prepared_image(prepared), record
        if not force_stage:
            return image_url, record

        if not self.exact_file_fallback:
            raise ValueError("exact_file_fallback_disabled")
        with tempfile.TemporaryDirectory(prefix="men-accessories-") as temp:
            temp_dir = Path(temp)
            original_path = temp_dir / record["Source Filename"]
            download_exact_source_file(image_url, original_path)
            actual_size = original_path.stat().st_size
            record["Baserow SHA256"] = sha256_file(original_path)
            if actual_size > self.image_max_bytes:
                if not self.allow_compression_fallback:
                    raise ValueError(
                        "original_over_20mb_compression_fallback_disabled"
                    )
                image_tools = load_image_prepare()
                prepared = image_tools.prepare_image_for_shopify(
                    image_url,
                    temp,
                    self.image_max_bytes,
                    force_stage=True,
                )
                record.update(
                    {
                        "Upload mode": prepared.upload_mode,
                        "Compressed yes/no": (
                            "yes" if prepared.compressed else "no"
                        ),
                        "Resized yes/no": (
                            "yes"
                            if prepared.original_dimensions
                            != prepared.final_dimensions
                            else "no"
                        ),
                        "Reencoded yes/no": (
                            "yes" if prepared.compressed else "no"
                        ),
                        "Compression reason": prepared.compression_reason,
                        "Final file size MB": round(
                            prepared.final_size_bytes / (1024 * 1024), 3
                        ),
                        "Final dimensions": prepared.final_dimensions,
                        "JPEG quality used": prepared.quality_used or "",
                    }
                )
                self.apply_prepared_expectations(record, prepared)
                return self.stage_prepared_image(prepared), record
            record.update(
                {
                    "Upload mode": "staged_original_file",
                    "Baserow Original Size Bytes": metadata_size or actual_size,
                    "Original file size MB": round(
                        (metadata_size or actual_size) / (1024 * 1024), 3
                    ),
                    "Final file size MB": round(
                        actual_size / (1024 * 1024), 3
                    ),
                }
            )
            mime_type = (
                str(image.get("mime_type") or "")
                or "application/octet-stream"
            )
            return self.stage_file(original_path, mime_type), record

    @staticmethod
    def apply_prepared_expectations(
        record: dict[str, Any], prepared: Any
    ) -> None:
        dimensions = str(prepared.final_dimensions or "")
        width = height = ""
        if "x" in dimensions:
            width, height = dimensions.split("x", 1)
        record.update(
            {
                "Expected Shopify Size Bytes": prepared.final_size_bytes,
                "Expected Shopify Width": width,
                "Expected Shopify Height": height,
                "Expected Shopify SHA256": prepared.sha256,
                "Output Size Bytes": prepared.final_size_bytes,
                "Output Width": width,
                "Output Height": height,
                "Output MIME": prepared.mime_type,
                "Selected URL Type": prepared.upload_mode,
                "Transfer Method": prepared.upload_mode,
            }
        )

    def stage_prepared_image(self, prepared: Any) -> str:
        return self.stage_file(Path(prepared.local_path), prepared.mime_type)

    def stage_file(self, path: Path, mime_type: str) -> str:
        content = path.read_bytes()
        mime_type = mime_type or "application/octet-stream"
        suffix = path.suffix or ".img"
        mutation = """mutation($input:[StagedUploadInput!]!){
          stagedUploadsCreate(input:$input){
            stagedTargets{url resourceUrl parameters{name value}}
            userErrors{field message}
          }
        }"""
        staged = self.shopify.graphql(
            mutation,
            {
                "input": [
                    {
                        "resource": "PRODUCT_IMAGE",
                        "filename": path.name or f"source{suffix}",
                        "mimeType": mime_type,
                        "httpMethod": "POST",
                        "fileSize": str(len(content)),
                    }
                ]
            },
        )["stagedUploadsCreate"]
        if staged["userErrors"]:
            raise ShopifyError(str(staged["userErrors"]))
        target = staged["stagedTargets"][0]
        fields = {item["name"]: item["value"] for item in target["parameters"]}
        upload = requests.post(
            target["url"],
            data=fields,
            files={"file": (path.name, content, mime_type)},
            timeout=180,
        )
        upload.raise_for_status()
        return target["resourceUrl"]

    def upload_product_images(
        self,
        product_id: str,
        sku: str,
        title: str,
        front_alt: str,
        images: list[dict[str, str]],
        baserow_row_id: Any = "",
    ) -> list[dict[str, Any]]:
        quality_rows: list[dict[str, Any]] = []
        role_counts: dict[str, int] = {}
        for candidate in images:
            label = str(candidate["label"])
            role_counts[label] = role_counts.get(label, 0) + 1
        role_seen: dict[str, int] = {}
        for index, image in enumerate(images):
            label = str(image["label"])
            role_seen[label] = role_seen.get(label, 0) + 1
            suffix = (
                f" {role_seen[label]}" if role_counts[label] > 1 else ""
            )
            alt = f"{sku} - {label}{suffix}"
            existing = self.get_product_media(product_id)
            normalized_source = str(image["url"]).split("?", 1)[0].casefold()
            duplicate = next(
                (
                    node
                    for node in existing
                    if visible(node.get("alt")).casefold() == alt.casefold()
                    or (
                        role_counts[label] == 1
                        and label.casefold()
                        in visible(node.get("alt")).casefold()
                    )
                    or str((node.get("originalSource") or {}).get("url") or "")
                    .split("?", 1)[0]
                    .casefold()
                    == normalized_source
                ),
                None,
            )
            if duplicate:
                if duplicate.get("status") != "READY":
                    raise ShopifyError(
                        f"existing_media_not_ready:{alt}:{duplicate.get('status')}"
                    )
                self.state.images_already_present += 1
                self.state.duplicate_media_prevented += 1
                quality_rows.append(
                    {
                        "Image field": image["label"],
                        "Image Field": image["label"],
                        "Baserow Row ID": baserow_row_id,
                        "Product Code": sku,
                        "Source URL": image["url"],
                        "Baserow Source URL": image["url"],
                        "Selected Source Type": "file_obj.url",
                        "Original file size MB": "",
                        "Original dimensions": "",
                        "Upload mode": "duplicate_skipped",
                        "Compressed yes/no": "no",
                        "Compression reason": "",
                        "Final file size MB": "",
                        "Final dimensions": "",
                        "JPEG quality used": "",
                        "Shopify media ID": duplicate["id"],
                        "Shopify Media ID": duplicate["id"],
                        "Media READY yes/no": (
                            "yes" if duplicate.get("status") == "READY" else "no"
                        ),
                        "Shopify Media Status": duplicate.get("status") or "",
                        "Source Metadata Verified": "",
                        "Exact Bytes Verified": "",
                        "Generated Front View main yes/no": "no",
                        "Generated Front View First": "",
                        "Warning": "same source image field key/alt already exists",
                        "Error": "",
                    }
                )
                continue
            source, record = self.prepare_media_source(
                image, force_stage=False
            )
            record["Baserow Row ID"] = baserow_row_id
            record["Product Code"] = sku
            media_id = ""
            ready_node: dict[str, Any] = {}
            try:
                media_id = self.create_single_media(product_id, source, alt)
                ready_node = self.wait_for_media_id(product_id, media_id)
            except Exception as direct_error:
                if not (
                    record["Upload mode"] in {"external_original_url", "external_url"}
                    and self.exact_file_fallback
                ):
                    record["Error"] = f"{type(direct_error).__name__}: {direct_error}"
                    self.state.image_failures.append(
                        {
                            "Product Code": sku,
                            **record,
                        }
                    )
                    if index == 0:
                        raise RuntimeError(
                            f"generated_front_original_upload_failed: {direct_error}"
                        ) from direct_error
                    raise
                self.logger.warning(
                    "Direct original URL rejected for SKU=%s field=%s; "
                    "retrying staged original/compressed source",
                    sku,
                    image["label"],
                )
                if media_id:
                    self.delete_product_media(product_id, [media_id])
                try:
                    source, record = self.prepare_media_source(
                        image, force_stage=True
                    )
                    media_id = self.create_single_media(product_id, source, alt)
                    ready_node = self.wait_for_media_id(product_id, media_id)
                except Exception as fallback_error:
                    record["Error"] = (
                        f"{type(fallback_error).__name__}: {fallback_error}"
                    )
                    self.state.image_failures.append(
                        {
                            "Product Code": sku,
                            **record,
                        }
                    )
                    if index == 0:
                        raise RuntimeError(
                            "generated_front_original_upload_failed: "
                            f"{fallback_error}"
                        ) from fallback_error
                    raise
                record["Warning"] = "; ".join(
                    part
                    for part in (
                        record.get("Warning", ""),
                        "Shopify rejected direct URL; staged fallback used",
                    )
                    if part
                )
            record["Shopify media ID"] = media_id
            record["Shopify Media ID"] = media_id
            record["Media READY yes/no"] = "yes"
            try:
                self.verify_media_source(record, ready_node)
            except Exception as verify_error:
                record["Error"] = f"{type(verify_error).__name__}: {verify_error}"
                self.state.image_failures.append(
                    {
                        "Product Code": sku,
                        **record,
                    }
                )
                if index == 0:
                    raise RuntimeError(
                        f"generated_front_source_verification_failed: {verify_error}"
                    ) from verify_error
                raise
            quality_rows.append(record)
            self.state.images_newly_uploaded += 1
        return quality_rows

    def create_single_media(self, product_id: str, source: str, alt: str) -> str:
        mutation = """mutation($id:ID!,$media:[CreateMediaInput!]!){
          productCreateMedia(productId:$id,media:$media){
            media{id alt status}
            mediaUserErrors{field message}
          }
        }"""
        result = self.shopify.graphql(
            mutation,
            {
                "id": product_id,
                "media": [
                    {
                        "mediaContentType": "IMAGE",
                        "originalSource": source,
                        "alt": alt,
                    }
                ],
            },
        )["productCreateMedia"]
        if result["mediaUserErrors"]:
            raise ShopifyError(str(result["mediaUserErrors"]))
        media = result.get("media") or []
        if not media:
            raise ShopifyError("Shopify returned no media after upload")
        return media[0]["id"]

    def complete_existing_product_media(
        self,
        row: dict[str, Any],
        sku: str,
        product: dict[str, Any],
        images: list[dict[str, Any]],
    ) -> dict[str, Any]:
        before_uploaded = self.state.images_newly_uploaded
        before_existing = self.state.images_already_present
        featured_label = self.featured_image_label(images)
        quality_rows = self.upload_product_images(
            product["id"],
            sku,
            visible(product.get("title")),
            f"{sku} - {featured_label}",
            images,
            baserow_row_id=row.get("id", ""),
        )
        if len(quality_rows) != len(images):
            raise ShopifyError("existing_media_completion_incomplete")
        if any(item.get("Media READY yes/no") != "yes" for item in quality_rows):
            raise ShopifyError("existing_media_not_ready")
        for item in quality_rows:
            self.state.image_quality.append(item)
            if item.get("Compressed yes/no") == "yes":
                self.state.compressed_images.append(item.copy())
        collection_details: dict[str, Any] = {}
        if self.fix_existing_collection:
            collection_details = self.assign_and_verify_required_collections(
                product["id"], row
            )
        uploaded = self.state.images_newly_uploaded - before_uploaded
        already_present = self.state.images_already_present - before_existing
        product_url = self.shopify_admin_product_url(product)
        self.update_baserow(
            int(row["id"]),
            (
                f"Existing Shopify product media verified: {product['id']}; "
                f"uploaded={uploaded}; already_present={already_present}"
            ),
            shopify_status="Synced",
            product_url=product_url,
        )
        self.state.baserow_updated += 1
        self.state.processed_success += 1
        return {
            "Baserow Row ID": row.get("id", ""),
            "Product Code": sku,
            "Product Title": visible(self.row_value(row, "Product Title")),
            "Shopify Product ID": product["id"],
            "Shopify Handle": visible(product.get("handle")),
            "New Product yes/no": "no",
            "Existing Product yes/no": "yes",
            "Duplicate SKU prevented": "yes",
            "AI Description Generated": "no",
            "Men's Ties collection assigned": (
                collection_details.get("Primary Collection Assigned", "not changed")
            ),
            "New Arrivals assigned": "not changed",
            "Images found in Baserow": len(images),
            "Images already present": already_present,
            "Images newly uploaded": uploaded,
            "Duplicate media prevented": already_present,
            "Media READY": "yes",
            "Main image verified": "preserved",
            "Inventory result": "not changed",
            "Online Store result": "not changed",
            "Baserow status before": normalize_status(
                self.row_value(row, "Generation Status")
            ),
            "Baserow status after": "shopify-sync",
            "Result": "existing_media_completed",
            "Error": "",
        }

    def get_product_media(self, product_id: str) -> list[dict[str, Any]]:
        query = """query($id:ID!){
          product(id:$id){
            media(first:100){
              nodes{
                id alt status mediaContentType
                ... on MediaImage {
                  image{url width height}
                  originalSource{fileSize url}
                }
              }
            }
          }
        }"""
        product = self.shopify.graphql(query, {"id": product_id}).get("product")
        return product["media"]["nodes"] if product else []

    def wait_for_media_id(
        self, product_id: str, media_id: str, timeout: int = 180
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = next(
                (
                    node
                    for node in self.get_product_media(product_id)
                    if node["id"] == media_id
                ),
                None,
            )
            if last and last.get("status") == "READY":
                return last
            if last and last.get("status") == "FAILED":
                raise ShopifyError(f"Shopify media processing failed: {last}")
            time.sleep(4)
        raise ShopifyError(
            f"Timed out waiting for media {media_id}; last state={last}"
        )

    def verify_media_source(
        self, record: dict[str, Any], ready_node: dict[str, Any]
    ) -> None:
        shopify_image = ready_node.get("image") or {}
        original_source = ready_node.get("originalSource") or {}
        source_size = int(
            record.get("Expected Shopify Size Bytes")
            or record.get("Baserow Size Bytes")
            or 0
        )
        source_width = int(
            record.get("Expected Shopify Width")
            or record.get("Baserow Width")
            or 0
        )
        source_height = int(
            record.get("Expected Shopify Height")
            or record.get("Baserow Height")
            or 0
        )
        shopify_size = original_source.get("fileSize")
        shopify_width = shopify_image.get("width")
        shopify_height = shopify_image.get("height")
        record["Shopify Media Status"] = ready_node.get("status") or ""
        record["Shopify Original Source Size"] = shopify_size or ""
        record["Shopify Width"] = shopify_width or ""
        record["Shopify Height"] = shopify_height or ""
        record["Shopify image width"] = shopify_width or ""
        record["Shopify image height"] = shopify_height or ""
        record["Shopify image URL"] = shopify_image.get("url") or ""
        metadata_errors: list[str] = []
        if self.verify_source_metadata:
            if source_size and int(shopify_size or 0) != source_size:
                metadata_errors.append(
                    f"size mismatch baserow={source_size} shopify={shopify_size}"
                )
            if source_width and int(shopify_width or 0) != source_width:
                metadata_errors.append(
                    f"width mismatch baserow={source_width} shopify={shopify_width}"
                )
            if source_height and int(shopify_height or 0) != source_height:
                metadata_errors.append(
                    f"height mismatch baserow={source_height} shopify={shopify_height}"
                )
        record["Source Metadata Verified"] = "yes" if not metadata_errors else "no"
        record["Metadata Match"] = record["Source Metadata Verified"]
        source_url = str(original_source.get("url") or "")
        hash_errors: list[str] = []
        if self.verify_source_hash and source_url:
            try:
                with tempfile.TemporaryDirectory(prefix="shopify-source-") as temp:
                    download_path = Path(temp) / "shopify-original-source"
                    download_exact_source_file(source_url, download_path)
                    record["Shopify Original Source SHA256"] = sha256_file(
                        download_path
                    )
            except Exception as exc:
                hash_errors.append(
                    f"Shopify originalSource download failed: {type(exc).__name__}: {exc}"
                )
            else:
                record["Exact Bytes Verified"] = (
                    "yes"
                    if (
                        record.get("Expected Shopify SHA256")
                        or record.get("Baserow SHA256")
                    )
                    and record["Shopify Original Source SHA256"]
                    == (
                        record.get("Expected Shopify SHA256")
                        or record["Baserow SHA256"]
                    )
                    else "no"
                )
                if record["Exact Bytes Verified"] != "yes":
                    hash_errors.append("sha256 mismatch")
        elif self.verify_source_hash:
            record["Exact Bytes Verified"] = "not available"
        if not source_url and self.verify_source_hash:
            record["Warning"] = "; ".join(
                part
                for part in (
                    record.get("Warning", ""),
                    "Shopify originalSource URL unavailable; exact-byte hash not compared",
                )
                if part
            )
        verification_errors = metadata_errors + hash_errors
        if record.get("Resize Enabled") == "yes":
            target_match = (
                int(shopify_width or 0)
                == int(record.get("Target Width") or 0)
                and int(shopify_height or 0)
                == int(record.get("Target Height") or 0)
            )
            record["Target Dimensions Verified"] = (
                "yes" if target_match else "no"
            )
            if self.require_target_dimensions and not target_match:
                verification_errors.append(
                    "shopify_resized_dimension_mismatch"
                )
        if verification_errors:
            record["Verification Failed"] = "; ".join(verification_errors)
            if self.fail_on_source_mismatch:
                raise ShopifyError(record["Verification Failed"])

    def delete_product_media(
        self, product_id: str, media_ids: list[str]
    ) -> None:
        if not media_ids:
            return
        mutation = """mutation($productId:ID!,$mediaIds:[ID!]!){
          productDeleteMedia(productId:$productId,mediaIds:$mediaIds){
            deletedMediaIds
            mediaUserErrors{field message}
          }
        }"""
        result = self.shopify.graphql(
            mutation, {"productId": product_id, "mediaIds": media_ids}
        )["productDeleteMedia"]
        if result["mediaUserErrors"]:
            raise ShopifyError(str(result["mediaUserErrors"]))

    def wait_for_media(self, product_id: str, expected: int, timeout: int = 180) -> None:
        deadline = time.monotonic() + timeout
        last: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            q = "query($id:ID!){product(id:$id){media(first:100){nodes{id alt status}}}}"
            last = self.shopify.graphql(q, {"id": product_id})["product"]["media"]["nodes"]
            if len(last) >= expected and all(node.get("status") == "READY" for node in last[:expected]):
                return
            if any(node.get("status") == "FAILED" for node in last):
                raise ShopifyError(f"Shopify media processing failed: {last}")
            time.sleep(4)
        raise ShopifyError(f"Timed out waiting for {expected} media items; last state={last}")

    def set_front_first(self, product_id: str, front_alt: str) -> None:
        q = "query($id:ID!){product(id:$id){media(first:100){nodes{id alt status}}}}"
        nodes = self.shopify.graphql(q, {"id": product_id})["product"]["media"]["nodes"]
        front = next((node for node in nodes if visible(node.get("alt")) == front_alt), None)
        if not front:
            raise ShopifyError("Generated Front View media was not found after upload")
        if nodes and nodes[0]["id"] != front["id"]:
            mutation = """mutation($id:ID!,$moves:[MoveInput!]!){
              productReorderMedia(id:$id,moves:$moves){job{id done} mediaUserErrors{field message}}
            }"""
            result = self.shopify.graphql(
                mutation, {"id": product_id, "moves": [{"id": front["id"], "newPosition": "0"}]}
            )["productReorderMedia"]
            if result["mediaUserErrors"]:
                raise ShopifyError(str(result["mediaUserErrors"]))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            refreshed = self.get_product_media(product_id)
            if refreshed and refreshed[0]["id"] == front["id"]:
                return
            time.sleep(3)
        raise ShopifyError("Generated Front View media reorder did not complete")

    def collection_title(self, row: dict[str, Any]) -> str:
        return self.reference_collection_title or self.primary_collection_title

    def get_publication_id_by_name(self, name: str) -> str:
        key = name.casefold()
        if key in self.publication_cache:
            return self.publication_cache[key]
        query = "query{publications(first:100){nodes{id name}}}"
        nodes = self.shopify.graphql(query)["publications"]["nodes"]
        match = next(
            (node for node in nodes if visible(node.get("name")).casefold() == key),
            None,
        )
        if not match:
            raise RuntimeError("online_store_publication_not_found")
        self.publication_cache[key] = match["id"]
        return match["id"]

    def verify_online_store_published(
        self, product_gid: str, publication_gid: str | None = None
    ) -> bool:
        publication_id = publication_gid or self.get_publication_id_by_name(
            self.publication_name
        )
        query = """query($id:ID!,$publicationId:ID!){
          product(id:$id){
            publishedOnPublication(publicationId:$publicationId)
          }
        }"""
        product = self.shopify.graphql(
            query, {"id": product_gid, "publicationId": publication_id}
        ).get("product")
        return bool(product and product.get("publishedOnPublication"))

    def publish_product_to_online_store(
        self, product_gid: str, publication_gid: str
    ) -> bool:
        if self.verify_online_store_published(product_gid, publication_gid):
            return True
        mutation = """mutation($id:ID!,$input:[PublicationInput!]!,$publicationId:ID!){
          publishablePublish(id:$id,input:$input){
            publishable{
              publishedOnPublication(publicationId:$publicationId)
            }
            userErrors{field message}
          }
        }"""
        result = self.shopify.graphql(
            mutation,
            {
                "id": product_gid,
                "input": [{"publicationId": publication_gid}],
                "publicationId": publication_gid,
            },
        )["publishablePublish"]
        if result["userErrors"]:
            raise ShopifyError(str(result["userErrors"]))
        published = bool(
            result.get("publishable", {}).get("publishedOnPublication")
        )
        return (
            self.verify_online_store_published(product_gid, publication_gid)
            if self.verify_publication
            else published
        )

    def resolve_inventory_location(self) -> dict[str, Any]:
        key = self.location_name.casefold()
        if key in self.location_cache:
            return self.location_cache[key]
        query = "query{locations(first:100,includeInactive:true){nodes{id name isActive}}}"
        nodes = self.shopify.graphql(query)["locations"]["nodes"]
        location = next(
            (
                node
                for node in nodes
                if node.get("isActive")
                and visible(node.get("name")).casefold() == key
            ),
            None,
        )
        if not location and self.allow_location_fallback:
            location = next((node for node in nodes if node.get("isActive")), None)
            if location:
                self.logger.warning(
                    "Location %s not found; using active fallback %s",
                    self.location_name,
                    location["name"],
                )
        if not location:
            raise RuntimeError("location_not_found")
        self.location_cache[key] = location
        return location

    def update_product_status_and_tags(
        self, product_gid: str, tags: list[str] | None
    ) -> None:
        product_input: dict[str, Any] = {
            "id": product_gid,
            "status": self.product_status,
        }
        if tags is not None:
            product_input["tags"] = tags
        mutation = """mutation($product:ProductUpdateInput!){
          productUpdate(product:$product){
            product{id status tags}
            userErrors{field message}
          }
        }"""
        result = self.shopify.graphql(
            mutation, {"product": product_input}
        )["productUpdate"]
        if result["userErrors"]:
            raise ShopifyError(str(result["userErrors"]))

    def update_variant_operational_settings(
        self, product_gid: str, variant_id: str, configure_tracking: bool
    ) -> dict[str, Any]:
        variant: dict[str, Any] = {
            "id": variant_id,
            "inventoryPolicy": self.inventory_policy,
            "taxable": self.taxable,
        }
        if configure_tracking:
            variant["inventoryItem"] = {"tracked": self.inventory_tracked}
        mutation = """mutation($productId:ID!,$variants:[ProductVariantsBulkInput!]!){
          productVariantsBulkUpdate(productId:$productId,variants:$variants){
            productVariants{
              id inventoryPolicy taxable
              inventoryItem{id tracked}
            }
            userErrors{field message}
          }
        }"""
        result = self.shopify.graphql(
            mutation, {"productId": product_gid, "variants": [variant]}
        )["productVariantsBulkUpdate"]
        if result["userErrors"]:
            raise ShopifyError(str(result["userErrors"]))
        variants = result.get("productVariants") or []
        if not variants:
            raise ShopifyError("Variant operational update returned no variant")
        return variants[0]

    def get_inventory_level(
        self, inventory_item_id: str, location_id: str
    ) -> dict[str, Any] | None:
        query = """query($id:ID!,$locationId:ID!){
          inventoryItem(id:$id){
            id tracked
            inventoryLevel(locationId:$locationId){
              id quantities(names:["available","on_hand"]){name quantity}
              location{id name}
            }
          }
        }"""
        item = self.shopify.graphql(
            query, {"id": inventory_item_id, "locationId": location_id}
        ).get("inventoryItem")
        return item.get("inventoryLevel") if item else None

    def set_inventory_quantity(
        self, inventory_item_id: str, location: dict[str, Any]
    ) -> bool:
        level = self.get_inventory_level(inventory_item_id, location["id"])
        if level is None:
            mutation = """mutation($inventoryItemId:ID!,$locationId:ID!,$available:Int,$onHand:Int){
              inventoryActivate(
                inventoryItemId:$inventoryItemId,
                locationId:$locationId,
                available:$available,
                onHand:$onHand
              ){
                inventoryLevel{id quantities(names:["available","on_hand"]){name quantity}}
                userErrors{field message}
              }
            }"""
            result = self.shopify.graphql(
                mutation,
                {
                    "inventoryItemId": inventory_item_id,
                    "locationId": location["id"],
                    "available": self.inventory_quantity,
                    "onHand": self.inventory_quantity,
                },
            )["inventoryActivate"]
            if result["userErrors"]:
                raise ShopifyError(str(result["userErrors"]))
        else:
            for quantity_name in ("on_hand", "available"):
                # Setting on_hand can also change available. Re-read immediately
                # before each compare-and-set so changeFromQuantity is never stale.
                current_level = self.get_inventory_level(
                    inventory_item_id, location["id"]
                )
                current = {
                    value["name"]: int(value["quantity"])
                    for value in (current_level or {}).get("quantities", [])
                }
                current_quantity = current.get(quantity_name)
                if current_quantity == self.inventory_quantity:
                    continue
                mutation = """mutation($input:InventorySetQuantitiesInput!,$key:String!){
                  inventorySetQuantities(input:$input) @idempotent(key:$key){
                    inventoryAdjustmentGroup{id}
                    userErrors{field message}
                  }
                }"""
                result = self.shopify.graphql(
                    mutation,
                    {
                        "key": self.inventory_idempotency_key(
                            inventory_item_id, location["id"], quantity_name
                        ),
                        "input": {
                            "reason": "correction",
                            "name": quantity_name,
                            "referenceDocumentUri": (
                                "https://janardhanasilk.com/inventory/"
                                f"men-accessories-{inventory_item_id.rsplit('/', 1)[-1]}"
                            ),
                            "quantities": [
                                {
                                    "inventoryItemId": inventory_item_id,
                                    "locationId": location["id"],
                                    "quantity": self.inventory_quantity,
                                    "changeFromQuantity": current_quantity,
                                }
                            ],
                        },
                    },
                )["inventorySetQuantities"]
                if result["userErrors"]:
                    raise ShopifyError(str(result["userErrors"]))
        verified = self.get_inventory_level(inventory_item_id, location["id"])
        quantities = {
            value["name"]: int(value["quantity"])
            for value in (verified or {}).get("quantities", [])
        }
        return (
            quantities.get("available") == self.inventory_quantity
            and quantities.get("on_hand") == self.inventory_quantity
        )

    def inventory_idempotency_key(
        self, inventory_item_id: str, location_id: str, quantity_name: str = "available"
    ) -> str:
        sku = visible(getattr(self, "current_inventory_sku", "")) or inventory_item_id.rsplit("/", 1)[-1]
        return (
            "men-accessories:v2:"
            f"{sku}:"
            f"inventory:{location_id.rsplit('/', 1)[-1]}:"
            f"{quantity_name}:"
            f"{self.inventory_quantity}"
        )

    def apply_operational_setup(
        self,
        product: dict[str, Any],
        row: dict[str, Any] | None,
        *,
        configure_inventory: bool,
        configure_tags: bool,
    ) -> dict[str, Any]:
        variants = product.get("variants", {}).get("nodes", [])
        if len(variants) != 1:
            raise ShopifyError(
                f"Expected one product variant, found {len(variants)}"
            )
        tags = self.clean_tags(row, visible(product.get("title"))) if configure_tags else None
        self.update_product_status_and_tags(product["id"], tags)
        variant = self.update_variant_operational_settings(
            product["id"], variants[0]["id"], configure_inventory
        )
        location: dict[str, Any] | None = None
        inventory_set = False
        if configure_inventory:
            location = self.resolve_inventory_location()
            inventory_item = variant.get("inventoryItem") or {}
            if self.inventory_tracked and not inventory_item.get("tracked"):
                raise ShopifyError("Inventory tracking verification failed")
            inventory_set = self.set_inventory_quantity(
                inventory_item["id"], location
            )
            if not inventory_set:
                raise ShopifyError("Inventory quantity verification failed")
        publication_id = ""
        online_published = False
        if self.publish_online_store:
            publication_id = self.get_publication_id_by_name(
                self.publication_name
            )
            online_published = self.publish_product_to_online_store(
                product["id"], publication_id
            )
            if self.verify_publication and not online_published:
                raise ShopifyError("Online Store publication verification failed")
        collection_details = self.assign_and_verify_required_collections(
            product["id"], row
        )
        return {
            "Product status": self.product_status,
            "Online Store publication ID": publication_id,
            "Online Store published yes/no": "yes" if online_published else "no",
            "Inventory tracked yes/no": (
                "yes" if configure_inventory and self.inventory_tracked else "no"
            ),
            "Inventory location name": location["name"] if location else "",
            "Inventory location ID": location["id"] if location else "",
            "Inventory quantity requested": (
                self.inventory_quantity if configure_inventory else ""
            ),
            "Inventory quantity set yes/no": "yes" if inventory_set else "no",
            "Inventory policy": self.inventory_policy,
            "Taxable": "yes" if self.taxable else "no",
            "Tags written": ", ".join(tags or []),
            "Tags count": len(tags or []),
            **collection_details,
        }

    def get_product_for_fix(self, product_gid: str) -> dict[str, Any]:
        query = """query($id:ID!){
          product(id:$id){
            id legacyResourceId title handle status vendor productType tags
            variants(first:10){
              nodes{
                id sku price inventoryPolicy taxable
                inventoryItem{id tracked}
              }
            }
          }
        }"""
        product = self.shopify.graphql(query, {"id": product_gid}).get("product")
        if not product:
            raise RuntimeError("shopify_product_not_found")
        return product

    def set_partial_product_draft(self, product_gid: str) -> None:
        mutation = """mutation($product:ProductUpdateInput!){
          productUpdate(product:$product){
            product{id status}
            userErrors{field message}
          }
        }"""
        result = self.shopify.graphql(
            mutation, {"product": {"id": product_gid, "status": "DRAFT"}}
        )["productUpdate"]
        if result["userErrors"]:
            raise ShopifyError(str(result["userErrors"]))

    def get_baserow_row(self, row_id: int) -> dict[str, Any]:
        url = (
            f"{self.baserow.base_url}/api/database/rows/table/"
            f"{self.table_id}/{row_id}/?user_field_names=true"
        )
        response = self.baserow.session.get(url, timeout=45)
        response.raise_for_status()
        return response.json()

    def verify_product_persistence(
        self,
        entry: dict[str, Any],
        checkpoint: str,
        *,
        require_baserow_writeback: bool,
        record_result: bool = True,
    ) -> dict[str, Any]:
        product_id = entry["Shopify Product ID"]
        sku = entry["Product Code"]
        publication_id = entry["Online Store publication ID"]
        location_id = entry["Inventory location ID"]
        query = """query(
          $id:ID!,$publicationId:ID!,$locationId:ID!,$skuQuery:String!
        ){
          product(id:$id){
            id handle status tags
            collections(first:250){nodes{id title}}
            publishedOnPublication(publicationId:$publicationId)
            variants(first:10){
              nodes{
                sku inventoryPolicy taxable
                inventoryItem{
                  tracked
                  inventoryLevel(locationId:$locationId){
                    quantities(names:["available","on_hand"]){name quantity}
                  }
                }
              }
            }
            media(first:100){
              nodes{
                id status
                ... on MediaImage{image{width height}}
              }
            }
          }
          productVariants(first:10,query:$skuQuery){
            nodes{sku product{id}}
          }
        }"""
        data = self.shopify.graphql(
            query,
            {
                "id": product_id,
                "publicationId": publication_id,
                "locationId": location_id,
                "skuQuery": f'sku:"{gql_escape(sku)}"',
            },
        )
        product = data.get("product")
        errors: list[str] = []
        variants = product.get("variants", {}).get("nodes", []) if product else []
        exact_variants = [variant for variant in variants if visible(variant.get("sku")) == sku]
        sku_product_ids = list(
            dict.fromkeys(
                node["product"]["id"]
                for node in data.get("productVariants", {}).get("nodes", [])
                if visible(node.get("sku")) == sku
            )
        )
        if not product:
            errors.append("exact_product_id_missing")
        if sku_product_ids != [product_id]:
            errors.append(
                f"exact_sku_lookup_mismatch:{','.join(sku_product_ids) or 'missing'}"
            )
        if product and product.get("status") != "ACTIVE":
            errors.append(f"product_status_{product.get('status')}")
        if product and not product.get("publishedOnPublication"):
            errors.append("online_store_not_published")
        if len(exact_variants) != 1:
            errors.append("exact_sku_variant_missing_or_multiple")
        variant = exact_variants[0] if len(exact_variants) == 1 else {}
        inventory_item = variant.get("inventoryItem") or {}
        quantities = {
            item["name"]: int(item["quantity"])
            for item in (
                (inventory_item.get("inventoryLevel") or {}).get(
                    "quantities", []
                )
            )
        }
        if inventory_item.get("tracked") is not True:
            errors.append("inventory_not_tracked")
        if quantities.get("available") != 1 or quantities.get("on_hand") != 1:
            errors.append(f"inventory_not_1:{quantities}")
        if variant.get("inventoryPolicy") != "DENY":
            errors.append("inventory_policy_not_deny")
        if variant.get("taxable") is not True:
            errors.append("taxable_not_true")
        if product and len(product.get("tags") or []) > self.max_clean_tags:
            errors.append("tag_count_exceeds_limit")
        collection_ids = {
            str(node.get("id"))
            for node in (
                product.get("collections", {}).get("nodes", []) if product else []
            )
        }
        primary_collection_id = str(entry.get("Primary Collection ID") or "")
        secondary_collection_id = str(
            entry.get("Secondary Collection ID") or ""
        )
        new_arrivals_collection_id = str(
            entry.get("New Arrivals Collection ID") or ""
        )
        if primary_collection_id and primary_collection_id not in collection_ids:
            errors.append("primary_collection_membership_missing")
        if (
            secondary_collection_id
            and secondary_collection_id not in collection_ids
        ):
            errors.append("secondary_collection_membership_missing")
        if (
            getattr(self, "add_new_arrivals", False)
            and new_arrivals_collection_id not in collection_ids
        ):
            errors.append("new_arrivals_membership_missing")

        media = product.get("media", {}).get("nodes", []) if product else []
        front = media[0] if media else {}
        expected_media_id = entry["Shopify Media ID"]
        expected_width = int(
            entry.get("Expected Shopify Width") or entry["Source Width"]
        )
        expected_height = int(
            entry.get("Expected Shopify Height") or entry["Source Height"]
        )
        front_image = front.get("image") or {}
        if front.get("id") != expected_media_id:
            errors.append("generated_front_not_first")
        if front.get("status") != "READY":
            errors.append(f"front_media_not_ready:{front.get('status')}")
        if (
            int(front_image.get("width") or 0) != expected_width
            or int(front_image.get("height") or 0) != expected_height
        ):
            errors.append(
                "front_dimensions_mismatch:"
                f"{front_image.get('width')}x{front_image.get('height')}"
            )

        baserow_row = self.get_baserow_row(int(entry["Baserow Row ID"]))
        baserow_status = status(
            baserow_row.get(self.fields["Generation Status"]["name"])
        )
        if require_baserow_writeback and baserow_status != "shopify-sync":
            errors.append(f"baserow_status_{baserow_status or 'missing'}")

        passed = not errors
        result = {
            **entry,
            "Shopify Handle": product.get("handle", "") if product else "",
            "Shopify Media Status": front.get("status", ""),
            "Shopify Width": front_image.get("width", ""),
            "Shopify Height": front_image.get("height", ""),
            "Source Metadata Match": (
                "yes"
                if int(front_image.get("width") or 0) == expected_width
                and int(front_image.get("height") or 0) == expected_height
                else "no"
            ),
            "Generated Front View First": (
                "yes" if front.get("id") == expected_media_id else "no"
            ),
            "Product ACTIVE": (
                "yes" if product and product.get("status") == "ACTIVE" else "no"
            ),
            "Online Store Published": (
                "yes"
                if product and product.get("publishedOnPublication")
                else "no"
            ),
            "Inventory Tracked": (
                "yes" if inventory_item.get("tracked") is True else "no"
            ),
            "Inventory Quantity": quantities.get("available", ""),
            "Primary Collection Assigned": (
                "yes" if primary_collection_id in collection_ids else "no"
            ),
            "Secondary Collection Assigned": (
                "yes" if secondary_collection_id in collection_ids else "no"
            ),
            "New Arrivals Assigned": (
                "yes"
                if new_arrivals_collection_id
                and new_arrivals_collection_id in collection_ids
                else (
                    "disabled"
                    if not getattr(self, "add_new_arrivals", False)
                    else "no"
                )
            ),
            "Baserow Writeback": baserow_status,
            f"Persistence Check {checkpoint}": "yes" if passed else "no",
            "Result": "passed" if passed else "failed",
            "Error": "; ".join(errors),
        }
        if record_result:
            self.state.persistence_checks.append(result)
        return result

    def audit_theme_image_delivery(self) -> None:
        themes_url = self.shopify.endpoint.replace("graphql.json", "themes.json")
        response = self.shopify.session.get(themes_url, timeout=45)
        base = {
            "Theme access HTTP": response.status_code,
            "Required scope": "read_themes (write_themes only for patching)",
        }
        if not response.ok:
            self.state.theme_audit.append(
                {
                    **base,
                    "Result": "theme_access_unavailable",
                    "Error": response.text[:500],
                }
            )
            return
        themes = response.json().get("themes", [])
        theme = next((item for item in themes if item.get("role") == "main"), None)
        if not theme:
            self.state.theme_audit.append(
                {**base, "Result": "main_theme_not_found", "Error": ""}
            )
            return
        assets_url = themes_url.replace(
            "themes.json", f"themes/{theme['id']}/assets.json"
        )
        assets_response = self.shopify.session.get(assets_url, timeout=60)
        if not assets_response.ok:
            self.state.theme_audit.append(
                {
                    **base,
                    "Theme ID": theme["id"],
                    "Theme Name": theme["name"],
                    "Result": "theme_assets_unavailable",
                    "Error": assets_response.text[:500],
                }
            )
            return
        candidates = (
            "sections/main-product.liquid",
            "sections/new-main-product.liquid",
            "snippets/product-media-gallery.liquid",
            "snippets/product-media-modal.liquid",
            "snippets/product-media.liquid",
            "snippets/product-thumbnail.liquid",
        )
        available = {
            item.get("key")
            for item in assets_response.json().get("assets", [])
        }
        for key in candidates:
            if key not in available:
                continue
            asset_response = self.shopify.session.get(
                assets_url, params={"asset[key]": key}, timeout=60
            )
            value = (
                (asset_response.json().get("asset") or {}).get("value", "")
                if asset_response.ok
                else ""
            )
            self.state.theme_audit.append(
                {
                    **base,
                    "Theme ID": theme["id"],
                    "Theme Name": theme["name"],
                    "Theme Role": theme["role"],
                    "Asset Key": key,
                    "Contains 384px": "yes" if re.search(r"\b384\b", value) else "no",
                    "Contains 1152px": "yes" if re.search(r"\b1152\b", value) else "no",
                    "Uses original image_url": (
                        "yes"
                        if re.search(r"\|\s*image_url\s*(?:\}\}|$)", value)
                        else "no"
                    ),
                    "Result": (
                        "audited" if asset_response.ok else "asset_read_failed"
                    ),
                    "Error": (
                        "" if asset_response.ok else asset_response.text[:500]
                    ),
                }
            )

    def guard_against_external_deletion(self) -> None:
        persistence_path = (
            OUTPUT / "men_accessories_product_persistence_check.csv"
        )
        if not persistence_path.exists():
            return
        with persistence_path.open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        prior = {
            row.get("Shopify Product ID", ""): row
            for row in rows
            if row.get("Persistence Check Final") == "yes"
            and row.get("Shopify Product ID")
        }
        missing: list[dict[str, Any]] = []
        for product_id, row in prior.items():
            try:
                product = self.get_product_for_fix(product_id)
            except Exception:
                product = None
            if not product:
                missing.append(
                    {
                        "Product Code": row.get("Product Code", ""),
                        "Shopify Product ID": product_id,
                        "Prior Final Persistence": "passed",
                        "Current Product Exists": "no",
                        "Detected UTC": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "Result": "external_product_deletion_detected",
                    }
                )
        if not missing:
            return
        self.write_csv(
            OUTPUT / "men_accessories_external_deletion_audit.csv",
            missing,
            [
                "Product Code",
                "Shopify Product ID",
                "Prior Final Persistence",
                "Current Product Exists",
                "Detected UTC",
                "Result",
            ],
        )
        if not self.acknowledge_external_deletion:
            raise RuntimeError(
                "External deletion detected for prior verified Men Accessories "
                "canaries. Coordinate with the Shopify operator and set "
                "ACKNOWLEDGE_MEN_ACCESSORIES_EXTERNAL_DELETION=true only after "
                "the deletion/reset cause is accepted."
            )

    def find_product_for_fix_by_sku(self, sku: str) -> dict[str, Any] | None:
        query = """query($q:String!){
          productVariants(first:10,query:$q){
            nodes{sku product{id}}
          }
        }"""
        nodes = self.shopify.graphql(
            query, {"q": f'sku:"{gql_escape(sku)}"'}
        )["productVariants"]["nodes"]
        exact_ids = list(
            dict.fromkeys(
                node["product"]["id"]
                for node in nodes
                if visible(node.get("sku")) == sku
            )
        )
        if len(exact_ids) > 1:
            raise RuntimeError("multiple_products_for_sku")
        return self.get_product_for_fix(exact_ids[0]) if exact_ids else None

    def record_operational_success(self, details: dict[str, Any]) -> None:
        if details.get("Product status") == "ACTIVE":
            self.state.products_active += 1
        if details.get("Online Store published yes/no") == "yes":
            self.state.products_online_store_published += 1
        if details.get("Inventory quantity set yes/no") == "yes":
            self.state.inventory_set_to_quantity += 1
        if details.get("Primary Collection Assigned") == "yes":
            self.state.primary_collections_assigned += 1
        if details.get("Secondary Collection Assigned") == "yes":
            self.state.secondary_collections_assigned += 1
        if details.get("New Arrivals Assigned") == "yes":
            self.state.new_arrivals_assigned += 1
        if details.get("New Arrivals Already Assigned") == "yes":
            self.state.new_arrivals_already_assigned += 1
        tag_count = int(details.get("Tags count") or 0)
        self.state.total_tags_written += tag_count
        if tag_count > self.max_clean_tags:
            self.state.products_with_more_than_max_tags += 1

    def run_direct_product_fix(self) -> int:
        product_gid = f"gid://shopify/Product/{self.fix_legacy_id}"
        product = self.get_product_for_fix(product_gid)
        sku = visible(product["variants"]["nodes"][0].get("sku"))
        matching_row = None
        self.fetch_and_validate_fields()
        for row in self.baserow.iter_rows():
            if visible(self.row_value(row, "Product Code")) == sku:
                matching_row = row
                break
        planned_tags = (
            self.clean_tags(matching_row, visible(product.get("title")))
            if self.fix_tags
            else None
        )
        base = {
            "Shopify Product ID": product_gid,
            "Legacy Product ID": self.fix_legacy_id,
            "Product Code": sku,
            "Mode": "direct_product_fix",
            "Title changed": "no",
            "Description changed": "no",
            "Price changed": "no",
            "Images changed": "no",
            "Collections changed": "no",
        }
        if self.dry_run:
            details = {
                "Product status": self.product_status,
                "Online Store publication ID": self.get_publication_id_by_name(
                    self.publication_name
                ) if self.publish_online_store else "",
                "Online Store published yes/no": "planned",
                "Inventory tracked yes/no": (
                    "planned" if self.fix_inventory else "not requested"
                ),
                "Inventory location name": (
                    self.resolve_inventory_location()["name"]
                    if self.fix_inventory else ""
                ),
                "Inventory location ID": (
                    self.resolve_inventory_location()["id"]
                    if self.fix_inventory else ""
                ),
                "Inventory quantity requested": (
                    self.inventory_quantity if self.fix_inventory else ""
                ),
                "Inventory quantity set yes/no": "planned",
                "Inventory policy": self.inventory_policy,
                "Taxable": "yes" if self.taxable else "no",
                "Tags written": ", ".join(planned_tags or []),
                "Tags count": len(planned_tags or []),
            }
        else:
            details = self.apply_operational_setup(
                product,
                matching_row,
                configure_inventory=self.fix_inventory,
                configure_tags=self.fix_tags,
            )
            self.record_operational_success(details)
        report = [{**base, **details, "Result": "planned" if self.dry_run else "fixed"}]
        self.write_fix_outputs(
            report,
            OUTPUT / "men_accessories_product_publish_fix_report.csv",
            OUTPUT / "final_men_accessories_product_publish_fix_report.txt",
            "Men Accessories Direct Product Publish Fix",
        )
        return 0

    def run_created_products_fix(self) -> int:
        self.fetch_and_validate_fields()
        report_rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        attempted = 0
        all_rows = list(self.baserow.iter_rows())
        # Recover partially created products first. Fully synced rows remain
        # eligible for later maintenance passes.
        candidate_rows = sorted(
            all_rows,
            key=lambda row: (
                0
                if status(self.row_value(row, "Generation Status")) == "approved"
                else 1,
                int(row.get("id") or 0),
            ),
        )
        seen_skus: set[str] = set()
        for row in candidate_rows:
            sku = visible(self.row_value(row, "Product Code"))
            generation_before = status(
                self.row_value(row, "Generation Status")
            )
            if (
                not sku
                or generation_before not in {"approved", "shopify-sync"}
                or status(self.row_value(row, "SHOPIFY Notes")) != "approved"
                or sku.casefold() in seen_skus
            ):
                continue
            seen_skus.add(sku.casefold())
            try:
                product = self.find_product_for_fix_by_sku(sku)
            except Exception as exc:
                failures.append(
                    {
                        "Row ID": row.get("id"),
                        "Product Code": sku,
                        "Error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if not product:
                # An Approved row without a Shopify product belongs to the
                # creation pipeline, not this repair mode.
                continue
            if self.max_products is not None and attempted >= self.max_products:
                break
            attempted += 1
            try:
                expected_handle = slugify(
                    f"{visible(self.row_value(row, 'Product Title'))} {sku}"
                )
                if visible(product.get("handle")) != expected_handle:
                    raise RuntimeError(
                        "product_not_confirmed_as_men_accessories_source"
                    )
                base = {
                    "Row ID": row["id"],
                    "Product Code": sku,
                    "Shopify Product ID": product["id"],
                    "Title changed": "no",
                    "Description changed": "no",
                    "Price changed": "no",
                    "Images changed": "no",
                    "Collections changed": "no",
                }
                if self.dry_run:
                    details = {
                        "Product status": self.product_status,
                        "Online Store publication ID": self.get_publication_id_by_name(
                            self.publication_name
                        ) if self.publish_online_store else "",
                        "Online Store published yes/no": "planned",
                        "Inventory tracked yes/no": "planned",
                        "Inventory location name": self.resolve_inventory_location()["name"],
                        "Inventory location ID": self.resolve_inventory_location()["id"],
                        "Inventory quantity requested": self.inventory_quantity,
                        "Inventory quantity set yes/no": "planned",
                        "Inventory policy": self.inventory_policy,
                        "Taxable": "yes" if self.taxable else "no",
                        "Tags written": ", ".join(self.clean_tags(row)),
                        "Tags count": len(self.clean_tags(row)),
                    }
                else:
                    details = self.apply_operational_setup(
                        product,
                        row,
                        configure_inventory=True,
                        configure_tags=True,
                    )
                    self.record_operational_success(details)
                    status_updated = False
                    if generation_before == "approved":
                        self.update_baserow(
                            int(row["id"]),
                            "Existing Men Accessories Shopify product operational setup "
                            "verified; Generation Status updated to Shopify-sync.",
                        )
                        status_updated = True
                    details["Generation Status updated yes/no"] = (
                        "yes" if status_updated else "not required"
                    )
                report_rows.append(
                    {
                        **base,
                        **details,
                        "Result": "planned" if self.dry_run else "fixed",
                    }
                )
            except Exception as exc:
                failures.append(
                    {
                        "Row ID": row.get("id"),
                        "Product Code": sku,
                        "Error": f"{type(exc).__name__}: {exc}",
                    }
                )
                self.logger.exception("Men Accessories created-product fix failed for %s", sku)
        report_rows.extend(failures)
        self.write_fix_outputs(
            report_rows,
            OUTPUT / "men_accessories_created_products_fix_report.csv",
            OUTPUT / "final_men_accessories_created_products_fix_report.txt",
            "Men Accessories Created Products Fix",
        )
        return 1 if failures else 0

    def write_fix_outputs(
        self,
        rows: list[dict[str, Any]],
        csv_path: Path,
        text_path: Path,
        title: str,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.write_csv(csv_path, rows, ["Product Code", "Result", "Error"])
        success_rows = [row for row in rows if row.get("Result") in {"fixed", "planned"}]
        failed_rows = [row for row in rows if row.get("Error")]
        average_tags = (
            sum(int(row.get("Tags count") or 0) for row in success_rows)
            / len(success_rows)
            if success_rows
            else 0
        )
        lines = [
            title,
            f"Completed UTC: {datetime.now(timezone.utc).isoformat()}",
            f"Mode: {'DRY_RUN' if self.dry_run else 'LIVE'}",
            f"Products attempted: {len(rows)}",
            f"Products fixed/planned: {len(success_rows)}",
            f"Products failed: {len(failed_rows)}",
            f"Products ACTIVE: {sum(row.get('Product status') == 'ACTIVE' for row in success_rows)}",
            "Products Online Store published: "
            f"{sum(row.get('Online Store published yes/no') == 'yes' for row in success_rows)}",
            "Online Store publish failures: "
            f"{sum('publication' in visible(row.get('Error')).casefold() for row in failed_rows)}",
            "Inventory set to 1: "
            f"{sum(row.get('Inventory quantity set yes/no') == 'yes' for row in success_rows)}",
            "Inventory set failures: "
            f"{sum('inventory' in visible(row.get('Error')).casefold() for row in failed_rows)}",
            "Location not found failures: "
            f"{sum('location_not_found' in visible(row.get('Error')) for row in failed_rows)}",
            f"Average tags per product: {average_tags:.2f}",
            "Products with more than 6 tags: "
            f"{sum(int(row.get('Tags count') or 0) > 6 for row in success_rows)}",
            "Products added to primary collections: "
            f"{sum(row.get('Primary Collection Assigned') == 'yes' for row in success_rows)}",
            "Products added to New Arrivals: "
            f"{sum(row.get('New Arrivals Assigned') == 'yes' for row in success_rows)}",
            "Products already in New Arrivals: "
            f"{sum(row.get('New Arrivals Already Assigned') == 'yes' for row in success_rows)}",
            "New Arrivals assignment failures: "
            f"{sum('new_arrivals' in visible(row.get('Error')).casefold() for row in failed_rows)}",
        ]
        text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_image_quality_audit(self) -> int:
        """Prove that only Baserow original file URLs are selected."""
        self.fetch_and_validate_fields()
        rows: list[dict[str, Any]] = []
        approved_products = 0
        failed = 0
        for row in self.baserow.iter_rows():
            if not self.approval_ok(row):
                continue
            if self.max_products is not None and approved_products >= self.max_products:
                break
            approved_products += 1
            sku = visible(self.row_value(row, "Product Code"))
            for image in self.images(row):
                selected_url = str(image["url"])
                contains_thumbnail = (
                    "/thumbnails/" in selected_url.casefold()
                )
                size = int(image.get("size") or 0)
                audit_row = {
                    "Row ID": row.get("id"),
                    "Product Code": sku,
                    "Image Field": image["label"],
                    "Baserow Original URL": selected_url,
                    "Baserow Original Size Bytes": size or "",
                    "Baserow Original MIME Type": image.get("mime_type", ""),
                    "Baserow Original Width": image.get("image_width") or "",
                    "Baserow Original Height": image.get("image_height") or "",
                    "Tiny Thumbnail URL": image.get(
                        "tiny_thumbnail_url", ""
                    ),
                    "Small Thumbnail URL": image.get(
                        "small_thumbnail_url", ""
                    ),
                    "Card Cover URL": image.get(
                        "card_cover_url", ""
                    ),
                    "Selected Upload URL": selected_url,
                    "Selected URL Type": "original",
                    "Selected URL Contains Thumbnail yes/no": (
                        "yes" if contains_thumbnail else "no"
                    ),
                    "Will Upload Original yes/no": (
                        "no" if contains_thumbnail else "yes"
                    ),
                    "Will Compress yes/no": "no",
                    "Will Resize yes/no": "no",
                    "Will Reencode yes/no": "no",
                    "Upload Mode": "external_original_url",
                    "Warning": "",
                    "Error": "",
                }
                if contains_thumbnail:
                    failed += 1
                    audit_row["Error"] = (
                        f"{image['label']} selected thumbnail url instead "
                        "of original"
                    )
                elif size and size > self.image_max_bytes:
                    if self.allow_compression_fallback:
                        audit_row["Will Compress yes/no"] = "yes"
                        audit_row["Will Reencode yes/no"] = "yes"
                        audit_row["Upload Mode"] = (
                            "staged_compressed_explicit_fallback"
                        )
                        audit_row["Warning"] = (
                            "original exceeds configured 20 MB limit"
                        )
                    else:
                        failed += 1
                        audit_row["Will Upload Original yes/no"] = "no"
                        audit_row["Upload Mode"] = "blocked_over_limit"
                        audit_row["Error"] = (
                            "original_over_20mb_compression_fallback_disabled"
                        )
                rows.append(audit_row)

        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.write_csv(
            OUTPUT / "men_accessories_image_upload_source_audit.csv",
            rows,
            ["Row ID", "Product Code", "Image Field", "Error"],
        )
        original = sum(
            row["Selected URL Type"] == "original"
            and row["Selected URL Contains Thumbnail yes/no"] == "no"
            for row in rows
        )
        lines = [
            "Men Accessories Image Upload Source Audit",
            f"Completed UTC: {datetime.now(timezone.utc).isoformat()}",
            "Mode: READ_ONLY",
            f"Approved products inspected: {approved_products}",
            f"Images inspected: {len(rows)}",
            f"Original Baserow URLs selected: {original}",
            "Thumbnail URLs selected: "
            f"{sum(row['Selected URL Contains Thumbnail yes/no'] == 'yes' for row in rows)}",
            "Compression planned: "
            f"{sum(row['Will Compress yes/no'] == 'yes' for row in rows)}",
            "Resize planned: "
            f"{sum(row['Will Resize yes/no'] == 'yes' for row in rows)}",
            "Re-encode planned: "
            f"{sum(row['Will Reencode yes/no'] == 'yes' for row in rows)}",
            f"Audit failures: {failed}",
            "Shopify called: no",
            "Shopify updated: no",
            "Baserow updated: no",
        ]
        (
            OUTPUT / "final_men_accessories_image_upload_source_audit.txt"
        ).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.logger.info(" | ".join(lines[2:]))
        return 1 if failed else 0

    @staticmethod
    def media_angle_matches(alt: str, label: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", " ", alt.casefold()).strip()
        label_normalized = re.sub(
            r"[^a-z0-9]+", " ", label.casefold()
        ).strip()
        if label_normalized in normalized:
            return True
        family = {
            "close up": ("close up", "closeup"),
            "pallu detail 1": ("pallu detail",),
            "pallu detail 2": ("pallu detail",),
            "body detail": ("body detail",),
            "border detail": ("border detail",),
        }
        return any(
            marker in normalized for marker in family.get(label_normalized, ())
        )

    def run_media_quality_repair(self) -> int:
        """Replace only clearly-owned automation media after HQ media is READY."""
        self.fetch_and_validate_fields()
        report: list[dict[str, Any]] = []
        attempted_products = 0
        successful_products = 0
        failed_products = 0
        seen_skus: set[str] = set()
        for row in self.baserow.iter_rows():
            generation = status(self.row_value(row, "Generation Status"))
            sku = visible(self.row_value(row, "Product Code"))
            if (
                generation not in {"approved", "shopify-sync"}
                or status(self.row_value(row, "SHOPIFY Notes")) != "approved"
                or not sku
                or sku.casefold() in seen_skus
            ):
                continue
            seen_skus.add(sku.casefold())
            product = self.find_product_for_fix_by_sku(sku)
            if not product:
                continue
            expected_handle = slugify(
                f"{visible(self.row_value(row, 'Product Title'))} {sku}"
            )
            if visible(product.get("handle")) != expected_handle:
                report.append(
                    {
                        "Row ID": row.get("id"),
                        "Product Code": sku,
                        "Shopify Product ID": product["id"],
                        "Error": "product_not_confirmed_as_men_accessories_source",
                    }
                )
                failed_products += 1
                continue
            if (
                self.max_products is not None
                and attempted_products >= self.max_products
            ):
                break
            attempted_products += 1
            product_failed = False
            front_alt = f"{sku} - Generated Front View - High Quality"
            try:
                for image in self.images(row):
                    desired_alt = (
                        front_alt
                        if image["label"] == "Generated Front View"
                        else f"{sku} - {image['label']} - High Quality"
                    )
                    existing = self.get_product_media(product["id"])
                    already_ready = next(
                        (
                            node
                            for node in existing
                            if visible(node.get("alt")) == desired_alt
                            and node.get("status") == "READY"
                        ),
                        None,
                    )
                    if already_ready:
                        old_safe_ids = [
                            node["id"]
                            for node in existing
                            if node["id"] != already_ready["id"]
                            and sku.casefold()
                            in visible(node.get("alt")).casefold()
                            and self.media_angle_matches(
                                visible(node.get("alt")), image["label"]
                            )
                        ]
                        warning = "high-quality automation media already exists"
                        if old_safe_ids and not self.dry_run:
                            try:
                                self.delete_product_media(
                                    product["id"], old_safe_ids
                                )
                            except Exception:
                                warning += (
                                    "; old_media_not_deleted_manual_review"
                                )
                        report.append(
                            {
                                "Row ID": row.get("id"),
                                "Product Code": sku,
                                "Shopify Product ID": product["id"],
                                "Image field": image["label"],
                                "Upload mode": "duplicate_skipped",
                                "Shopify media ID": already_ready["id"],
                                "Media READY yes/no": "yes",
                                "Warning": warning,
                                "Error": "",
                            }
                        )
                        continue
                    safe_old = [
                        node
                        for node in existing
                        if sku.casefold() in visible(node.get("alt")).casefold()
                        and self.media_angle_matches(
                            visible(node.get("alt")), image["label"]
                        )
                    ]
                    risky_old = [
                        node
                        for node in existing
                        if node not in safe_old
                        and self.media_angle_matches(
                            visible(node.get("alt")), image["label"]
                        )
                    ]
                    if self.dry_run:
                        size = int(image.get("size") or 0)
                        dimensions = (
                            f"{image.get('image_width')}x"
                            f"{image.get('image_height')}"
                            if image.get("image_width")
                            and image.get("image_height")
                            else ""
                        )
                        quality = {
                            "Image field": image["label"],
                            "Source URL": image["url"],
                            "Baserow Original URL": image["url"],
                            "Baserow Original Size Bytes": size or "",
                            "Baserow Original MIME Type": image.get(
                                "mime_type", ""
                            ),
                            "Original file size MB": (
                                round(size / (1024 * 1024), 3) if size else ""
                            ),
                            "Original dimensions": dimensions,
                            "Upload mode": (
                                "planned_external_original_url"
                                if not size or size <= self.image_max_bytes
                                else "planned_blocked_over_limit"
                            ),
                            "Compressed yes/no": "no",
                            "Resized yes/no": "no",
                            "Reencoded yes/no": "no",
                            "Compression reason": "",
                            "Final file size MB": (
                                round(size / (1024 * 1024), 3) if size else ""
                            ),
                            "Final dimensions": dimensions,
                            "JPEG quality used": "",
                            "Shopify media ID": "",
                            "Media READY yes/no": "planned",
                            "Generated Front View main yes/no": "planned",
                            "Warning": "",
                            "Error": (
                                "original_over_20mb_compression_fallback_disabled"
                                if size > self.image_max_bytes
                                and not self.allow_compression_fallback
                                else ""
                            ),
                        }
                    else:
                        quality = self.upload_product_images(
                            product["id"],
                            sku,
                            "",
                            desired_alt,
                            [image],
                        )[0]
                    quality.update(
                        {
                            "Row ID": row.get("id"),
                            "Product Code": sku,
                            "Shopify Product ID": product["id"],
                        }
                    )
                    if not self.dry_run and safe_old:
                        try:
                            self.delete_product_media(
                                product["id"],
                                [node["id"] for node in safe_old],
                            )
                        except Exception as exc:
                            quality["Warning"] = "; ".join(
                                part
                                for part in (
                                    quality.get("Warning", ""),
                                    "old_media_not_deleted_manual_review",
                                )
                                if part
                            )
                            quality["Error"] = (
                                f"Safe old media deletion failed: {exc}"
                            )
                    if risky_old:
                        quality["Warning"] = "; ".join(
                            part
                            for part in (
                                quality.get("Warning", ""),
                                "old_media_not_deleted_manual_review",
                            )
                            if part
                        )
                    report.append(quality)
                    self.state.image_quality.append(quality.copy())
                    if quality.get("Compressed yes/no") == "yes":
                        self.state.compressed_images.append(quality.copy())
                if not self.dry_run:
                    self.set_front_first(product["id"], front_alt)
                    current_media = self.get_product_media(product["id"])
                    current_front_id = (
                        current_media[0]["id"] if current_media else ""
                    )
                    for item in report:
                        if (
                            item.get("Shopify Product ID") == product["id"]
                            and item.get("Image field") == "Generated Front View"
                        ):
                            item["Generated Front View main yes/no"] = (
                                "yes"
                                if item.get("Shopify media ID")
                                == current_front_id
                                else "no"
                            )
                successful_products += 1
            except Exception as exc:
                product_failed = True
                failed_products += 1
                report.append(
                    {
                        "Row ID": row.get("id"),
                        "Product Code": sku,
                        "Shopify Product ID": product["id"],
                        "Warning": "",
                        "Error": f"{type(exc).__name__}: {exc}",
                    }
                )
                self.logger.exception("Media quality repair failed for %s", sku)
            if product_failed:
                continue

        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.write_csv(
            OUTPUT / "men_accessories_low_quality_media_repair_report.csv",
            report,
            ["Row ID", "Product Code", "Shopify Product ID", "Error"],
        )
        self.write_csv(
            OUTPUT / "men_accessories_product_create_compressed_images.csv",
            self.state.compressed_images,
            ["Product Code", "Image field", "Compression reason", "Error"],
        )
        lines = [
            "Men Accessories Media Quality Repair",
            f"Completed UTC: {datetime.now(timezone.utc).isoformat()}",
            f"Mode: {'DRY_RUN' if self.dry_run else 'LIVE'}",
            f"Products attempted: {attempted_products}",
            f"Products successful: {successful_products}",
            f"Products failed: {failed_products}",
            f"Image rows: {len(report)}",
            "Baserow updated: no",
            "Product attributes changed: no",
        ]
        (
            OUTPUT / "final_men_accessories_low_quality_media_repair_report.txt"
        ).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1 if failed_products else 0

    def baserow_status_value(
        self, field_name: str, preferred_values: list[str]
    ) -> Any:
        field = self.fields.get(field_name)
        if not field:
            return None
        if field.get("type") != "single_select":
            return preferred_values[0]
        normalized = {value.casefold() for value in preferred_values}
        for option in field.get("select_options", []):
            if normalize_status(option) in normalized:
                return int(option["id"])
        return None

    def update_baserow(
        self,
        row_id: int,
        comment: str = "",
        *,
        shopify_status: str = "Created",
        product_url: str = "",
    ) -> None:
        values: dict[str, Any] = {
            self.fields["Generation Status"]["name"]: self.status_option_id,
        }
        status_value = self.baserow_status_value(
            "Shopify Status",
            [
                shopify_status,
                "Already Exists" if shopify_status == "Synced" else "Uploaded",
                "Uploaded",
                "Created",
            ],
        )
        if status_value is not None:
            values[self.fields["Shopify Status"]["name"]] = status_value
        if product_url and "Shopify Product URL" in self.fields:
            values[self.fields["Shopify Product URL"]["name"]] = product_url
        if self.write_comments and comment and "Comment" in self.fields:
            values[self.fields["Comment"]["name"]] = comment[:10000]
        self.baserow.update_row(row_id, values)

    def restore_baserow_approved(self, row_id: int, comment: str = "") -> None:
        values: dict[str, Any] = {
            self.fields["Generation Status"]["name"]:
                self.approved_status_option_id,
        }
        if self.write_comments and comment and "Comment" in self.fields:
            values[self.fields["Comment"]["name"]] = comment[:10000]
        self.baserow.update_row(row_id, values)

    def write_failure_comment(self, row_id: int, error: str) -> None:
        if not self.write_comments or "Comment" not in self.fields:
            return
        try:
            self.baserow.update_row(
                row_id, {self.fields["Comment"]["name"]: f"Shopify create failed: {error}"[:10000]}
            )
        except Exception as exc:
            self.logger.warning("Could not write failure comment for row %s: %s", row_id, exc)

    def base_record(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "Row ID": row.get("id", ""),
            "Product Code": visible(self.row_value(row, "Product Code")),
            "Source Title": visible(self.row_value(row, "Product Title")),
            "Generation Status": visible(self.row_value(row, "Generation Status")),
            "SHOPIFY Notes": visible(self.row_value(row, "SHOPIFY Notes")),
        }

    def run_baserow_access_check(self) -> int:
        required_fields = {
            "Tie Image": 8133049,
            "Generated Front View": 9684129,
            "Generation Status": 8133054,
            "Comment": 8133056,
            "SHOPIFY Notes": 8133057,
        }
        token = os.getenv(self.baserow_token_env, "").strip() if self.baserow_token_env != "none" else ""
        base_url = self.baserow.base_url
        field_url = f"{base_url}/api/database/fields/table/{self.table_id}/"
        rows_url = (
            f"{base_url}/api/database/rows/table/{self.table_id}/"
            "?user_field_names=true&size=1"
        )
        report: dict[str, Any] = {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "CHECK_MEN_ACCESSORIES_BASEROW_ACCESS",
            "database_id": int(self.config["database_id"]),
            "table_id": int(self.table_id),
            "table_name": self.config["table_name"],
            "token_environment_variable_used": self.baserow_token_env,
            "token_environment_variables_present": {
                "BASEROW_TOKEN": bool(os.getenv("BASEROW_TOKEN", "").strip()),
                "BASEROW_API_TOKEN": bool(os.getenv("BASEROW_API_TOKEN", "").strip()),
            },
            "token_present": bool(token),
            "token_length": len(token),
            "api_base_url": base_url,
            "field_endpoint": {
                "url": field_url,
                "status_code": None,
                "reason": "",
                "accessible": False,
                "error": "",
            },
            "rows_endpoint": {
                "url": rows_url,
                "status_code": None,
                "reason": "",
                "accessible": False,
                "error": "",
            },
            "required_fields": {},
            "generation_status_options": {
                "values": [],
                "approved_present": False,
                "shopify_sync_present": False,
            },
            "shopify_notes_options": {
                "field_type": "",
                "check_method": "not_available",
                "values": [],
                "approved_present": False,
            },
            "shopify_notes_approved_value_probe": {
                "performed": False,
                "status_code": None,
                "matching_rows": 0,
                "error": "",
            },
            "read_access": False,
            "update_access": "not_tested",
            "shopify_called": False,
            "openrouter_called": False,
            "products_created": 0,
            "baserow_rows_updated": 0,
            "messages": [],
        }
        metadata: list[dict[str, Any]] = []
        if not token:
            report["messages"].append(
                "Neither BASEROW_TOKEN nor BASEROW_API_TOKEN is present."
            )
        else:
            for key, url in (("field_endpoint", field_url), ("rows_endpoint", rows_url)):
                try:
                    response = self.baserow.session.get(url, timeout=45)
                    report[key].update(
                        {
                            "status_code": response.status_code,
                            "reason": response.reason,
                            "accessible": response.status_code == 200,
                        }
                    )
                    if key == "field_endpoint" and response.status_code == 200:
                        payload = response.json()
                        metadata = payload if isinstance(payload, list) else []
                except Exception as exc:
                    report[key]["error"] = f"{type(exc).__name__}: {exc}"

        field_status = report["field_endpoint"]["status_code"]
        rows_status = report["rows_endpoint"]["status_code"]
        permission_message = (
            "The Baserow token is valid or loaded, but it does not have permission "
            "to access Men Accessories table 936098. Create or update a Baserow database "
            "token with read access to table 936098 and update access to Generation "
            "Status / Comment fields."
        )
        if field_status == 401 or rows_status == 401:
            report["messages"].append(permission_message)
        if report["field_endpoint"]["accessible"] and not report["rows_endpoint"]["accessible"]:
            report["messages"].append("Token can read fields but cannot read rows.")
        if report["rows_endpoint"]["accessible"]:
            report["read_access"] = True
            report["messages"].append(
                "Read access works. Update access will be tested only in DRY_RUN=false "
                "mode unless a safe test row is configured."
            )

        fields_by_id = {
            int(item["id"]): item
            for item in metadata
            if isinstance(item, dict) and item.get("id") is not None
        }
        for expected_name, expected_id in required_fields.items():
            found = fields_by_id.get(expected_id)
            actual_name = visible(found.get("name")) if found else ""
            accepted_names = {expected_name.casefold()}
            if expected_name == "Generated Front View":
                accepted_names |= {
                    "front view",
                    "full-length front flat-lay view",
                    "field_8133053",
                    "field_9684129",
                }
            report["required_fields"][expected_name] = {
                "expected_id": expected_id,
                "exists": found is not None,
                "actual_name": actual_name,
                "field_type": visible(found.get("type")) if found else "",
                "name_matches": actual_name.casefold() in accepted_names if actual_name else False,
            }

        generation = fields_by_id.get(required_fields["Generation Status"])
        generation_values = [
            visible(option)
            for option in (generation or {}).get("select_options", [])
            if visible(option)
        ]
        generation_normalized = {value.casefold() for value in generation_values}
        report["generation_status_options"] = {
            "values": generation_values,
            "approved_present": "approved" in generation_normalized,
            "shopify_sync_present": "shopify-sync" in generation_normalized,
        }
        notes = fields_by_id.get(required_fields["SHOPIFY Notes"])
        notes_type = visible((notes or {}).get("type"))
        notes_values = [
            visible(option)
            for option in (notes or {}).get("select_options", [])
            if visible(option)
        ]
        notes_normalized = {value.casefold() for value in notes_values}
        notes_approved_present = "approved" in notes_normalized
        notes_check_method = "single_select_metadata"
        if (
            notes
            and notes_type != "single_select"
            and report["rows_endpoint"]["accessible"]
        ):
            report["shopify_notes_approved_value_probe"]["performed"] = True
            try:
                probe = self.baserow.session.get(
                    f"{base_url}/api/database/rows/table/{self.table_id}/",
                    params={
                        "user_field_names": "true",
                        "size": 1,
                        f"filter__field_{required_fields['SHOPIFY Notes']}__equal": "Approved",
                    },
                    timeout=45,
                )
                report["shopify_notes_approved_value_probe"]["status_code"] = (
                    probe.status_code
                )
                if probe.status_code == 200:
                    matching_rows = int(probe.json().get("count") or 0)
                    report["shopify_notes_approved_value_probe"]["matching_rows"] = (
                        matching_rows
                    )
                    notes_approved_present = matching_rows > 0
                    notes_check_method = "exact_text_value_probe"
                    if notes_approved_present:
                        notes_values = ["Approved"]
            except Exception as exc:
                report["shopify_notes_approved_value_probe"]["error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                notes_check_method = "exact_text_value_probe_failed"
        report["shopify_notes_options"] = {
            "field_type": notes_type,
            "check_method": notes_check_method,
            "values": notes_values,
            "approved_present": notes_approved_present,
        }

        field_checks_ok = all(
            item["exists"] and item["name_matches"]
            for item in report["required_fields"].values()
        )
        option_checks_ok = (
            report["generation_status_options"]["approved_present"]
            and report["generation_status_options"]["shopify_sync_present"]
            and report["shopify_notes_options"]["approved_present"]
        )
        report["access_check_passed"] = bool(
            report["field_endpoint"]["accessible"]
            and report["rows_endpoint"]["accessible"]
            and field_checks_ok
            and option_checks_ok
        )
        self.write_baserow_access_reports(report)
        for message in report["messages"]:
            self.logger.warning(message)
        self.logger.info(
            "Baserow access diagnostic completed: token_env=%s token_present=%s "
            "token_length=%s fields_status=%s rows_status=%s passed=%s",
            report["token_environment_variable_used"],
            report["token_present"],
            report["token_length"],
            field_status,
            rows_status,
            report["access_check_passed"],
        )
        print((OUTPUT / "men_accessories_baserow_access_check.txt").read_text(encoding="utf-8"))
        return 0 if report["access_check_passed"] else 1

    @staticmethod
    def write_baserow_access_reports(report: dict[str, Any]) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        json_path = OUTPUT / "men_accessories_baserow_access_check.json"
        text_path = OUTPUT / "men_accessories_baserow_access_check.txt"
        json_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        lines = [
            "Men Accessories Baserow Access Check",
            f"Checked UTC: {report['checked_at_utc']}",
            f"Database: {report['database_id']}",
            f"Table: {report['table_name']} ({report['table_id']})",
            f"Token environment variable used: {report['token_environment_variable_used']}",
            f"Token present: {'yes' if report['token_present'] else 'no'}",
            f"Token length: {report['token_length']}",
            f"API base URL: {report['api_base_url']}",
            f"Field endpoint: HTTP {report['field_endpoint']['status_code']} "
            f"{report['field_endpoint']['reason']}".rstrip(),
            f"Rows endpoint: HTTP {report['rows_endpoint']['status_code']} "
            f"{report['rows_endpoint']['reason']}".rstrip(),
            f"Read access: {'yes' if report['read_access'] else 'no'}",
            f"Update access: {report['update_access']}",
            "",
            "Required fields:",
        ]
        for name, item in report["required_fields"].items():
            lines.append(
                f"- {name} ({item['expected_id']}): "
                f"{'present' if item['exists'] else 'not confirmed'}"
                + (
                    f"; actual name={item['actual_name']}; "
                    f"name match={'yes' if item['name_matches'] else 'no'}"
                    if item["exists"]
                    else ""
                )
            )
        generation = report["generation_status_options"]
        notes = report["shopify_notes_options"]
        lines.extend(
            [
                "",
                "Select options:",
                f"- Generation Status Approved: {'yes' if generation['approved_present'] else 'not confirmed'}",
                f"- Generation Status Shopify-sync: {'yes' if generation['shopify_sync_present'] else 'not confirmed'}",
                f"- SHOPIFY Notes Approved: {'yes' if notes['approved_present'] else 'not confirmed'}",
                f"- SHOPIFY Notes field type: {notes['field_type'] or 'not confirmed'}",
                f"- SHOPIFY Notes check method: {notes['check_method']}",
                "",
                f"Access check passed: {'yes' if report['access_check_passed'] else 'no'}",
                "Shopify called: no",
                "OpenRouter called: no",
                "Products created: 0",
                "Baserow rows updated: 0",
                "",
                "Messages:",
            ]
        )
        lines.extend(f"- {message}" for message in report["messages"])
        text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_resized_media_repair(self) -> int:
        self.fetch_and_validate_fields()
        successes = 0
        failures = 0
        for row in self.baserow.iter_rows():
            if self.max_products is not None and successes >= self.max_products:
                break
            if status(self.row_value(row, "SHOPIFY Notes")) != "approved":
                continue
            sku = visible(self.row_value(row, "Product Code"))
            if not sku or not original_file_urls(
                self.row_value(row, "Generated Front View")
            ):
                continue
            product_dir: Path | None = None
            try:
                product = self.find_product_for_fix_by_sku(sku)
                if not product:
                    raise RuntimeError("shopify_product_not_found_for_sku")
                front_images = [
                    image
                    for image in self.images(row)
                    if image["label"] == "Generated Front View"
                ]
                prepared, product_dir = self.prepare_images_before_create(
                    front_images, sku, row["id"]
                )
                old_media = self.get_product_media(product["id"])
                old_front = old_media[0] if old_media else {}
                front_alt = (
                    f"{sku} - Resized Generated Front View "
                    f"{self.resize_target_width}x{self.resize_target_height}"
                )
                quality = self.upload_product_images(
                    product["id"],
                    sku,
                    visible(product.get("title")) or sku,
                    front_alt,
                    prepared,
                    baserow_row_id=row["id"],
                )
                self.set_front_first(product["id"], front_alt)
                refreshed = self.get_product_media(product["id"])
                new_front = refreshed[0] if refreshed else {}
                if (
                    old_front.get("id")
                    and old_front.get("id") != new_front.get("id")
                    and "Generated Front View"
                    in visible(old_front.get("alt")).casefold()
                ):
                    self.delete_product_media(
                        product["id"], [old_front["id"]]
                    )
                for record in quality:
                    record.update(
                        {
                            "Shopify Product ID": product["id"],
                            "Generated Front View First": (
                                "yes"
                                if record.get("Shopify Media ID")
                                == new_front.get("id")
                                else "no"
                            ),
                            "Result": "resized_media_repaired",
                        }
                    )
                    self.state.image_quality.append(record)
                    self.state.report.append(record.copy())
                self.cleanup_prepared_images(product_dir)
                product_dir = None
                successes += 1
            except Exception as exc:
                failures += 1
                error = f"{type(exc).__name__}: {exc}"
                self.state.image_failures.append(
                    {
                        "Baserow Row ID": row.get("id", ""),
                        "Product Code": sku,
                        "Result": "resized_media_repair_failed",
                        "Error": error,
                    }
                )
                if product_dir and not self.keep_failed_temp_files:
                    self.cleanup_prepared_images(product_dir)
                self.logger.exception(
                    "Resized media repair failed for SKU=%s", sku
                )
        self.state.created = successes
        self.state.failed = failures
        self.write_outputs()
        return 1 if failures else 0

    def verify_reconciliation_collections(self) -> dict[str, str]:
        def collection_gid(name: str, default_legacy_id: str) -> str:
            raw = os.getenv(name, default_legacy_id).strip()
            if raw.startswith("gid://shopify/Collection/"):
                return raw
            if not raw.isdigit():
                raise ValueError(f"{name} must be a Shopify collection legacy ID or GID")
            return f"gid://shopify/Collection/{raw}"

        expected = {
            "Men Accessories": collection_gid(
                "MEN_ACCESSORIES_REQUIRED_COLLECTION_1_ID", "339913605318"
            ),
            "Men's Ties": collection_gid(
                "MEN_ACCESSORIES_REQUIRED_COLLECTION_2_ID", "339549880518"
            ),
        }
        query = "query($ids:[ID!]!){nodes(ids:$ids){... on Collection{id title}}}"
        nodes = self.shopify.graphql(query, {"ids": list(expected.values())})["nodes"]
        actual = {
            visible(node.get("title")): visible(node.get("id"))
            for node in nodes if node
        }
        for title, collection_id in expected.items():
            if actual.get(title) != collection_id:
                raise ShopifyError(
                    f"required_collection_verification_failed:{title}:"
                    f"expected={collection_id}:actual={actual.get(title, '')}"
                )
        self.primary_collection_title = "Men Accessories"
        self.secondary_collection_title = "Men's Ties"
        self.secondary_collection_id = expected["Men's Ties"]
        return expected

    @staticmethod
    def reconciliation_alt(
        sku: str, image: dict[str, Any], role_counts: dict[str, int]
    ) -> str:
        label = str(image["label"])
        ordinal = int(image.get("field_index") or 0) + 1
        suffix = f" {ordinal}" if role_counts.get(label, 0) > 1 else ""
        return f"{sku} - {label}{suffix}"

    def reconciliation_media_match(
        self,
        sku: str,
        image: dict[str, Any],
        role_counts: dict[str, int],
        media: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        expected_alt = self.reconciliation_alt(sku, image, role_counts)
        normalized_url = str(image["url"]).split("?", 1)[0].casefold()
        label = str(image["label"]).casefold()
        return next(
            (
                node for node in media
                if visible(node.get("alt")).casefold() == expected_alt.casefold()
                or (
                    role_counts.get(str(image["label"]), 0) == 1
                    and label in visible(node.get("alt")).casefold()
                )
                or str((node.get("originalSource") or {}).get("url") or "")
                .split("?", 1)[0].casefold() == normalized_url
            ),
            None,
        )

    def get_reconciliation_product_state(
        self,
        product_id: str,
        sku: str,
        publication_id: str,
        location_id: str,
        images: list[dict[str, Any]],
        collection_ids: dict[str, str],
    ) -> dict[str, Any]:
        query = """query($id:ID!,$publicationId:ID!,$locationId:ID!){
          product(id:$id){
            id legacyResourceId title handle status
            collections(first:250){nodes{id title}}
            publishedOnPublication(publicationId:$publicationId)
            variants(first:20){nodes{id sku price inventoryPolicy taxable
              inventoryItem{id tracked inventoryLevel(locationId:$locationId){
                quantities(names:["available","on_hand"]){name quantity}}}}}
            media(first:100){nodes{id alt status mediaContentType
              ... on MediaImage{image{url width height} originalSource{fileSize url}}}}
          }
        }"""
        product = self.shopify.graphql(
            query,
            {"id": product_id, "publicationId": publication_id, "locationId": location_id},
        ).get("product")
        if not product:
            return {"exists": False, "verified": False, "product": None}
        normalized = self.normalized_sku_for_search(sku)
        variants = [
            item for item in product.get("variants", {}).get("nodes", [])
            if self.normalized_sku_for_search(item.get("sku")) == normalized
        ]
        variant = variants[0] if len(variants) == 1 else {}
        inventory_item = variant.get("inventoryItem") or {}
        quantities = {
            item["name"]: int(item["quantity"])
            for item in (inventory_item.get("inventoryLevel") or {}).get("quantities", [])
        }
        memberships = {
            visible(node.get("id"))
            for node in product.get("collections", {}).get("nodes", [])
        }
        media = product.get("media", {}).get("nodes", [])
        role_counts: dict[str, int] = {}
        for image in images:
            role_counts[str(image["label"])] = role_counts.get(str(image["label"]), 0) + 1
        matched = [
            self.reconciliation_media_match(sku, image, role_counts, media)
            for image in images
        ]
        existing = sum(item is not None for item in matched)
        ready = all(item is not None and item.get("status") == "READY" for item in matched)
        state = {
            "exists": True, "product": product, "sku_exact": len(variants) == 1,
            "active": product.get("status") == "ACTIVE",
            "published": bool(product.get("publishedOnPublication")),
            "men_accessories": collection_ids["Men Accessories"] in memberships,
            "mens_ties": collection_ids["Men's Ties"] in memberships,
            "inventory_tracked": inventory_item.get("tracked") is True,
            "inventory_quantity": quantities.get("available"),
            "inventory_on_hand": quantities.get("on_hand"),
            "inventory_policy": variant.get("inventoryPolicy"),
            "taxable": variant.get("taxable") is True,
            "images_expected": len(images), "images_existing": existing,
            "images_missing": len(images) - existing, "media_ready": ready,
        }
        state["verified"] = all((
            state["sku_exact"], state["active"], state["published"],
            state["men_accessories"], state["mens_ties"],
            state["inventory_tracked"], state["inventory_quantity"] == 1,
            state["inventory_on_hand"] == 1, state["inventory_policy"] == "DENY",
            state["taxable"], state["media_ready"],
        ))
        return state

    def find_reconciliation_product_candidates(
        self, row: dict[str, Any], sku: str
    ) -> tuple[list[dict[str, Any]], str]:
        matches = self.find_products_by_sku(sku)
        if matches:
            return matches, "exact_normalized_sku"
        product_url = visible(self.row_value(row, "Shopify Product URL"))
        numeric = re.search(r"(?:Product/|products/)(\d+)(?:\D|$)", product_url)
        if numeric:
            try:
                product = self.get_product_for_fix(
                    f"gid://shopify/Product/{numeric.group(1)}"
                )
            except Exception:
                product = None
            if product and any(
                self.normalized_sku_for_search(item.get("sku"))
                == self.normalized_sku_for_search(sku)
                for item in product.get("variants", {}).get("nodes", [])
            ):
                return [product], "baserow_product_url"
        handle_match = re.search(r"/products/([^/?#]+)", product_url)
        if handle_match:
            query = "query($q:String!){products(first:10,query:$q){nodes{id legacyResourceId title handle status variants(first:20){nodes{sku}}}}}"
            nodes = self.shopify.graphql(
                query, {"q": f'handle:"{gql_escape(handle_match.group(1))}"'}
            )["products"]["nodes"]
            exact = [
                product for product in nodes
                if any(
                    self.normalized_sku_for_search(item.get("sku"))
                    == self.normalized_sku_for_search(sku)
                    for item in product.get("variants", {}).get("nodes", [])
                )
            ]
            if exact:
                return exact, "baserow_product_handle"
        title = visible(self.row_value(row, "Product Title"))
        if title:
            query = "query($q:String!){products(first:20,query:$q){nodes{id title handle variants(first:20){nodes{sku}}}}}"
            title_nodes = self.shopify.graphql(
                query, {"q": f'title:"{gql_escape(title)}"'}
            )["products"]["nodes"]
            exact_titles = [
                product for product in title_nodes
                if visible(product.get("title")).casefold() == title.casefold()
            ]
            matching = [
                product for product in exact_titles
                if any(
                    self.normalized_sku_for_search(item.get("sku"))
                    == self.normalized_sku_for_search(sku)
                    for item in product.get("variants", {}).get("nodes", [])
                )
            ]
            if matching:
                return matching, "exact_title_and_sku"
            if exact_titles:
                return [], "exact_title_conflict"
        return [], "missing"

    def ensure_reconciliation_featured_media(
        self, product_id: str, sku: str, images: list[dict[str, Any]]
    ) -> bool:
        featured = self.featured_image_label(images)
        if not featured:
            return True
        media = self.get_product_media(product_id)
        role_counts: dict[str, int] = {}
        for image in images:
            role_counts[str(image["label"])] = role_counts.get(str(image["label"]), 0) + 1
        candidate = next((item for item in images if item.get("label") == featured), None)
        match = self.reconciliation_media_match(sku, candidate, role_counts, media) if candidate else None
        if not match:
            raise ShopifyError("featured_media_not_found")
        if media and media[0]["id"] != match["id"]:
            self.set_front_first(product_id, visible(match.get("alt")))
        refreshed = self.get_product_media(product_id)
        return bool(refreshed and refreshed[0]["id"] == match["id"])

    def reconciliation_record(
        self, row: dict[str, Any], sku: str, state: dict[str, Any], *,
        recreated: bool = False, uploaded: int = 0, duplicate_media: int = 0,
        persistence: str = "", result: str = "", error: str = "",
    ) -> dict[str, Any]:
        product = state.get("product") or {}
        return {
            "Baserow Row ID": row.get("id", ""), "Product Code": sku,
            "Generation Status Before": visible(self.row_value(row, "Generation Status")),
            "Shopify Status Before": visible(self.row_value(row, "Shopify Status")),
            "Shopify SKU Found": "yes" if state.get("sku_exact") else "no",
            "Shopify Product ID": product.get("id", ""),
            "Product Exists": "yes" if state.get("exists") else "no",
            "Product Recreated": "yes" if recreated else "no",
            "Product ACTIVE": "yes" if state.get("active") else "no",
            "Online Store Published": "yes" if state.get("published") else "no",
            "Men Accessories Collection Present": "yes" if state.get("men_accessories") else "no",
            "Men's Ties Collection Present": "yes" if state.get("mens_ties") else "no",
            "Inventory Tracked": "yes" if state.get("inventory_tracked") else "no",
            "Inventory Quantity": state.get("inventory_quantity", ""),
            "Images Expected": state.get("images_expected", 0),
            "Images Existing": state.get("images_existing", 0),
            "Images Missing": state.get("images_missing", 0),
            "Images Uploaded": uploaded, "Duplicate Images Prevented": duplicate_media,
            "Shopify Product URL": self.shopify_admin_product_url(product) if product else "",
            "Persistence Verified": persistence,
            "Generation Status After": "Shopify-sync",
            "Result": result, "Error": error,
        }

    def write_reconciliation_reports(
        self, rows: list[dict[str, Any]], missing: list[dict[str, Any]],
        recreated: list[dict[str, Any]], repaired: list[dict[str, Any]],
        duplicates: list[dict[str, Any]], image_rows: list[dict[str, Any]], *,
        audit: bool,
    ) -> None:
        fields = [
            "Baserow Row ID", "Product Code", "Generation Status Before",
            "Shopify Status Before", "Shopify SKU Found", "Shopify Product ID",
            "Product Exists", "Product Recreated", "Product ACTIVE",
            "Online Store Published", "Men Accessories Collection Present",
            "Men's Ties Collection Present", "Inventory Tracked",
            "Inventory Quantity", "Images Expected", "Images Existing",
            "Images Missing", "Images Uploaded", "Duplicate Images Prevented",
            "Shopify Product URL", "Persistence Verified",
            "Generation Status After", "Result", "Error",
        ]
        if audit:
            self.write_csv(OUTPUT / "men_accessories_shopify_sync_reconciliation_audit.csv", rows, fields)
            return
        self.write_csv(OUTPUT / "men_accessories_shopify_sync_reconciliation.csv", rows, fields)
        self.write_csv(OUTPUT / "men_accessories_shopify_sync_missing_products.csv", missing, fields)
        self.write_csv(OUTPUT / "men_accessories_shopify_sync_recreated_products.csv", recreated, fields)
        self.write_csv(OUTPUT / "men_accessories_shopify_sync_repaired_products.csv", repaired, fields)
        self.write_csv(OUTPUT / "men_accessories_shopify_sync_duplicates.csv", duplicates, fields)
        self.write_csv(
            OUTPUT / "men_accessories_shopify_sync_image_report.csv", image_rows,
            ["Baserow Row ID", "Product Code", "Image field", "Upload mode", "Media READY yes/no", "Error"],
        )
        verified_codes = {
            row.get("Product Code")
            for row in rows
            if row.get("Persistence Verified") == "yes" and row.get("Product Code")
        }
        verified = len(verified_codes)
        unresolved = sum(row.get("Persistence Verified") != "yes" for row in rows)
        found_codes = {
            row.get("Product Code") for row in rows
            if row.get("Product Exists") == "yes" and row.get("Product Code")
        }
        active_codes = {
            row.get("Product Code") for row in rows
            if row.get("Product ACTIVE") == "yes" and row.get("Product Code")
        }
        published_codes = {
            row.get("Product Code") for row in rows
            if row.get("Online Store Published") == "yes" and row.get("Product Code")
        }
        mens_ties_present = sum(
            row.get("Men's Ties Collection Present") == "yes"
            for row in repaired + recreated
        )
        lines = [
            "Men Accessories Shopify-sync Reconciliation",
            f"Completed UTC: {datetime.now(timezone.utc).isoformat()}",
            f"Mode: {'AUDIT' if self.dry_run else 'LIVE'}",
            f"Baserow Shopify-sync rows: {len(rows)}",
            f"Unique Product Codes: {len({row.get('Product Code') for row in rows if row.get('Product Code')})}",
            f"Shopify products currently found: {len(found_codes)}",
            f"Products missing from Shopify: {len(missing)}",
            f"Products recreated: {len(recreated)}", f"Products repaired: {len(repaired)}",
            f"Products set ACTIVE: {len(active_codes)}",
            f"Products published to Online Store: {len(published_codes)}",
            f"Products added to Men Accessories: {sum(row.get('Men Accessories Collection Present') == 'yes' for row in repaired + recreated)}",
            f"Products added to Men's Ties: {mens_ties_present}",
            f"Products inventory fixed to 1: {sum(str(row.get('Inventory Quantity')) == '1' for row in repaired + recreated)}",
            f"Products already correct: {sum(row.get('Result') == 'already_correct' for row in rows)}",
            f"Images missing: {sum(int(row.get('Images Missing') or 0) for row in rows)}",
            f"Images uploaded: {sum(int(row.get('Images Uploaded') or 0) for row in rows)}",
            f"Duplicate media prevented: {sum(int(row.get('Duplicate Images Prevented') or 0) for row in rows)}",
            f"Multiple SKU conflicts: {sum('multiple_shopify_products_for_sku' in row.get('Error', '') for row in rows)}",
            f"Persistence failures: {sum(row.get('Persistence Verified') == 'no' for row in rows)}",
            f"Final verified Shopify products: {verified}", f"Final unresolved rows: {unresolved}",
        ]
        (OUTPUT / "final_men_accessories_shopify_sync_reconciliation.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_shopify_sync_reconciliation(self) -> int:
        self.fetch_and_validate_fields()
        collection_ids = self.verify_reconciliation_collections()
        self.audit_reference_product()
        publication_id = self.get_publication_id_by_name(self.publication_name)
        location = self.resolve_inventory_location()
        source_rows = [
            row for row in self.baserow.iter_rows()
            if normalize_status(self.row_value(row, "Generation Status")) == "shopify-sync"
        ]
        seen: set[str] = set()
        audit_rows: list[dict[str, Any]] = []
        contexts: list[tuple[dict[str, Any], str, list[dict[str, Any]], list[dict[str, Any]], str]] = []
        duplicates: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for row in source_rows:
            sku = visible(self.row_value(row, "Product Code"))
            normalized = self.normalized_sku_for_search(sku)
            if not sku:
                error = "product_code_missing"
                record = self.reconciliation_record(row, sku, {}, result="manual_review", error=error)
                audit_rows.append(record)
                duplicates.append(record)
                continue
            seen.add(normalized)
            images = self.images(row)
            products, lookup = self.find_reconciliation_product_candidates(row, sku)
            if len(products) > 1:
                ids = ",".join(product["id"] for product in products)
                record = self.reconciliation_record(
                    row, sku, {}, result="manual_review",
                    error=f"multiple_shopify_products_for_sku:{ids}",
                )
                audit_rows.append(record)
                duplicates.append(record)
                continue
            if not products:
                state = {
                    "exists": False, "verified": False,
                    "images_expected": len(images), "images_existing": 0,
                    "images_missing": len(images),
                }
                error = "shopify_product_missing" if lookup == "missing" else lookup
                record = self.reconciliation_record(row, sku, state, result="missing", error=error)
                audit_rows.append(record)
                missing.append(record)
                contexts.append((row, sku, images, [], lookup))
                continue
            product = products[0]
            state = self.get_reconciliation_product_state(
                product["id"], sku, publication_id, location["id"], images, collection_ids
            )
            record = self.reconciliation_record(
                row, sku, state,
                result="already_correct" if state.get("verified") else "repair_required",
            )
            audit_rows.append(record)
            contexts.append((row, sku, images, products, lookup))
        self.write_reconciliation_reports(audit_rows, missing, [], [], duplicates, [], audit=True)
        if self.dry_run:
            self.write_reconciliation_reports(audit_rows, missing, [], [], duplicates, [], audit=False)
            return 0

        final_rows: list[dict[str, Any]] = []
        repaired: list[dict[str, Any]] = []
        recreated: list[dict[str, Any]] = []
        image_rows: list[dict[str, Any]] = []
        successful = 0
        for row, sku, images, products, lookup in contexts:
            if self.max_products is not None and successful >= self.max_products:
                break
            try:
                recreated_product = False
                before_uploaded = self.state.images_newly_uploaded
                before_existing = self.state.images_already_present
                if products:
                    product = products[0]
                    current_state = self.get_reconciliation_product_state(
                        product["id"], sku, publication_id, location["id"],
                        images, collection_ids,
                    )
                    if current_state.get("verified"):
                        self.update_baserow(
                            int(row["id"]),
                            f"Men Accessories reconciliation verified without repair: {product['id']}",
                            shopify_status="Synced",
                            product_url=self.shopify_admin_product_url(product),
                        )
                        record = self.reconciliation_record(
                            row, sku, current_state, duplicate_media=len(images),
                            persistence="yes", result="already_correct",
                        )
                        final_rows.append(record)
                        successful += 1
                        continue
                    product_for_fix = self.get_product_for_fix(product["id"])
                    self.current_inventory_sku = sku
                    preserve_new_arrivals_rule = self.add_new_arrivals
                    self.add_new_arrivals = False
                    try:
                        self.apply_operational_setup(
                            product_for_fix, row,
                            configure_inventory=True, configure_tags=False,
                        )
                    finally:
                        self.add_new_arrivals = preserve_new_arrivals_rule
                    quality = self.upload_product_images(
                        product["id"], sku, visible(product.get("title")),
                        f"{sku} - {self.featured_image_label(images)}", images,
                        baserow_row_id=row.get("id", ""),
                    )
                else:
                    missing_fields = self.missing_required_fields(row)
                    if missing_fields:
                        raise ValueError("; ".join(missing_fields))
                    repeat = self.find_products_by_sku(sku)
                    if len(repeat) > 1:
                        raise ShopifyError("multiple_shopify_products_for_sku")
                    if repeat:
                        product = repeat[0]
                        product_for_fix = self.get_product_for_fix(product["id"])
                        self.current_inventory_sku = sku
                        preserve_new_arrivals_rule = self.add_new_arrivals
                        self.add_new_arrivals = False
                        try:
                            self.apply_operational_setup(
                                product_for_fix, row,
                                configure_inventory=True, configure_tags=False,
                            )
                        finally:
                            self.add_new_arrivals = preserve_new_arrivals_rule
                        quality = self.upload_product_images(
                            product["id"], sku, visible(product.get("title")),
                            f"{sku} - {self.featured_image_label(images)}", images,
                            baserow_row_id=row.get("id", ""),
                        )
                    else:
                        handle = slugify(f"{visible(self.row_value(row, 'Product Title'))} {sku}")
                        reason, duplicate = self.find_duplicates(
                            sku, handle, visible(self.row_value(row, "Product Title"))
                        )
                        if reason:
                            raise ShopifyError(
                                f"recreation_duplicate_guard:{reason}:{(duplicate or {}).get('id', '')}"
                            )
                        ai = self.generate_ai(row, sku, images)
                        self.state.ai_success += 1
                        product, _ = self.create_product(
                            row, sku, handle, money(self.row_value(row, "Price")), ai, images
                        )
                        quality = []
                        recreated_product = True
                self.ensure_reconciliation_featured_media(product["id"], sku, images)
                image_rows.extend(quality)
                state = self.get_reconciliation_product_state(
                    product["id"], sku, publication_id, location["id"], images, collection_ids
                )
                if not state.get("verified"):
                    raise RuntimeError("reconciliation_verification_failed")
                self.update_baserow(
                    int(row["id"]),
                    f"Men Accessories reconciliation verified: {product['id']}",
                    shopify_status="Created" if recreated_product else "Synced",
                    product_url=self.shopify_admin_product_url(product),
                )
                if recreated_product:
                    wait_seconds = int(os.getenv("MEN_ACCESSORIES_RECONCILIATION_PERSISTENCE_SECONDS", "60"))
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    state = self.get_reconciliation_product_state(
                        product["id"], sku, publication_id, location["id"], images, collection_ids
                    )
                    if not state.get("verified"):
                        raise RuntimeError("product_disappeared_after_recreation")
                uploaded = self.state.images_newly_uploaded - before_uploaded
                existing = self.state.images_already_present - before_existing
                record = self.reconciliation_record(
                    row, sku, state, recreated=recreated_product,
                    uploaded=uploaded, duplicate_media=existing,
                    persistence="yes", result="recreated" if recreated_product else "repaired",
                )
                final_rows.append(record)
                (recreated if recreated_product else repaired).append(record)
                successful += 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.write_failure_comment(int(row["id"]), error)
                final_rows.append(
                    self.reconciliation_record(
                        row, sku, {}, persistence="no", result="unresolved", error=error
                    )
                )
        handled = {row.get("Baserow Row ID") for row in final_rows}
        final_rows.extend(
            row for row in audit_rows
            if row.get("Baserow Row ID") not in handled and row.get("Result") == "manual_review"
        )
        self.write_reconciliation_reports(
            final_rows, missing, recreated, repaired, duplicates, image_rows, audit=False
        )
        return 1 if any(row.get("Persistence Verified") != "yes" for row in final_rows) else 0

    def run(self) -> int:
        if self.check_baserow_access:
            return self.run_baserow_access_check()
        if self.audit_image_quality:
            self.logger.info(
                "Mode=READ_ONLY Men Accessories image quality audit table=%s",
                self.table_id,
            )
            return self.run_image_quality_audit()
        if self.shopify is None:
            raise RuntimeError("Shopify client is unavailable outside diagnostic mode")
        shop_name = self.shopify.check_connection()
        if self.reconcile_shopify_sync:
            self.logger.info(
                "Mode=%s Men Accessories Shopify-sync reconciliation shop=%s",
                "AUDIT" if self.dry_run else "LIVE",
                shop_name,
            )
            return self.run_shopify_sync_reconciliation()
        if self.repair_resized_media:
            self.logger.info(
                "Mode=%s Men Accessories resized-media repair shop=%s "
                "max_products=%s",
                "DRY_RUN" if self.dry_run else "LIVE",
                shop_name,
                self.max_products,
            )
            return self.run_resized_media_repair()
        if self.repair_media_quality:
            self.logger.info(
                "Mode=%s Men Accessories media quality repair shop=%s max_products=%s",
                "DRY_RUN" if self.dry_run else "LIVE",
                shop_name,
                self.max_products,
            )
            return self.run_media_quality_repair()
        if self.fix_direct_product:
            self.logger.info("Mode=%s direct product fix legacy_id=%s shop=%s",
                "DRY_RUN" if self.dry_run else "LIVE", self.fix_legacy_id, shop_name)
            return self.run_direct_product_fix()
        if self.fix_created_products:
            self.logger.info("Mode=%s Men Accessories created-products fix shop=%s",
                "DRY_RUN" if self.dry_run else "LIVE", shop_name)
            return self.run_created_products_fix()
        if not self.enabled:
            raise RuntimeError("Set CREATE_PRODUCTS_FROM_MEN_ACCESSORIES=true to enable this script")
        self.validate_run_scope()
        self.fetch_and_validate_fields()
        self.audit_reference_product()
        self.audit_theme_image_delivery()
        self.guard_against_external_deletion()
        publication_id = (
            self.get_publication_id_by_name(self.publication_name)
            if self.publish_online_store
            else ""
        )
        location = (
            self.resolve_inventory_location()
            if self.inventory_tracked
            else None
        )
        self.logger.info(
            "Mode=%s table=Men Accessories (%s) shop=%s max_products=%s",
            "DRY_RUN" if self.dry_run else "LIVE", self.table_id, shop_name, self.max_products,
        )
        seen_skus: set[str] = set()
        canaries: list[dict[str, Any]] = []
        for row in self.baserow.iter_rows():
            if self.run_cap_reached():
                break
            self.state.rows_loaded += 1
            record = self.base_record(row)
            if not self.approval_ok(row):
                continue
            self.state.approved_rows += 1
            missing = self.missing_required_fields(row)
            if missing:
                self.state.missing_fields += 1
                self.state.missing_rows.append(
                    {**record, "Missing fields": "; ".join(missing)}
                )
                continue
            sku = record["Product Code"]
            if sku.casefold() in seen_skus:
                self.state.duplicates += 1
                self.state.source_duplicate_skus += 1
                self.state.duplicate_rows.append(
                    {**record, "Duplicate reason": "duplicate_source_sku"}
                )
                continue
            seen_skus.add(sku.casefold())
            handle = slugify(f"{record['Source Title']} {sku}")
            stage = "generated_front_resolution_validation"
            resolution_record: dict[str, Any] | None = None
            product_temp_dir: Path | None = None
            try:
                images = self.images(row)
                featured_label = self.featured_image_label(images)
                front = next(
                    (
                        image
                        for image in images
                        if image.get("label") == featured_label
                    ),
                    {},
                )
                resolution_record = self.validate_generated_front_image(
                    front,
                    row_id=row.get("id", ""),
                    sku=sku,
                )
                resolution_record["Image Field"] = featured_label
                self.state.resolution_validation.append(resolution_record)
                if resolution_record["Resolution Eligible"] != "yes":
                    self.state.failed += 1
                    failure = {
                        **record,
                        **resolution_record,
                        "Stage": stage,
                    }
                    self.state.failures.append(failure)
                    self.state.image_failures.append(failure.copy())
                    self.logger.warning(
                        "Row %s SKU=%s rejected before Shopify create: %s",
                        row.get("id"),
                        sku,
                        resolution_record["Error"],
                    )
                    continue
                self.state.eligible += 1
                self.state.images_found += len(images)
                stage = "shopify_sku_lookup"
                sku_products = self.find_products_by_sku(sku)
                if len(sku_products) > 1:
                    raise ShopifyError("multiple_shopify_products_for_sku")
                if len(sku_products) == 1:
                    product = sku_products[0]
                    self.state.existing_products_matched += 1
                    self.state.duplicate_products_prevented += 1
                    self.state.duplicates += 1
                    self.state.existing_shopify_skus += 1
                    self.state.duplicate_rows.append(
                        {
                            **record,
                            "Duplicate reason": "existing_sku_media_completion",
                            "Shopify Product ID": product["id"],
                        }
                    )
                    if not self.media_completion_existing:
                        raise ShopifyError("existing_media_completion_disabled")
                    stage = "existing_product_image_url_validation"
                    image_checks = self.validate_image_urls(images)
                    invalid_images = [
                        check for check in image_checks if not check["valid"]
                    ]
                    if invalid_images:
                        raise ValueError(
                            "Invalid image URL(s): "
                            + "; ".join(
                                f"{check['label']} HTTP {check['status_code']}"
                                for check in invalid_images
                            )
                        )
                    self.state.attempted += 1
                    if self.dry_run:
                        self.state.preview.append(
                            {
                                **record,
                                "Shopify Product ID": product["id"],
                                "New Product yes/no": "no",
                                "Existing Product yes/no": "yes",
                                "Duplicate SKU prevented": "yes",
                                "Images found in Baserow": len(images),
                                "Result": "existing_media_completion_planned",
                            }
                        )
                        continue
                    stage = "existing_product_media_completion"
                    existing_result = self.complete_existing_product_media(
                        row, sku, product, images
                    )
                    resolution_record.update(
                        {
                            "Shopify Product ID": product["id"],
                            "Shopify Handle": product.get("handle", ""),
                            "Baserow Writeback": "shopify-sync",
                            "Result": "existing_media_completed",
                            "Error": "",
                        }
                    )
                    self.state.report.append(
                        {**record, **resolution_record, **existing_result}
                    )
                    self.logger.info(
                        "Completed existing product media SKU=%s id=%s",
                        sku,
                        product["id"],
                    )
                    continue
                stage = "shopify_duplicate_check"
                reason, duplicate = self.find_duplicates(
                    sku, handle, record["Source Title"]
                )
                if reason:
                    self.state.duplicates += 1
                    self.state.duplicate_products_prevented += 1
                    self.count_shopify_duplicate(reason)
                    self.state.duplicate_rows.append(
                        {**record, "Duplicate reason": reason, "Shopify Product ID": duplicate["id"]}
                    )
                    resolution_record["Result"] = reason
                    continue
                price = money(self.row_value(row, "Price"))
                stage = "image_url_validation"
                image_checks = self.validate_image_urls(images)
                invalid_images = [check for check in image_checks if not check["valid"]]
                if invalid_images:
                    raise ValueError(
                        "Invalid image URL(s): "
                        + "; ".join(
                            f"{check['label']} HTTP {check['status_code']} "
                            f"{check['content_type']} {check['error']}".strip()
                            for check in invalid_images
                        )
                    )
                stage = "collection_mapping"
                collection_title, collection = self.resolve_collection_for_plan(row)
                secondary_collection_id = (
                    self.get_collection_id_by_title(
                        self.secondary_collection_title
                    )
                    if self.secondary_collection_title
                    else ""
                )
                if (
                    self.secondary_collection_id
                    and secondary_collection_id != self.secondary_collection_id
                ):
                    raise ShopifyError(
                        "secondary_collection_id_mismatch:"
                        f"expected={self.secondary_collection_id}:"
                        f"actual={secondary_collection_id}"
                    )
                new_arrivals_collection_id = (
                    self.get_collection_id_by_title(
                        self.new_arrivals_collection_name
                    )
                    if self.add_new_arrivals
                    else ""
                )
                self.state.collection_success += 1
                stage = "openrouter"
                try:
                    ai = self.generate_ai(row, sku, images)
                except Exception:
                    self.state.ai_failed += 1
                    raise
                self.state.ai_success += 1
                stage = "shopify_duplicate_check_generated_title"
                post_reason, post_duplicate = self.find_duplicates(sku, handle, ai["title"])
                if post_reason:
                    self.state.duplicates += 1
                    self.count_shopify_duplicate(post_reason)
                    self.state.duplicate_rows.append(
                        {**record, "Duplicate reason": post_reason, "Shopify Product ID": post_duplicate["id"]}
                    )
                    resolution_record["Result"] = post_reason
                    continue
                stage = "image_resize"
                images, product_temp_dir = self.prepare_images_before_create(
                    images, sku, row["id"]
                )
                self.state.attempted += 1
                ai_record = {
                    **record,
                    "Generated Title": ai["title"],
                    "AI warnings": "; ".join(map(str, ai.get("warnings", []))),
                    "OpenRouter called": "yes" if self.openrouter else "no",
                }
                self.state.ai_rows.append(ai_record)
                planned = {
                    **record,
                    "Product Title": record["Source Title"],
                    "New Product yes/no": "yes",
                    "Existing Product yes/no": "no",
                    "Duplicate SKU prevented": "no",
                    "AI Description Generated": "yes",
                    "Handle": handle,
                    "Generated Title": ai["title"],
                    "Price": price,
                    "Image count": len(images),
                    "Image URL validation": "passed",
                    "Collection": collection_title,
                    "Collection ID": collection["id"],
                    "Collection mapping": "passed",
                    "Primary Collection": collection_title,
                    "Primary Collection Assigned": "planned",
                    "Secondary Collection": self.secondary_collection_title,
                    "Secondary Collection ID": secondary_collection_id,
                    "Secondary Collection Assigned": "planned",
                    "Secondary Collection Already Assigned": "unknown",
                    "New Arrivals Collection ID": new_arrivals_collection_id,
                    "New Arrivals Assignment Attempted": (
                        "planned" if self.add_new_arrivals else "no"
                    ),
                    "New Arrivals Assigned": (
                        "planned" if self.add_new_arrivals else "disabled"
                    ),
                    "New Arrivals Already Assigned": "unknown",
                    "New Arrivals Error": "",
                    "Status": self.product_status,
                    "Product status": self.product_status,
                    "Online Store publication ID": publication_id,
                    "Online Store published yes/no": (
                        "planned" if self.publish_online_store else "no"
                    ),
                    "Inventory tracked yes/no": (
                        "yes" if self.inventory_tracked else "no"
                    ),
                    "Inventory location name": location["name"] if location else "",
                    "Inventory location ID": location["id"] if location else "",
                    "Inventory quantity requested": (
                        self.inventory_quantity if self.inventory_tracked else ""
                    ),
                    "Inventory quantity set yes/no": (
                        "planned" if self.inventory_tracked else "no"
                    ),
                    "Inventory policy": self.inventory_policy,
                    "Taxable": "yes" if self.taxable else "no",
                    "Tags written": ", ".join(self.clean_tags(row)),
                    "Tags count": len(self.clean_tags(row)),
                    "Images found in Baserow": len(images),
                }
                if self.dry_run:
                    self.state.preview.append(planned)
                    resolution_record["Result"] = "dry_run_eligible"
                    self.cleanup_prepared_images(product_temp_dir)
                    product_temp_dir = None
                    continue
                stage = "shopify_product_create"
                self.current_partial_product_id = ""
                product, operational = self.create_product(
                    row, sku, handle, price, ai, images
                )
                persistence_entry = {
                    "Baserow Row ID": row["id"],
                    "Product Code": sku,
                    "Baserow Source URL": resolution_record["Source URL"],
                    "Source URL Type": "file_obj.url",
                    "Source Width": resolution_record["Source Width"],
                    "Source Height": resolution_record["Source Height"],
                    "Source Size": resolution_record["Source Size"],
                    "Source MIME": resolution_record["Source MIME"],
                    "Expected Shopify Width": operational.get(
                        "Output Width", resolution_record["Source Width"]
                    ),
                    "Expected Shopify Height": operational.get(
                        "Output Height", resolution_record["Source Height"]
                    ),
                    "Resolution Gate Enabled": resolution_record[
                        "Resolution Gate Enabled"
                    ],
                    "Resolution Result": resolution_record[
                        "Resolution Result"
                    ],
                    "Shopify Product ID": product["id"],
                    "Shopify Handle": product.get("handle", handle),
                    "Shopify Media ID": operational.get(
                        "Shopify Media ID",
                        operational.get("Shopify media ID", ""),
                    ),
                    "Online Store publication ID": operational[
                        "Online Store publication ID"
                    ],
                    "Inventory location ID": operational[
                        "Inventory location ID"
                    ],
                    "Primary Collection": operational["Primary Collection"],
                    "Primary Collection ID": operational["Primary Collection ID"],
                    "Primary Collection Assigned": operational[
                        "Primary Collection Assigned"
                    ],
                    "Secondary Collection": operational[
                        "Secondary Collection"
                    ],
                    "Secondary Collection ID": operational[
                        "Secondary Collection ID"
                    ],
                    "Secondary Collection Assigned": operational[
                        "Secondary Collection Assigned"
                    ],
                    "Secondary Collection Already Assigned": operational[
                        "Secondary Collection Already Assigned"
                    ],
                    "New Arrivals Collection ID": operational[
                        "New Arrivals Collection ID"
                    ],
                    "New Arrivals Assignment Attempted": operational[
                        "New Arrivals Assignment Attempted"
                    ],
                    "New Arrivals Assigned": operational[
                        "New Arrivals Assigned"
                    ],
                    "New Arrivals Already Assigned": operational[
                        "New Arrivals Already Assigned"
                    ],
                    "New Arrivals Error": operational["New Arrivals Error"],
                }
                stage = "persistence_pre_writeback"
                pre_writeback = self.verify_product_persistence(
                    persistence_entry,
                    "Pre-Writeback",
                    require_baserow_writeback=False,
                    record_result=False,
                )
                if pre_writeback["Result"] != "passed":
                    raise RuntimeError(
                        "pre_writeback_persistence_failed: "
                        + pre_writeback["Error"]
                    )
                stage = "baserow_status_update"
                self.update_baserow(
                    row["id"],
                    f"Shopify product created and verified: {product['id']}",
                    shopify_status="Created",
                    product_url=self.shopify_admin_product_url(product),
                )
                stage = "persistence_immediate"
                immediate = self.verify_product_persistence(
                    persistence_entry,
                    "Immediate",
                    require_baserow_writeback=True,
                )
                if immediate["Result"] != "passed":
                    self.restore_baserow_approved(
                        int(row["id"]),
                        "Shopify persistence verification failed immediately; "
                        "Generation Status restored to Approved.",
                    )
                    raise RuntimeError(
                        "immediate_persistence_failed: " + immediate["Error"]
                    )
                self.record_operational_success(operational)
                self.state.created += 1
                self.state.processed_success += 1
                self.state.baserow_updated += 1
                self.current_partial_product_id = ""
                self.cleanup_prepared_images(product_temp_dir)
                product_temp_dir = None
                canaries.append(persistence_entry)
                resolution_record.update(
                    {
                        "Shopify Product ID": product["id"],
                        "Shopify Handle": product.get("handle", handle),
                        "Shopify Media ID": operational.get(
                            "Shopify Media ID",
                            operational.get("Shopify media ID", ""),
                        ),
                        "Shopify Media Status": operational.get(
                            "Shopify Media Status", ""
                        ),
                        "Shopify Width": operational.get("Shopify Width", ""),
                        "Shopify Height": operational.get("Shopify Height", ""),
                        "Metadata Match": operational.get(
                            "Source Metadata Verified", ""
                        ),
                        "Source Metadata Match": operational.get(
                            "Source Metadata Verified", ""
                        ),
                        "Generated Front View First": operational.get(
                            "Generated Front View First", ""
                        ),
                        "Product ACTIVE": (
                            "yes"
                            if operational.get("Product status") == "ACTIVE"
                            else "no"
                        ),
                        "Online Store Published": operational.get(
                            "Online Store published yes/no", ""
                        ),
                        "Inventory Tracked": operational.get(
                            "Inventory tracked yes/no", ""
                        ),
                        "Inventory Quantity": operational.get(
                            "Inventory quantity requested", ""
                        ),
                        "Persistence Check Immediate": "yes",
                        "Baserow Writeback": "shopify-sync",
                        "Result": "created",
                        "Error": "",
                    }
                )
                self.state.report.append(
                    {
                        **planned,
                        **resolution_record,
                        **operational,
                        "Shopify Product ID": product["id"],
                        "Result": "created",
                    }
                )
                self.logger.info(
                    "Created %s product SKU=%s id=%s",
                    self.product_status,
                    sku,
                    product["id"],
                )
            except Exception as exc:
                self.state.failed += 1
                if stage == "collection_mapping":
                    self.state.collection_failed += 1
                error = f"{type(exc).__name__}: {exc}"
                lowered_error = error.casefold()
                new_arrivals_error = (
                    error if "new_arrivals" in lowered_error else ""
                )
                if "publication" in lowered_error or "online store" in lowered_error:
                    self.state.online_store_publish_failures += 1
                if "inventory" in lowered_error:
                    self.state.inventory_set_failures += 1
                if "location_not_found" in lowered_error:
                    self.state.location_not_found_failures += 1
                partial_product_id = self.current_partial_product_id
                if partial_product_id:
                    try:
                        self.set_partial_product_draft(partial_product_id)
                    except Exception as rollback_error:
                        error += (
                            "; partial_product_draft_rollback_failed: "
                            f"{type(rollback_error).__name__}: {rollback_error}"
                        )
                if resolution_record is not None:
                    resolution_record["Result"] = "failed"
                    resolution_record["Error"] = error
                self.state.failures.append(
                    {
                        **record,
                        "Stage": stage,
                        "Shopify Product ID": partial_product_id,
                        "New Arrivals Error": new_arrivals_error,
                        "Temporary File Path": (
                            str(product_temp_dir) if product_temp_dir else ""
                        ),
                        "Error": error,
                    }
                )
                if (
                    stage in {"image_url_validation", "image_resize"}
                    or "media" in error.casefold()
                ):
                    self.state.image_failures.append(
                        {
                            **record,
                            "Stage": stage,
                            "Temporary File Path": (
                                str(product_temp_dir)
                                if product_temp_dir
                                else ""
                            ),
                            "Error": error,
                        }
                    )
                if not self.dry_run:
                    self.write_failure_comment(int(row["id"]), error)
                if product_temp_dir and not self.keep_failed_temp_files:
                    self.cleanup_prepared_images(product_temp_dir)
                self.logger.exception("Row %s SKU=%s failed", row.get("id"), sku)
                self.current_partial_product_id = ""
        if not self.dry_run and canaries:
            sixty_seconds = int(
                os.getenv("MEN_ACCESSORIES_PERSISTENCE_WAIT_SECONDS", "60")
            )
            final_seconds = int(
                os.getenv("MEN_ACCESSORIES_FINAL_PERSISTENCE_WAIT_SECONDS", "120")
            )
            if sixty_seconds > 0:
                self.logger.info(
                    "Waiting %s seconds for canary persistence checkpoint",
                    sixty_seconds,
                )
                time.sleep(sixty_seconds)
            sixty_failed = False
            for entry in canaries:
                result = self.verify_product_persistence(
                    entry,
                    "60 Seconds",
                    require_baserow_writeback=True,
                )
                if result["Result"] != "passed":
                    sixty_failed = True
                    self.restore_baserow_approved(
                        int(entry["Baserow Row ID"]),
                        "Shopify 60-second persistence verification failed; "
                        "Generation Status restored to Approved.",
                    )
            if not sixty_failed and final_seconds > 0:
                self.logger.info(
                    "Waiting %s additional seconds for final persistence checkpoint",
                    final_seconds,
                )
                time.sleep(final_seconds)
            if not sixty_failed:
                for entry in canaries:
                    result = self.verify_product_persistence(
                        entry,
                        "Final",
                        require_baserow_writeback=True,
                    )
                    if result["Result"] != "passed":
                        self.state.failed += 1
                        self.restore_baserow_approved(
                            int(entry["Baserow Row ID"]),
                            "Shopify final persistence verification failed; "
                            "Generation Status restored to Approved.",
                        )
            else:
                self.state.failed += 1
        self.write_outputs()
        return 1 if self.state.failed else 0

    @staticmethod
    def write_csv(path: Path, rows: list[dict[str, Any]], fallback_fields: list[str]) -> None:
        source_fields = (
            [key for row in rows for key in row]
            if rows
            else fallback_fields
        )
        fields: list[str] = []
        seen_fields: set[str] = set()
        for key in source_fields:
            normalized = key.casefold()
            if normalized in seen_fields:
                continue
            seen_fields.add(normalized)
            fields.append(key)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def write_outputs(self) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        base = ["Row ID", "Product Code", "Source Title"]
        resolution_fields = [
            "Baserow Row ID",
            "Product Code",
            "Image Field",
            "Source URL",
            "Baserow Source URL",
            "Source URL Type",
            "Source Width",
            "Source Height",
            "Source Size",
            "Source Size Bytes",
            "Source MIME",
            "Source Filename",
            "Resize Enabled",
            "Target Width",
            "Target Height",
            "Resize Filter",
            "Output Filename",
            "Output Width",
            "Output Height",
            "Output Size Bytes",
            "Output MIME",
            "Resolution Gate Enabled",
            "Resolution Result",
            "Minimum Width Required",
            "Resolution Eligible",
            "Shopify Product ID",
            "Shopify Handle",
            "Shopify Media ID",
            "Shopify Media Status",
            "Shopify Width",
            "Shopify Height",
            "Target Dimensions Verified",
            "Source Metadata Match",
            "Metadata Match",
            "Generated Front View First",
            "Product ACTIVE",
            "Online Store Published",
            "Inventory Tracked",
            "Inventory Quantity",
            "Persistence Check Immediate",
            "Persistence Check 60 Seconds",
            "Persistence Check Final",
            "Baserow Writeback",
            "Result",
            "Error",
        ]
        collection_fields = [
            "Primary Collection",
            "Primary Collection Assigned",
            "Secondary Collection",
            "Secondary Collection ID",
            "Secondary Collection Assigned",
            "Secondary Collection Already Assigned",
            "New Arrivals Collection ID",
            "New Arrivals Assignment Attempted",
            "New Arrivals Assigned",
            "New Arrivals Already Assigned",
            "New Arrivals Error",
        ]
        self.write_csv(OUTPUT / "men_accessories_product_create_preview.csv", self.state.preview, base)
        self.write_csv(
            OUTPUT / "men_accessories_product_create_report.csv",
            self.state.report,
            base + collection_fields + resolution_fields,
        )
        self.write_csv(OUTPUT / "men_accessories_product_create_failed.csv", self.state.failures, base + ["Error"])
        self.write_csv(OUTPUT / "men_accessories_product_create_duplicates.csv", self.state.duplicate_rows, base + ["Duplicate reason"])
        self.write_csv(OUTPUT / "men_accessories_product_duplicates.csv", self.state.duplicate_rows, base + ["Duplicate reason"])
        self.write_csv(OUTPUT / "men_accessories_product_create_missing_fields.csv", self.state.missing_rows, base + ["Missing fields"])
        self.write_csv(OUTPUT / "men_accessories_product_create_ai_report.csv", self.state.ai_rows, base)
        self.write_csv(
            OUTPUT / "men_accessories_product_create_image_failures.csv",
            self.state.image_failures,
            collection_fields + resolution_fields,
        )
        self.write_csv(
            OUTPUT / "men_accessories_image_failures.csv",
            self.state.image_failures,
            collection_fields + resolution_fields,
        )
        self.write_csv(
            OUTPUT / "men_accessories_resolution_validation.csv",
            self.state.resolution_validation,
            collection_fields + resolution_fields,
        )
        self.write_csv(
            OUTPUT / "men_accessories_product_persistence_check.csv",
            self.state.persistence_checks,
            resolution_fields,
        )
        self.write_csv(
            OUTPUT / "men_accessories_theme_image_delivery_audit.csv",
            self.state.theme_audit,
            [
                "Theme access HTTP",
                "Theme ID",
                "Theme Name",
                "Theme Role",
                "Asset Key",
                "Contains 384px",
                "Contains 1152px",
                "Uses original image_url",
                "Required scope",
                "Result",
                "Error",
            ],
        )
        self.write_csv(
            OUTPUT / "men_accessories_product_create_image_quality.csv",
            self.state.image_quality,
            base + ["Image field", "Upload mode", "Error"],
        )
        self.write_csv(
            OUTPUT / "men_accessories_image_sync_report.csv",
            self.state.image_quality,
            base + ["Image field", "Upload mode", "Error"],
        )
        self.write_csv(
            OUTPUT / "men_accessories_product_create_compressed_images.csv",
            self.state.compressed_images,
            base + ["Image field", "Compression reason", "Error"],
        )
        summary = [
            "Men Accessories -> Shopify Product Create",
            f"Completed UTC: {datetime.now(timezone.utc).isoformat()}",
            f"Mode: {'DRY_RUN' if self.dry_run else 'LIVE'}",
            f"Table: Men Accessories ({self.table_id})",
            f"Rows loaded: {self.state.rows_loaded}",
            f"Approved rows: {self.state.approved_rows}",
            f"Eligible rows: {self.state.eligible}",
            f"Rows attempted: {self.state.attempted}",
            f"Preview rows: {len(self.state.preview)}",
            f"Products created: {self.state.created}",
            f"Existing products matched: {self.state.existing_products_matched}",
            "Duplicate product creation prevented: "
            f"{self.state.duplicate_products_prevented}",
            f"Duplicates skipped: {self.state.duplicates}",
            f"Duplicate source Product Codes: {self.state.source_duplicate_skus}",
            f"Existing Shopify SKUs: {self.state.existing_shopify_skus}",
            f"Existing Shopify handles: {self.state.existing_shopify_handles}",
            f"Existing Shopify exact titles: {self.state.existing_shopify_titles}",
            f"Rows with missing required fields: {self.state.missing_fields}",
            f"OpenRouter successes: {self.state.ai_success}",
            f"OpenRouter failures: {self.state.ai_failed}",
            f"Collection mapping successes: {self.state.collection_success}",
            f"Collection mapping failures: {self.state.collection_failed}",
            "Products added to Men's Ties: "
            f"{self.state.primary_collections_assigned}",
            "Products added to New Arrivals: "
            f"{self.state.new_arrivals_assigned}",
            "Products already in New Arrivals: "
            f"{self.state.new_arrivals_already_assigned}",
            "New Arrivals assignment failures: "
            f"{self.state.new_arrivals_assignment_failures}",
            f"Image URLs checked: {self.state.image_urls_checked}",
            f"Image URLs valid: {self.state.image_urls_valid}",
            f"Image URLs invalid: {self.state.image_urls_invalid}",
            f"Images uploaded without compression: "
            f"{sum(row.get('Compressed yes/no') == 'no' for row in self.state.image_quality)}",
            f"Images compressed over limit: {len(self.state.compressed_images)}",
            f"Images found in Baserow: {self.state.images_found}",
            f"Images already in Shopify: {self.state.images_already_present}",
            f"Missing images uploaded: {self.state.images_newly_uploaded}",
            f"Duplicate images prevented: {self.state.duplicate_media_prevented}",
            "Generated Front View resolution eligible: "
            f"{sum(row.get('Resolution Eligible') == 'yes' for row in self.state.resolution_validation)}",
            "Generated Front View resolution rejected: "
            f"{sum(row.get('Resolution Eligible') == 'no' for row in self.state.resolution_validation)}",
            f"Minimum Generated Front View width: {self.min_front_width}",
            f"Recommended Generated Front View width: {self.recommended_front_width}",
            "Resolution gate enabled: "
            f"{'yes' if self.require_min_front_width else 'no'}",
            f"Persistence check rows: {len(self.state.persistence_checks)}",
            "Persistence failures: "
            f"{sum(row.get('Result') != 'passed' for row in self.state.persistence_checks)}",
            f"Theme audit rows: {len(self.state.theme_audit)}",
            f"Products ACTIVE: {self.state.products_active}",
            "Products Online Store published: "
            f"{self.state.products_online_store_published}",
            "Online Store publish failures: "
            f"{self.state.online_store_publish_failures}",
            f"Inventory set to 1: {self.state.inventory_set_to_quantity}",
            f"Inventory set failures: {self.state.inventory_set_failures}",
            f"Location not found failures: {self.state.location_not_found_failures}",
            "Average tags per product: "
            f"{(self.state.total_tags_written / self.state.created) if self.state.created else 0:.2f}",
            "Products with more than 6 tags: "
            f"{self.state.products_with_more_than_max_tags}",
            f"Failed rows: {self.state.failed}",
            f"Baserow rows updated: {self.state.baserow_updated}",
            f"OpenRouter configured: {'yes' if self.openrouter else 'no'}",
        ]
        (OUTPUT / "final_men_accessories_product_create_report.txt").write_text(
            "\n".join(summary) + "\n", encoding="utf-8"
        )
        self.logger.info(" | ".join(summary[2:]))


def main() -> int:
    try:
        return MenAccessoriesCreator().run()
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

