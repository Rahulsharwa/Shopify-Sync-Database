from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from html import escape
import re
from bs4 import BeautifulSoup

from utils import text


META_FIELDS = {
    "Specification_": ("specification", "multi_line_text_field"),
    "Fabric": ("fabric", "single_line_text_field"),
    "Color": ("color", "single_line_text_field"),
    "Pattern": ("pattern", "single_line_text_field"),
    "Occasion": ("occasion", "single_line_text_field"),
    "Zari": ("zari", "single_line_text_field"),
    "Weave": ("weave", "single_line_text_field"),
    "Care instructions": ("care_instructions", "multi_line_text_field"),
    "Size": ("size", "single_line_text_field"),
    "Age group": ("age_group", "single_line_text_field"),
    "Target gender": ("target_gender", "single_line_text_field"),
    "Region": ("region", "single_line_text_field"),
}
IMAGE_FIELDS = ["Fabrics Image", "Fabrics Image 2", "Fabrics Image 3", "Fabrics Image 4"]


@dataclass
class FabricProduct:
    row_id: int
    code: str
    title: str
    description_html: str
    price: str
    product_type: str
    tags: list[str]
    seo_title: str
    seo_description: str
    shopify_id: str
    images: list[dict[str, str]] = field(default_factory=list)
    metafields: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "FabricProduct":
        price_raw = text(row.get("Price (INR)")).replace(",", "")
        try:
            price = format(Decimal(price_raw), ".2f") if price_raw else ""
        except InvalidOperation:
            price = price_raw
        tag_values = []
        for key in ("Tags", "Fabric", "Color", "Pattern", "Weave", "product_type"):
            tag_values.extend(part.strip() for part in text(row.get(key)).split(",") if part.strip())
        tags = list(dict.fromkeys(tag_values))
        images = []
        for field_name in IMAGE_FIELDS:
            files = row.get(field_name) or []
            if isinstance(files, dict):
                files = [files]
            for item in files:
                if isinstance(item, dict) and item.get("url"):
                    images.append({"url": item["url"], "alt": text(item.get("name")) or text(row.get("Product Title"))})
        metafields = []
        for source, (key, field_type) in META_FIELDS.items():
            value = text(row.get(source))
            if value:
                metafields.append({"namespace": "custom", "key": key, "type": field_type, "value": value})
        code_value = text(row.get("Product Code"))
        if code_value:
            metafields.append({"namespace": "custom", "key": "product_code", "type": "single_line_text_field", "value": code_value})
        return cls(
            row_id=int(row["id"]), code=text(row.get("Product Code")), title=text(row.get("Product Title")),
            description_html=text(row.get("Description HTML")), price=price,
            product_type=text(row.get("type")) or text(row.get("product_type")), tags=tags,
            seo_title=text(row.get("SEO title")), seo_description=text(row.get("meta_description")),
            shopify_id=text(row.get("Shopify Id")), images=images, metafields=metafields,
        )

    def validation_errors(self) -> list[str]:
        errors = []
        if not self.code: errors.append("missing Product Code")
        if not self.title: errors.append("missing Product Title")
        try:
            if not self.price or Decimal(self.price) < 0: raise InvalidOperation
        except InvalidOperation:
            errors.append("invalid Price (INR)")
        return errors


def build_premium_fabric_description(row: dict[str, Any]) -> str:
    def safe(key: str, fallback: str = "") -> str:
        value = text(row.get(key))
        if value.lower() in {"none", "null", "nan"}: value = ""
        return escape(value or fallback, quote=True)
    title = safe("Product Title", "premium fabric")
    fabric = safe("Fabric", "premium silk fabric")
    pattern = safe("Pattern", "artistic textile pattern")
    weave = safe("Weave", "fine weave")
    color = safe("Color", "rich colour palette")
    care = safe("Care instructions", "Dry clean only")
    code, zari, size = safe("Product Code"), safe("Zari"), safe("Size")
    highlights = [f"<li>Fabric: {fabric}</li>", f"<li>Colour: {color}</li>",
                  f"<li>Pattern: {pattern}</li>", f"<li>Weave: {weave}</li>"]
    if zari: highlights.append(f"<li>Zari: {zari}</li>")
    if size: highlights.append(f"<li>Size/Width: {size}</li>")
    if code: highlights.append(f"<li>Product Code: {code}</li>")
    highlights.append(f"<li>Care: {care}</li>")
    return (f"<p>Introducing an epitome of refined grace, this {title} captures the timeless allure of Indian textile heritage with a contemporary finish.</p>"
            f"<h3>Fabric &amp; Craftsmanship</h3><p>Crafted from {fabric}, this fabric offers a refined texture, graceful fall, and premium hand-feel. The {pattern} detailing, {weave} weave, and {color} colour palette create a sophisticated textile character suitable for elegant custom creations.</p>"
            "<h3>Styling &amp; Occasion</h3><p>Designed for sarees, ethnic ensembles, festive wear, bridal silhouettes, and upscale occasion wear, this fabric gives designers and customers the flexibility to create refined garments with a luxurious finish.</p>"
            f"<h3>Product Highlights</h3><ul>{''.join(highlights)}</ul>"
            f"<h3>Material &amp; Wash Care</h3><p>{care}. Store in a breathable fabric bag to preserve colour, texture, and sheen.</p>"
            "<h3>Note</h3><p>Due to natural fabric fibres, dyeing, weaving, and photography conditions, slight colour variation may occur, adding to the unique character of each piece.</p>")


