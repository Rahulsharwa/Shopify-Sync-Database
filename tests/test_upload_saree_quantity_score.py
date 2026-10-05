from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from create_shopify_products_from_upload_saree import RunState, UploadSareeCreator

ROOT = Path(__file__).resolve().parents[1]


def make_creator() -> UploadSareeCreator:
    creator = object.__new__(UploadSareeCreator)
    creator.config = json.loads(
        (ROOT / "config" / "upload_saree_product_create_config.json").read_text(
            encoding="utf-8"
        )
    )
    creator.collection_map = json.loads(
        (ROOT / "config" / "upload_saree_collection_map.json").read_text(
            encoding="utf-8"
        )
    )
    creator.fields = {name: {"name": name} for name in creator.config["field_ids"]}
    creator.inventory_quantity = 1
    creator.max_clean_tags = 6
    creator.state = RunState()
    return creator


def row(quantity= None) -> dict:
    return {"Quantity Score": quantity}


def test_quantity_score_field_id_is_configured() -> None:
    config = json.loads(
        (ROOT / "config" / "upload_saree_product_create_config.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["field_ids"]["Quantity Score"] == 10424794


def test_quantity_score_blank_defaults_to_one() -> None:
    quantity, resolution, warning = make_creator().resolve_inventory_quantity(row())
    assert (quantity, resolution) == (1, "blank_default")
    assert "defaulted to 1" in warning


def test_quantity_score_positive_integer_and_numeric_string_are_used() -> None:
    creator = make_creator()
    assert creator.resolve_inventory_quantity(row(7)) == (7, "row_value", "")
    assert creator.resolve_inventory_quantity(row("3")) == (3, "row_value", "")
    assert creator.resolve_inventory_quantity(row("2.0")) == (2, "row_value", "")


def test_quantity_score_unsafe_values_fallback_to_one_with_warning() -> None:
    creator = make_creator()
    for value in (0, -2, 1.5, "abc"):
        quantity, resolution, warning = creator.resolve_inventory_quantity(row(value))
        assert quantity == 1
        assert resolution == "invalid_default"
        assert warning


def test_operational_setup_uses_row_quantity_without_mutating_global_default() -> None:
    creator = make_creator()
    creator.inventory_tracked = True
    creator.inventory_policy = "DENY"
    creator.taxable = True
    creator.product_status = "ACTIVE"
    creator.publish_online_store = False
    creator.verify_publication = False
    creator.update_product_status_and_tags = Mock()
    creator.update_variant_operational_settings = Mock(
        return_value={"id": "variant-1", "inventoryItem": {"id": "item-1", "tracked": True}}
    )
    creator.resolve_inventory_location = Mock(return_value={"id": "loc-1", "name": "Test"})
    creator.set_inventory_quantity = Mock(return_value=True)
    creator.assign_and_verify_required_collections = Mock(return_value={})
    creator.clean_tags = Mock(return_value=["Saree"])
    creator.product_type_for_row = Mock(return_value="Saree")

    details = creator.apply_operational_setup(
        {"id": "product-1", "title": "Test", "variants": {"nodes": [{"id": "variant-1"}]}},
        {"Category": "Cotton Saree", "Quantity Score": 9},
        configure_inventory=True,
        configure_tags=True,
    )

    assert creator.set_inventory_quantity.call_args.args[2] == 9
    assert details["Inventory quantity requested"] == 9
    assert details["Quantity Resolution"] == "row_value"
    assert details["Inventory Quantity Verified"] == "yes"
    assert creator.inventory_quantity == 1
