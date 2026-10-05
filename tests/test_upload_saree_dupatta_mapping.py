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
    creator.max_clean_tags = 6
    creator.state = RunState()
    creator._taxonomy_entries = []
    creator._taxonomy_alias_index = {}
    creator._load_taxonomy_index()
    return creator


def dupatta_row(notes: str = "Approved") -> dict:
    return {
        "id": 1,
        "Product Title": "Blue Dupatta",
        "Product Code": "DUP-1",
        "Category": "Dupatta",
        "catalog": "",
        "Descriptions": "Blue dupatta.",
        "Generation Status": {"value": "Approved"},
        "SHOPIFY Notes": notes,
    }


def test_dupatta_alias_and_tags_include_accessories_without_exceeding_limit() -> None:
    creator = make_creator()
    tags = creator.clean_tags(dupatta_row())
    assert tags[:3] == ["Dupattas", "Accessories", "Saree"]
    assert len(tags) <= 6
    assert creator.resolve_category("Dupattas")["canonical_name"] == "Dupattas"


def test_dupatta_collection_assignment_is_idempotent_and_verifies_both_collections() -> None:
    creator = make_creator()
    creator.add_new_arrivals = False
    creator.shopify = Mock()
    creator.shopify.ensure_product_in_collection.return_value = (False, True)
    creator.shopify.product_collection_ids.return_value = {
        "gid://shopify/Collection/339913638086",
        "gid://shopify/Collection/339913277638",
    }
    creator.get_collection_id_by_title = Mock(
        return_value="gid://shopify/Collection/339913638086"
    )

    details = creator.assign_and_verify_required_collections(
        "gid://shopify/Product/1", dupatta_row()
    )

    calls = [
        call.args[1]
        for call in creator.shopify.ensure_product_in_collection.call_args_list
    ]
    assert calls == [
        "gid://shopify/Collection/339913638086",
        "gid://shopify/Collection/339913277638",
    ]
    assert details["Additional Collections"] == "Accessories"
    assert details["Additional Collections Assigned"] == "no"


def test_dupatta_approval_still_requires_shopify_notes() -> None:
    creator = make_creator()
    assert creator.approval_ok(dupatta_row("Approved")) is True
    assert creator.approval_ok(dupatta_row("")) is False


def test_dupatta_category_filter_excludes_other_categories() -> None:
    creator = make_creator()
    creator.category_filter = creator.normalize_category_key("Dupattas")
    other = dupatta_row()
    other["Category"] = "Tussar Silk Saree"
    assert creator.category_filter_ok(dupatta_row()) is True
    assert creator.category_filter_ok(other) is False


def test_dupatta_product_type_is_dupatta_and_other_categories_remain_saree() -> None:
    creator = make_creator()
    assert creator.product_type_for_row(dupatta_row()) == "Dupatta"
    other = dupatta_row()
    other["Category"] = "Tussar Silk Saree"
    assert creator.product_type_for_row(other) == "Saree"
