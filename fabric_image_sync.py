from __future__ import annotations

import csv
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import saree_image_sync as engine
from saree_image_sync import SareeImageSync, normalize_field_name
from utils import env_bool

ROOT = Path(__file__).resolve().parent
CATALOG_MAP = ROOT / "config" / "fabric_catalog_field_map.json"
IMAGE_MAP = ROOT / "config" / "fabric_image_field_map.json"

CATALOGS = json.loads(CATALOG_MAP.read_text(encoding="utf-8"))
IMAGE_FIELDS = next(iter(json.loads(IMAGE_MAP.read_text(encoding="utf-8")).values()))
TABLE_NAMES = {int(config["table_id"]): name for name, config in CATALOGS.items()}
STATUS_FIELD_IDS = {int(config["table_id"]): int(config["generation_status_field_id"]) for config in CATALOGS.values()}
NOTES_FIELD_IDS = {int(config["table_id"]): int(config["shopify_notes_field_id"]) for config in CATALOGS.values()}

engine.IMAGE_FIELDS = IMAGE_FIELDS
engine.TABLE_NAMES = TABLE_NAMES
engine.CATEGORY_TABLE_IDS = set(TABLE_NAMES)
engine.ALL_REAL_TABLE_IDS = set(TABLE_NAMES)
engine.SAREE_GENERATED_FRONT_FIELD_IDS = {}


class FabricImageSync(SareeImageSync):
    def _select_ids(self) -> tuple[set[int], set[int]]:
        if not env_bool("PROCESS_FABRIC_SYNC", False):
            raise ValueError("Set PROCESS_FABRIC_SYNC=true to run fabric image sync")
        return set(TABLE_NAMES), set()

    @staticmethod
    def _logger() -> logging.Logger:
        (ROOT / "logs").mkdir(exist_ok=True)
        logger = logging.getLogger("fabric_image_sync")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        for handler in (logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "fabric_image_sync.log", encoding="utf-8")):
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        return logger

    def resolve_fields(self, field_map: dict[str, dict[str, Any]], table_id: int | None = None) -> dict[str, dict[str, Any]]:
        by_id = {int(field["id"]): field for field in field_map.values()}
        status = by_id.get(STATUS_FIELD_IDS[int(table_id)])
        notes = by_id.get(NOTES_FIELD_IDS[int(table_id)])
        if not status or not notes:
            raise ValueError("configured Generation Status or SHOPIFY Notes field ID is absent from table metadata")
        resolved = {"Generation Status": status, "SHOPIFY Notes": notes}
        for canonical in ["Product Code", *IMAGE_FIELDS, *engine.OPTIONAL_FIELDS]:
            match = field_map.get(normalize_field_name(canonical))
            if match:
                resolved[canonical] = match
        if "Product Code" not in resolved:
            raise ValueError("missing fields: Product Code")
        return resolved

    def write_outputs(self) -> int:
        output = ROOT / "output"
        output.mkdir(exist_ok=True)
        target = output / ("fabric_shopify_image_sync_preview.csv" if self.dry_run else "fabric_shopify_image_sync_report.csv")
        failed = output / "fabric_shopify_image_sync_failed.csv"
        image_failed = output / "fabric_shopify_image_failures.csv"
        missing_skus = output / "fabric_shopify_missing_skus.csv"
        duplicates = output / "fabric_shopify_duplicates_skipped.csv"
        compressed = output / "fabric_shopify_compressed_images.csv"
        self._csv(target, self.records)
        self._csv(failed, [r for r in self.records if r["error"]])
        self._csv(missing_skus, [r for r in self.records if r.get("skip_reason") == "Shopify product not found by SKU"])
        self._csv(duplicates, [r for r in self.records if int(r.get("duplicates_skipped") or 0) > 0])
        compressed.write_text("catalog_name,table_id,row_id,product_code,image_label,source_bytes,final_bytes\n", encoding="utf-8-sig")
        with image_failed.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, engine.IMAGE_FAILURE_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.image_failures)
        summary = output / "final_fabric_shopify_image_sync_report.txt"
        lines = [*(f"{k}: {v}" for k, v in asdict(self.stats).items()), "", "Per-table summary:"]
        lines.extend(str(item) for item in self.table_summaries)
        summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for key, value in asdict(self.stats).items():
            print(f"{key.replace('_', ' ').title()}: {value}")
        print(f"Report: {target}\nFailed CSV: {failed}\nMissing SKU CSV: {missing_skus}\nImage Failures CSV: {image_failed}\nSummary: {summary}")
        return 1 if self.stats.rows_failed else 0


def main() -> int:
    try:
        return FabricImageSync().run()
    except Exception as exc:
        print(f"Fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
