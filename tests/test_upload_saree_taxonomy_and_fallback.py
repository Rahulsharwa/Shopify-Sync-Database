from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from create_shopify_products_from_upload_saree import RunState, UploadSareeCreator


ROOT = Path(__file__).resolve().parents[1]


def make_creator() -> UploadSareeCreator:
    instance = object.__new__(UploadSareeCreator)
    instance.config = json.loads(
        (ROOT / "config" / "upload_saree_product_create_config.json").read_text(
            encoding="utf-8"
        )
    )
    instance.collection_map = json.loads(
        (ROOT / "config" / "upload_saree_collection_map.json").read_text(
            encoding="utf-8"
        )
    )
    instance.fields = {
        name: {"name": name} for name in instance.config["field_ids"]
    }
    instance.collection_cache = {}
    instance.state = RunState()
    instance.max_clean_tags = 6
    instance._taxonomy_entries = []
    instance._taxonomy_alias_index = {}
    instance._taxonomy_validation_by_name = {}
    instance._load_taxonomy_index()
    instance.state.alias_collisions = list(instance._alias_collisions_raw)
    instance.logger = Mock()
    return instance


def make_row(category: str = "Tussar silk") -> dict:
    return {
        "id": 1,
        "Product Title": "Blue Saree",
        "Product Code": "SKU-1",
        "Category": category,
        "Price": "1000",
        "catalog": "",
        "Descriptions": "Blue saree with a geometric pattern.",
        "Generation Status": {"value": "Approved"},
        "SHOPIFY Notes": "Approved",
    }


def valid_ai_result() -> dict:
    description = "".join(
        (
            "<h2>Introduction</h2><p>Blue Saree.</p>",
            "<h2>Fabric &amp; Craftsmanship</h2><p>Source details.</p>",
            "<h2>Styling &amp; Occasion</h2><p>See images.</p>",
            "<h2>Product Highlights</h2><p>Product Code: SKU-1</p>",
            "<h2>Material &amp; Wash Care</h2><p>Not specified.</p>",
            "<h2>Note</h2><p>Source record.</p>",
        )
    )
    return {
        "title": "Blue Saree SKU-1",
        "description_html": description,
        "seo_title": "Blue Saree",
        "seo_description": "Blue Saree SKU-1",
        "image_alt_text": "Blue Saree Front View",
        "tags": ["Tussar Silk Saree", "Saree"],
        "product_highlights": {
            "fabric": "",
            "zari": "",
            "colour": "Blue",
            "pattern": "Geometric",
            "border": "",
            "technique": "",
            "weave": "",
            "occasion": "",
            "product_code": "SKU-1",
        },
        "warnings": [],
    }


class UploadSareeTaxonomyTests(unittest.TestCase):
    def test_all_requested_categories_resolve_exact_lowercase_and_whitespace(self) -> None:
        instance = make_creator()
        # The supplied prompt says 25, but its five sections enumerate 26 names.
        self.assertEqual(len(instance._taxonomy_entries), 26)
        for entry in instance._taxonomy_entries:
            canonical = entry["canonical_name"]
            with self.subTest(canonical=canonical):
                self.assertEqual(instance.resolve_category(canonical), entry)
                self.assertEqual(instance.resolve_category(canonical.lower()), entry)
                padded = "  " + canonical.replace(" ", "   ") + "  "
                self.assertEqual(instance.resolve_category(padded), entry)

    def test_every_explicit_alias_resolves_without_collisions(self) -> None:
        instance = make_creator()
        self.assertEqual(instance.state.alias_collisions, [])
        for entry in instance._taxonomy_entries:
            for alias in entry["aliases"]:
                with self.subTest(alias=alias):
                    self.assertEqual(
                        instance.resolve_category(alias)["canonical_name"],
                        entry["canonical_name"],
                    )

    def test_current_baserow_aliases_resolve(self) -> None:
        instance = make_creator()
        expected = {
            "Banarasi silk": "Banarasi Silk",
            "Tussar silk": "Tussar Silk Saree",
            "Kanjivaram Silks": "Pure Kanjeevaram",
            "Kanjivaram silk": "Pure Kanjeevaram",
            "Soft silk": "Soft Silk Sarees",
            "Gadwal Handloom": "Gadwal Sarees",
            "Mysore Crepe": "Mysore Crepe Sarees",
        }
        for raw, canonical in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    instance.resolve_category(raw)["canonical_name"], canonical
                )

    def test_supported_ampersand_and_slash_normalization(self) -> None:
        instance = make_creator()
        self.assertEqual(
            instance.resolve_category("Patola and Orissa Silk")["canonical_name"],
            "Patola & Orissa Silk",
        )
        self.assertEqual(
            instance.resolve_category("Ikat/Pochampally")["canonical_name"],
            "Ikat / Pochampally",
        )
        self.assertEqual(
            instance.resolve_category("Linen and Kota Silk")["canonical_name"],
            "Linen & Kota Silk",
        )

    def test_printed_tussar_is_distinct_from_plain_tussar(self) -> None:
        instance = make_creator()
        self.assertEqual(
            instance.resolve_category("Printed Tussar Silk Saree")["canonical_name"],
            "Printed Tussar Silk",
        )
        self.assertEqual(
            instance.resolve_category("Tussar Silk Saree")["canonical_name"],
            "Tussar Silk Saree",
        )

    def test_unknown_category_is_not_fuzzy_matched(self) -> None:
        instance = make_creator()
        self.assertIsNone(instance.resolve_category("Tussar-inspired synthetic"))
        self.assertIsNone(instance.resolve_category("Approved"))

    def test_verified_collection_ids_and_one_intentional_missing_collection(self) -> None:
        instance = make_creator()
        nodes = [
            {
                "id": entry["shopify_collection_id"],
                "title": entry["shopify_collection_title"],
            }
            for entry in instance._taxonomy_entries
            if entry.get("shopify_collection_id")
        ]
        instance.fetch_shopify_collections = Mock(return_value=nodes)
        validation = instance.validate_taxonomy_collections()
        self.assertEqual(len(validation), 26)
        self.assertEqual(
            sum(record["Status"] == "resolved" for record in validation), 25
        )
        missing = [record for record in validation if record["Status"] != "resolved"]
        self.assertEqual(missing[0]["Canonical Category"], "Wedding Kanchipuram")
        self.assertEqual(missing[0]["Status"], "canonical_collection_not_found")

    def test_canonical_category_is_first_tag_and_tags_are_clean(self) -> None:
        instance = make_creator()
        tags = instance.clean_tags(
            make_row("Banarasi silk"),
            ai_tags=["Banarasi Silk", "Saree", "source:upload_saree", "Geometric"],
        )
        self.assertEqual(tags[0], "Banarasi Silk")
        self.assertLessEqual(len(tags), 6)
        self.assertFalse(any(":" in tag for tag in tags))
        self.assertEqual(len(tags), len({tag.casefold() for tag in tags}))

    def test_missing_price_uses_stable_reason(self) -> None:
        instance = make_creator()
        source = make_row()
        source["Price"] = ""
        self.assertEqual(instance.missing_required_fields(source), ["missing_price"])


