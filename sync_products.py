from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from baserow_client import BaserowClient
from models import FabricProduct, build_enrichment_tags, build_premium_fabric_description, mapped_collections, normalize_color, parse_specification_html
from shopify_client import ShopifyClient
from utils import get_single_select_value, text, utc_now


@dataclass
class Stats:
    rows_loaded: int = 0; rows_source_status: int = 0; skipped_not_source_status: int = 0
    skipped_missing_product_code: int = 0; skipped_missing_product_title: int = 0
    attempted: int = 0; skipped: int = 0; created: int = 0; updated: int = 0
    collection_added: int = 0; collection_existing: int = 0; failed: int = 0
    images_attached: int = 0; image_failures: int = 0; metafields_updated: int = 0; baserow_updated: int = 0
    rows_marked_synced: int = 0


REPORT_FIELDS = ["row_id", "product_code", "title", "previous_generation_status", "new_generation_status",
                 "action", "shopify_id", "shopify_url", "status", "warnings", "error"]


class ProductSync:
    def __init__(self, baserow: BaserowClient, shopify: ShopifyClient | None, root: Path, logger: logging.Logger,
                 dry_run: bool, status: str, collection_title: str, image_mode: str, force_status: bool,
                 source_generation_status: str, synced_generation_status: str, synced_generation_status_id: int):
        self.baserow, self.shopify, self.root, self.log = baserow, shopify, root, logger
        self.dry_run, self.status, self.collection_title = dry_run, status, collection_title
        self.image_mode, self.force_status = image_mode, force_status
        self.source_generation_status = source_generation_status
        self.synced_generation_status = synced_generation_status
        self.synced_generation_status_id = synced_generation_status_id
        self.stats, self.records = Stats(), []
        self.enrichment_mode = bool(os.getenv("BASEROW_DONE_GENERATION_STATUS"))
        self.specification_mode = os.getenv("OVERWRITE_BASEROW_PARSED_FIELDS") is not None
        self.metafield_fix_mode = os.getenv("ENABLE_CATEGORY_METAFIELDS", "false").lower() == "true"
        self.category_fix_mode = os.getenv("ENABLE_COLLECTION_MAPPING", "false").lower() == "true" and not self.specification_mode and not self.metafield_fix_mode
        self.done_status = os.getenv("BASEROW_DONE_GENERATION_STATUS", synced_generation_status)
        self.done_status_id = int(os.getenv("BASEROW_DONE_GENERATION_STATUS_ID", str(synced_generation_status_id)))
        self.enable_unit_price = os.getenv("ENABLE_UNIT_PRICE", "true").lower() == "true"
        self.vendor = os.getenv("SHOPIFY_VENDOR", "Janardhana Silk House")
        self.enrichment_rows: dict[int, dict[str, Any]] = {}
        self.category_id: str | None = None

    def validate_image(self, image: dict[str, str]) -> tuple[bool, str]:
        url = image["url"]
        if any(token in url.lower() for token in ("/tiny/", "/small/", "thumbnail")):
            return False, "thumbnail URL rejected"
        try:
            response = requests.get(url, stream=True, timeout=20, allow_redirects=True,
                                    headers={"User-Agent": "baserow-shopify-sync/1.0"})
            ok = response.status_code == 200 and response.headers.get("content-type", "").lower().startswith("image/")
            response.close()
            return (True, "") if ok else (False, f"invalid image response ({response.status_code}, {response.headers.get('content-type', '')})")
        except requests.RequestException as exc:
            return False, f"image validation failed: {type(exc).__name__}"

    @staticmethod
    def normalized_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))

    def payload(self, product: FabricProduct, existing: dict[str, Any] | None) -> dict[str, Any]:
        variant: dict[str, Any] = {
            "sku": product.code,
            "price": product.price,
            "optionValues": [{"optionName": "Title", "name": "Default Title"}],
        }
        if existing and existing.get("variants", {}).get("nodes"):
            variant["id"] = existing["variants"]["nodes"][0]["id"]
        value: dict[str, Any] = {
            "title": product.title, "descriptionHtml": product.description_html,
            "vendor": "Janardhana Silk House", "productType": product.product_type,
            "tags": product.tags, "seo": {"title": product.seo_title, "description": product.seo_description},
            "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
            "variants": [variant],
        }
        if existing: value["id"] = existing["id"]
        if not existing or self.force_status: value["status"] = self.status
        return value

    def enrichment_payload(self, product: FabricProduct, row: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
        variant = {"id": existing["variants"]["nodes"][0]["id"], "sku": product.code, "price": product.price,
                   "optionValues": [{"optionName": "Title", "name": "Default Title"}]}
        if self.enable_unit_price:
            variant.update({"showUnitPrice": True, "unitPriceMeasurement": {
                "quantityValue": 1, "quantityUnit": "M", "referenceValue": 1, "referenceUnit": "M"}})
        tags = build_enrichment_tags(row)
        if self.category_fix_mode or self.specification_mode or self.metafield_fix_mode:
            normalized, individual = normalize_color(" ".join([text(row.get("Color")), text(row.get("Color Filter")), product.title]))
            tags = list(dict.fromkeys([*tags, normalized, *individual, *mapped_collections(row)]))
        value = {"id": existing["id"], "title": product.title,
                "descriptionHtml": build_premium_fabric_description(row), "vendor": self.vendor,
                "productType": os.getenv("SHOPIFY_PRODUCT_TYPE", "Fabric"), "tags": build_enrichment_tags(row),
                "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}], "variants": [variant]}
        value["tags"] = tags
        if (self.category_fix_mode or self.metafield_fix_mode) and os.getenv("FORCE_PRODUCT_STATUS", "false").lower() == "true": value["status"] = "ACTIVE"
        if (self.category_fix_mode or self.metafield_fix_mode) and self.category_id: value["category"] = self.category_id
        return value

    def sync(self, rows: list[dict[str, Any]], collections: list[dict[str, Any]]) -> Stats:
        self.stats.rows_loaded = len(rows)
        for row in rows:
            row = dict(row)
            parsed = parse_specification_html(text(row.get("Specification_")))
            overwrite_parsed = os.getenv("OVERWRITE_BASEROW_PARSED_FIELDS", "false").lower() == "true"
            original_fabric, original_color = text(row.get("Fabric")), text(row.get("Color"))
            if self.metafield_fix_mode:
                row["Fabric"] = original_fabric or parsed.get("fabric_collections") or text(row.get("product_type")) or "Silk"
                row["Color"] = original_color or parsed.get("color") or text(row.get("Color Filter")) or text(row.get("mapped_color")) or "Multicolor"
                row["Care instructions"] = text(row.get("Care instructions")) or "Dry clean only"
            if parsed.get("fabric_collections") and (overwrite_parsed or not original_fabric): row["Fabric"] = parsed["fabric_collections"]
            if parsed.get("color") and (overwrite_parsed or not original_color): row["Color"] = parsed["color"]
            normalized_preview, _ = normalize_color(text(row.get("Color")))
            spec_report = {"raw_specification_length": len(text(row.get("Specification_"))),
                "parsed_fabric_collections": parsed.get("fabric_collections", ""), "parsed_color": parsed.get("color", ""),
                "resolved_fabric": text(row.get("Fabric")), "resolved_color": text(row.get("Color")),
                "normalized_color": normalized_preview,
                "baserow_fabric_updated": "yes" if parsed.get("fabric_collections") and (overwrite_parsed or not original_fabric) else "no",
                "baserow_color_updated": "yes" if parsed.get("color") and (overwrite_parsed or not original_color) else "no"}
            product = FabricProduct.from_row(row)
            self.enrichment_rows[product.row_id] = row
            previous_status = get_single_select_value(row.get("Generation Status"))
            self.stats.attempted += 1
            errors = product.validation_errors()
            if errors:
                self.stats.skipped += 1
                self.log.warning("Row %s skipped: %s", product.row_id, "; ".join(errors))
                self.records.append(self.record(product, "skipped", "skipped", previous_status=previous_status, error="; ".join(errors)))
                continue
            valid_images, warnings = [], []
            for image in product.images:
                valid, reason = self.validate_image(image)
                if valid: valid_images.append(image)
                else:
                    warnings.append(reason); self.stats.image_failures += 1
                    self.log.warning("Image failure for SKU %s: %s", product.code, reason)
            if self.dry_run:
                preview_existing = None
                if self.enrichment_mode and self.shopify:
                    preview_existing = self.shopify.get_product(product.shopify_id) if product.shopify_id else None
                    if not preview_existing: preview_existing = self.shopify.find_by_sku(product.code)
                    if not preview_existing:
                        self.stats.failed += 1
                        self.records.append(self.record(product, "failed", "failed", previous_status=previous_status,
                            new_status=previous_status, error="Shopify product not found for enrichment"))
                        continue
                preview_payload = (self.enrichment_payload(product, row, preview_existing) if self.enrichment_mode
                                   else self.payload(product, None))
                self.records.append(self.record(product, "preview", "valid", previous_status=previous_status,
                    new_status=self.done_status if self.enrichment_mode else self.synced_generation_status,
                    warnings="; ".join(warnings), payload=preview_payload, images=[] if self.enrichment_mode else valid_images, **spec_report))
                continue
            try:
                assert self.shopify
                existing = self.shopify.get_product(product.shopify_id) if product.shopify_id else None
                if product.shopify_id and existing: self.log.info("Product found by Shopify Id for SKU %s", product.code)
                if not existing:
                    existing = self.shopify.find_by_sku(product.code)
                    if existing: self.log.info("Product found by SKU %s", product.code)
                if self.enrichment_mode and not existing:
                    raise RuntimeError("Shopify product not found for enrichment")
                action = "update" if existing else "create"
                if self.enrichment_mode:
                    saved, unit_warnings = self.shopify.product_set_with_unit_fallback(self.enrichment_payload(product, row, existing))
                    warnings.extend(unit_warnings)
                    if self.category_fix_mode and not self.category_id:
                        category = os.getenv("SHOPIFY_CATEGORY_NAME", "Fabric in Textiles")
                        warnings.append(f"Could not set Shopify product category: {category}.")
                else:
                    saved, _ = self.shopify.product_set(self.payload(product, existing))
                setattr(self.stats, "updated" if existing else "created", getattr(self.stats, "updated" if existing else "created") + 1)
                self.log.info("Product %sd for SKU %s", action, product.code)
                collection_ids = {node["id"] for node in saved.get("collections", {}).get("nodes", [])}
                wanted = set(mapped_collections(row)) if (self.category_fix_mode or self.specification_mode or self.metafield_fix_mode) else {c["title"] for c in collections}
                for collection in [c for c in collections if c["title"] in wanted]:
                    if collection["id"] in collection_ids:
                        self.stats.collection_existing += 1; self.log.info("Product already in %s collection: %s", collection["title"], product.code)
                    else:
                        self.shopify.add_to_collection(collection["id"], saved["id"]); self.stats.collection_added += 1
                        self.log.info("Product added to %s collection: %s", collection["title"], product.code)
                if self.category_fix_mode or self.specification_mode or self.metafield_fix_mode:
                    normalized_color, _ = normalize_color(" ".join([text(row.get("Color")), text(row.get("Color Filter")), product.title]))
                    for field in product.metafields:
                        if field["key"] == "color":
                            field["value"] = json.dumps([normalized_color]); field["type"] = "list.single_line_text_field"
                        elif field["key"] in {"fabric", "weave", "occasion", "pattern", "region", "zari"}:
                            field["value"] = json.dumps([field["value"]]); field["type"] = "list.single_line_text_field"
                    if not any(field["key"] == "color" for field in product.metafields):
                        product.metafields.append({"namespace": "custom", "key": "color", "type": "list.single_line_text_field", "value": json.dumps([normalized_color])})
                    if not any(field["key"] == "care_instructions" for field in product.metafields):
                        product.metafields.append({"namespace": "custom", "key": "care_instructions", "type": "multi_line_text_field", "value": "Dry clean only"})
                    if self.metafield_fix_mode:
                        resolved_fabric = text(row.get("Fabric")) or "Silk"
                        for key, value in (("embellishment", text(row.get("Zari"))), ("material", resolved_fabric)):
                            if value and not any(field["key"] == key for field in product.metafields):
                                product.metafields.append({"namespace": "custom", "key": key, "type": "single_line_text_field", "value": value})
                self.shopify.set_metafields(saved["id"], product.metafields)
                if self.metafield_fix_mode:
                    detailed_fabric = text(row.get("Fabric")) or "Silk"
                    category_fabric = "Silk" if ("silk" in detailed_fabric.casefold() or detailed_fabric.casefold() == "fabric") else detailed_fabric
                    definitions = {"color-pattern": ("gid://shopify/MetaobjectDefinition/13370884294", normalize_color(text(row.get("Color")))[0]),
                        "fabric": ("gid://shopify/MetaobjectDefinition/13370917062", category_fabric),
                        "care-instructions": ("gid://shopify/MetaobjectDefinition/15348072646", text(row.get("Care instructions")) or "Dry clean only")}
                    category_fields = []
                    for key, (definition_id, value) in definitions.items():
                        ref = self.shopify.find_metaobject_value(definition_id, value)
                        if ref: category_fields.append({"namespace": "shopify", "key": key, "type": "list.metaobject_reference", "value": json.dumps([ref])})
                        else: warnings.append(f"Could not resolve category metafield {key}: {value}")
                    try: self.shopify.set_metafields(saved["id"], category_fields)
                    except Exception as exc: warnings.append(f"Could not set category metafields: {exc}")
                if product.metafields: self.stats.metafields_updated += len(product.metafields)
                existing_urls = {
                    self.normalized_url((node.get("image") or {}).get("url", ""))
                    for node in saved.get("media", {}).get("nodes", [])
                }
                missing = [] if self.enrichment_mode else [image for image in valid_images if self.normalized_url(image["url"]) not in existing_urls]
                if self.image_mode == "REPLACE": warnings.append("REPLACE currently preserves existing media and adds missing media")
                self.shopify.create_media(saved["id"], missing)
                self.stats.images_attached += len(missing)
                admin_url = f"https://{self.shopify.domain}/admin/products/{saved['id'].rsplit('/', 1)[-1]}"
                target_status = self.done_status if self.enrichment_mode else self.synced_generation_status
                target_id = self.done_status_id if self.enrichment_mode else self.synced_generation_status_id
                writeback = {"Generation Status": target_status,
                    "Shopify Id": saved["id"], "Shopify Product URL": admin_url,
                    "Shopify Status": "Generated" if self.enrichment_mode else "Synced Draft",
                    "Status shopify": "Generated" if self.enrichment_mode else "Synced Draft", "Last Modified": utc_now(),
                    "warnings": "; ".join(warnings), "Error Notes": ""}
                if self.enrichment_mode:
                    writeback.update({"Description HTML": build_premium_fabric_description(row),
                        "Tags": ", ".join(build_enrichment_tags(row)), "type": "Fabric", "product_type": "Fabric"})
                if self.category_fix_mode:
                    normalized_color, individual_colors = normalize_color(" ".join([text(row.get("Color")), text(row.get("Color Filter")), product.title]))
                    final_collections = mapped_collections(row)
                    final_tags = list(dict.fromkeys([*build_enrichment_tags(row), normalized_color, *individual_colors, *final_collections]))
                    writeback.update({"Category": os.getenv("SHOPIFY_CATEGORY_NAME", "Fabric in Textiles"),
                        "Collection Name": ", ".join(final_collections), "Tags": ", ".join(final_tags),
                        "Color Filter": normalized_color, "mapped_color": normalized_color, "color_tag": normalized_color})
                if self.specification_mode:
                    normalized_color, individual_colors = normalize_color(text(row.get("Color")))
                    resolved_fabric, resolved_color = text(row.get("Fabric")), text(row.get("Color"))
                    spec_tags = list(dict.fromkeys([*build_enrichment_tags(row), resolved_fabric, normalized_color, *individual_colors]))
                    writeback.update({"Color Filter": normalized_color, "mapped_color": normalized_color,
                        "color_tag": normalized_color, "Tags": ", ".join(filter(None, spec_tags))})
                    if parsed.get("fabric_collections") and (overwrite_parsed or not original_fabric): writeback["Fabric"] = resolved_fabric
                    if parsed.get("color") and (overwrite_parsed or not original_color): writeback["Color"] = resolved_color
                if self.metafield_fix_mode:
                    normalized_color, individual_colors = normalize_color(text(row.get("Color")))
                    final_collections = mapped_collections(row)
                    final_tags = list(dict.fromkeys([*build_enrichment_tags(row), normalized_color, *individual_colors, *final_collections]))
                    writeback.update({"Category": os.getenv("SHOPIFY_CATEGORY_NAME", "Fabric in Textiles"),
                        "Fabric": text(row.get("Fabric")) or "Silk", "Color": text(row.get("Color")) or normalized_color,
                        "Care instructions": text(row.get("Care instructions")) or "Dry clean only", "Color Filter": normalized_color,
                        "mapped_color": normalized_color, "color_tag": normalized_color, "Tags": ", ".join(final_tags),
                        "Collection Name": ", ".join(final_collections), "type": "Fabric", "product_type": "Fabric"})
                self.baserow.update_row_with_select_fallback(product.row_id, writeback, "Generation Status", target_id)
                self.stats.baserow_updated += 1; self.stats.rows_marked_synced += 1
                self.records.append(self.record(product, action + "d", "success", saved["id"], "; ".join(warnings),
                    previous_status=previous_status, new_status=target_status, shopify_url=admin_url, **spec_report))
            except Exception as exc:
                self.stats.failed += 1
                message = f"{type(exc).__name__}: {exc}"
                self.log.error("Product sync failure for row %s SKU %s: %s", product.row_id, product.code, message)
                try: self.baserow.update_row(product.row_id, {"Error Notes": message[:1000], "warnings": message[:1000], "Last Modified": utc_now()})
                except Exception: self.log.exception("Could not write failure to Baserow row %s", product.row_id)
                self.records.append(self.record(product, "failed", "failed", previous_status=previous_status,
                    new_status=previous_status, error=message, warnings=message))
        return self.stats

    def record(self, p: FabricProduct, action: str, status: str, shopify_id: str = "", warnings: str = "", error: str = "",
               previous_status: str = "", new_status: str = "", shopify_url: str = "", **extra: Any) -> dict[str, Any]:
        return {"row_id": p.row_id, "product_code": p.code, "title": p.title, "action": action,
                "previous_generation_status": previous_status, "new_generation_status": new_status,
                "shopify_id": shopify_id or p.shopify_id, "shopify_url": shopify_url,
                "status": status, "warnings": warnings, "error": error, **extra}

    def write_outputs(self) -> list[Path]:
        output = self.root / "output"; output.mkdir(exist_ok=True)
        paths = []
        prefix = "shopify_metafield_fix" if self.metafield_fix_mode else ("shopify_category_fix" if self.category_fix_mode else ("shopify_enrichment" if self.enrichment_mode else "shopify_sync"))
        if self.dry_run:
            path = output / f"{prefix}_preview.csv"
            fields = REPORT_FIELDS + (["raw_specification_length", "parsed_fabric_collections", "parsed_color", "resolved_fabric",
                "resolved_color", "normalized_color", "baserow_fabric_updated", "baserow_color_updated"] if self.specification_mode else []) + ["payload", "images"]
            self._csv(path, self.records, fields); paths.append(path)
        else:
            report = output / f"{prefix}_report.csv"; failed = output / f"{prefix}_failed.csv"
            fields = REPORT_FIELDS + (["raw_specification_length", "parsed_fabric_collections", "parsed_color", "resolved_fabric",
                "resolved_color", "normalized_color", "baserow_fabric_updated", "baserow_color_updated"] if self.specification_mode else [])
            self._csv(report, self.records, fields); self._csv(failed, [r for r in self.records if r["status"] == "failed"], fields)
            paths.extend([report, failed])
        summary = output / f"final_{prefix}_report.txt"
        summary.write_text("\n".join(f"{k}: {v}" for k, v in asdict(self.stats).items()) + "\n", encoding="utf-8")
        paths.append(summary); return paths

    @staticmethod
    def _csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fields, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