def build_enrichment_tags(row: dict[str, Any]) -> list[str]:
    values = ["Fabric"]
    for key in ("Tags", "Fabric", "Color", "Pattern", "Weave", "Occasion", "Zari", "product_type"):
        values.extend(part.strip() for part in text(row.get(key)).split(",") if part.strip())
    clean = []
    seen = set()
    for value in values:
        if value.lower() in {"none", "null", "nan"} or value.casefold() in seen: continue
        seen.add(value.casefold()); clean.append(value)
    return clean


APPROVED_COLORS = ["Beige", "Black", "Blue", "Brown", "Gray", "Green", "Multicolor", "Orange", "Pink", "Purple", "Red", "White", "Yellow"]
COLOR_ALIASES = {
    "Multicolor": ["multi", "multi color", "multicolour", "multi-colour", "multi-color", "assorted", "mixed"],
    "Gray": ["gray", "grey"], "White": ["white", "off white", "ivory", "cream"],
    "Yellow": ["yellow", "gold", "mustard"], "Red": ["red", "maroon", "burgundy", "wine"],
    "Blue": ["blue", "navy", "sky blue", "royal blue"], "Pink": ["pink", "magenta", "rose", "baby pink"],
    "Purple": ["purple", "violet", "lavender"], "Orange": ["orange", "rust", "peach"],
    "Brown": ["brown", "coffee", "tan"], "Beige": ["beige"], "Black": ["black"], "Green": ["green"],
}
COLLECTION_RULES = [
    ("Jamawar Silk Fabric", ["jamawar"]), ("Kanjeevaram Silk Fabric", ["kanjeevaram", "kanjivaram", "kanchipuram"]),
    ("Printed Silk Fabrics", ["printed", "print"]), ("Raw Silk Fabric", ["raw silk"]),
    ("Banarasi Silk Fabric", ["banarasi", "banaras"]), ("Twill Silk Fabric", ["twill silk"]),
    ("Twill Fabric", ["twill"]), ("Pure Silk Fabric", ["pure silk", "100% pure silk"]), ("Silk Fabric", ["silk"]),
]


def normalize_color(value: str) -> tuple[str, list[str]]:
    haystack = text(value).casefold().replace("_", " ")
    found = []
    for approved, aliases in COLOR_ALIASES.items():
        if any(alias in haystack for alias in aliases): found.append(approved)
    found = list(dict.fromkeys(found))
    if "Multicolor" in found or len(found) > 1: return "Multicolor", [c for c in found if c != "Multicolor"]
    return (found[0], found) if found else ("Multicolor", [])


def mapped_collections(row: dict[str, Any]) -> list[str]:
    haystack = " ".join(text(row.get(k)) for k in ("Product Title", "Product Code", "Fabric", "Pattern", "Weave", "Tags", "Description HTML")).casefold()
    matches = [name for name, keywords in COLLECTION_RULES if any(keyword in haystack for keyword in keywords)]
    if "pure" in haystack and "silk" in haystack and "Pure Silk Fabric" not in matches: matches.append("Pure Silk Fabric")
    if not matches: matches = ["Silk Fabric"]
    return list(dict.fromkeys(["Fabrics & Weaves", *matches]))


def parse_specification_html(spec_html: str) -> dict[str, str]:
    useful = {"fabric collections": "fabric_collections", "color": "color", "colour": "color",
              "fabric": "fabric", "pattern": "pattern", "weave": "weave", "zari": "zari", "occasion": "occasion"}
    result: dict[str, str] = {}
    soup = BeautifulSoup(spec_html or "", "html.parser")
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != 2: continue
        key = " ".join(cells[0].get_text(" ", strip=True).split())
        value = " ".join(cells[1].get_text(" ", strip=True).split())
        if not key or not value or key.casefold() in {"qty", "price"}: continue
        if re.fullmatch(r"\d+\s*-\s*\d+", key) or re.fullmatch(r"\d+\s*-\s*\d+", value): continue
        normalized = useful.get(key.casefold())
        if normalized and normalized not in result: result[normalized] = value
    return result
