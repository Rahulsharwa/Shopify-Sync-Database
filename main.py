from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from baserow_client import BaserowClient
from shopify_client import ShopifyClient
from sync_products import ProductSync
from utils import env_bool, get_single_select_value, row_readiness, setup_logging, text
from models import COLLECTION_RULES


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value: raise ValueError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    root = Path(__file__).resolve().parent; load_dotenv(root / ".env")
    logger = setup_logging(root)
    try:
        dry_run = env_bool("DRY_RUN", True)
        max_raw = os.getenv("MAX_PRODUCTS", "").strip(); max_products = int(max_raw) if max_raw else None
        if max_products is not None and max_products <= 0: raise ValueError("MAX_PRODUCTS must be positive")
        status = os.getenv("SHOPIFY_PRODUCT_STATUS", "DRAFT").upper()
        if status not in {"DRAFT", "ACTIVE"}: raise ValueError("SHOPIFY_PRODUCT_STATUS must be DRAFT or ACTIVE")
        image_mode = os.getenv("IMAGE_MODE", "ADD_MISSING").upper()
        if image_mode not in {"ADD_MISSING", "REPLACE"}: raise ValueError("IMAGE_MODE must be ADD_MISSING or REPLACE")
        baserow = BaserowClient(required("BASEROW_API_BASE"), required("BASEROW_TOKEN"), required("BASEROW_TABLE_ID"))
        token = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "").strip()
        shopify = None
        collections = []
        if token:
            shopify = ShopifyClient(required("SHOPIFY_STORE_DOMAIN"), token, required("SHOPIFY_API_VERSION"))
            logger.info("Connected to Shopify store %s", shopify.check_connection())
            titles_raw = os.getenv("SHOPIFY_COLLECTION_TITLES") or os.getenv("SHOPIFY_COLLECTION_TITLE", "Fabrics")
            titles = [item.strip() for item in titles_raw.split(",") if item.strip()]
            if env_bool("ENABLE_COLLECTION_MAPPING", False) or os.getenv("OVERWRITE_BASEROW_PARSED_FIELDS") is not None or env_bool("ENABLE_CATEGORY_METAFIELDS", False):
                titles.extend(name for name, _ in COLLECTION_RULES)
            for title in dict.fromkeys(titles):
                collection = shopify.find_collection(title)
                if collection:
                    collections.append(collection); logger.info("Found Shopify collection %s", collection["title"])
                else:
                    logger.warning("Shopify collection not found: %s", title)
            if not collections: raise RuntimeError("No configured Shopify collection was found")
        elif not dry_run:
            raise ValueError("Missing required environment variable: SHOPIFY_ADMIN_ACCESS_TOKEN")
        else:
            logger.warning("Shopify token unavailable; dry run will skip Shopify connection and collection checks")
        source_generation_status = os.getenv("BASEROW_SOURCE_GENERATION_STATUS", "Scraped")
        source_statuses = [s.strip() for s in os.getenv("BASEROW_SOURCE_GENERATION_STATUSES", "").split(",") if s.strip()]
        if source_statuses: source_generation_status = ",".join(source_statuses)
        all_rows = list(baserow.iter_rows())
        ready_rows = []
        readiness_counts = {"not_source_status": 0, "missing_product_code": 0, "missing_product_title": 0}
        for row in all_rows:
            if source_statuses:
                status = get_single_select_value(row.get("Generation Status"))
                if status not in source_statuses: ready, reason = False, "not_source_status"
                elif not text(row.get("Product Code")): ready, reason = False, "missing_product_code"
                elif not text(row.get("Product Title")): ready, reason = False, "missing_product_title"
                else: ready, reason = True, ""
            else:
                ready, reason = row_readiness(row, source_generation_status)
            if ready: ready_rows.append(row)
            else: readiness_counts[reason] += 1
        rows = ready_rows[:max_products] if max_products is not None else ready_rows
        logger.info("Rows loaded from Baserow: %d", len(all_rows))
        source_status_count = len(all_rows) - readiness_counts["not_source_status"]
        logger.info("Rows with Generation Status = %s: %d", source_generation_status, source_status_count)
        logger.info("Rows skipped because not %s: %d", source_generation_status, readiness_counts["not_source_status"])
        logger.info("Rows skipped because missing Product Code: %d", readiness_counts["missing_product_code"])
        logger.info("Rows skipped because missing Product Title: %d", readiness_counts["missing_product_title"])
        sync = ProductSync(baserow, shopify, root, logger, dry_run, status,
            os.getenv("SHOPIFY_COLLECTION_TITLE", "Fabrics"), image_mode, env_bool("FORCE_PRODUCT_STATUS", False),
            source_generation_status, os.getenv("BASEROW_SYNCED_GENERATION_STATUS", "shopify-Sync"),
            int(os.getenv("BASEROW_SYNCED_GENERATION_STATUS_ID", "6556673")))
        if shopify and (env_bool("ENABLE_COLLECTION_MAPPING", False) or env_bool("ENABLE_CATEGORY_METAFIELDS", False)):
            category_name = os.getenv("SHOPIFY_CATEGORY_NAME", "Fabric in Textiles")
            category = shopify.find_taxonomy_category(category_name)
            if category:
                sync.category_id = category["id"]; logger.info("Resolved Shopify category %s", category.get("fullName") or category["name"])
            else:
                logger.warning("Could not resolve Shopify category %s", category_name)
        stats = sync.sync(rows, collections)
        stats.rows_loaded = len(all_rows)
        stats.rows_source_status = source_status_count
        stats.skipped_not_source_status = readiness_counts["not_source_status"]
        stats.skipped_missing_product_code = readiness_counts["missing_product_code"]
        stats.skipped_missing_product_title = readiness_counts["missing_product_title"]
        paths = sync.write_outputs()
        print("\nSync summary")
        for key, value in asdict(stats).items(): print(f"{key.replace('_', ' ').title()}: {value}")
        for path in paths: print(f"Output: {path}")
        return 1 if stats.failed else 0
    except Exception as exc:
        logger.error("Fatal error: %s: %s", type(exc).__name__, exc)
        return 2


if __name__ == "__main__": sys.exit(main())
