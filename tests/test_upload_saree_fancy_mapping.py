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


def fancy_row(notes: str = "Approved") -> dict:
    return {"id": 22, "Product Title": "Fancy Saree", "Product Code": "AB202601", "Category": "Fancy Saree", "Generation Status": {"value": "Approved"}, "SHOPIFY Notes": notes}


def test_fancy_aliases_and_saree_type() -> None:
    creator = make_creator()
    assert creator.resolve_category("Fancy Saree")["canonical_name"] == "Fancy Sarees"
    assert creator.resolve_category("Fancy Sarees")["canonical_name"] == "Fancy Sarees"
    assert creator.product_type_for_row(fancy_row()) == "Saree"


def test_fancy_tags_are_exact_and_bounded() -> None:
    tags = make_creator().clean_tags(fancy_row())
    assert tags[:2] == ["Fancy Sarees", "Saree"]
    assert len(tags) <= 6 and all(":" not in tag for tag in tags)


def test_fancy_collections_are_assigned_in_order() -> None:
    creator = make_creator()
    creator.add_new_arrivals = False
    creator.shopify = Mock()
    creator.shopify.ensure_product_in_collection.return_value = (False, True)
    creator.shopify.product_collection_ids.return_value = {"gid://shopify/Collection/352494649542", "gid://shopify/Collection/350149148870"}
    creator.get_collection_id_by_title = Mock(return_value="gid://shopify/Collection/352494649542")
    details = creator.assign_and_verify_required_collections("gid://shopify/Product/1", fancy_row())
    calls = [call.args[1] for call in creator.shopify.ensure_product_in_collection.call_args_list]
    assert calls == ["gid://shopify/Collection/352494649542", "gid://shopify/Collection/350149148870"]
    assert details["Additional Collections"] == "Daily Buys"


def test_fancy_filter_and_approval() -> None:
    creator = make_creator()
    creator.category_filter = creator.normalize_category_key("Fancy Sarees")
    assert creator.category_filter_ok(fancy_row()) is True
    assert creator.approval_ok(fancy_row()) is True
    assert creator.approval_ok(fancy_row("")) is False
    other = fancy_row(); other["Category"] = "Tussar Silk Saree"
    assert creator.category_filter_ok(other) is False
