from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "men_accessories_image_prepare_test", ROOT / "utils" / "image_prepare.py"
)
assert spec is not None and spec.loader is not None
image_prepare = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = image_prepare
spec.loader.exec_module(image_prepare)

from create_shopify_products_from_men_accessories import (
    ShopifyError,
    MenAccessoriesCreator,
    download_exact_source_file,
    get_baserow_original_file_url,
    get_baserow_source_file,
    normalize_status,
)


def creator_for_validation(
    *, gate: bool = True, minimum: int = 1946, recommended: int = 1152
) -> MenAccessoriesCreator:
    creator = object.__new__(MenAccessoriesCreator)
    creator.min_front_width = minimum
    creator.recommended_front_width = recommended
    creator.require_min_front_width = gate
    creator.image_max_bytes = 20 * 1024 * 1024
    return creator


def front(width: int, *, height: int = 2048, mime: str = "image/png") -> dict:
    return {
        "label": "Generated Front View",
        "url": "https://files.example/user_files/original.png",
        "name": "original.png",
        "size": 3_000_000,
        "mime_type": mime,
        "image_width": width,
        "image_height": height,
        "is_image": True,
    }


class ImagePrepareTests(unittest.TestCase):
    def test_men_accessories_gets_primary_and_new_arrivals(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.shopify = Mock()
        creator.shopify.get_collection_id_by_title.side_effect = lambda title: {
            "Men's Ties": "gid://shopify/Collection/primary",
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
        creator.collection_title = Mock(return_value="Men's Ties")
        creator.secondary_collection_title = ""
        creator.secondary_collection_id = ""
        creator.add_new_arrivals = True
        creator.new_arrivals_collection_name = "New Arrivals"
        creator.state = SimpleNamespace(new_arrivals_assignment_failures=0)
        details = creator.assign_and_verify_required_collections(
            "gid://shopify/Product/1", {}
        )
        self.assertEqual(details["Primary Collection"], "Men's Ties")
        self.assertEqual(details["Primary Collection Assigned"], "yes")
        self.assertEqual(details["New Arrivals Assigned"], "yes")

    def test_empty_shopify_notes_is_allowed_but_nonapproved_is_rejected(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.require_shopify_notes_approved = True
        creator.fields = {
            "Generation Status": {"name": "Generation Status"},
            "SHOPIFY Notes": {"name": "SHOPIFY Notes"},
        }
        row = {"Generation Status": "Approved", "SHOPIFY Notes": ""}
        self.assertTrue(creator.approval_ok(row))
        row["SHOPIFY Notes"] = "Pending"
        self.assertFalse(creator.approval_ok(row))

    def test_normalized_sku_lookup_returns_unique_products(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.shopify = Mock()
        creator.shopify.graphql.return_value = {
            "productVariants": {
                "nodes": [
                    {
                        "sku": " AB 123 ",
                        "product": {"id": "gid://shopify/Product/1"},
                    },
                    {
                        "sku": "ab123",
                        "product": {"id": "gid://shopify/Product/1"},
                    },
                ]
            }
        }
        matches = creator.find_products_by_sku("AB123")
        self.assertEqual([item["id"] for item in matches], ["gid://shopify/Product/1"])

    def test_reconciliation_verifies_both_required_collection_ids(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.shopify = Mock()
        creator.shopify.graphql.return_value = {
            "nodes": [
                {
                    "id": "gid://shopify/Collection/339913605318",
                    "title": "Men Accessories",
                },
                {
                    "id": "gid://shopify/Collection/339549880518",
                    "title": "Men's Ties",
                },
            ]
        }
        result = creator.verify_reconciliation_collections()
        self.assertEqual(result["Men Accessories"].rsplit("/", 1)[-1], "339913605318")
        self.assertEqual(result["Men's Ties"].rsplit("/", 1)[-1], "339549880518")
        self.assertEqual(creator.primary_collection_title, "Men Accessories")
        self.assertEqual(creator.secondary_collection_title, "Men's Ties")

    def test_reconciliation_state_detects_unpublished_product(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.shopify = Mock()
        creator.shopify.graphql.return_value = {
            "product": {
                "id": "gid://shopify/Product/1",
                "status": "ACTIVE",
                "publishedOnPublication": False,
                "collections": {
                    "nodes": [
                        {"id": "gid://shopify/Collection/men"},
                        {"id": "gid://shopify/Collection/ties"},
                    ]
                },
                "variants": {
                    "nodes": [
                        {
                            "sku": "AB123",
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
                "media": {"nodes": []},
            }
        }
        state = creator.get_reconciliation_product_state(
            "gid://shopify/Product/1",
            "AB123",
            "gid://shopify/Publication/1",
            "gid://shopify/Location/1",
            [],
            {
                "Men Accessories": "gid://shopify/Collection/men",
                "Men's Ties": "gid://shopify/Collection/ties",
            },
        )
        self.assertTrue(state["active"])
        self.assertFalse(state["published"])
        self.assertFalse(state["verified"])

    def test_existing_product_uploads_only_missing_role(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.state = SimpleNamespace(
            images_already_present=0,
            images_newly_uploaded=0,
            duplicate_media_prevented=0,
            image_failures=[],
        )
        creator.get_product_media = Mock(
            return_value=[
                {
                    "id": "gid://shopify/MediaImage/hero",
                    "alt": "AB123 - Rolled Tie Hero View",
                    "status": "READY",
                    "originalSource": {"url": "https://cdn.example/hero.jpg"},
                }
            ]
        )
        creator.prepare_media_source = Mock(
            return_value=(
                "https://files.example/user_files/close.jpg",
                {"Upload mode": "external_original_url", "Error": ""},
            )
        )
        creator.create_single_media = Mock(return_value="gid://shopify/MediaImage/close")
        creator.wait_for_media_id = Mock(
            return_value={
                "id": "gid://shopify/MediaImage/close",
                "status": "READY",
                "image": {},
                "originalSource": {},
            }
        )
        creator.verify_media_source = Mock()
        rows = creator.upload_product_images(
            "gid://shopify/Product/1",
            "AB123",
            "Tie AB123",
            "AB123 - Rolled Tie Hero View",
            [
                {
                    "label": "Rolled Tie Hero View",
                    "url": "https://files.example/user_files/hero.jpg",
                },
                {
                    "label": "Diagonal Close-Up Detail View",
                    "url": "https://files.example/user_files/close.jpg",
                },
            ],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(creator.state.images_already_present, 1)
        self.assertEqual(creator.state.images_newly_uploaded, 1)
        creator.create_single_media.assert_called_once()
        self.assertEqual(
            creator.create_single_media.call_args.args[2],
            "AB123 - Diagonal Close-Up Detail View",
        )

    def test_approval_normalization(self) -> None:
        self.assertEqual(normalize_status({"value": " Approved\u00a0"}), "approved")
        self.assertEqual(normalize_status("  APPROVED  "), "approved")

    def test_shopify_notes_gate_can_be_disabled(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.require_shopify_notes_approved = False
        creator.fields = {
            "Generation Status": {"name": "Generation Status"},
            "SHOPIFY Notes": {"name": "SHOPIFY Notes"},
        }
        row = {
            "Generation Status": {"value": "Approved"},
            "SHOPIFY Notes": "",
        }
        self.assertTrue(creator.approval_ok(row))

    def test_all_five_men_accessories_image_fields_are_uploaded_in_order(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.config = {
            "image_order": [
                "Rolled Tie Hero View",
                "Diagonal Close-Up Detail View",
                "Opened Back Construction View",
                "Full-Length Front Flat-Lay View",
                "Tie Image",
            ],
            "featured_image_fallback_order": [
                "Rolled Tie Hero View",
                "Full-Length Front Flat-Lay View",
                "Tie Image",
                "Diagonal Close-Up Detail View",
                "Opened Back Construction View",
            ],
        }
        creator.fields = {
            "Rolled Tie Hero View": {"name": "Rolled Tie Hero View"},
            "Diagonal Close-Up Detail View": {"name": "Diagonal Close-Up Detail View"},
            "Opened Back Construction View": {"name": "Opened Back Construction View"},
            "Full-Length Front Flat-Lay View": {"name": "Full-Length Front Flat-Lay View"},
            "Tie Image": {"name": "Tie Image"},
        }
        def file(name: str) -> dict:
            return {
                "url": f"https://files.example/user_files/{name}.png",
                "size": 1,
                "mime_type": "image/png",
                "image_width": 1152,
                "image_height": 2048,
                "is_image": True,
            }
        row = {
            "Rolled Tie Hero View": [file("rolled")],
            "Diagonal Close-Up Detail View": [file("diagonal")],
            "Opened Back Construction View": [file("opened")],
            "Full-Length Front Flat-Lay View": [file("flat")],
            "Tie Image": [file("tie")],
        }
        images = creator.images(row)
        self.assertEqual(
            [image["label"] for image in images],
            [
                "Rolled Tie Hero View",
                "Diagonal Close-Up Detail View",
                "Opened Back Construction View",
                "Full-Length Front Flat-Lay View",
                "Tie Image",
            ],
        )
        self.assertEqual(creator.featured_image_label(images), "Rolled Tie Hero View")

    def test_tie_image_is_uploaded_even_when_not_featured(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.config = {
            "image_order": ["Full-Length Front Flat-Lay View", "Tie Image"],
            "featured_image_fallback_order": [
                "Rolled Tie Hero View",
                "Full-Length Front Flat-Lay View",
                "Tie Image",
            ],
        }
        creator.fields = {
            "Full-Length Front Flat-Lay View": {"name": "Full-Length Front Flat-Lay View"},
            "Tie Image": {"name": "Tie Image"},
        }
        row = {
            "Full-Length Front Flat-Lay View": [
                {
                    "url": "https://files.example/user_files/flat.png",
                    "size": 1,
                    "mime_type": "image/png",
                    "image_width": 1152,
                    "image_height": 2048,
                    "is_image": True,
                }
            ],
            "Tie Image": [
                {
                    "url": "https://files.example/user_files/tie.png",
                    "size": 1,
                    "mime_type": "image/png",
                    "image_width": 1152,
                    "image_height": 2048,
                    "is_image": True,
                }
            ],
        }
        images = creator.images(row)
        self.assertEqual([image["label"] for image in images], ["Full-Length Front Flat-Lay View", "Tie Image"])
        self.assertEqual(creator.featured_image_label(images), "Full-Length Front Flat-Lay View")

    def test_multiple_files_per_baserow_image_field_are_uploaded(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.config = {"image_order": ["Rolled Tie Hero View"]}
        creator.fields = {"Rolled Tie Hero View": {"name": "Rolled Tie Hero View"}}
        row = {
            "Rolled Tie Hero View": [
                {
                    "url": "https://files.example/user_files/rolled-1.png",
                    "size": 1,
                    "mime_type": "image/png",
                    "image_width": 1152,
                    "image_height": 2048,
                    "is_image": True,
                },
                {
                    "url": "https://files.example/user_files/rolled-2.png",
                    "size": 1,
                    "mime_type": "image/png",
                    "image_width": 1152,
                    "image_height": 2048,
                    "is_image": True,
                },
            ]
        }
        images = creator.images(row)
        self.assertEqual(len(images), 2)
        self.assertIn("rolled-1.png", images[0]["url"])
        self.assertIn("rolled-2.png", images[1]["url"])

    def test_clean_tags_max_six_and_no_internal_colon_tags(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.max_clean_tags = 6
        creator.fields = {
            "Product Title": {"name": "Product Title"},
            "Category": {"name": "Category"},
            "Descriptions": {"name": "Descriptions"},
        }
        row = {
            "Product Title": "Printed Silk Formal Wedding Festive Business Tie",
            "Category": "Men Accessories",
            "Descriptions": "Business office occasion",
        }
        tags = creator.clean_tags(row)
        self.assertLessEqual(len(tags), 6)
        self.assertNotIn(":", ",".join(tags))
        self.assertIn("Men Accessories", tags)
        self.assertIn("Men's Ties", tags)

    def test_inventory_idempotency_key_uses_product_code_location_and_quantity(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.current_inventory_sku = "AE202782"
        creator.inventory_quantity = 1
        key = creator.inventory_idempotency_key(
            "gid://shopify/InventoryItem/10",
            "gid://shopify/Location/20",
        )
        self.assertEqual(key, "men-accessories:v2:AE202782:inventory:20:available:1")

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
            get_baserow_original_file_url(files, "Generated Front View"), files[0]["url"]
        )
        selected = get_baserow_source_file({"Generated Front View": files}, "Generated Front View")
        self.assertEqual(selected["url"], files[0]["url"])
        self.assertNotIn("thumbnails", selected)

    def test_thumbnail_rejection(self) -> None:
        with self.assertRaisesRegex(ValueError, "thumbnail"):
            get_baserow_source_file(
                {
                    "Generated Front View": [
                        {
                            "url": (
                                "https://files.example/user_files/"
                                "thumbnails/small.png"
                            )
                        }
                    ]
                },
                "Generated Front View",
            )

    def test_1152_generated_front_accepted_when_gate_disabled(self) -> None:
        result = creator_for_validation(
            gate=False, minimum=0, recommended=1152
        ).validate_generated_front_image(front(1152))
        self.assertEqual(result["Resolution Eligible"], "yes")
        self.assertEqual(result["Resolution Result"], "resolution_gate_disabled")

    def test_1152_generated_front_rejected_only_when_gate_explicitly_enabled(self) -> None:
        result = creator_for_validation(
            gate=True, minimum=1946, recommended=1152
        ).validate_generated_front_image(front(1152))
        self.assertEqual(result["Resolution Eligible"], "no")
        self.assertEqual(result["Result"], "resolution_gate_rejected")
        self.assertIn("configured minimum width 1946", result["Error"])

    def test_1946_generated_front_accepted(self) -> None:
        result = creator_for_validation().validate_generated_front_image(front(1946))
        self.assertEqual(result["Resolution Eligible"], "yes")
        self.assertEqual(result["Resolution Result"], "resolution_gate_passed")

    def test_2304_generated_front_accepted(self) -> None:
        result = creator_for_validation().validate_generated_front_image(front(2304))
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
        creator = object.__new__(MenAccessoriesCreator)
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

    @patch("create_shopify_products_from_men_accessories.requests.get")
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

    def test_over_limit_jpeg_is_prepared_below_limit_without_resizing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "large.jpg"
            Image.effect_noise((1600, 1600), 100).convert("RGB").save(
                source, format="JPEG", quality=100, subsampling=0
            )
            original_size = source.stat().st_size
            max_bytes = int(original_size * 0.9)
            with patch.object(
                image_prepare,
                "get_remote_file_info",
                return_value={
                    "content_length": original_size,
                    "content_type": "image/jpeg",
                    "filename": source.name,
                    "warning": "",
                },
            ), patch.object(
                image_prepare,
                "download_exact_original",
                return_value=(source, "image/jpeg"),
            ):
                prepared = image_prepare.prepare_image_for_shopify(
                    "https://files.example/user_files/large.jpg",
                    temp,
                    max_bytes=max_bytes,
                )
            self.assertTrue(prepared.compressed)
            self.assertLessEqual(prepared.final_size_bytes, max_bytes)
            self.assertEqual(prepared.original_dimensions, "1600x1600")
            self.assertEqual(prepared.final_dimensions, "1600x1600")
            self.assertIn(prepared.quality_used, {95, 93, 91, 89, 87})

    def test_shopify_dimension_verification(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
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
        creator = object.__new__(MenAccessoriesCreator)
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
        creator = object.__new__(MenAccessoriesCreator)
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
        rejected = creator.validate_generated_front_image(front(1152))
        self.assertEqual(rejected["Resolution Eligible"], "no")
        creator.update_baserow.assert_not_called()
        creator.upload_product_images = Mock(side_effect=RuntimeError("media failed"))
        with self.assertRaisesRegex(RuntimeError, "media failed"):
            creator.upload_product_images("product", "SKU", "Title", "Front", [])
        creator.update_baserow.assert_not_called()

    def test_retry_duplicate_sku_is_detected_before_create(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
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
        creator = object.__new__(MenAccessoriesCreator)
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
        creator = object.__new__(MenAccessoriesCreator)
        creator.max_products = 5
        creator.dry_run = False
        creator.state = Mock()
        creator.state.created = 4
        self.assertFalse(creator.run_cap_reached())
        creator.state.created = 5
        self.assertTrue(creator.run_cap_reached())

    def test_full_sync_requires_explicit_confirmation(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
        creator.max_products = None
        creator.confirm_full_sync = False
        with self.assertRaisesRegex(RuntimeError, "Full Men Accessories sync is blocked"):
            creator.validate_run_scope()
        creator.confirm_full_sync = True
        creator.validate_run_scope()

    def test_product_persistence_verification(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
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
                "tags": ["Tie"],
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
        self.assertEqual(result["Generated Front View First"], "yes")

    def test_canary_deletion_fails_persistence_and_blocks_success(self) -> None:
        creator = object.__new__(MenAccessoriesCreator)
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

