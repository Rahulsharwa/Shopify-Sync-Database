from __future__ import annotations

import csv
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
from io import BytesIO
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError

from baserow_client import BaserowClient
from shopify_client import ShopifyClient, ShopifyError
from utils import env_bool, utc_now

ROOT = Path(__file__).resolve().parent
COTTON_SUITS_CONFIG = {
    "catalog_name": "Cotton Suits",
    "database_id": 419985,
    "table_id": 936059,
    "view_id": 1836484,
    "generation_status_field_id": 8132747,
    "shopify_notes_field_id": 8132750,
}
IMAGE_FIELDS = ["Generated Front View", "Front View", "Side View", "Back View", "Close Up View", "Close Up", "Grid View", "Full Suit Image", "Back Image", "Reference Images"]
FORCE_COTTON_IMAGE_FIELDS = ["Generated Front View", "Front View", "Full Suit Image", "Reference Images", "Back Image", "Side View", "Back View", "Close Up View", "Close Up", "Grid View"]
REFERENCE_FIELDS = ["Reference Images", "Full Suit Image", "Back Image"]
REQUIRED_FIELDS = ["Product Code", "Generation Status", "SHOPIFY Notes"]
OPTIONAL_FIELDS = ["Shopify Status", "Last Modified", "Error Notes", "warnings", "Comment"]
TABLE_NAMES = {
    936059: "Cotton Suits",
    936060: "Silk Suits",
}
SUIT_TABLE_IDS = {936059, 936060}
ALIASES = {
    "Product Code": ["product code", "sku", "product id"],
    "Generated Front View": ["generated front view", "generated front"],
    "Front View": ["front view", "front"],
    "Side View": ["side view", "generated side view", "side"],
    "Back View": ["back view", "generated back view", "back"],
    "Close Up View": ["close up view", "closeup view", "generated close up view", "close up"],
    "Close Up": ["close up", "closeup", "close-up"],
    "Grid View": ["grid view", "generated grid view", "grid"],
    "Full Suit Image": ["full suit image", "full suit", "front suit image"],
    "Back Image": ["back image", "suit back image"],
    "Reference Images": ["reference images", "reference image", "references"],
    "Video": ["video", "videos"],
    "Generation Status": ["generation status", "generate status"],
    "SHOPIFY Notes": ["shopify notes", "shopify note"],
}
REPORT_FIELDS = ["table_id", "table_name", "row_id", "product_code", "generation_status_before",
    "shopify_notes", "shopify_product_found", "missing_sku", "new_media_uploaded_count",
    "existing_generated_media_reused_count", "media_ready", "media_reordered", "main_image_set",
    "generation_status_updated_to_shopify_sync", "final_sync_status",
    "eligible", "skip_reason", "images_found", "reference_images_found", "video_available",
    "front_present", "side_present", "back_present", "close_up_present", "grid_present", "full_suit_present", "back_image_present",
    "shopify_product_title", "shopify_product_id", "shopify_action",
    "images_uploaded", "duplicates_skipped", "new_generation_status",
    "existing_media_count_before", "direct_url_upload_count", "staged_upload_count", "media_ids_created",
    "media_ready_count", "product_media_count_after", "reorder_success", "baserow_status_updated",
    "duplicate_product_code", "warning", "error"]
IMAGE_FAILURE_FIELDS = ["catalog_name", "table_id", "row_id", "product_code", "image_label", "image_url",
    "direct_upload_error", "staged_upload_error", "final_reason"]
ROW_DEBUG_FIELDS = ["Row ID", "Product Code", "Generation Status raw", "Generation Status parsed",
    "SHOPIFY Notes raw", "SHOPIFY Notes parsed", "Has Product Code", "Image fields found",
    "Usable image count", "Eligible yes/no", "Skip reason"]


def normalize_field_name(name: str) -> str:
    value = re.sub(r"[_\-]+", " ", str(name or "").strip().lower())
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def get_select_value(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, dict): return str(value.get("value") or "").strip()
    return str(value).strip()


def normalize_status(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, dict): value = value.get("value") or ""
    value = str(value).replace("\u00a0", " ")
    value = " ".join(value.split())
    return value.strip().lower()


@dataclass
class Stats:
    tables_scanned: int = 0
    tables_skipped: int = 0
    rows_loaded: int = 0
    eligible_rows: int = 0
    rows_skipped: int = 0
    products_found: int = 0
    products_not_found: int = 0
    duplicate_sku_matches: int = 0
    images_uploaded: int = 0
    duplicates_skipped: int = 0
    main_images_set: int = 0
    baserow_rows_updated: int = 0
    rows_failed: int = 0
    duplicate_product_codes_skipped: int = 0
    media_successes: int = 0
    status_warnings: int = 0
    rows_attempted: int = 0
    missing_shopify_skus: int = 0
    existing_media_reused: int = 0
    media_reordered_successfully: int = 0
    baserow_status_update_failures: int = 0
    final_successful_sync_rows: int = 0
    final_failed_rows: int = 0


