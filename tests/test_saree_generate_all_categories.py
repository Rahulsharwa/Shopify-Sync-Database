from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import saree_image_sync
from saree_image_sync import SareeImageSync


class SareeGenerateAllCategoriesTests(unittest.TestCase):
    def test_sync_all_mode_selects_all_mapped_tables_and_disables_product_creation(self) -> None:
        env = {
            "SYNC_ALL_SAREE_GENERATE_CATEGORIES": "true",
            "PROCESS_ALL_SAREE_CATALOGS": "false",
            "MAX_PRODUCTS": "5",
            "BASEROW_API_BASE": "https://api.baserow.io",
            "BASEROW_TOKEN": "token",
            "SHOPIFY_STORE_DOMAIN": "example.myshopify.com",
            "SHOPIFY_ADMIN_ACCESS_TOKEN": "shpat_test",
            "SHOPIFY_API_VERSION": "2026-04",
        }
        with patch.dict("os.environ", env, clear=False):
            sync = SareeImageSync()
        self.assertTrue(sync.sync_all_saree_generate_categories)
        self.assertFalse(sync.product_creation_allowed)
        self.assertIsNone(sync.max_products)
        self.assertIn(948083, sync.include_ids)
        self.assertIn(935212, sync.include_ids)
        self.assertNotIn(935200, sync.include_ids)

    def test_all_category_report_marks_missing_sku_without_product_create(self) -> None:
        row = SareeImageSync.all_categories_report_row(
            {
                "table_name": "Kanjivaram Silks",
                "table_id": 948083,
                "row_id": 1,
                "product_code": "SKU-1",
                "skip_reason": "Shopify product not found by SKU",
                "error": "RuntimeError: Shopify product not found by SKU",
                "images_uploaded": 0,
                "duplicates_skipped": 0,
                "front_present": "yes",
                "main_image_set": "no",
                "generation_status_before": "Approved",
                "new_generation_status": "Approved",
            }
        )
        self.assertEqual(row["Result"], "shopify_sku_not_found")
        self.assertEqual(row["Total Media Uploaded"], 0)

    def test_source_does_not_contain_product_create_mutation(self) -> None:
        source = Path(saree_image_sync.__file__).read_text(encoding="utf-8")
        self.assertNotIn("productCreate(", source)
        self.assertNotIn("mutation productCreate", source)


if __name__ == "__main__":
    unittest.main()
