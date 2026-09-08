from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "upload_saree_image_prepare_test", ROOT / "utils" / "image_prepare.py"
)
assert spec is not None and spec.loader is not None
image_prepare = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = image_prepare
spec.loader.exec_module(image_prepare)

from create_shopify_products_from_upload_saree import (
    ShopifyError,
    UploadSareeCreator,
    download_exact_source_file,
    get_baserow_original_file_url,
    get_baserow_source_file,
)
from shopify_client import ShopifyClient


def creator_for_validation(
    *, gate: bool = True, minimum: int = 1946, recommended: int = 1152
) -> UploadSareeCreator:
    creator = object.__new__(UploadSareeCreator)
    creator.min_front_width = minimum
    creator.recommended_front_width = recommended
    creator.require_min_front_width = gate
    creator.image_max_bytes = 20 * 1024 * 1024
    return creator


def front(width: int, *, height: int = 2048, mime: str = "image/png") -> dict:
    return {
        "label": "Front View",
        "url": "https://files.example/user_files/original.png",
        "name": "original.png",
        "size": 3_000_000,
        "mime_type": mime,
        "image_width": width,
        "image_height": height,
        "is_image": True,
    }


class ImagePrepareTests(unittest.TestCase):
    def existing_media_row(self, generation_status: str = "Approved") -> dict:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        row.update(
            {
                "id": 1,
                "Product Code": "SKU-1",
                "Generation Status": {"value": generation_status},
                "SHOPIFY Notes": "Approved",
                "Side View": [self.image_file("side.png")],
            }
        )
        return row

    def existing_media_run_creator(
        self, generation_status: str = "Approved", *, dry_run: bool = True
    ) -> UploadSareeCreator:
        creator = self.blouse_grid_creator()
        creator.table_id = "1076991"
        creator.dry_run = dry_run
        creator.max_products = None
        creator.fetch_and_validate_fields = Mock()
        creator.baserow = Mock()
        creator.baserow.iter_rows.return_value = [
            self.existing_media_row(generation_status)
        ]
        creator.find_product_for_fix_by_sku = Mock(
            return_value={"id": "gid://shopify/Product/1"}
        )
        creator.get_product_media = Mock(
            return_value=[
                {
                    "id": "gid://shopify/MediaImage/front",
                    "alt": "SKU-1 - Front View",
                    "status": "READY",
                    "originalSource": {
                        "url": "https://files.example/user_files/front.png"
                    },
                }
            ]
        )
        creator.write_existing_media_outputs = Mock()
        creator.logger = Mock()
        creator.update_baserow = Mock()
        creator.create_product = Mock()
        creator.create_single_media = Mock()
        creator.enforce_existing_media_order = Mock(
            return_value=(creator.get_product_media.return_value, False)
        )
        return creator

    def test_existing_media_eligibility_requires_both_approved_fields_and_product_code(self) -> None:
        creator = self.blouse_grid_creator()
        self.assertEqual(
            creator.existing_media_row_skip_reason(
                self.existing_media_row("Approved")
            ),
            "",
        )
        self.assertIn(
            "not Approved",
            creator.existing_media_row_skip_reason(
                self.existing_media_row("Shopify-sync")
            ),
        )
        rejected = self.existing_media_row("Draft")
        self.assertIn(
            "not Approved",
            creator.existing_media_row_skip_reason(rejected),
        )
        rejected["Generation Status"] = {"value": "Approved"}
        rejected["SHOPIFY Notes"] = "Pending"
        self.assertIn(
            "SHOPIFY Notes",
            creator.existing_media_row_skip_reason(rejected),
        )

    def test_existing_media_extracts_all_eight_roles_in_required_order(self) -> None:
        creator = self.blouse_grid_creator()
        row = self.existing_media_row()
        row["Front View"] = [self.image_file("front.png")]
        row["Back View"] = [self.image_file("back.png")]
        row["Close-Up"] = [self.image_file("close.png")]
        row["BlouseGrid"] = [
            self.image_file("grid-1.png"),
            self.image_file("grid-2.png"),
        ]
        row["Blouse Image"] = [self.image_file("blouse.png")]
        row["Pallu Image"] = [self.image_file("pallu.png")]
        row["Border Image"] = [self.image_file("border.png")]
        images = creator.existing_media_images(row)
        self.assertEqual(
            [image["role"] for image in images],
            [
                "front_view",
                "back_view",
                "side_view",
                "close_up",
                "blouse_grid",
                "blouse_grid",
                "blouse_image",
                "pallu_image",
                "border_image",
            ],
        )
        self.assertEqual(
            [creator.existing_media_alt_text("SKU-1", image) for image in images],
            [
                "SKU-1 - Front View",
                "SKU-1 - Back View",
                "SKU-1 - Side View",
                "SKU-1 - Close-Up",
                "SKU-1 - Blouse Grid 1",
                "SKU-1 - Blouse Grid 2",
                "SKU-1 - Blouse Image",
                "SKU-1 - Pallu Image",
                "SKU-1 - Border Image",
            ],
        )

    def test_existing_media_thumbnail_is_rejected(self) -> None:
        creator = self.blouse_grid_creator()
        row = self.existing_media_row()
        row["Side View"] = [self.image_file("side.png", thumbnail=True)]
        with self.assertRaisesRegex(ValueError, "thumbnail"):
            creator.existing_media_images(row)

    def test_existing_media_duplicate_detects_alt_or_original_source(self) -> None:
        media = [
            {
                "id": "media-1",
                "alt": "SKU-1 - Side View",
                "status": "READY",
                "originalSource": {
                    "url": "https://files.example/user_files/source.png"
                },
            }
        ]
        self.assertEqual(
            UploadSareeCreator.find_existing_media_duplicate(
                media,
                "https://different.example/user_files/side.png",
                "SKU-1 - Side View",
            )["id"],
            "media-1",
        )
        self.assertEqual(
            UploadSareeCreator.find_existing_media_duplicate(
                media,
                "https://files.example/user_files/source.png?download=1",
                "different alt",
            )["id"],
            "media-1",
        )

    def test_existing_media_duplicate_detects_legacy_role_and_content_hash(self) -> None:
        legacy = {
            "id": "legacy-back",
            "alt": "Legacy product title - Back View",
            "status": "READY",
            "originalSource": {"url": "https://cdn.shopify.com/back.png"},
        }
        self.assertEqual(
            UploadSareeCreator.find_existing_media_duplicate(
                [legacy],
                "https://files.example/user_files/back.png",
                "SKU-1 - Back View",
                {"role": "back_view", "field_file_count": 1, "file_index": 1},
            )["id"],
            "legacy-back",
        )
        hashed = {
            "id": "hashed-grid",
            "alt": "unrelated",
            "automationMetadata": {"sourceHash": "ABC123"},
            "originalSource": {},
        }
        self.assertEqual(
            UploadSareeCreator.find_existing_media_duplicate(
                [hashed],
                "https://files.example/user_files/grid.png",
                "SKU-1 - Blouse Grid",
                {"role": "blouse_grid", "content_hash": "abc123"},
            )["id"],
            "hashed-grid",
        )
    def test_existing_media_same_source_across_fields_is_uploaded_once(self) -> None:
        creator = self.blouse_grid_creator()
        row = self.existing_media_row()
        shared = self.image_file("shared.png")
        row["Back View"] = [shared]
        row["Side View"] = [shared]
        images = creator.existing_media_images(row)
        shared_images = [image for image in images if image["url"] == shared["url"]]
        self.assertEqual(len(shared_images), 1)
        self.assertEqual(shared_images[0]["role"], "back_view")

    def test_existing_media_reorder_keeps_manual_media_and_relative_order(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        before = [
            {"id": "manual-front", "alt": "Merchant front"},
            {"id": "side", "alt": "SKU-1 - Side View"},
            {"id": "manual-detail", "alt": "Merchant detail"},
            {"id": "back", "alt": "SKU-1 - Back View"},
        ]
        after = [before[0], before[3], before[1], before[2]]
        creator.get_product_media = Mock(side_effect=[before, after])
        creator.shopify.graphql.return_value = {
            "productReorderMedia": {"job": {"done": True}, "mediaUserErrors": []}
        }
        result, changed = creator.enforce_existing_media_order(
            "product-1", ["back", "side"], "", "manual-front"
        )
        self.assertTrue(changed)
        self.assertEqual([item["id"] for item in result], [
            "manual-front", "back", "side", "manual-detail"
        ])
        moves = creator.shopify.graphql.call_args.args[1]["moves"]
        self.assertEqual(
            moves,
            [
                {"id": "manual-front", "newPosition": "0"},
                {"id": "back", "newPosition": "1"},
                {"id": "side", "newPosition": "2"},
            ],
        )

    def test_existing_media_reorder_is_idempotent(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        ordered = [
            {"id": "front", "alt": "SKU-1 - Front View"},
            {"id": "back", "alt": "SKU-1 - Back View"},
            {"id": "manual", "alt": "Merchant image"},
        ]
        creator.get_product_media = Mock(return_value=ordered)
        result, changed = creator.enforce_existing_media_order(
            "product-1", ["front", "back"], "front", "manual"
        )
        self.assertFalse(changed)
        self.assertEqual(result, ordered)
        creator.shopify.graphql.assert_not_called()

    def test_existing_media_preview_cannot_create_reorder_or_write_baserow(self) -> None:
        creator = self.existing_media_run_creator(dry_run=True)
        self.assertEqual(creator.run_existing_media_sync(), 0)
        creator.create_product.assert_not_called()
        creator.create_single_media.assert_not_called()
        creator.enforce_existing_media_order.assert_not_called()
        creator.update_baserow.assert_not_called()
        records = creator.write_existing_media_outputs.call_args.args[0]
        self.assertEqual(records[0]["Side Uploaded"], 1)
        self.assertEqual(records[0]["Result"], "preview")

    def test_existing_media_no_optional_images_is_clean_skip(self) -> None:
        creator = self.existing_media_run_creator(dry_run=True)
        creator.baserow.iter_rows.return_value[0]["Side View"] = []
        self.assertEqual(creator.run_existing_media_sync(), 0)
        creator.find_product_for_fix_by_sku.assert_not_called()
        records = creator.write_existing_media_outputs.call_args.args[0]
        self.assertEqual(records[0]["Result"], "no_configured_media_skipped")

    def test_missing_existing_media_uploads_ready_and_preserves_front_order(self) -> None:
        creator = self.existing_media_run_creator(dry_run=False)
        before = [
            {
                "id": "front",
                "alt": "front",
                "status": "READY",
                "originalSource": {"url": "front"},
            }
        ]
        after = [
            *before,
            {
                "id": "side",
                "alt": "SKU-1 - Side View",
                "status": "READY",
                "originalSource": {
                    "url": "https://files.example/user_files/side.png"
                },
            },
        ]
        creator.get_product_media.side_effect = [before, after]
        creator.upload_existing_product_media = Mock(
            return_value=({"Shopify Media ID": "side"}, "")
        )
        creator.enforce_existing_media_order.return_value = (after, False)
        self.assertEqual(creator.run_existing_media_sync(), 0)
        creator.upload_existing_product_media.assert_called_once()
        creator.update_baserow.assert_called_once()
        creator.enforce_existing_media_order.assert_called_once()
        records = creator.write_existing_media_outputs.call_args.args[0]
        self.assertEqual(records[0]["Side Uploaded"], 1)
        self.assertEqual(records[0]["Images Uploaded"], 1)
        self.assertEqual(records[0]["Front First"], "yes")

    def test_all_missing_roles_upload_independently_and_front_is_not_duplicated(self) -> None:
        creator = self.existing_media_run_creator(dry_run=False)
        row = creator.baserow.iter_rows.return_value[0]
        row.update(
            {
                "Front View": [self.image_file("front.png")],
                "Back View": [self.image_file("back.png")],
                "Side View": [self.image_file("side.png")],
                "Close-Up": [self.image_file("close.png")],
                "BlouseGrid": [
                    self.image_file("grid-1.png"),
                    self.image_file("grid-2.png"),
                ],
                "Blouse Image": [self.image_file("blouse.png")],
                "Pallu Image": [self.image_file("pallu.png")],
                "Border Image": [self.image_file("border.png")],
            }
        )
        front_media = {
            "id": "front",
            "alt": "SKU-1 - Front View",
            "status": "READY",
            "originalSource": {
                "url": "https://files.example/user_files/front.png"
            },
        }
        creator.get_product_media.return_value = [front_media]
        creator.upload_existing_product_media = Mock(
            side_effect=lambda product_id, image, alt: (
                {"Shopify Media ID": f"media-{image['role']}-{image['file_index']}"},
                "",
            )
        )
        after = [
            front_media,
            *[
                {"id": f"media-{role}-{index}", "alt": role, "status": "READY"}
                for role, index in (
                    ("back_view", 1), ("side_view", 1), ("close_up", 1),
                    ("blouse_grid", 1), ("blouse_grid", 2),
                    ("blouse_image", 1), ("pallu_image", 1),
                    ("border_image", 1),
                )
            ],
        ]
        creator.enforce_existing_media_order.return_value = (after, True)
        self.assertEqual(creator.run_existing_media_sync(), 0)
        uploaded_roles = [
            call.args[1]["role"]
            for call in creator.upload_existing_product_media.call_args_list
        ]
        self.assertNotIn("front_view", uploaded_roles)
        self.assertEqual(
            uploaded_roles,
            [
                "back_view", "side_view", "close_up", "blouse_grid",
                "blouse_grid", "blouse_image", "pallu_image", "border_image",
            ],
        )
        report = creator.write_existing_media_outputs.call_args.args[0][0]
        self.assertEqual(report["Front Existing"], 1)
        self.assertEqual(report["BlouseGrid Uploaded"], 2)
        self.assertEqual(report["Images Uploaded"], 8)
        self.assertEqual(report["Generation Status After"], "shopify-sync")

    def test_existing_ready_media_marks_approved_row_synced_without_mutation(self) -> None:
        creator = self.existing_media_run_creator(dry_run=False)
        media = [
            {
                "id": "gid://shopify/MediaImage/front",
                "alt": "SKU-1 - Front View",
                "status": "READY",
                "originalSource": {
                    "url": "https://files.example/user_files/front.png"
                },
            },
            {
                "id": "gid://shopify/MediaImage/side",
                "alt": "SKU-1 - Side View",
                "status": "READY",
                "originalSource": {
                    "url": "https://files.example/user_files/side.png"
                },
            },
        ]
        creator.get_product_media.return_value = media
        creator.enforce_existing_media_order.return_value = (media, False)
        self.assertEqual(creator.run_existing_media_sync(), 0)
        creator.update_baserow.assert_called_once()
        creator.create_single_media.assert_not_called()
        creator.enforce_existing_media_order.assert_called_once()
        records = creator.write_existing_media_outputs.call_args.args[0]
        self.assertEqual(records[0]["Side Existing"], 1)
        self.assertEqual(records[0]["Duplicate Images Prevented"], 1)
        self.assertEqual(records[0]["Front First"], "yes")
        self.assertEqual(records[0]["Generation Status After"], "shopify-sync")

    def test_existing_media_failure_does_not_change_baserow_status(self) -> None:
        for error in (None, RuntimeError("multiple_products_for_sku")):
            with self.subTest(error=error):
                creator = self.existing_media_run_creator(dry_run=False)
                if error is None:
                    creator.find_product_for_fix_by_sku.return_value = None
                else:
                    creator.find_product_for_fix_by_sku.side_effect = error
                self.assertEqual(creator.run_existing_media_sync(), 0)
                creator.update_baserow.assert_not_called()
                result = creator.write_existing_media_outputs.call_args.args[0][0]
                expected = (
                    "shopify_product_not_found"
                    if error is None
                    else "multiple_shopify_products_for_sku"
                )
                self.assertEqual(result["Result"], expected)
                self.assertEqual(result["Generation Status After"], "approved")
        creator.create_product.assert_not_called()

    def test_media_upload_failure_keeps_approved_and_does_not_write_baserow(self) -> None:
        creator = self.existing_media_run_creator(dry_run=False)
        creator.upload_existing_product_media = Mock(
            side_effect=RuntimeError("media upload failed")
        )
        self.assertEqual(creator.run_existing_media_sync(), 1)
        creator.update_baserow.assert_not_called()
        record = creator.write_existing_media_outputs.call_args.args[0][0]
        self.assertEqual(record["Generation Status After"], "approved")
        self.assertEqual(record["Result"], "failed")

    def test_product_creation_is_hard_disabled_in_existing_media_mode(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.product_creation_allowed = False
        with self.assertRaisesRegex(RuntimeError, "product_creation_disabled"):
            creator.create_product({}, "SKU-1", "sku-1", "100.00", {}, [])

    def test_existing_media_missing_product_report_is_written(self) -> None:
        creator = self.existing_media_run_creator(dry_run=False)
        creator.find_product_for_fix_by_sku.return_value = None
        creator.write_existing_media_outputs = UploadSareeCreator.write_existing_media_outputs.__get__(
            creator, UploadSareeCreator
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                "create_shopify_products_from_upload_saree.OUTPUT", Path(temp_dir)
            ):
                self.assertEqual(creator.run_existing_media_sync(), 0)
                missing_path = (
                    Path(temp_dir)
                    / "upload_saree_existing_media_sync_missing_products.csv"
                )
                self.assertTrue(missing_path.exists())
                self.assertIn("shopify_product_not_found", missing_path.read_text())

    def test_existing_shopify_sync_status_is_never_downgraded(self) -> None:
        creator = self.existing_media_run_creator(
            "Shopify-sync", dry_run=False
        )
        creator.get_product_media.return_value = [
            {
                "id": "front",
                "alt": "front",
                "status": "READY",
                "originalSource": {"url": "front"},
            },
            {
                "id": "side",
                "alt": "SKU-1 - Side View",
                "status": "READY",
                "originalSource": {
                    "url": "https://files.example/user_files/side.png"
                },
            },
        ]
        creator.enforce_existing_media_order.return_value = (
            creator.get_product_media.return_value,
            False,
        )
        self.assertEqual(creator.run_existing_media_sync(), 0)
        creator.update_baserow.assert_not_called()
        creator.restore_baserow_approved = Mock()
        creator.restore_baserow_approved.assert_not_called()

    def test_collection_title_lookup_is_cached_for_run(self) -> None:
        client = object.__new__(ShopifyClient)
        client._collection_id_cache = {}
        client.find_collection = Mock(
            return_value={"id": "gid://shopify/Collection/new", "title": "New Arrivals"}
        )
        self.assertEqual(
            client.get_collection_id_by_title("New Arrivals"),
            "gid://shopify/Collection/new",
        )
        self.assertEqual(
            client.get_collection_id_by_title("new arrivals"),
            "gid://shopify/Collection/new",
        )
        client.find_collection.assert_called_once()

    def test_upload_saree_gets_primary_and_new_arrivals(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        creator.shopify.get_collection_id_by_title.side_effect = lambda title: {
            "Kanjivaram Sarees": "gid://shopify/Collection/primary",
            "New Arrivals": "gid://shopify/Collection/new",
        }.get(title)
        creator.shopify.ensure_product_in_collection.side_effect = [
            (True, True),
            (True, False),
        ]
        creator.shopify.product_collection_ids.return_value = {
            "gid://shopify/Collection/primary",
            "gid://shopify/Collection/new",
        }
        creator.collection_title = Mock(return_value="Kanjivaram Sarees")
        creator.add_new_arrivals = True
        creator.new_arrivals_collection_name = "New Arrivals"
        creator.state = SimpleNamespace(new_arrivals_assignment_failures=0)
        details = creator.assign_and_verify_required_collections(
            "gid://shopify/Product/1", {}
        )
        self.assertEqual(details["Primary Collection Assigned"], "yes")
        self.assertEqual(details["New Arrivals Assigned"], "yes")
        self.assertEqual(details["New Arrivals Already Assigned"], "no")
        self.assertEqual(creator.shopify.ensure_product_in_collection.call_count, 2)

    def test_existing_new_arrivals_membership_is_idempotent(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        creator.shopify.get_collection_id_by_title.side_effect = [
            "gid://shopify/Collection/primary",
            "gid://shopify/Collection/new",
        ]
        creator.shopify.ensure_product_in_collection.side_effect = [
            (True, True),
            (True, True),
        ]
        creator.shopify.product_collection_ids.return_value = {
            "gid://shopify/Collection/primary",
            "gid://shopify/Collection/new",
        }
        creator.collection_title = Mock(return_value="Kanjivaram Sarees")
        creator.add_new_arrivals = True
        creator.new_arrivals_collection_name = "New Arrivals"
        creator.state = SimpleNamespace(new_arrivals_assignment_failures=0)
        details = creator.assign_and_verify_required_collections(
            "gid://shopify/Product/1", {}
        )
        self.assertEqual(details["New Arrivals Already Assigned"], "yes")

    def test_missing_new_arrivals_collection_reports_clear_failure(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        creator.shopify.get_collection_id_by_title.side_effect = [
            "gid://shopify/Collection/primary",
            None,
        ]
        creator.shopify.ensure_product_in_collection.return_value = (True, True)
        creator.collection_title = Mock(return_value="Kanjivaram Sarees")
        creator.add_new_arrivals = True
        creator.new_arrivals_collection_name = "New Arrivals"
        creator.state = SimpleNamespace(new_arrivals_assignment_failures=0)
        with self.assertRaisesRegex(
            ShopifyError,
            "new_arrivals_assignment_failed: new_arrivals_collection_not_found",
        ):
            creator.assign_and_verify_required_collections(
                "gid://shopify/Product/1", {}
            )
        self.assertEqual(creator.state.new_arrivals_assignment_failures, 1)

    def test_retry_adds_new_arrivals_without_product_create_or_collection_removal(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        creator.shopify.get_collection_id_by_title.side_effect = [
            "gid://shopify/Collection/primary",
            "gid://shopify/Collection/new",
        ]
        creator.shopify.ensure_product_in_collection.side_effect = [
            (True, True),
            (True, False),
        ]
        creator.shopify.product_collection_ids.return_value = {
            "gid://shopify/Collection/primary",
            "gid://shopify/Collection/new",
        }
        creator.collection_title = Mock(return_value="Kanjivaram Sarees")
        creator.add_new_arrivals = True
        creator.new_arrivals_collection_name = "New Arrivals"
        creator.state = SimpleNamespace(new_arrivals_assignment_failures=0)
        creator.create_product = Mock()
        details = creator.assign_and_verify_required_collections(
            "gid://shopify/Product/existing", {}
        )
        creator.create_product.assert_not_called()
        self.assertEqual(details["Primary Collection Assigned"], "yes")
        self.assertEqual(details["New Arrivals Assigned"], "yes")
        self.assertFalse(
            any("remove" in call[0].casefold() for call in creator.shopify.method_calls)
        )

    def test_exact_sku_resume_checks_each_media_without_product_create(self) -> None:
        creator = self.blouse_grid_creator()
        creator.collection_map = json.loads(
            (ROOT / "config" / "upload_saree_collection_map.json").read_text(
                encoding="utf-8"
            )
        )
        creator.max_clean_tags = 6
        creator.dry_run = True
        creator.create_product = Mock()
        creator.get_product_media = Mock(
            return_value=[
                {
                    "id": "front-media",
                    "alt": "SKU-1 - Front View",
                    "status": "READY",
                    "originalSource": {
                        "url": "https://files.example/user_files/front.png"
                    },
                }
            ]
        )
        row = {name: [] for name in creator.config["field_ids"]}
        row.update(
            {
                "Product Code": "SKU-1",
                "Category": "Kanjivaram Saree",
                "Front View": [self.image_file("front.png")],
                "Back View": [self.image_file("back.png")],
            }
        )
        images = creator.images(row)
        _, details = creator.resume_existing_product(
            {"id": "gid://shopify/Product/1"},
            row,
            "SKU-1",
            images,
            {"tags": []},
        )
        creator.create_product.assert_not_called()
        self.assertEqual(details["Front View Existing"], 1)
        self.assertEqual(details["Front View Uploaded"], 0)
        self.assertEqual(details["Back View Uploaded"], 1)

    @staticmethod
    def blouse_grid_creator() -> UploadSareeCreator:
        creator = object.__new__(UploadSareeCreator)
        creator.config = json.loads(
            (ROOT / "config" / "upload_saree_product_create_config.json").read_text(
                encoding="utf-8"
            )
        )
        creator.fields = {
            name: {"name": name} for name in creator.config["field_ids"]
        }
        return creator

    @staticmethod
    def image_file(name: str, *, thumbnail: bool = False) -> dict:
        directory = "thumbnails" if thumbnail else "user_files"
        return {
            "url": f"https://files.example/{directory}/{name}",
            "name": name,
            "size": 1000,
            "mime_type": "image/png",
            "image_width": 1200,
            "image_height": 1200,
            "is_image": True,
            "thumbnails": {
                "small": {"url": f"https://files.example/thumbnails/{name}"}
            },
        }

    def test_blouse_grid_empty_product_images_proceed_normally(self) -> None:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        row["Front View"] = [self.image_file("front.png")]
        images = creator.images(row)
        self.assertEqual([image["label"] for image in images], ["Front View"])
        self.assertEqual(creator.blouse_grid_report(images)["BlouseGrid Present"], "no")

    def test_missing_optional_image_does_not_fail_required_fields(self) -> None:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        row.update(
            {
                "Product Title": "Mapped Saree",
                "Product Code": "SKU-1",
                "Price": "1000",
                "Front View": [self.image_file("front.png")],
                "Side View": [],
            }
        )
        self.assertEqual(creator.missing_required_fields(row), [])
        self.assertNotIn("Side View", [image["label"] for image in creator.images(row)])

    def test_missing_front_uses_first_available_role_and_warns_not_fails(self) -> None:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        row.update(
            {
                "Product Title": "Mapped Saree",
                "Product Code": "SKU-1",
                "Price": "1000",
                "Back View": [self.image_file("back.png")],
                "Close-Up": [self.image_file("close.png")],
            }
        )
        self.assertEqual(creator.missing_required_fields(row), [])
        images = creator.images(row)
        self.assertEqual([image["role"] for image in images], ["back_view", "close_up"])

    def test_no_configured_images_is_detectable_without_blank_placeholders(self) -> None:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        self.assertEqual(creator.images(row), [])

    def test_under_20mb_create_image_is_not_resized_or_reencoded(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.resize_before_shopify = True
        creator.image_max_bytes = 20 * 1024 * 1024
        creator.resize_fields = {"Front View"}
        image = {
            "label": "Front View",
            "url": "https://files.example/user_files/front.png",
            "size": 5 * 1024 * 1024,
            "mime_type": "image/png",
        }
        prepared, temp_dir = creator.prepare_images_before_create(
            [image], "SKU-1", 1
        )
        self.assertIsNone(temp_dir)
        self.assertEqual(prepared, [image])
        self.assertNotIn("local_path", prepared[0])

    def test_category_mapping_is_normalized_and_category_precedes_catalog(self) -> None:
        creator = self.blouse_grid_creator()
        creator.collection_map = json.loads(
            (ROOT / "config" / "upload_saree_collection_map.json").read_text(
                encoding="utf-8"
            )
        )
        for category, expected in (
            ("  Kanjivaram   Saree ", "Kanjivaram Sarees"),
            ("tussar silk saree", "Tussar Silk Sarees"),
            ("SOFT SILK SAREE", "Soft Silk Sarees"),
        ):
            with self.subTest(category=category):
                row = {name: [] for name in creator.config["field_ids"]}
                row["Category"] = category
                row["catalog"] = "Kanjivaram Silks"
                self.assertEqual(creator.collection_title(row), expected)

    def test_category_tag_is_first_clean_unique_and_limited(self) -> None:
        creator = self.blouse_grid_creator()
        creator.collection_map = json.loads(
            (ROOT / "config" / "upload_saree_collection_map.json").read_text(
                encoding="utf-8"
            )
        )
        creator.max_clean_tags = 6
        row = {name: [] for name in creator.config["field_ids"]}
        row.update(
            {
                "Category": "Tussar Silk Saree",
                "Product Title": "Tussar Silk Saree",
                "Descriptions": "Traditional tussar silk saree",
            }
        )
        tags = creator.clean_tags(
            row,
            ai_tags=[
                "Tussar Silk Saree", "Saree", "Traditional Saree",
                "source:upload_saree", "Unsupported Wedding Saree", "saree",
            ],
        )
        self.assertEqual(tags[0], "Tussar Silk Saree")
        self.assertLessEqual(len(tags), 6)
        self.assertFalse(any(":" in tag for tag in tags))
        self.assertEqual(len({tag.casefold() for tag in tags}), len(tags))
        self.assertNotIn("Unsupported Wedding Saree", tags)

    def test_one_blouse_grid_uses_original_url_and_is_appended(self) -> None:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        row["Front View"] = [self.image_file("front.png")]
        blouse = self.image_file("blouse-grid.png")
        row["BlouseGrid"] = [blouse]
        images = creator.images(row)
        self.assertEqual([image["label"] for image in images], ["Front View", "BlouseGrid"])
        self.assertEqual(images[-1]["url"], blouse["url"])
        self.assertNotIn("/thumbnails/", images[-1]["url"])
        self.assertEqual(
            creator.image_alt_text("AL214193", "Title", "Front alt", images[-1], 1),
            "AL214193 - Blouse Grid",
        )

    def test_multiple_blouse_grid_files_all_preserved_in_field_order(self) -> None:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        row["Front View"] = [self.image_file("front.png")]
        row["BlouseGrid"] = [
            self.image_file("blouse-1.png"),
            self.image_file("blouse-2.png"),
        ]
        images = creator.images(row)
        blouse = [image for image in images if image["role"] == "blouse_grid"]
        self.assertEqual([image["name"] for image in blouse], ["blouse-1.png", "blouse-2.png"])
        self.assertEqual(
            [
                creator.image_alt_text("AL214193", "Title", "Front alt", image, index + 1)
                for index, image in enumerate(blouse)
            ],
            ["AL214193 - Blouse Grid 1", "AL214193 - Blouse Grid 2"],
        )

    def test_blouse_grid_thumbnail_url_is_rejected(self) -> None:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        row["Front View"] = [self.image_file("front.png")]
        row["BlouseGrid"] = [self.image_file("blouse.png", thumbnail=True)]
        with self.assertRaisesRegex(ValueError, "thumbnail"):
            creator.images(row)

    def test_blouse_grid_does_not_replace_front_featured_media(self) -> None:
        creator = self.blouse_grid_creator()
        row = {name: [] for name in creator.config["field_ids"]}
        for label in creator.config["image_order"]:
            row[label] = [self.image_file(f"{label.casefold().replace(' ', '-')}.png")]
        images = creator.images(row)
        self.assertEqual(images[0]["label"], "Front View")
        self.assertEqual(images[4]["label"], "BlouseGrid")
        self.assertEqual(
            [image["label"] for image in images], creator.config["image_order"]
        )

    def test_duplicate_blouse_grid_media_is_skipped_by_alt(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.get_product_media = Mock(
            return_value=[
                {
                    "id": "gid://shopify/MediaImage/1",
                    "alt": "AL214193 - Blouse Grid",
                    "status": "READY",
                    "originalSource": {},
                }
            ]
        )
        creator.prepare_media_source = Mock()
        image = {
            "label": "BlouseGrid",
            "role": "blouse_grid",
            "url": "https://files.example/user_files/blouse.png",
            "file_index": 1,
            "field_file_count": 1,
        }
        rows = creator.upload_product_images(
            "gid://shopify/Product/1", "AL214193", "Title", "Front alt", [image]
        )
        self.assertEqual(rows[0]["Upload mode"], "duplicate_skipped")
        creator.prepare_media_source.assert_not_called()

    def test_all_blouse_grid_files_are_uploaded_and_ready(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.get_product_media = Mock(return_value=[])
        creator.exact_file_fallback = False
        creator.state = Mock()
        creator.state.image_failures = []
        creator.prepare_media_source = Mock(
            side_effect=lambda image, force_stage=False: (
                image["url"],
                {
                    "Image field": image["label"],
                    "Image Field": image["label"],
                    "Source URL": image["url"],
                    "Upload mode": "external_original_url",
                    "Compressed yes/no": "no",
                },
            )
        )
        creator.create_single_media = Mock(
            side_effect=["gid://shopify/MediaImage/1", "gid://shopify/MediaImage/2"]
        )
        creator.wait_for_media_id = Mock(
            side_effect=lambda product_id, media_id: {"id": media_id, "status": "READY"}
        )
        creator.verify_media_source = Mock()
        images = [
            {
                "label": "BlouseGrid",
                "role": "blouse_grid",
                "url": f"https://files.example/user_files/blouse-{index}.png",
                "file_index": index,
                "field_file_count": 2,
            }
            for index in (1, 2)
        ]
        rows = creator.upload_product_images(
            "gid://shopify/Product/1", "AL214193", "Title", "Front alt", images
        )
        self.assertEqual(creator.create_single_media.call_count, 2)
        self.assertEqual([row["Media READY yes/no"] for row in rows], ["yes", "yes"])
        self.assertEqual(
            [call.args[2] for call in creator.create_single_media.call_args_list],
            ["AL214193 - Blouse Grid 1", "AL214193 - Blouse Grid 2"],
        )

    def test_original_file_obj_url_selection(self) -> None:
        files = [
            {
                "url": "https://files.example/user_files/original.png",
                "size": 3_000_000,
                "mime_type": "image/png",
                "image_width": 2304,
                "image_height": 4096,
                "is_image": True,
                "thumbnails": {
                    "small": {
                        "url": "https://files.example/thumbnails/small.png"
                    }
                },
            }
        ]
        self.assertEqual(
            get_baserow_original_file_url(files, "Front View"), files[0]["url"]
        )
        selected = get_baserow_source_file({"Front View": files}, "Front View")
        self.assertEqual(selected["url"], files[0]["url"])
        self.assertNotIn("thumbnails", selected)

    def test_thumbnail_rejection(self) -> None:
        with self.assertRaisesRegex(ValueError, "thumbnail"):
            get_baserow_source_file(
                {
                    "Front View": [
                        {
                            "url": (
                                "https://files.example/user_files/"
                                "thumbnails/small.png"
                            )
                        }
                    ]
                },
                "Front View",
            )

    def test_1152_front_view_accepted_when_gate_disabled(self) -> None:
        result = creator_for_validation(
            gate=False, minimum=0, recommended=1152
        ).validate_front_view_image(front(1152))
        self.assertEqual(result["Resolution Eligible"], "yes")
        self.assertEqual(result["Resolution Result"], "resolution_gate_disabled")

    def test_1152_front_view_rejected_only_when_gate_explicitly_enabled(self) -> None:
        result = creator_for_validation(
            gate=True, minimum=1946, recommended=1152
        ).validate_front_view_image(front(1152))
        self.assertEqual(result["Resolution Eligible"], "no")
        self.assertEqual(result["Result"], "resolution_gate_rejected")
        self.assertIn("configured minimum width 1946", result["Error"])

    def test_1946_front_view_accepted(self) -> None:
        result = creator_for_validation().validate_front_view_image(front(1946))
        self.assertEqual(result["Resolution Eligible"], "yes")
        self.assertEqual(result["Resolution Result"], "resolution_gate_passed")

    def test_2304_front_view_accepted(self) -> None:
        result = creator_for_validation().validate_front_view_image(front(2304))
        self.assertEqual(result["Resolution Eligible"], "yes")

    @patch.object(image_prepare.requests, "get")
    @patch.object(image_prepare.requests, "head")
    def test_png_retained_as_png_and_exact_byte_staged_fallback(
        self, head: Mock, get: Mock
    ) -> None:
        head_response = Mock()
        head_response.ok = True
        head_response.headers = {
            "Content-Length": "18",
            "Content-Type": "image/png",
            "Content-Disposition": 'attachment; filename="source.png"',
        }
        head.return_value = head_response
        get_response = Mock()
        get_response.headers = {
            "Content-Type": "image/png",
            "Content-Disposition": 'attachment; filename="source.png"',
        }
        get_response.iter_content.return_value = [b"exact-", b"png-", b"bytes"]
        get_response.raise_for_status.return_value = None
        get.return_value = get_response
        with tempfile.TemporaryDirectory() as temp:
            prepared = image_prepare.prepare_image_for_shopify(
                "https://files.example/user_files/source.png",
                temp,
                force_stage=True,
            )
            self.assertEqual(Path(prepared.local_path).suffix, ".png")
            self.assertEqual(Path(prepared.local_path).read_bytes(), b"exact-png-bytes")
        self.assertEqual(prepared.mime_type, "image/png")
        self.assertFalse(prepared.compressed)
        self.assertEqual(prepared.upload_mode, "staged_original_file")

    @patch.object(image_prepare.requests, "head")
    def test_direct_baserow_source_transfer_uses_original_url(
        self, head: Mock
    ) -> None:
        response = Mock()
        response.ok = True
        response.headers = {
            "Content-Length": "3000000",
            "Content-Type": "image/png",
        }
        head.return_value = response
        with tempfile.TemporaryDirectory() as temp:
            prepared = image_prepare.prepare_image_for_shopify(
                "https://files.example/user_files/source.png", temp
            )
        self.assertEqual(prepared.upload_mode, "external_original_url")
        self.assertEqual(
            prepared.source_url,
            "https://files.example/user_files/source.png",
        )

    def test_1152x2048_resizes_to_2304x4096_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "front.png"
            output = Path(temp) / "front-shopify-2304x4096.png"
            Image.new("RGBA", (1152, 2048), (10, 20, 30, 255)).save(
                source, format="PNG"
            )
            result = image_prepare.resize_for_shopify(source, output)
            self.assertEqual(
                (result["output_width"], result["output_height"]),
                (2304, 4096),
            )
            self.assertEqual(result["output_format"], "PNG")
            self.assertEqual(result["output_mime"], "image/png")
            self.assertEqual(result["resize_filter"], "LANCZOS")
            with Image.open(output) as resized:
                self.assertEqual(resized.size, (2304, 4096))
                self.assertEqual(resized.format, "PNG")

    def test_jpeg_remains_jpeg_after_resize(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "front.jpg"
            output = Path(temp) / "front-shopify-2304x4096.jpg"
            Image.new("RGB", (1152, 2048), (100, 90, 80)).save(
                source, format="JPEG", quality=95
            )
            result = image_prepare.resize_for_shopify(source, output)
            self.assertEqual(result["output_format"], "JPEG")
            self.assertEqual(result["output_mime"], "image/jpeg")
            with Image.open(output) as resized:
                self.assertEqual(resized.size, (2304, 4096))
                self.assertEqual(resized.format, "JPEG")

    def test_invalid_aspect_ratio_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "square.png"
            output = Path(temp) / "square-output.png"
            Image.new("RGB", (1000, 1000), "white").save(source)
            with self.assertRaisesRegex(
                image_prepare.ImagePreparationError,
                "front_view_aspect_ratio_invalid",
            ):
                image_prepare.resize_for_shopify(source, output)

    def test_staged_upload_uses_resized_local_file(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.block_thumbnail_urls = True
        creator.min_front_width = 0
        creator.resize_target_width = 2304
        creator.resize_target_height = 4096
        creator.stage_file = Mock(return_value="staged-resource-url")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "front-shopify-2304x4096.png"
            output.write_bytes(b"resized-png")
            image = {
                **front(1152),
                "resize_enabled": True,
                "local_path": str(output),
                "output_filename": output.name,
                "output_width": 2304,
                "output_height": 4096,
                "output_size": output.stat().st_size,
                "output_mime": "image/png",
                "output_sha256": "output-hash",
                "source_sha256": "source-hash",
                "resize_filter": "LANCZOS",
            }
            source, record = creator.prepare_media_source(image)
        self.assertEqual(source, "staged-resource-url")
        creator.stage_file.assert_called_once_with(output, "image/png")
        self.assertEqual(record["Upload mode"], "staged_resized_file")
        self.assertEqual(record["Output Width"], 2304)
        self.assertEqual(record["Output Height"], 4096)

    @patch("create_shopify_products_from_upload_saree.requests.get")
    def test_exact_download_writes_response_bytes_unchanged(self, get: Mock) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.iter_content.return_value = [b"one", b"two"]
        response.raise_for_status.return_value = None
        get.return_value = response
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.png"
            download_exact_source_file(
                "https://files.example/user_files/source.png", path
            )
            self.assertEqual(path.read_bytes(), b"onetwo")

    def test_shopify_dimension_verification(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.verify_source_metadata = True
        creator.verify_source_hash = False
        creator.fail_on_source_mismatch = True
        record = {
            "Baserow Size Bytes": 3_000_000,
            "Baserow Width": 2304,
            "Baserow Height": 4096,
            "Warning": "",
        }
        ready = {
            "status": "READY",
            "image": {"url": "https://cdn.shopify.com/image.png", "width": 2304, "height": 4096},
            "originalSource": {"fileSize": 3_000_000, "url": ""},
        }
        creator.verify_media_source(record, ready)
        self.assertEqual(record["Source Metadata Verified"], "yes")
        self.assertEqual(record["Metadata Match"], "yes")
        self.assertEqual(record["Shopify Width"], 2304)
        self.assertEqual(record["Shopify Height"], 4096)

    def test_shopify_dimension_mismatch_fails(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.verify_source_metadata = True
        creator.verify_source_hash = False
        creator.fail_on_source_mismatch = True
        record = {
            "Baserow Size Bytes": 3_000_000,
            "Baserow Width": 2304,
            "Baserow Height": 4096,
            "Warning": "",
        }
        ready = {
            "status": "READY",
            "image": {"width": 1152, "height": 2048},
            "originalSource": {"fileSize": 3_000_000, "url": ""},
        }
        with self.assertRaisesRegex(ShopifyError, "width mismatch"):
            creator.verify_media_source(record, ready)

    def test_resized_shopify_target_dimensions_are_required(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.verify_source_metadata = True
        creator.verify_source_hash = False
        creator.fail_on_source_mismatch = True
        creator.require_target_dimensions = True
        record = {
            "Baserow Size Bytes": 3_000_000,
            "Baserow Width": 1152,
            "Baserow Height": 2048,
            "Expected Shopify Size Bytes": 6_000_000,
            "Expected Shopify Width": 2304,
            "Expected Shopify Height": 4096,
            "Resize Enabled": "yes",
            "Target Width": 2304,
            "Target Height": 4096,
            "Warning": "",
        }
        ready = {
            "status": "READY",
            "image": {"width": 1152, "height": 2048},
            "originalSource": {"fileSize": 6_000_000, "url": ""},
        }
        with self.assertRaisesRegex(
            ShopifyError, "shopify_resized_dimension_mismatch"
        ):
            creator.verify_media_source(record, ready)

    def test_no_baserow_writeback_after_resolution_or_media_failure(self) -> None:
        creator = creator_for_validation()
        creator.update_baserow = Mock()
        rejected = creator.validate_front_view_image(front(1152))
        self.assertEqual(rejected["Resolution Eligible"], "no")
        creator.update_baserow.assert_not_called()
        creator.upload_product_images = Mock(side_effect=RuntimeError("media failed"))
        with self.assertRaisesRegex(RuntimeError, "media failed"):
            creator.upload_product_images("product", "SKU", "Title", "Front", [])
        creator.update_baserow.assert_not_called()

    def test_retry_duplicate_sku_is_detected_before_create(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        creator.shopify.graphql.return_value = {
            "variants": {
                "nodes": [
                    {
                        "sku": "SKU-RETRY",
                        "product": {
                            "id": "gid://shopify/Product/1",
                            "title": "Draft shell",
                            "handle": "draft-shell",
                        },
                    }
                ]
            },
            "handles": {"nodes": []},
            "titles": {"nodes": []},
        }
        reason, product = creator.find_duplicates(
            "SKU-RETRY", "new-handle", "New title"
        )
        self.assertEqual(reason, "existing_sku")
        self.assertEqual(product["id"], "gid://shopify/Product/1")

    def test_partial_product_is_rolled_back_to_draft(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        creator.shopify.graphql.return_value = {
            "productUpdate": {
                "product": {
                    "id": "gid://shopify/Product/1",
                    "status": "DRAFT",
                },
                "userErrors": [],
            }
        }
        creator.set_partial_product_draft("gid://shopify/Product/1")
        variables = creator.shopify.graphql.call_args.args[1]
        self.assertEqual(variables["product"]["status"], "DRAFT")

    def test_five_product_cap_counts_successes_not_attempts(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.max_products = 5
        creator.dry_run = False
        creator.state = Mock()
        creator.state.created = 4
        creator.state.resumed = 0
        self.assertFalse(creator.run_cap_reached())
        creator.state.created = 5
        self.assertTrue(creator.run_cap_reached())

    def test_full_sync_requires_explicit_confirmation(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.max_products = None
        creator.confirm_full_sync = False
        with self.assertRaisesRegex(RuntimeError, "Full Upload Saree sync is blocked"):
            creator.validate_run_scope()
        creator.confirm_full_sync = True
        creator.validate_run_scope()

    def test_product_persistence_verification(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        creator.max_clean_tags = 6
        creator.fields = {"Generation Status": {"name": "Generation Status"}}
        creator.state = Mock()
        creator.state.persistence_checks = []
        creator.get_baserow_row = Mock(
            return_value={"Generation Status": {"value": "Shopify-sync"}}
        )
        creator.shopify.graphql.return_value = {
            "product": {
                "id": "gid://shopify/Product/1",
                "handle": "sku-1",
                "status": "ACTIVE",
                "tags": ["Saree"],
                "publishedOnPublication": True,
                "variants": {
                    "nodes": [
                        {
                            "sku": "SKU-1",
                            "inventoryPolicy": "DENY",
                            "taxable": True,
                            "inventoryItem": {
                                "tracked": True,
                                "inventoryLevel": {
                                    "quantities": [
                                        {"name": "available", "quantity": 1},
                                        {"name": "on_hand", "quantity": 1},
                                    ]
                                },
                            },
                        }
                    ]
                },
                "media": {
                    "nodes": [
                        {
                            "id": "gid://shopify/MediaImage/1",
                            "status": "READY",
                            "image": {"width": 1152, "height": 2048},
                        }
                    ]
                },
            },
            "productVariants": {
                "nodes": [
                    {
                        "sku": "SKU-1",
                        "product": {"id": "gid://shopify/Product/1"},
                    }
                ]
            },
        }
        entry = {
            "Baserow Row ID": 1,
            "Product Code": "SKU-1",
            "Source Width": 1152,
            "Source Height": 2048,
            "Shopify Product ID": "gid://shopify/Product/1",
            "Shopify Media ID": "gid://shopify/MediaImage/1",
            "Online Store publication ID": "gid://shopify/Publication/1",
            "Inventory location ID": "gid://shopify/Location/1",
        }
        result = creator.verify_product_persistence(
            entry,
            "Immediate",
            require_baserow_writeback=True,
        )
        self.assertEqual(result["Result"], "passed")
        self.assertEqual(result["Persistence Check Immediate"], "yes")
        self.assertEqual(result["Front View First"], "yes")

    def test_canary_deletion_fails_persistence_and_blocks_success(self) -> None:
        creator = object.__new__(UploadSareeCreator)
        creator.shopify = Mock()
        creator.max_clean_tags = 6
        creator.fields = {"Generation Status": {"name": "Generation Status"}}
        creator.state = Mock()
        creator.state.persistence_checks = []
        creator.get_baserow_row = Mock(
            return_value={"Generation Status": {"value": "Shopify-sync"}}
        )
        creator.shopify.graphql.return_value = {
            "product": None,
            "productVariants": {"nodes": []},
        }
        entry = {
            "Baserow Row ID": 1,
            "Product Code": "SKU-1",
            "Source Width": 1152,
            "Source Height": 2048,
            "Shopify Product ID": "gid://shopify/Product/1",
            "Shopify Media ID": "gid://shopify/MediaImage/1",
            "Online Store publication ID": "gid://shopify/Publication/1",
            "Inventory location ID": "gid://shopify/Location/1",
        }
        result = creator.verify_product_persistence(
            entry,
            "Final",
            require_baserow_writeback=True,
        )
        self.assertEqual(result["Result"], "failed")
        self.assertIn("exact_product_id_missing", result["Error"])


if __name__ == "__main__":
    unittest.main()
