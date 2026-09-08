from __future__ import annotations

from typing import Any, Iterator
import requests


class BaserowClient:
    def __init__(self, base_url: str, token: str, table_id: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.table_id = table_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Token {token}", "User-Agent": "baserow-shopify-sync/1.0"})

    def iter_rows(self, max_products: int | None = None) -> Iterator[dict[str, Any]]:
        url = f"{self.base_url}/api/database/rows/table/{self.table_id}/"
        params: dict[str, Any] | None = {"user_field_names": "true", "size": 200}
        yielded = 0
        while url:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            for row in body.get("results", []):
                if max_products is not None and yielded >= max_products:
                    return
                yielded += 1
                yield row
            url = body.get("next")
            params = None

    def update_row(self, row_id: int, values: dict[str, Any]) -> None:
        url = f"{self.base_url}/api/database/rows/table/{self.table_id}/{row_id}/"
        response = self.session.patch(url, params={"user_field_names": "true"}, json=values, timeout=self.timeout)
        response.raise_for_status()

    def update_row_with_select_fallback(self, row_id: int, values: dict[str, Any], field: str, option_id: int) -> None:
        """Retry a select write with its numeric option ID when text is rejected."""
        try:
            self.update_row(row_id, values)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in {400, 422}:
                raise
            retry = dict(values)
            retry[field] = option_id
            self.update_row(row_id, retry)