class SuitImageSync:
    def __init__(self) -> None:
        input_env = ROOT / "Input" / ".env"
        load_dotenv(input_env if input_env.exists() else ROOT / ".env")
        self.validate_mapping_only = env_bool("VALIDATE_SUIT_MAPPING_ONLY", False)
        self.dry_run = env_bool("DRY_RUN", True)
        raw_max = os.getenv("MAX_PRODUCTS", "").strip()
        self.max_products = int(raw_max) if raw_max else None
        per_table_raw = os.getenv("MAX_PRODUCTS_PER_TABLE", "").strip()
        self.max_products_per_table = int(per_table_raw) if per_table_raw else None
        self.source_status = os.getenv("SOURCE_STATUS_NAME", "Approved")
        self.done_status = os.getenv("DONE_STATUS_NAME", "Shopify-sync")
        self.process_mode = "suit_catalogs" if env_bool("PROCESS_SUIT_CATALOGS", False) else os.getenv("PROCESS_TABLE_MODE", "suit_catalogs").strip().lower()
        self.force_cotton = env_bool("FORCE_COTTON_SUITS_SYNC", False)
        self.use_baserow_view = env_bool("USE_BASEROW_VIEW", False)
        view_raw = os.getenv("BASEROW_VIEW_ID", "").strip()
        self.baserow_view_id = int(view_raw) if view_raw else COTTON_SUITS_CONFIG["view_id"]
        self.single_catalog = env_bool("PROCESS_SINGLE_SUIT_CATALOG", False) or env_bool("PROCESS_SINGLE_CATALOG", False)
        self.catalog_name = os.getenv("CATALOG_NAME", "").strip()
        catalog_table_raw = os.getenv("CATALOG_TABLE_ID", "").strip()
        self.catalog_table_id = int(catalog_table_raw) if catalog_table_raw else None
        generation_field_raw = os.getenv("GENERATION_STATUS_FIELD_ID", "").strip()
        notes_field_raw = os.getenv("SHOPIFY_NOTES_FIELD_ID", "").strip()
        self.generation_status_field_id = int(generation_field_raw) if generation_field_raw else None
        self.shopify_notes_field_id = int(notes_field_raw) if notes_field_raw else None
        if self.force_cotton:
            self.single_catalog = True
            self.catalog_name = COTTON_SUITS_CONFIG["catalog_name"]
            self.catalog_table_id = COTTON_SUITS_CONFIG["table_id"]
            self.generation_status_field_id = COTTON_SUITS_CONFIG["generation_status_field_id"]
            self.shopify_notes_field_id = COTTON_SUITS_CONFIG["shopify_notes_field_id"]
            self.baserow_view_id = COTTON_SUITS_CONFIG["view_id"]
            self.max_products = None
        if self.single_catalog:
            if not all((self.catalog_name, self.catalog_table_id, self.generation_status_field_id, self.shopify_notes_field_id)):
                raise ValueError("Single suit catalog mode requires CATALOG_NAME, CATALOG_TABLE_ID, GENERATION_STATUS_FIELD_ID, and SHOPIFY_NOTES_FIELD_ID")
            if self.catalog_name.casefold() == "cotton suits" and not env_bool("ALLOW_SINGLE_CATALOG_MAX_PRODUCTS", False):
                self.max_products = None
            self._save_catalog_mapping()
        single_raw = os.getenv("PROCESS_SINGLE_TABLE_ID", "").strip()
        self.single_table_id = int(single_raw) if single_raw else None
        self.single_table_name = os.getenv("PROCESS_SINGLE_TABLE_NAME", "").strip()
        self.auto_discover = env_bool("AUTO_DISCOVER_TABLES", False)
        self.include_ids, self.exclude_ids = self._select_ids()
        self.base_url = self.required("BASEROW_API_BASE").rstrip("/")
        self.baserow = BaserowClient(self.base_url, self.required("BASEROW_TOKEN"), "unused")
        token = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "").strip()
        if not token and not self.dry_run and not self.validate_mapping_only: raise ValueError("Missing required environment variable: SHOPIFY_ADMIN_ACCESS_TOKEN")
        self.shopify = (ShopifyClient(self.required("SHOPIFY_STORE_DOMAIN"), token, self.required("SHOPIFY_API_VERSION"))
            if token and not self.validate_mapping_only else None)
        self.stats, self.records, self.image_failures, self.row_debug = Stats(), [], [], []
        self.processed_product_codes: set[str] = set()
        self.table_summaries: list[dict[str, Any]] = []
        self.log = self._logger()
        self._log_startup()

    @staticmethod
    def required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value: raise ValueError(f"Missing required environment variable: {name}")
        return value

    @staticmethod
    def _ids(name: str) -> set[int]:
        return {int(value.strip()) for value in os.getenv(name, "").split(",") if value.strip()}

    def _select_ids(self) -> tuple[set[int], set[int]]:
        if self.single_catalog: return {int(self.catalog_table_id)}, set()
        if self.single_table_id: return {self.single_table_id}, set()
        if self.process_mode in {"suit_catalogs", "category_tables"}:
            configured = self._ids("INCLUDE_SUIT_TABLE_IDS")
            included = configured or set(SUIT_TABLE_IDS)
            excluded = self._ids("EXCLUDE_SUIT_TABLE_IDS")
            included -= excluded
            return included, excluded
        raise ValueError("PROCESS_TABLE_MODE must be suit_catalogs for suit_image_sync.py")

    def _log_startup(self) -> None:
        details = {"BASEROW_DATABASE_ID": os.getenv("BASEROW_DATABASE_ID", ""), "PROCESS_TABLE_MODE": self.process_mode,
            "FORCE_COTTON_SUITS_SYNC": self.force_cotton, "DRY_RUN": self.dry_run,
            "USE_BASEROW_VIEW": self.use_baserow_view, "BASEROW_VIEW_ID": self.baserow_view_id if self.force_cotton else "",
            "AUTO_DISCOVER_TABLES": self.auto_discover, "INCLUDE_TABLE_IDS": sorted(self.include_ids),
            "EXCLUDE_TABLE_IDS": sorted(self.exclude_ids), "MAX_PRODUCTS active": self.max_products is not None,
            "MAX_PRODUCTS value": self.max_products, "Total selected tables": len(self.include_ids),
            "Selected tables": [f"{i}:{TABLE_NAMES.get(i, f'Table {i}')}" for i in sorted(self.include_ids)]}
        for key, value in details.items(): self.log.info("%s: %s", key, value)
        if self.single_table_id:
            self.log.info("Single table test mode: %s (%s)", self.single_table_name or TABLE_NAMES.get(self.single_table_id, "Table"), self.single_table_id)
        if self.single_catalog:
            self.log.info("Single catalog mode: %s (%s)", self.catalog_name, self.catalog_table_id)
            self.log.info("Catalog: %s", self.catalog_name)
            self.log.info("Table ID: %s", self.catalog_table_id)
            if self.force_cotton:
                self.log.info("Database ID: %s", COTTON_SUITS_CONFIG["database_id"])
                self.log.info("View ID: %s", COTTON_SUITS_CONFIG["view_id"])
            self.log.info("Generation Status field ID: %s", self.generation_status_field_id)
            self.log.info("SHOPIFY Notes field ID: %s", self.shopify_notes_field_id)
        if self.max_products is not None: self.log.warning("MAX_PRODUCTS is active; full catalog will not run")

    @staticmethod
    def _logger() -> logging.Logger:
        (ROOT / "logs").mkdir(exist_ok=True)
        logger = logging.getLogger("suit_image_sync"); logger.setLevel(logging.INFO); logger.handlers.clear()
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        single_cotton = env_bool("FORCE_COTTON_SUITS_SYNC", False) or (env_bool("PROCESS_SINGLE_SUIT_CATALOG", False) and os.getenv("CATALOG_NAME", "").strip().casefold() == "cotton suits")
        log_name = "cotton_suits_image_sync.log" if single_cotton else "suit_image_sync.log"
        for handler in (logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / log_name, encoding="utf-8")):
            handler.setFormatter(fmt); logger.addHandler(handler)
        return logger

    def get_tables(self) -> list[dict[str, Any]]:
        if not self.auto_discover:
            return [{"id": table_id, "name": self.catalog_name if self.single_catalog else
                (self.single_table_name if self.single_table_id == table_id and self.single_table_name else TABLE_NAMES.get(table_id, f"Table {table_id}"))}
                for table_id in sorted(self.include_ids)]
        url = f"{self.base_url}/api/database/tables/database/{self.required('BASEROW_DATABASE_ID')}/"
        response = self.baserow.session.get(url, timeout=30)
        if response.ok:
            tables = response.json()
            for table in tables: self.log.info("Discovered table %s (%s): %s", table["name"], table["id"], "included" if int(table["id"]) in self.include_ids else "excluded")
            return [table for table in tables if int(table["id"]) in self.include_ids and int(table["id"]) not in self.exclude_ids]
        if response.status_code in {401, 403}:
            self.log.warning("Database table listing unavailable; using configured include table IDs")
            return [{"id": table_id, "name": TABLE_NAMES.get(table_id, f"Table {table_id}")} for table_id in sorted(self.include_ids)]
        response.raise_for_status()
        return []

    def get_table_field_map(self, table_id: int) -> dict[str, dict[str, Any]]:
        response = self.baserow.session.get(f"{self.base_url}/api/database/fields/table/{table_id}/", timeout=30)
        response.raise_for_status()
        return {normalize_field_name(field["name"]): field for field in response.json()}

    def resolve_required_fields(self, field_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        resolved: dict[str, dict[str, Any]] = {}
        for canonical in REQUIRED_FIELDS + IMAGE_FIELDS + REFERENCE_FIELDS + ["Video"] + OPTIONAL_FIELDS:
            candidates = ALIASES.get(canonical, [normalize_field_name(canonical)])
            match = next((field_map.get(normalize_field_name(alias)) for alias in candidates if field_map.get(normalize_field_name(alias))), None)
            if match: resolved[canonical] = match
        missing = [name for name in REQUIRED_FIELDS if name not in resolved]
        if missing: raise ValueError("missing fields: " + ", ".join(missing))
        return resolved

    def resolve_fields(self, field_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not self.single_catalog:
            return self.resolve_required_fields(field_map)
        by_id = {int(field["id"]): field for field in field_map.values()}
        status = by_id.get(int(self.generation_status_field_id))
        notes = by_id.get(int(self.shopify_notes_field_id))
        if not status or not notes:
            raise ValueError("configured Generation Status or SHOPIFY Notes field ID is absent from table metadata")
        resolved: dict[str, dict[str, Any]] = {"Generation Status": status, "SHOPIFY Notes": notes}
        for canonical in ["Product Code", *IMAGE_FIELDS, *REFERENCE_FIELDS, "Video", *OPTIONAL_FIELDS]:
            candidates = ALIASES.get(canonical, [normalize_field_name(canonical)])
            match = next((field_map.get(normalize_field_name(alias)) for alias in candidates if field_map.get(normalize_field_name(alias))), None)
            if match: resolved[canonical] = match
        missing = [name for name in ("Product Code",) if name not in resolved]
        if missing: raise ValueError("missing fields: " + ", ".join(missing))
        return resolved

    def _save_catalog_mapping(self) -> None:
        path = ROOT / "config" / "suit_catalog_field_map.json"
        path.parent.mkdir(exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try: data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc: raise ValueError(f"Invalid catalog mapping file: {exc}") from exc
        data[self.catalog_name] = {"table_id": self.catalog_table_id,
            "generation_status_field_id": self.generation_status_field_id,
            "shopify_notes_field_id": self.shopify_notes_field_id}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def validate_expected_field_ids(self, table_id: int, fields: dict[str, dict[str, Any]]) -> None:
        if not self.force_cotton:
            return
        problems = []
        status = fields.get("Generation Status")
        notes = fields.get("SHOPIFY Notes")
        if table_id != 936059: problems.append(f"Expected Cotton Suits table 936059, got {table_id}")
        if not status or int(status.get("id", 0)) != 8132747: problems.append(f"Generation Status field ID mismatch/absent: {status}")
        if not notes or int(notes.get("id", 0)) != 8132750: problems.append(f"SHOPIFY Notes field ID mismatch/absent: {notes}")
        if status and normalize_field_name(status.get("name")) != "generation status": problems.append(f"Field 8132747 name mismatch: {status}")
        if notes and normalize_field_name(notes.get("name")) != "shopify notes": problems.append(f"Field 8132750 name mismatch: {notes}")
        options = {str(item.get("value", "")).strip().casefold() for item in (status or {}).get("select_options", [])}
        if "approved" not in options: problems.append(f"Generation Status missing Approved option: {status}")
        if "shopify-sync" not in options: problems.append(f"Generation Status missing Shopify-sync option: {status}")
        if problems:
            for problem in problems: self.log.error("Cotton validation failed: %s", problem)
            raise ValueError("Cotton Suits field validation failed: " + " | ".join(problems))

    def iter_rows(self, table_id: int):
        url = f"{self.base_url}/api/database/rows/table/{table_id}/"; params = {"user_field_names": "true", "size": 100}
        if self.force_cotton and self.use_baserow_view:
            params["view_id"] = self.baserow_view_id
        while url:
            response = self.baserow.session.get(url, params=params, timeout=30); response.raise_for_status(); body = response.json()
            yield from body.get("results", []); url = body.get("next"); params = None

    @staticmethod
    def field_value(row: dict[str, Any], fields: dict[str, dict[str, Any]], canonical: str) -> Any:
        return row.get(fields[canonical]["name"])

    def extract_generated_image_urls(self, row: dict[str, Any], fields: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
        images = []
        close_up_view_added = False
        labels = FORCE_COTTON_IMAGE_FIELDS if self.force_cotton else IMAGE_FIELDS
        for label in labels:
            if label == "Close Up" and close_up_view_added:
                continue
            if label not in fields: continue
            files = self.field_value(row, fields, label) or []
            if isinstance(files, dict): files = [files]
            found = [item for item in files if isinstance(item, dict) and item.get("url")]
            for item in (found if label == "Reference Images" and self.force_cotton else found[:1]):
                images.append({"label": label, "url": item["url"]})
                if label == "Close Up View":
                    close_up_view_added = True
        return images

    def extract_reference_image_urls(self, row: dict[str, Any], fields: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
        labels = ["Reference Images"] if "Reference Images" in fields and self.field_value(row, fields, "Reference Images") else ["Full Suit Image", "Back Image"]
        images = []
        for label in labels:
            if label not in fields: continue
            files = self.field_value(row, fields, label) or []
            if isinstance(files, dict): files = [files]
            for item in files:
                if isinstance(item, dict) and item.get("url"):
                    images.append({"label": label, "url": item["url"]})
        return images

    def row_skip_reason(self, row: dict[str, Any], fields: dict[str, dict[str, Any]]) -> str:
        if normalize_status(self.field_value(row, fields, "Generation Status")) != "approved": return "Generation Status not Approved"
        if normalize_status(row.get(fields["SHOPIFY Notes"]["name"])) != "approved": return "SHOPIFY Notes not Approved"
        if not str(self.field_value(row, fields, "Product Code") or "").strip(): return "Product Code missing"
        if not self.extract_generated_image_urls(row, fields): return "No usable image"
        return ""

    def append_row_debug(self, row: dict[str, Any], fields: dict[str, dict[str, Any]], reason: str) -> None:
        def has_file(label: str) -> str:
            if label not in fields: return "no"
            files = self.field_value(row, fields, label) or []
            if isinstance(files, dict): files = [files]
            return "yes" if any(isinstance(item, dict) and item.get("url") for item in files) else "no"
        found_fields = []
        usable_count = 0
        for label in FORCE_COTTON_IMAGE_FIELDS:
            if label in fields:
                files = self.field_value(row, fields, label) or []
                if isinstance(files, dict): files = [files]
                count = sum(1 for item in files if isinstance(item, dict) and item.get("url"))
                if count:
                    found_fields.append(label)
                    usable_count += count
        status_raw = self.field_value(row, fields, "Generation Status")
        notes_raw = self.field_value(row, fields, "SHOPIFY Notes")
        code = str(self.field_value(row, fields, "Product Code") or "").strip()
        self.row_debug.append({"Row ID": row.get("id"), "Product Code": code,
            "Generation Status raw": json.dumps(status_raw, ensure_ascii=False) if isinstance(status_raw, (dict, list)) else str(status_raw or ""),
            "Generation Status parsed": normalize_status(status_raw),
            "SHOPIFY Notes raw": json.dumps(notes_raw, ensure_ascii=False) if isinstance(notes_raw, (dict, list)) else str(notes_raw or ""),
            "SHOPIFY Notes parsed": normalize_status(notes_raw),
            "Has Product Code": "yes" if code else "no",
            "Image fields found": ", ".join(found_fields),
            "Usable image count": usable_count,
            "Eligible yes/no": "no" if reason else "yes", "Skip reason": reason})

    def is_row_approved(self, row: dict[str, Any], fields: dict[str, dict[str, Any]]) -> bool:
        return not self.row_skip_reason(row, fields)

    @staticmethod
    def validate_url(url: str) -> tuple[bool, str]:
        try:
            response = requests.get(url, stream=True, timeout=20, allow_redirects=True, headers={"User-Agent": "suit-image-sync/1.0"})
            ok = response.status_code == 200 and response.headers.get("content-type", "").lower().startswith("image/")
            reason = "" if ok else f"invalid image response ({response.status_code}, {response.headers.get('content-type', '')})"
            response.close(); return ok, reason
        except requests.RequestException as exc:
            return False, f"image validation failed: {type(exc).__name__}"

    def find_shopify_product_by_sku(self, sku: str) -> tuple[dict[str, Any] | None, str]:
        if not self.shopify: return None, ""
        query = """query($q:String!){productVariants(first:20,query:$q){nodes{sku product{id title media(first:100){nodes{id alt ... on MediaImage{image{url}}}}}}}}"""
        sku = str(sku or "").strip()
        candidates = [sku]
        compact = re.sub(r"\s+", "", sku)
        if compact and compact not in candidates: candidates.append(compact)
        upper = sku.upper()
        if upper and upper not in candidates: candidates.append(upper)
        nodes = []
        products: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            for search_query in (f"sku:{candidate}", f"sku:'{candidate}'"):
                self.log.info("Shopify SKU search GraphQL query: %s variables=%s", query, {"q": search_query})
                found_nodes = self.shopify.graphql(query, {"q": search_query})["productVariants"]["nodes"]
                self.log.info("Shopify SKU search for %s returned variant_nodes=%s", search_query, len(found_nodes))
                nodes.extend(found_nodes)
                for node in found_nodes:
                    if str(node.get("sku") or "").strip().casefold() == candidate.casefold():
                        products[node["product"]["id"]] = node["product"]
                if products:
                    break
            if products:
                break
        self.log.info("Shopify SKU exact match for %s exact_product_count=%s", sku, len(products))
        if not products: return None, "Shopify product not found by SKU"
        if len(products) > 1: return None, "Multiple Shopify products found for SKU"
        return next(iter(products.values())), ""

    def create_product_media_from_url(self, product_gid: str, image_url: str, alt_text: str) -> dict[str, Any]:
        assert self.shopify
        mutation = "mutation($id:ID!,$media:[CreateMediaInput!]!){productCreateMedia(productId:$id,media:$media){media{id alt status} mediaUserErrors{field message}}}"
        try:
            result = self.shopify.graphql(mutation, {"id": product_gid, "media": [{"mediaContentType": "IMAGE", "originalSource": image_url, "alt": alt_text}]})["productCreateMedia"]
            errors = [e["message"] for e in result.get("mediaUserErrors", [])]
            media = result.get("media") or []
            return {"success": bool(media) and not errors, "media_id": media[0]["id"] if media else None, "errors": errors, "raw": result}
        except Exception as exc:
            return {"success": False, "media_id": None, "errors": [f"{type(exc).__name__}: {exc}"], "raw": {}}

    def create_product_media_via_staged_upload(self, product_gid: str, image_url: str, alt_text: str) -> dict[str, Any]:
        assert self.shopify
        try:
            download = requests.get(image_url, timeout=60, allow_redirects=True, headers={"User-Agent": "suit-image-sync/1.0"})
            download.raise_for_status()
            mime = download.headers.get("content-type", "").split(";", 1)[0]
            if not mime.startswith("image/"): raise ValueError(f"download content type is {mime or 'unknown'}")
            filename = Path(urlsplit(image_url).path).name or "generated-image.jpg"
            if not Path(filename).suffix: filename += mimetypes.guess_extension(mime) or ".jpg"
            mutation = """mutation($input:[StagedUploadInput!]!){stagedUploadsCreate(input:$input){stagedTargets{url resourceUrl parameters{name value}} userErrors{field message}}}"""
            staged = self.shopify.graphql(mutation, {"input": [{"resource": "IMAGE", "filename": filename,
                "mimeType": mime, "httpMethod": "POST", "fileSize": str(len(download.content))}]})["stagedUploadsCreate"]
            errors = [e["message"] for e in staged.get("userErrors", [])]
            if errors or not staged.get("stagedTargets"):
                return {"success": False, "media_id": None, "errors": errors or ["No staged upload target returned"], "raw": staged}
            target = staged["stagedTargets"][0]
            form = {p["name"]: p["value"] for p in target["parameters"]}
            with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix) as handle:
                handle.write(download.content); handle.flush(); handle.seek(0)
                upload = requests.post(target["url"], data=form, files={"file": (filename, handle, mime)}, timeout=120)
                upload.raise_for_status()
            attached = self.create_product_media_from_url(product_gid, target["resourceUrl"], alt_text)
            attached["raw"] = {"staged": staged, "attach": attached.get("raw", {})}
            return attached
        except Exception as exc:
            return {"success": False, "media_id": None, "errors": [f"{type(exc).__name__}: {exc}"], "raw": {}}

    def create_product_media_via_reencoded_staged_upload(self, product_gid: str, image_url: str, alt_text: str) -> dict[str, Any]:
        assert self.shopify
        try:
            download = requests.get(image_url, timeout=60, allow_redirects=True, headers={"User-Agent": "suit-image-sync/1.0"})
            download.raise_for_status()
            source_mime = download.headers.get("content-type", "").split(";", 1)[0].lower()
            if source_mime and not source_mime.startswith("image/"):
                raise ValueError(f"download content type is {source_mime}")
            source_size = len(download.content)
            if source_size <= 0:
                raise ValueError("downloaded image is empty")
            try:
                with Image.open(BytesIO(download.content)) as image:
                    image.verify()
                with Image.open(BytesIO(download.content)) as image:
                    width, height = image.size
                    if width <= 0 or height <= 0:
                        raise ValueError(f"invalid image dimensions {width}x{height}")
                    image.load()
                    max_dimension = 4472
                    if max(width, height) > max_dimension:
                        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                        background = Image.new("RGB", image.size, (255, 255, 255))
                        alpha = image.convert("RGBA").getchannel("A")
                        background.paste(image.convert("RGBA"), mask=alpha)
                        image = background
                    else:
                        image = image.convert("RGB")
                    buffer = BytesIO()
                    image.save(buffer, format="JPEG", quality=92, optimize=True, progressive=True)
                    content = buffer.getvalue()
            except UnidentifiedImageError as exc:
                raise ValueError("downloaded file is not a readable image") from exc
            filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(urlsplit(image_url).path).stem or "reencoded-suit-image") + ".jpg"
            return self.create_product_media_from_bytes(product_gid, content, filename, "image/jpeg", alt_text,
                {"source_mime": source_mime, "source_size": source_size, "reencoded_size": len(content)})
        except Exception as exc:
            return {"success": False, "media_id": None, "errors": [f"{type(exc).__name__}: {exc}"], "raw": {}}

    def create_product_media_from_bytes(self, product_gid: str, content: bytes, filename: str, mime: str, alt_text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.shopify
        try:
            mutation = """mutation($input:[StagedUploadInput!]!){stagedUploadsCreate(input:$input){stagedTargets{url resourceUrl parameters{name value}} userErrors{field message}}}"""
            staged = self.shopify.graphql(mutation, {"input": [{"resource": "IMAGE", "filename": filename,
                "mimeType": mime, "httpMethod": "POST", "fileSize": str(len(content))}]})["stagedUploadsCreate"]
            errors = [e["message"] for e in staged.get("userErrors", [])]
            if errors or not staged.get("stagedTargets"):
                return {"success": False, "media_id": None, "errors": errors or ["No staged upload target returned"], "raw": staged}
            target = staged["stagedTargets"][0]
            form = {p["name"]: p["value"] for p in target["parameters"]}
            with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix) as handle:
                handle.write(content); handle.flush(); handle.seek(0)
                upload = requests.post(target["url"], data=form, files={"file": (filename, handle, mime)}, timeout=120)
                upload.raise_for_status()
            attached = self.create_product_media_from_url(product_gid, target["resourceUrl"], alt_text)
            attached["raw"] = {"staged": staged, "attach": attached.get("raw", {}), "metadata": metadata or {}}
            return attached
        except Exception as exc:
            return {"success": False, "media_id": None, "errors": [f"{type(exc).__name__}: {exc}"], "raw": {}}

    def get_product_media(self, product_gid: str) -> list[dict[str, Any]]:
        assert self.shopify
        query = """query($id:ID!){product(id:$id){media(first:250){nodes{id alt status mediaContentType ... on MediaImage{image{url}}}}}}"""
        product = self.shopify.graphql(query, {"id": product_gid}).get("product")
        if not product: return []
        return [{**node, "position": index} for index, node in enumerate(product["media"]["nodes"])]

    def wait_for_media_ready(self, product_gid: str, media_ids: list[str], timeout_seconds: int = 120) -> dict[str, Any]:
        wanted = set(media_ids); deadline = time.monotonic() + timeout_seconds; statuses: dict[str, str] = {}
        while wanted and time.monotonic() < deadline:
            media = self.get_product_media(product_gid)
            statuses = {item["id"]: item.get("status", "UNKNOWN") for item in media if item["id"] in wanted}
            self.log.info("Media statuses product %s: %s", product_gid, statuses)
            if statuses and all(status == "READY" for status in statuses.values()) and set(statuses) == wanted:
                return {"ready": True, "ready_ids": list(wanted), "failed_ids": [], "statuses": statuses}
            failed = [mid for mid, status in statuses.items() if status == "FAILED"]
            if failed: return {"ready": False, "ready_ids": [m for m, s in statuses.items() if s == "READY"], "failed_ids": failed, "statuses": statuses}
            time.sleep(2)
        return {"ready": False, "ready_ids": [m for m, s in statuses.items() if s == "READY"],
                "failed_ids": [m for m in wanted if statuses.get(m) != "READY"], "statuses": statuses}

    def reorder_product_media(self, product_gid: str, generated_media_ids: list[str]) -> dict[str, Any]:
        assert self.shopify
        mutation = "mutation($id:ID!,$moves:[MoveInput!]!){productReorderMedia(id:$id,moves:$moves){job{id} mediaUserErrors{field message}}}"
        moves = [{"id": media_id, "newPosition": str(index)} for index, media_id in enumerate(generated_media_ids)]
        try:
            result = self.shopify.graphql(mutation, {"id": product_gid, "moves": moves})["productReorderMedia"]
            errors = [e["message"] for e in result.get("mediaUserErrors", [])]
            if errors: return {"success": False, "errors": errors, "raw": result}
            for _ in range(120):
                current = [m["id"] for m in self.get_product_media(product_gid)]
                if current[:len(generated_media_ids)] == generated_media_ids:
                    return {"success": True, "errors": [], "raw": result}
                time.sleep(1)
            return {"success": False, "errors": ["Media reorder verification timed out"], "raw": result}
        except Exception as exc:
            return {"success": False, "errors": [f"{type(exc).__name__}: {exc}"], "raw": {}}

    @staticmethod
    def media_alt_text(code: str, label: str) -> str:
        alt_labels = {
            "Generated Front View": "Generated Front View",
            "Side View": "Generated Side View",
            "Back View": "Generated Back View",
            "Close Up View": "Generated Close Up View",
            "Close Up": "Generated Close Up View",
            "Grid View": "Grid View",
            "Full Suit Image": "Full Suit Image",
            "Back Image": "Back Image",
        }
        return f"{code} {alt_labels.get(label, label)}"

    def sync_shopify_generated_images(self, product: dict[str, Any], images: list[dict[str, str]], code: str) -> dict[str, Any]:
        assert self.shopify
        existing = self.get_product_media(product["id"])
        ready_existing = [node for node in existing if node.get("status") == "READY"]
        existing_urls = {(node.get("image") or {}).get("url", "").split("?", 1)[0] for node in ready_existing}
        existing_alts = {str(node.get("alt") or "").casefold() for node in ready_existing}
        existing_by_alt = {str(node.get("alt") or "").casefold(): node["id"] for node in ready_existing}
        pending, duplicates, ordered_ids = [], 0, []
        for image in images:
            alt = self.media_alt_text(code, image["label"])
            if image["url"].split("?", 1)[0] in existing_urls or alt.casefold() in existing_alts:
                duplicates += 1
                media_id = existing_by_alt.get(alt.casefold())
                if media_id: ordered_ids.append(media_id)
                continue
            pending.append({"originalSource": image["url"], "alt": alt, "mediaContentType": "IMAGE", "label": image["label"]})
        uploaded, warnings, front_id, direct_count, staged_count, created_ids, ready_count = 0, [], None, 0, 0, [], 0
        image_failures: list[dict[str, str]] = []
        for item in pending:
            result = self.create_product_media_from_url(product["id"], item["originalSource"], item["alt"])
            direct_errors = list(result["errors"])
            method = "direct URL"
            if result["success"]:
                status = self.wait_for_media_ready(product["id"], [result["media_id"]])
                if not status["ready"]:
                    direct_errors.append(f"media not READY: {status['statuses']}")
                    warnings.append(f"{item['label']} direct media not READY: {status['statuses']}"); result["success"] = False
            if not result["success"]:
                self.log.warning("Direct URL upload failed product %s %s: %s", product["id"], item["label"], direct_errors)
                result = self.create_product_media_via_staged_upload(product["id"], item["originalSource"], item["alt"]); method = "staged upload"
                staged_errors = list(result["errors"])
                if not result["success"]:
                    self.log.warning("Staged upload failed product %s %s: %s", product["id"], item["label"], result["errors"])
                    result = self.create_product_media_via_reencoded_staged_upload(product["id"], item["originalSource"], item["alt"]); method = "reencoded staged upload"
                    staged_errors.extend(result["errors"])
                    if not result["success"]:
                        self.log.warning("Re-encoded staged upload failed product %s %s: %s", product["id"], item["label"], result["errors"])
                if result["success"]:
                    status = self.wait_for_media_ready(product["id"], [result["media_id"]])
                    if not status["ready"]:
                        staged_errors.append(f"media not READY: {status['statuses']}")
                        warnings.append(f"{item['label']} {method} media not READY: {status['statuses']}"); result["success"] = False
            if not result["success"]:
                final_reason = "; ".join(staged_errors or result["errors"] or ["Shopify media processing failed"])
                image_failures.append({"image_label": item["label"], "image_url": item["originalSource"],
                    "direct_upload_error": "; ".join(direct_errors), "staged_upload_error": "; ".join(staged_errors),
                    "final_reason": final_reason})
                warnings.append(f"{item['label']}: {final_reason}"); continue
            uploaded += 1; ready_count += 1; created_ids.append(result["media_id"]); ordered_ids.append(result["media_id"])
            direct_count += method == "direct URL"; staged_count += method in {"staged upload", "reencoded staged upload"}
            self.log.info("Media created product %s label %s method %s media_id %s", product["id"], item["label"], method, result["media_id"])
            if item["label"] in {"Generated Front View", "Full Suit Image"} and not front_id:
                front_id = result["media_id"]
        # Preserve exact source order, including existing duplicate generated media.
        alt_order = [self.media_alt_text(code, image["label"]).casefold() for image in images]
        refreshed = self.get_product_media(product["id"])
        by_alt = {str(m.get("alt") or "").casefold(): m["id"] for m in refreshed if m.get("status") == "READY"}
        preferred_alts = [self.media_alt_text(code, "Generated Front View").casefold(), self.media_alt_text(code, "Full Suit Image").casefold()]
        main_alt = next((alt for alt in preferred_alts if alt in by_alt), None)
        front_id = by_alt.get(main_alt) if main_alt else front_id
        ordered_alts = ([main_alt] if main_alt else []) + [alt for alt in alt_order if alt in by_alt and alt != main_alt]
        ordered_ids = [by_alt[alt] for alt in ordered_alts]
        reorder = self.reorder_product_media(product["id"], ordered_ids) if ordered_ids else {"success": False, "errors": ["No generated media IDs available"]}
        warnings.extend(f"Media reorder: {error}" for error in reorder.get("errors", []))
        after = self.get_product_media(product["id"])
        attached_ids = {m["id"] for m in after}; front_attached = bool(front_id and front_id in attached_ids)
        main_set = bool(front_id and after and after[0]["id"] == front_id)
        return {"uploaded_count": uploaded, "duplicate_skipped_count": duplicates, "main_image_set": main_set,
            "reorder_success": reorder["success"], "warnings": warnings, "direct_count": direct_count,
            "staged_count": staged_count, "created_ids": created_ids, "ready_count": ready_count,
            "media_count_before": len(existing), "media_count_after": len(after), "front_attached": front_attached,
            "generated_attached_count": sum(media_id in attached_ids for media_id in ordered_ids),
            "existing_reused_count": duplicates, "media_ready": bool(ordered_ids and all(mid in attached_ids for mid in ordered_ids)),
            "image_failures": image_failures}

    def ensure_done_status_option(self, field: dict[str, Any]) -> tuple[int | None, str]:
        existing = next((item for item in field.get("select_options", []) if item.get("value", "").casefold() == self.done_status.casefold()), None)
        if existing: return int(existing["id"]), ""
        options = [{"id": item["id"], "value": item["value"], "color": item.get("color", "light-gray")} for item in field.get("select_options", [])]
        options.append({"value": self.done_status, "color": "light-green"})
        url = f"{self.base_url}/api/database/fields/{field['id']}/"
        response = self.baserow.session.patch(url, json={"select_options": options}, timeout=30)
        if not response.ok: return None, f"Could not create Shopify-sync option (HTTP {response.status_code})"
        updated = response.json()
        created = next((item for item in updated.get("select_options", []) if item.get("value", "").casefold() == self.done_status.casefold()), None)
        return (int(created["id"]), "") if created else (None, "Shopify-sync option creation returned no matching option")

    def update_baserow_row_status(self, table_id: int, row_id: int, fields: dict[str, dict[str, Any]], success: bool, warnings: str = "", error: str = "") -> dict[str, Any]:
        status_updated, status_warning = False, ""
        if success:
            status_field = fields["Generation Status"]
            option = next((item for item in status_field.get("select_options", []) if item.get("value", "").casefold() == self.done_status.casefold()), None)
            url = f"{self.base_url}/api/database/rows/table/{table_id}/{row_id}/"
            response = self.baserow.session.patch(url, params={"user_field_names": "true"},
                json={status_field["name"]: self.done_status}, timeout=30)
            if not response.ok and response.status_code in {400, 422} and option:
                response = self.baserow.session.patch(url, params={"user_field_names": "true"},
                    json={status_field["name"]: option["id"]}, timeout=30)
            if response.ok: status_updated = True
            else:
                status_warning = "Shopify media uploaded, but Generation Status is missing Shopify-sync option."
        values: dict[str, Any] = {}
        optional = {"Error Notes": error if not success else None,
                    "warnings": (warnings or error) if not success else None,
                    "Comment": error if not success else None}
        for canonical, value in optional.items():
            if canonical not in fields or value is None: continue
            field = fields[canonical]
            if field.get("type") in {"last_modified", "created_on", "last_modified_by", "created_by"}: continue
            if field.get("type") == "single_select":
                option = next((item for item in field.get("select_options", []) if item.get("value", "").casefold() == str(value).casefold()), None)
                if not option: continue
                value = option["id"]
            values[field["name"]] = value[:1000] if isinstance(value, str) else value
        if status_warning:
            warning_field = next((name for name in ("Comment", "Error Notes", "warnings") if name in fields), None)
            if warning_field:
                values[fields[warning_field]["name"]] = status_warning
        if not values: return {"status_updated": status_updated, "warning": status_warning, "fallback_updated": False}
        url = f"{self.base_url}/api/database/rows/table/{table_id}/{row_id}/"
        response = self.baserow.session.patch(url, params={"user_field_names": "true"}, json=values, timeout=30)
        if not response.ok:
            self.log.warning("Optional Baserow writeback failed table %s row %s status %s", table_id, row_id, response.status_code)
        return {"status_updated": status_updated, "warning": status_warning, "fallback_updated": response.ok}

    def has_done_status_option(self, fields: dict[str, dict[str, Any]]) -> bool:
        field = fields["Generation Status"]
        if field.get("type") != "single_select": return True
        return any(item.get("value", "").casefold() == self.done_status.casefold() for item in field.get("select_options", []))

    def run(self) -> int:
        if self.validate_mapping_only:
            return self.validate_catalog_mapping()
        eligible_seen = 0
        for table in self.get_tables():
            table_id = int(table["id"])
            if table_id not in self.include_ids or table_id in self.exclude_ids: continue
            self.stats.tables_scanned += 1; self.log.info("Loaded table %s (%s)", table["name"], table_id)
            try:
                fields = self.resolve_fields(self.get_table_field_map(table_id))
                self.validate_expected_field_ids(table_id, fields)
                self.log.info("Resolved fields for table %s", table_id)
            except Exception as exc:
                self.stats.tables_skipped += 1; self.log.error("Skipping table %s: %s", table_id, exc)
                self.table_summaries.append({"table_id": table_id, "table_name": table["name"], "rows_loaded": 0,
                    "eligible": 0, "synced": 0, "failed": 0, "skipped": 0, "missing_fields": str(exc), "skip_reasons": {"Required field missing": 1}}); continue
            try:
                rows = list(self.iter_rows(table_id))
            except requests.RequestException as exc:
                self.stats.tables_skipped += 1; self.log.error("Skipping table %s after row load failure: %s", table_id, exc)
                self.table_summaries.append({"table_id": table_id, "table_name": table["name"], "rows_loaded": 0,
                    "eligible": 0, "synced": 0, "failed": 0, "skipped": 0, "missing_fields": "", "skip_reasons": {"Row load failure": 1}}); continue
            self.stats.rows_loaded += len(rows); self.log.info("Rows loaded for table %s: %d", table_id, len(rows))
            table_stats = {"table_id": table_id, "table_name": table["name"], "rows_loaded": len(rows),
                "eligible": 0, "rows_attempted": 0, "synced": 0, "failed": 0, "image_failure_count": 0,
                "status_warnings": 0, "skipped": 0, "missing_fields": "",
                "generation_status_field_id": fields["Generation Status"]["id"],
                "shopify_notes_field_id": fields["SHOPIFY Notes"]["id"],
                "missing_shopify_sync_option": not self.has_done_status_option(fields), "warnings": [], "errors": [], "skip_reasons": {}}
            for row in rows:
                code = str(self.field_value(row, fields, "Product Code") or "").strip()
                reason = self.row_skip_reason(row, fields)
                if self.force_cotton:
                    self.append_row_debug(row, fields, reason)
                if not reason and self.max_products is not None and eligible_seen >= self.max_products: reason = "MAX_PRODUCTS limit reached"
                if not reason and self.max_products_per_table is not None and table_stats["eligible"] >= self.max_products_per_table: reason = "MAX_PRODUCTS_PER_TABLE limit reached"
                normalized_code = code.upper()
                if not reason and normalized_code in self.processed_product_codes:
                    reason = "Duplicate Product Code already processed from another table"; self.stats.duplicate_product_codes_skipped += 1
                if reason:
                    self.stats.rows_skipped += 1; table_stats["skipped"] += 1
                    table_stats["skip_reasons"][reason] = table_stats["skip_reasons"].get(reason, 0) + 1
                    self.records.append({"table_id": table_id, "table_name": table["name"], "row_id": row["id"],
                        "product_code": code, "generation_status_before": get_select_value(self.field_value(row, fields, "Generation Status")),
                        "shopify_notes": get_select_value(row.get(fields["SHOPIFY Notes"]["name"])) if "SHOPIFY Notes" in fields else "",
                        "shopify_product_found": "", "missing_sku": "no", "new_media_uploaded_count": 0,
                        "existing_generated_media_reused_count": 0, "media_ready": "no", "media_reordered": "no",
                        "main_image_set": "no", "generation_status_updated_to_shopify_sync": "no", "final_sync_status": "skipped",
                        "eligible": "no", "skip_reason": reason, "images_found": 0,
                        "reference_images_found": len(self.extract_reference_image_urls(row, fields)),
                        "video_available": "yes" if "Video" in fields and self.field_value(row, fields, "Video") else "no",
                        "duplicate_product_code": "yes" if reason.startswith("Duplicate Product Code") else "no",
                        "shopify_action": "skipped", "shopify_search_query": "", "error": "", "warning": ""})
                    continue
                eligible_seen += 1; self.stats.eligible_rows += 1
                self.stats.rows_attempted += 1
                table_stats["eligible"] += 1; table_stats["rows_attempted"] += 1; self.processed_product_codes.add(normalized_code)
                images = self.extract_generated_image_urls(row, fields)
                warnings, valid_images = [], []
                if "Video" in fields and self.field_value(row, fields, "Video"):
                    warnings.append("Video available but skipped; Shopify video upload is not enabled in this script")
                for image in images:
                    valid, reason = self.validate_url(image["url"])
                    if valid: valid_images.append(image)
                    else: warnings.append(f"{image['label']}: {reason}")
                reference_images = self.extract_reference_image_urls(row, fields)
                record = {"table_id": table_id, "table_name": table["name"], "row_id": row["id"], "product_code": code,
                    "generation_status_before": get_select_value(self.field_value(row, fields, "Generation Status")),
                    "shopify_notes": get_select_value(row.get(fields["SHOPIFY Notes"]["name"])) if "SHOPIFY Notes" in fields else "",
                    "images_found": len(images), "reference_images_found": len(reference_images),
                    "video_available": "yes" if "Video" in fields and self.field_value(row, fields, "Video") else "no",
                    "front_present": "yes" if any(i["label"] == "Generated Front View" for i in images) else "no",
                    "side_present": "yes" if any(i["label"] == "Side View" for i in images) else "no",
                    "back_present": "yes" if any(i["label"] == "Back View" for i in images) else "no",
                    "close_up_present": "yes" if any(i["label"] == "Close Up View" for i in images) else "no",
                    "grid_present": "yes" if any(i["label"] == "Grid View" for i in images) else "no",
                    "full_suit_present": "yes" if any(i["label"] == "Full Suit Image" for i in images) else "no",
                    "back_image_present": "yes" if any(i["label"] == "Back Image" for i in images) else "no",
                    "shopify_product_found": "no", "shopify_product_title": "", "shopify_product_id": "",
                    "shopify_search_query": f"sku:{code}",
                    "missing_sku": "no", "new_media_uploaded_count": 0, "existing_generated_media_reused_count": 0,
                    "media_ready": "no", "media_reordered": "no", "generation_status_updated_to_shopify_sync": "no",
                    "final_sync_status": "pending",
                    "shopify_action": "preview" if self.dry_run else "failed", "images_uploaded": 0,
                    "duplicates_skipped": 0, "main_image_set": "no", "new_generation_status": record_status(self.source_status),
                    "existing_media_count_before": 0, "direct_url_upload_count": 0, "staged_upload_count": 0,
                    "media_ids_created": "", "media_ready_count": 0, "product_media_count_after": 0,
                    "reorder_success": "no", "baserow_status_updated": "no", "warning": "; ".join(warnings), "error": ""}
                record.update({"eligible": "yes", "skip_reason": "", "duplicate_product_code": "no"})
                try:
                    product, match_error = self.find_shopify_product_by_sku(code)
                    if match_error:
                        if "Multiple" in match_error: self.stats.duplicate_sku_matches += 1
                        else:
                            self.stats.products_not_found += 1
                            self.stats.missing_shopify_skus += 1
                            record["missing_sku"] = "yes"
                        raise RuntimeError(match_error)
                    if product:
                        self.stats.products_found += 1; record.update({"shopify_product_found": "yes", "shopify_product_title": product["title"], "shopify_product_id": product["id"]})
                    if self.dry_run:
                        media = self.get_product_media(product["id"]) if product else []
                        record["existing_media_count_before"] = len(media)
                        record["product_media_count_after"] = len(media)
                        record["duplicates_skipped"] = self._planned_duplicates(product, valid_images, code) if product else 0
                    else:
                        if not valid_images: raise RuntimeError("No valid generated images available")
                        result = self.sync_shopify_generated_images(product, valid_images, code)
                        for failure in result["image_failures"]:
                            self.image_failures.append({"catalog_name": table["name"], "table_id": table_id,
                                "row_id": row["id"], "product_code": code, **failure})
                        table_stats["image_failure_count"] += len(result["image_failures"])
                        warnings.extend(result["warnings"])
                        record["warning"] = "; ".join(warnings)
                        success = (result["generated_attached_count"] > 0 and result["media_ready"]
                            and result["reorder_success"] and result["front_attached"] and result["main_image_set"])
                        if not success: raise RuntimeError("Shopify media upload failed; no generated media attached or verified in required order")
                        record.update({"shopify_action": "images synced", "images_uploaded": result["uploaded_count"],
                            "new_media_uploaded_count": result["uploaded_count"],
                            "existing_generated_media_reused_count": result["existing_reused_count"],
                            "media_ready": "yes" if result["media_ready"] else "no",
                            "media_reordered": "yes" if result["reorder_success"] else "no",
                            "duplicates_skipped": result["duplicate_skipped_count"], "main_image_set": "yes",
                            "new_generation_status": self.done_status, "existing_media_count_before": result["media_count_before"],
                            "direct_url_upload_count": result["direct_count"], "staged_upload_count": result["staged_count"],
                            "media_ids_created": ",".join(result["created_ids"]), "media_ready_count": result["ready_count"],
                            "product_media_count_after": result["media_count_after"], "reorder_success": "yes",
                            "baserow_status_updated": "no", "warning": "; ".join(warnings)})
                        writeback = self.update_baserow_row_status(table_id, row["id"], fields, True, "; ".join(warnings))
                        if writeback["warning"]:
                            warnings.append(writeback["warning"]); record["warning"] = "; ".join(warnings)
                            table_stats["warnings"].append(writeback["warning"])
                            self.stats.status_warnings += 1; table_stats["status_warnings"] += 1
                        record["baserow_status_updated"] = "yes" if writeback["status_updated"] else "no"
                        record["generation_status_updated_to_shopify_sync"] = "yes" if writeback["status_updated"] else "no"
                        record["final_sync_status"] = "synced" if writeback["status_updated"] else "media_success_status_update_failed"
                        self.stats.images_uploaded += result["uploaded_count"]; self.stats.duplicates_skipped += result["duplicate_skipped_count"]
                        self.stats.existing_media_reused += result["existing_reused_count"]
                        self.stats.media_reordered_successfully += 1
                        self.stats.main_images_set += 1; self.stats.media_successes += 1
                        if writeback["status_updated"]: self.stats.baserow_rows_updated += 1
                        if not writeback["status_updated"]: self.stats.baserow_status_update_failures += 1
                        if writeback["status_updated"]: self.stats.final_successful_sync_rows += 1
                        table_stats["synced"] += 1
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"; record["error"] = message; self.stats.rows_failed += 1
                    self.stats.final_failed_rows += 1
                    record["final_sync_status"] = "failed"
                    if "not found by SKU" in message:
                        record["skip_reason"] = "Shopify product not found by SKU"; record["missing_sku"] = "yes"; record["final_sync_status"] = "missing_sku"
                    elif "Multiple Shopify products" in message: record["skip_reason"] = "Multiple Shopify products found for SKU"
                    elif "media" in message.lower(): record["skip_reason"] = "Media upload failed"
                    self.log.error("Failure table %s row %s SKU %s: %s", table_id, row["id"], code, message)
                    table_stats["errors"].append(message)
                    table_stats["failed"] += 1
                    if not self.dry_run and "not found by SKU" not in message:
                        try: self.update_baserow_row_status(table_id, row["id"], fields, False, "; ".join(warnings), message)
                        except Exception: self.log.exception("Failure writeback failed for table %s row %s", table_id, row["id"])
                self.records.append(record)
            self.table_summaries.append(table_stats)
            self.log.info("Table summary %s (%s): loaded=%s eligible=%s synced=%s failed=%s skipped=%s reasons=%s",
                table["name"], table_id, table_stats["rows_loaded"], table_stats["eligible"], table_stats["synced"],
                table_stats["failed"], table_stats["skipped"], table_stats["skip_reasons"])
        return self.write_outputs()

    def validate_catalog_mapping(self) -> int:
        mapping_path = ROOT / "config" / "suit_catalog_field_map.json"
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        output = ROOT / "output"; output.mkdir(exist_ok=True)
        logs = ROOT / "logs"; logs.mkdir(exist_ok=True)
        validation_log = logs / "suit_catalog_field_mapping_validation.log"
        records: list[dict[str, Any]] = []
        log_lines: list[str] = []
        for catalog_name, config in mapping.items():
            table_id = int(config["table_id"]); generation_id = int(config["generation_status_field_id"])
            notes_id = int(config["shopify_notes_field_id"])
            record = {"Catalog name": catalog_name, "Table ID": table_id, "Table found yes/no": "no",
                "Generation Status field ID": generation_id, "Generation Status field found yes/no": "no",
                "Generation Status field name": "", "Approved option found yes/no": "no",
                "Shopify-sync option found yes/no": "no", "SHOPIFY Notes field ID": notes_id,
                "SHOPIFY Notes field found yes/no": "no", "SHOPIFY Notes field name": "",
                "Valid yes/no": "no", "Warning": "", "Error": ""}
            try:
                response = self.baserow.session.get(f"{self.base_url}/api/database/fields/table/{table_id}/", timeout=30)
                response.raise_for_status(); fields = response.json(); record["Table found yes/no"] = "yes"
                by_id = {int(field["id"]): field for field in fields}
                generation = by_id.get(generation_id); notes = by_id.get(notes_id)
                if generation:
                    record["Generation Status field found yes/no"] = "yes"
                    record["Generation Status field name"] = generation.get("name", "")
                    options = {str(item.get("value", "")).casefold() for item in generation.get("select_options", [])}
                    record["Approved option found yes/no"] = "yes" if "approved" in options else "no"
                    record["Shopify-sync option found yes/no"] = "yes" if "shopify-sync" in options else "no"
                if notes:
                    record["SHOPIFY Notes field found yes/no"] = "yes"
                    record["SHOPIFY Notes field name"] = notes.get("name", "")
                names_valid = (generation and normalize_field_name(generation.get("name")) == "generation status"
                    and notes and normalize_field_name(notes.get("name")) == "shopify notes")
                valid = bool(names_valid and record["Approved option found yes/no"] == "yes")
                record["Valid yes/no"] = "yes" if valid else "no"
                warnings = []
                if record["Shopify-sync option found yes/no"] == "no": warnings.append("Generation Status is missing Shopify-sync option")
                if generation and normalize_field_name(generation.get("name")) != "generation status": warnings.append("Generation Status field name mismatch")
                if notes and normalize_field_name(notes.get("name")) != "shopify notes": warnings.append("SHOPIFY Notes field name mismatch")
                record["Warning"] = "; ".join(warnings)
            except Exception as exc:
                record["Error"] = f"{type(exc).__name__}: {exc}"
            records.append(record); log_lines.append(str(record))
        columns = ["Catalog name", "Table ID", "Table found yes/no", "Generation Status field ID",
            "Generation Status field found yes/no", "Generation Status field name", "Approved option found yes/no",
            "Shopify-sync option found yes/no", "SHOPIFY Notes field ID", "SHOPIFY Notes field found yes/no",
            "SHOPIFY Notes field name", "Valid yes/no", "Warning", "Error"]
        csv_path = output / "suit_catalog_field_mapping_validation.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, columns); writer.writeheader(); writer.writerows(records)
        counts = {"Total catalogs in mapping": len(records),
            "Valid catalogs": sum(r["Valid yes/no"] == "yes" for r in records),
            "Invalid catalogs": sum(r["Valid yes/no"] == "no" for r in records),
            "Catalogs missing Shopify-sync option": sum(r["Shopify-sync option found yes/no"] == "no" for r in records),
            "Catalogs missing Approved option": sum(r["Approved option found yes/no"] == "no" for r in records),
            "Catalogs with missing SHOPIFY Notes field": sum(r["SHOPIFY Notes field found yes/no"] == "no" for r in records),
            "Catalogs with missing Generation Status field": sum(r["Generation Status field found yes/no"] == "no" for r in records)}
        text_path = output / "suit_catalog_field_mapping_validation.txt"
        text_path.write_text("\n".join(f"{key}: {value}" for key, value in counts.items()) + "\n", encoding="utf-8")
        validation_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        for key, value in counts.items(): print(f"{key}: {value}")
        print(f"Validation CSV: {csv_path}\nValidation report: {text_path}\nValidation log: {validation_log}")
        return 1 if counts["Invalid catalogs"] else 0

    @staticmethod
    def _planned_duplicates(product: dict[str, Any], images: list[dict[str, str]], code: str) -> int:
        nodes = [n for n in product.get("media", {}).get("nodes", []) if n.get("status") == "READY"]
        urls = {(n.get("image") or {}).get("url", "").split("?", 1)[0] for n in nodes}; alts = {str(n.get("alt") or "").casefold() for n in nodes}
        return sum(i["url"].split("?", 1)[0] in urls or SuitImageSync.media_alt_text(code, i["label"]).casefold() in alts for i in images)

    def write_outputs(self) -> int:
        output = ROOT / "output"; output.mkdir(exist_ok=True)
        single_cotton = self.single_catalog and self.catalog_name.casefold() == "cotton suits"
        prefix = "cotton_suits" if single_cotton else "suit"
        preview = output / f"{prefix}_shopify_image_sync_preview.csv"
        report = output / f"{prefix}_shopify_image_sync_report.csv"
        failed = output / f"{prefix}_shopify_image_sync_failed.csv"
        image_failed = output / (f"{prefix}_image_failures.csv" if single_cotton else f"{prefix}_shopify_image_failures.csv")
        missing_skus = output / (f"{prefix}_missing_skus.csv" if single_cotton else f"{prefix}_shopify_missing_skus.csv")
        target = preview if self.dry_run else report
        self._csv(target, self.records); self._csv(failed, [r for r in self.records if r["error"]])
        with image_failed.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, IMAGE_FAILURE_FIELDS, extrasaction="ignore")
            writer.writeheader(); writer.writerows(self.image_failures)
        if single_cotton:
            with (output / "cotton_suits_row_debug.csv").open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, ROW_DEBUG_FIELDS, extrasaction="ignore")
                writer.writeheader(); writer.writerows(self.row_debug)
            with (output / "cotton_suits_image_failures.csv").open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, IMAGE_FAILURE_FIELDS, extrasaction="ignore")
                writer.writeheader(); writer.writerows(self.image_failures)
        missing_records = [r for r in self.records if r.get("skip_reason") == "Shopify product not found by SKU"]
        if single_cotton:
            with missing_skus.open("w", newline="", encoding="utf-8-sig") as handle:
                columns = ["Row ID", "Product Code", "Product Title", "Reason", "Shopify search query"]
                writer = csv.DictWriter(handle, columns, extrasaction="ignore")
                writer.writeheader()
                for item in missing_records:
                    writer.writerow({"Row ID": item.get("row_id"), "Product Code": item.get("product_code"),
                        "Product Title": item.get("shopify_product_title", ""), "Reason": "Shopify product not found by SKU",
                        "Shopify search query": item.get("shopify_search_query", f"sku:{item.get('product_code', '')}")})
        else:
            self._csv(missing_skus, missing_records)
        summary = output / ("final_cotton_suits_shopify_image_sync_report.txt" if single_cotton else "final_suit_shopify_image_sync_report.txt")
        header = {"database_id": os.getenv("BASEROW_DATABASE_ID", ""), "processing_mode": self.process_mode,
            "total_tables_selected": len(self.include_ids), "max_products_active": "yes" if self.max_products is not None else "no",
            "max_products_value": self.max_products if self.max_products is not None else ""}
        if single_cotton:
            header = {"Catalog processed": "Cotton Suits", "Table ID": 936059, **header}
        cotton_debug_counts = {}
        if single_cotton:
            cotton_debug_counts = {
                "Rows loaded": len(self.row_debug),
                "Approved Generation Status count": sum(str(r.get("Generation Status parsed", "")).strip().casefold() == "approved" for r in self.row_debug),
                "Approved SHOPIFY Notes count": sum(str(r.get("SHOPIFY Notes parsed", "")).strip().casefold() == "approved" for r in self.row_debug),
                "Rows with Product Code": sum(r.get("Has Product Code") == "yes" for r in self.row_debug),
                "Rows with usable images": sum(int(r.get("Usable image count") or 0) > 0 for r in self.row_debug),
                "Database ID": COTTON_SUITS_CONFIG["database_id"],
                "View ID": COTTON_SUITS_CONFIG["view_id"],
                "USE_BASEROW_VIEW": self.use_baserow_view,
                "Top skip reasons": dict(sorted({r.get("Skip reason", ""): sum(x.get("Skip reason", "") == r.get("Skip reason", "") for x in self.row_debug) for r in self.row_debug if r.get("Skip reason")}.items(), key=lambda kv: kv[1], reverse=True)[:10]),
            }
        media_failures = sum(1 for r in self.records if r.get("skip_reason") == "Media upload failed")
        requested = {
            "Rows attempted": self.stats.rows_attempted,
            "Shopify products found": self.stats.products_found,
            "Missing Shopify SKUs": self.stats.missing_shopify_skus,
            "New media uploaded": self.stats.images_uploaded,
            "Existing media reused": self.stats.existing_media_reused,
            "Media reordered successfully": self.stats.media_reordered_successfully,
            "Main image set successfully": self.stats.main_images_set,
            "Generation Status updated to Shopify-sync": self.stats.baserow_rows_updated,
            "Media failures": media_failures,
            "Baserow status update failures": self.stats.baserow_status_update_failures,
            "Final successful sync rows": self.stats.final_successful_sync_rows,
            "Final failed rows": self.stats.final_failed_rows,
        }
        lines = [*(f"{k}: {v}" for k, v in header.items()), *(f"{k}: {v}" for k, v in cotton_debug_counts.items()), "", "Required final counts:",
            *(f"{k}: {v}" for k, v in requested.items()), "", "Raw counters:",
            *(f"{k}: {v}" for k, v in asdict(self.stats).items()), "", "Per-table summary:"]
        lines.extend(str(item) for item in self.table_summaries)
        summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for key, value in asdict(self.stats).items(): print(f"{key.replace('_', ' ').title()}: {value}")
        print(f"Report: {target}\nFailed CSV: {failed}\nMissing SKU CSV: {missing_skus}\nImage Failures CSV: {image_failed}\nSummary: {summary}")
        self.log.info("Final summary: %s", asdict(self.stats))
        return 1 if self.stats.rows_failed else 0

    @staticmethod
    def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, REPORT_FIELDS, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)


def record_status(value: str) -> str:
    return value


def main() -> int:
    try: return SuitImageSync().run()
    except Exception as exc:
        print(f"Fatal error: {type(exc).__name__}: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": sys.exit(main())