class UploadSareeFallbackTests(unittest.TestCase):
    def configured_creator(self) -> UploadSareeCreator:
        instance = make_creator()
        instance.use_image_input = True
        instance.openrouter_max_attempts = 3
        instance.openrouter_retry_base_seconds = 0
        return instance

    def test_openrouter_success_path(self) -> None:
        instance = self.configured_creator()
        instance.openrouter = Mock()
        instance.openrouter.generate_product_copy.return_value = valid_ai_result()
        result = instance.generate_ai(make_row(), "SKU-1", [])
        self.assertEqual(result["_ai_provider"], "openrouter")
        self.assertFalse(result["_fallback_content_used"])
        self.assertEqual(instance.state.ai_success, 1)
        self.assertEqual(instance.state.ai_failed, 0)

    def test_timeout_http_and_malformed_failures_use_factual_fallback(self) -> None:
        failures = (
            requests.Timeout("timeout"),
            requests.HTTPError("400 bad request"),
            ValueError("malformed JSON"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                instance = self.configured_creator()
                instance.openrouter = Mock()
                instance.openrouter.generate_product_copy.side_effect = failure
                result = instance.generate_ai(make_row(), "SKU-1", [])
                self.assertEqual(instance.openrouter.generate_product_copy.call_count, 3)
                self.assertEqual(result["_ai_provider"], "fallback")
                self.assertTrue(result["_openrouter_failed"])
                self.assertTrue(result["_fallback_content_used"])
                self.assertEqual(instance.state.ai_failed, 1)
                self.assertEqual(instance.state.fallback_success, 1)

    def test_fallback_never_invents_unsupported_facts(self) -> None:
        instance = self.configured_creator()
        instance.openrouter = None
        source = make_row("Cotton Saree")
        source["Descriptions"] = "Blue geometric saree."
        result = instance.generate_ai(source, "SKU-1", [])
        html = result["description_html"].casefold()
        for unsupported in (
            "100% silk", "pure silk", "handloom", "handwoven", "zari",
            "mulberry", "wedding wear", "festival wear", "dry clean",
            "blouse included",
        ):
            self.assertNotIn(unsupported, html)
        self.assertIn("sku-1", html)
        self.assertEqual(result["tags"][:2], ["Cotton Saree", "Saree"])
        self.assertEqual(result["product_highlights"]["product_code"], "SKU-1")

    def test_over_limit_prepared_media_verifies_against_prepared_output(self) -> None:
        instance = self.configured_creator()
        prepared = SimpleNamespace(
            final_dimensions="1152x2048",
            final_size_bytes=12_395_440,
            sha256="prepared-sha256",
            mime_type="image/jpeg",
        )
        expected = instance.prepared_media_expectations(prepared)
        self.assertEqual(expected["Expected Shopify Size Bytes"], 12_395_440)
        self.assertEqual(expected["Expected Shopify Width"], 1152)
        self.assertEqual(expected["Expected Shopify Height"], 2048)
        self.assertEqual(expected["Expected Shopify SHA256"], "prepared-sha256")
        record = {
            **expected,
            "Baserow Size Bytes": 47_635_615,
            "Baserow Width": 1152,
            "Baserow Height": 2048,
            "Compressed yes/no": "yes",
        }
        instance.verify_source_metadata = True
        instance.verify_source_hash = False
        instance.fail_on_source_mismatch = True
        instance.require_target_dimensions = False
        instance.verify_media_source(
            record,
            {
                "status": "READY",
                "image": {"width": 1152, "height": 2048, "url": "https://cdn/image.jpg"},
                "originalSource": {"fileSize": 12_395_440, "url": ""},
            },
        )
        self.assertEqual(record["Source Metadata Verified"], "yes")


if __name__ == "__main__":
    unittest.main()
