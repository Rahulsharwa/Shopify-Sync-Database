from __future__ import annotations

from typing import Any
import requests


class ShopifyError(RuntimeError): pass


class ShopifyClient:
    def __init__(self, domain: str, token: str, api_version: str, timeout: int = 45):
        self.domain = domain.strip().removeprefix("https://").rstrip("/")
        self.endpoint = f"https://{self.domain}/admin/api/{api_version}/graphql.json"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Shopify-Access-Token": token, "Content-Type": "application/json", "User-Agent": "baserow-shopify-sync/1.0"})
        self._collection_id_cache: dict[str, str | None] = {}

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.post(self.endpoint, json={"query": query, "variables": variables or {}}, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        if body.get("errors"): raise ShopifyError(str(body["errors"]))
        return body["data"]

    def check_connection(self) -> str:
        return self.graphql("query { shop { name } }")["shop"]["name"]

    def find_collection(self, title: str) -> dict[str, Any] | None:
        q = "query($q:String!){collections(first:10,query:$q){nodes{id title}}}"
        nodes = self.graphql(q, {"q": f"title:{title}"})["collections"]["nodes"]
        return next((node for node in nodes if node["title"].casefold() == title.casefold()), None)

    def get_collection_id_by_title(self, title: str) -> str | None:
        """Resolve and cache an exact Shopify collection title for this run."""
        normalized = title.strip().casefold()
        if normalized not in self._collection_id_cache:
            collection = self.find_collection(title.strip())
            self._collection_id_cache[normalized] = (
                str(collection["id"]) if collection else None
            )
        return self._collection_id_cache[normalized]

    def product_collection_ids(self, product_id: str) -> set[str]:
        query = """query($id:ID!){
          product(id:$id){collections(first:250){nodes{id title}}}
        }"""
        product = self.graphql(query, {"id": product_id}).get("product")
        if not product:
            raise ShopifyError("shopify_product_not_found")
        return {
            str(node["id"])
            for node in product.get("collections", {}).get("nodes", [])
        }

    def ensure_product_in_collection(
        self, product_id: str, collection_id: str
    ) -> tuple[bool, bool]:
        """Return (assigned, already_assigned) after verified idempotent add."""
        if collection_id in self.product_collection_ids(product_id):
            return True, True
        mutation = """mutation($id:ID!,$productIds:[ID!]!){
          collectionAddProducts(id:$id,productIds:$productIds){
            collection{id}
            userErrors{field message}
          }
        }"""
        result = self.graphql(
            mutation,
            {"id": collection_id, "productIds": [product_id]},
        )["collectionAddProducts"]
        if result["userErrors"]:
            raise ShopifyError(str(result["userErrors"]))
        if collection_id not in self.product_collection_ids(product_id):
            raise ShopifyError("collection_membership_verification_failed")
        return True, False

    def find_taxonomy_category(self, name: str) -> dict[str, Any] | None:
        q = "query($search:String!){taxonomy{categories(first:50,search:$search){nodes{id name fullName}}}}"
        search = "Fabric" if name.casefold() == "fabric in textiles" else name
        nodes = self.graphql(q, {"search": search})["taxonomy"]["categories"]["nodes"]
        target = name.casefold()
        exact = next((node for node in nodes if node.get("name", "").casefold() == target or node.get("fullName", "").casefold() == target), None)
        if exact: return exact
        if target == "fabric in textiles":
            return next((node for node in nodes if node.get("name") == "Fabric" and node.get("fullName", "").endswith("Textiles > Fabric")), None)
        return None

    def find_metaobject_value(self, definition_id: str, value: str) -> str | None:
        q = "query($id:ID!){metaobjectDefinition(id:$id){type}}"
        definition = self.graphql(q, {"id": definition_id}).get("metaobjectDefinition")
        if not definition: return None
        q = "query($type:String!){metaobjects(type:$type,first:250){nodes{id displayName}}}"
        nodes = self.graphql(q, {"type": definition["type"]})["metaobjects"]["nodes"]
        target = value.casefold()
        exact = next((node for node in nodes if node.get("displayName", "").casefold() == target), None)
        return exact["id"] if exact else None

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        q = "query($id:ID!){product(id:$id){id title status variants(first:10){nodes{id sku}} media(first:100){nodes{id ... on MediaImage{image{url}}}} collections(first:100){nodes{id}}}}"
        return self.graphql(q, {"id": product_id}).get("product")

    def find_by_sku(self, sku: str) -> dict[str, Any] | None:
        q = "query($q:String!){productVariants(first:2,query:$q){nodes{id sku product{id title status variants(first:10){nodes{id sku}} media(first:100){nodes{id ... on MediaImage{image{url}}}} collections(first:100){nodes{id}}}}}}"
        nodes = self.graphql(q, {"q": f"sku:{sku}"})["productVariants"]["nodes"]
        exact = [node for node in nodes if node.get("sku") == sku]
        if len(exact) > 1: raise ShopifyError(f"multiple Shopify variants found for SKU {sku}")
        return exact[0]["product"] if exact else None

    def product_set(self, product_input: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        mutation = """mutation($input:ProductSetInput!){productSet(synchronous:true,input:$input){product{id title status variants(first:10){nodes{id sku}} media(first:100){nodes{id ... on MediaImage{image{url}}}} collections(first:100){nodes{id}}} userErrors{field message}}}"""
        result = self.graphql(mutation, {"input": product_input})["productSet"]
        if result["userErrors"]: raise ShopifyError(str(result["userErrors"]))
        return result["product"], []

    def product_set_with_unit_fallback(self, product_input: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        try:
            return self.product_set(product_input)
        except ShopifyError as exc:
            variants = product_input.get("variants", [])
            if not variants or not any("unitPriceMeasurement" in v or "showUnitPrice" in v for v in variants):
                raise
            for variant in variants:
                variant.pop("unitPriceMeasurement", None); variant.pop("showUnitPrice", None)
            product, _ = self.product_set(product_input)
            return product, [f"Unit price unavailable: {exc}"]

    def add_to_collection(self, collection_id: str, product_id: str) -> None:
        q = "mutation($id:ID!,$products:[ID!]!){collectionAddProducts(id:$id,productIds:$products){userErrors{field message}}}"
        errors = self.graphql(q, {"id": collection_id, "products": [product_id]})["collectionAddProducts"]["userErrors"]
        if errors: raise ShopifyError(str(errors))

    def set_metafields(self, product_id: str, fields: list[dict[str, str]]) -> None:
        if not fields: return
        values = [{**field, "ownerId": product_id} for field in fields]
        q = "mutation($values:[MetafieldsSetInput!]!){metafieldsSet(metafields:$values){userErrors{field message}}}"
        errors = self.graphql(q, {"values": values})["metafieldsSet"]["userErrors"]
        if errors: raise ShopifyError(str(errors))

    def create_media(self, product_id: str, media: list[dict[str, str]]) -> None:
        if not media: return
        values = [{"originalSource": m["url"], "alt": m["alt"], "mediaContentType": "IMAGE"} for m in media]
        q = "mutation($id:ID!,$media:[CreateMediaInput!]!){productCreateMedia(productId:$id,media:$media){media{id} mediaUserErrors{field message}}}"
        result = self.graphql(q, {"id": product_id, "media": values})["productCreateMedia"]
        if result["mediaUserErrors"]: raise ShopifyError(str(result["mediaUserErrors"]))
