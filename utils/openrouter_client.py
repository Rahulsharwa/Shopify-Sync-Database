from __future__ import annotations

import json
import html
import re
from typing import Any

import requests


class OpenRouterError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(self, api_key: str, model: str, timeout: int = 120) -> None:
        self.model = model
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://janardhanasilk.com",
                "X-Title": "Janardhana Silk House Upload Saree",
            }
        )

    def generate_product_copy(
        self,
        facts: dict[str, Any],
        image_urls: list[str],
        use_image_input: bool = True,
    ) -> dict[str, Any]:
        system = """You write accurate premium ecommerce copy for Janardhana Silk House.
Return one strict JSON object only, without markdown.
Never invent unsupported fibre, zari, handloom, dimensions, blouse-piece, dye, or occasion claims.
The description_html section order must be: Introduction; Fabric & Craftsmanship;
Styling & Occasion; Product Highlights; Material & Wash Care; Note.
Product Highlights order: Fabric, Zari, Colour, Pattern, Border, Technique, Weave,
Occasion, Product Code. Put uncertainties only in warnings, never in customer HTML."""
        schema = {
            "title": "",
            "description_html": "",
            "seo_title": "",
            "seo_description": "",
            "image_alt_text": "",
            "tags": [],
            "product_highlights": {
                "fabric": "",
                "zari": "",
                "colour": "",
                "pattern": "",
                "border": "",
                "technique": "",
                "weave": "",
                "occasion": "",
                "product_code": "",
            },
            "warnings": [],
        }
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": "Source facts:\n"
                + json.dumps(facts, ensure_ascii=False)
                + "\nRequired JSON shape:\n"
                + json.dumps(schema),
            }
        ]
        if use_image_input:
            content.extend(
                {"type": "image_url", "image_url": {"url": url}}
                for url in image_urls[:10]
                if url
            )
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        }
        result = self._request_json(payload)
        if not self._has_required_description_sections(result["description_html"]):
            payload["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": json.dumps(result, ensure_ascii=False),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Correct the JSON. Keep supported facts, but rebuild "
                            "description_html with these literal H2 headings in this exact "
                            "order: <h2>Introduction</h2>, "
                            "<h2>Fabric &amp; Craftsmanship</h2>, "
                            "<h2>Styling &amp; Occasion</h2>, "
                            "<h2>Product Highlights</h2>, "
                            "<h2>Material &amp; Wash Care</h2>, <h2>Note</h2>. "
                            "Return the complete strict JSON object only."
                        ),
                    },
                ]
            )
            result = self._request_json(payload)
        return result

    def _request_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        try:
            raw = body["choices"][0]["message"]["content"]
            result = json.loads(raw)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OpenRouterError(f"OpenRouter returned invalid JSON: {exc}") from exc
        self._validate(result)
        return result

    @staticmethod
    def _has_required_description_sections(description_html: str) -> bool:
        normalized = html.unescape(description_html)
        expected = (
            "Introduction",
            "Fabric & Craftsmanship",
            "Styling & Occasion",
            "Product Highlights",
            "Material & Wash Care",
            "Note",
        )
        headings = [
            re.sub(r"<[^>]+>", "", value).strip()
            for value in re.findall(
                r"<h2\b[^>]*>(.*?)</h2>", normalized, flags=re.IGNORECASE | re.DOTALL
            )
        ]
        return [value.casefold() for value in headings] == [
            value.casefold() for value in expected
        ]

    @staticmethod
    def _validate(result: dict[str, Any]) -> None:
        required_strings = (
            "title",
            "description_html",
            "seo_title",
            "seo_description",
            "image_alt_text",
        )
        if not isinstance(result, dict):
            raise OpenRouterError("OpenRouter result is not an object")
        for key in required_strings:
            if not isinstance(result.get(key), str):
                raise OpenRouterError(f"OpenRouter field {key} must be a string")
        if not isinstance(result.get("tags"), list):
            raise OpenRouterError("OpenRouter tags must be an array")
        if not isinstance(result.get("product_highlights"), dict):
            raise OpenRouterError("OpenRouter product_highlights must be an object")
        if not isinstance(result.get("warnings", []), list):
            raise OpenRouterError("OpenRouter warnings must be an array")
