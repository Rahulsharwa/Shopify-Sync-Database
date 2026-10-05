from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from create_shopify_products_from_upload_saree import RunState, UploadSareeCreator

ROOT = Path(__file__).resolve().parents[1]


def make_creator() -> UploadSareeCreator:
    creator = object.__new__(UploadSareeCreator)
    creator.config = json.loads((ROOT / "config" / "upload_saree_product_create_config.json").read_text(encoding="utf-8"))
    creator.collection_map = json.loads((ROOT / "config" / "upload_saree_collection_map.json").read_text(encoding="utf-8"))
    creator.fields = {name: {"name": name} for name in creator.config["field_ids"]}
    creator.max_clean_tags = 6
    creator.state = RunState()
    creator._taxonomy_entries = []
    creator._taxonomy_alias_index = {}
    creator._load_taxonomy_index()
    return creator


def pavada_row(notes: str = "Approved") -> dict:
    return {
        "id": 729,
        "Product Title": "Silk Pavada",
        "Product Code": "AL203199",
        "Category": "Silk Pavada",
        "Generation Status": {"value": "Approved"},
        "SHOPIFY Notes": notes,
    }


def test_silk_pavada_aliases_product_type_and_exact_tags() -> None:
    creator = make_creator()
    assert creator.resolve_category("Silk Pavada")["canonical_name"] == "Silk Pavada"
    assert creator.resolve_category("Pattu Pavadai")["canonical_name"] == "Silk Pavada"
    assert creator.product_type_for_row(pavada_row()) == "Kids Wear"
    assert creator.clean_tags(pavada_row()) == [
        "Silk Pavada", "Pattu Pavadai", "Kids Wear", "Girls Ethnic Wear",
        "Traditional Wear", "South Indian Wear", "Silk Dress", "Festive Wear",
        "Wedding Wear", "Temple Wear", "Indian Ethnic Wear", "Kids Silk Wear",
        "Silk", "Festive", "Wedding", "Traditional", "South Indian",
    ]
    assert creator.tag_limit_for_row(pavada_row()) == 17


def test_other_categories_keep_six_tag_limit() -> None:
    creator = make_creator()
    row = pavada_row()
    row["Category"] = "Banarasi silk"
    assert creator.tag_limit_for_row(row) == 6


def test_silk_pavada_assigns_primary_and_new_arrivals_idempotently() -> None:
    creator = make_creator()
    creator.add_new_arrivals = True
    creator.new_arrivals_collection_name = "New Arrivals"
    creator.shopify = Mock()
    creator.shopify.ensure_product_in_collection.return_value = (False, True)
    creator.shopify.product_collection_ids.return_value = {
        "gid://shopify/Collection/352781140166",
        "gid://shopify/Collection/new-arrivals",
    }
    creator.get_collection_id_by_title = Mock(
        side_effect=lambda title: (
            "gid://shopify/Collection/352781140166"
            if title == "Silk Pavada"
            else "gid://shopify/Collection/new-arrivals"
        )
    )
    details = creator.assign_and_verify_required_collections("gid://shopify/Product/1", pavada_row())
    calls = [call.args[1] for call in creator.shopify.ensure_product_in_collection.call_args_list]
    assert calls == [
        "gid://shopify/Collection/352781140166",
        "gid://shopify/Collection/new-arrivals",
    ]
    assert details["New Arrivals Assigned"] == "no"


def test_silk_pavada_filter_and_approval_requirements() -> None:
    creator = make_creator()
    creator.category_filter = creator.normalize_category_key("Silk Pavada")
    assert creator.category_filter_ok(pavada_row()) is True
    assert creator.approval_ok(pavada_row()) is True
    assert creator.approval_ok(pavada_row("")) is False
    other = pavada_row()
    other["Category"] = "Cotton Saree"
    assert creator.category_filter_ok(other) is False
