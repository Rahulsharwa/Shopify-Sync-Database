# Codex Task: Baserow → Shopify Saree Generated Image Sync

## Purpose

Build a new **approved generated saree image replacement pipeline** inside the existing `shopify-sync` project.

This task is separate from the existing fabric sync/enrichment workflow.

The new pipeline must:

1. Read approved generated saree image rows from Baserow.
2. Find the matching existing Shopify product by `Product Code` / SKU.
3. Add generated saree images to the Shopify product.
4. Set the generated front image as the main/first image.
5. Keep old mannequin images.
6. Update Baserow `Generation Status` to `Shopify-sync` only after successful image sync.

---

## Project Location

Use the existing project:

```powershell
C:\Users\rahul\Documents\Fabrics\shopify-sync
```

Do **not** create a new repository.

Do **not** merge this into the existing fabric `main.py` flow.

Create a separate script:

```text
saree_image_sync.py
```

---

## Non-Negotiable Safety Rules

- Never hardcode credentials.
- Never print tokens in logs.
- Never commit `.env`.
- Read all secrets from environment variables.
- Use Shopify Admin GraphQL API only.
- Do not use Shopify browser/admin UI automation.
- Do not create new Shopify products.
- Do not duplicate Shopify products.
- Do not change product title, description, price, inventory, variants, or publishing.
- Do not delete old mannequin images in this version.
- Only add generated saree images and set generated front image as main/first image.
- Always support `DRY_RUN=true`.
- Always support `MAX_PRODUCTS=5`.
- Always create preview, report, and failed CSV files.
- Always run 5-product dry run and 5-product live test before full production runs.

---

## Environment Variables

Add these to `.env.example`.

Do not put real values in `.env.example`.

```env
BASEROW_API_BASE=https://api.baserow.io
BASEROW_TOKEN=

SHOPIFY_STORE_DOMAIN=janardhana-silk-house-2.myshopify.com
SHOPIFY_ADMIN_ACCESS_TOKEN=
SHOPIFY_API_VERSION=2026-04

BASEROW_DATABASE_ID=419522

DRY_RUN=true
MAX_PRODUCTS=5

SOURCE_STATUS_NAME=Approved
DONE_STATUS_NAME=Shopify-sync

INCLUDE_TABLE_IDS=948083,935204,948245,935205,935207,935208,935203,935215,935206,935209,935210,935211,935213,935214,935216,935217,935218,935212
EXCLUDE_TABLE_IDS=935200,935202,985829
```

---

## Baserow Database

Database:

```text
Saree Generate
Database ID: 419522
```

---

## Included Baserow Tables

Process only these tables:

| Collection | Table ID |
|---|---:|
| Kanjivaram Silks | 948083 |
| Pure Silk Sarees | 935204 |
| Tussar Silk Saree | 948245 |
| South Weaves – South Silk Sarees | 935205 |
| Soft Silk Sarees | 935207 |
| Patola & Orissa Silk Sarees | 935208 |
| Printed Pure Silk Sarees | 935203 |
| Cotton Silk Sarees | 935215 |
| Paithani Silk Sarees | 935206 |
| Banarasi Georgette Silk Sarees | 935209 |
| Banarasi Silk Sarees | 935210 |
| Banarasi Kora Silk Saree | 935211 |
| Gadwal Handloom | 935213 |
| Jamawar Silk Sarees | 935214 |
| Cotton Saree | 935216 |
| Linen & Kota Silk Sarees | 935217 |
| Art Silk Sarees | 935218 |
| Bandhani Silk Saree | 935212 |

---

## Excluded Tables

Do not process these unless explicitly requested later:

| Collection | Table ID |
|---|---:|
| All_saree | 935200 |
| Tussar Silk Saree2 | 935202 |
| testing | 985829 |

---

## Dynamic Field Discovery Requirement

Field IDs may differ across Baserow tables.

Therefore, **do not hardcode field IDs**.

For each table:

1. Fetch Baserow table fields metadata.
2. Resolve required fields by normalized field names.
3. Use the resolved field names/IDs to read and update rows.

### Required Fields

Resolve these fields dynamically:

```text
Product Code
Generated Front View
Side View
Back View
Close Up View
Generation Status
SHOPIFY Notes
```

### Optional Writeback Fields

If these fields exist, use them:

```text
Shopify Status
Last Modified
Error Notes
warnings
Comment
```

Do not fail if optional fields are missing.

---

## Field Name Matching Rules

Normalize field names:

- Case-insensitive
- Trim whitespace
- Replace underscores/hyphens with spaces
- Collapse repeated spaces
- Ignore minor punctuation differences

Support variations:

| Canonical | Variations |
|---|---|
| Product Code | Product code, SKU, Product ID |
| Generated Front View | Front View, Generated Front, Front |
| Side View | Generated Side View, Side |
| Back View | Generated Back View, Back |
| Close Up View | Close-up View, Closeup View, Generated Close Up View |
| Generation Status | generate status, Generation status |
| SHOPIFY Notes | Shopify Notes, SHOPIFY notes, shopify notes |

---

## Eligibility Rule

A row is eligible only if all conditions are true:

```text
Generation Status = Approved
SHOPIFY Notes = Approved
Product Code is not empty
At least one generated image exists
```

Do not process rows where:

```text
Generation Status != Approved
SHOPIFY Notes != Approved
Product Code is empty
No generated images exist
```

---

## Baserow Single Select Handling

Baserow single select fields may return either:

```python
"Approved"
```

or:

```python
{"id": 123, "value": "Approved"}
```

Implement a helper:

```python
def get_select_value(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("value") or "").strip()
    return str(value).strip()
```

Use this helper for:

```text
Generation Status
SHOPIFY Notes
```

---

## Image Source Fields

Read Baserow file fields from:

```text
Generated Front View
Side View
Back View
Close Up View
```

Use only the original uploaded file URL:

```python
file_obj["url"]
```

Do not use:

```text
thumbnails.tiny.url
thumbnails.small.url
```

---

## Image Order

Upload/add images to Shopify in this exact order:

```text
1. Generated Front View
2. Side View
3. Back View
4. Close Up View
```

The **Generated Front View** image must become the first/main Shopify product image.

---

## Shopify Product Matching

Find Shopify product by SKU.

Use:

```text
Product Code = Shopify variant SKU
```

Matching behavior:

1. Search Shopify products by SKU = Baserow `Product Code`.
2. If exactly one product is found:
   - Use that product.
3. If zero products are found:
   - Do not create a new product.
   - Mark row failed.
   - Error: `Shopify product not found by SKU`.
4. If multiple products are found:
   - Do not update any product.
   - Mark row failed.
   - Error: `Multiple Shopify products found for SKU`.
5. Never create Shopify products in this task.

---

## Shopify Media Behavior

For each eligible row:

1. Find Shopify product by SKU.
2. Read existing Shopify product media/images.
3. Add generated images to the product.
4. Avoid duplicate uploads if the same source URL or alt text already exists.
5. Preserve image order:
   - front
   - side
   - back
   - close-up
6. Set generated front image as the first/main media.
7. Keep old mannequin images.
8. Do not delete existing Shopify images.
9. If one image upload fails:
   - Continue with other images.
   - Record warning.
10. If no images upload successfully:
   - Treat row as failed.
   - Keep Baserow `Generation Status = Approved`.

---

## Shopify Image Alt Text

Set alt text for uploaded generated images.

Format:

```text
<Product Code> Generated Front View
<Product Code> Generated Side View
<Product Code> Generated Back View
<Product Code> Generated Close Up View
```

Example:

```text
AB123456 Generated Front View
```

---

## Baserow Writeback

After successful Shopify image update:

```text
Generation Status = Shopify-sync
```

If these fields exist, also update:

```text
Shopify Status = Image Synced
Last Modified = current UTC timestamp
Error Notes = empty
warnings = warning text, if any non-fatal issue occurred
```

Do not modify:

```text
SHOPIFY Notes
```

unless explicitly requested later.

---

## Failure Writeback

If Shopify update fails:

```text
Generation Status remains Approved
```

If available, write error into:

```text
Error Notes
warnings
Comment
```

Also save the failed row in:

```text
output/saree_shopify_image_sync_failed.csv
```

---

## Required Helper Functions

Create these functions, or equivalent clean methods.

### `normalize_field_name`

```python
def normalize_field_name(name: str) -> str:
    """
    Normalize Baserow field names for robust dynamic matching.
    Lowercase, trim, replace underscores/hyphens with spaces,
    remove extra spaces, and simplify punctuation.
    """
```

### `get_table_field_map`

```python
def get_table_field_map(table_id: int) -> dict:
    """
    Fetch Baserow field metadata for one table.
    Return normalized field name -> field metadata.
    """
```

### `resolve_required_fields`

```python
def resolve_required_fields(field_map: dict) -> dict:
    """
    Resolve actual table field names/IDs for:
    Product Code,
    Generated Front View,
    Side View,
    Back View,
    Close Up View,
    Generation Status,
    SHOPIFY Notes.
    Raise/skips table gracefully if required fields are missing.
    """
```

### `is_row_approved`

```python
def is_row_approved(row: dict, fields: dict) -> bool:
    """
    True only if:
    Generation Status = Approved
    SHOPIFY Notes = Approved
    Product Code exists
    At least one generated image exists
    """
```

### `extract_generated_image_urls`

```python
def extract_generated_image_urls(row: dict, fields: dict) -> list[dict]:
    """
    Return ordered generated image list:
    [
      {"label": "Generated Front View", "url": "..."},
      {"label": "Side View", "url": "..."},
      {"label": "Back View", "url": "..."},
      {"label": "Close Up View", "url": "..."}
    ]
    Use original Baserow file URL only.
    """
```

### `find_shopify_product_by_sku`

```python
def find_shopify_product_by_sku(product_code: str) -> dict:
    """
    Search Shopify product by SKU.
    Return exactly one matching Shopify product or a structured error.
    """
```

### `sync_shopify_generated_images`

```python
def sync_shopify_generated_images(product: dict, images: list[dict], product_code: str) -> dict:
    """
    Add generated images to Shopify product.
    Set generated front image as first/main image.
    Keep existing mannequin images.
    Skip duplicate generated images.
    Return structured result:
    uploaded_count,
    duplicate_skipped_count,
    main_image_set,
    warnings
    """
```

### `update_baserow_row_status`

```python
def update_baserow_row_status(table_id: int, row_id: int, fields: dict, success: bool, warnings: str = "", error: str = ""):
    """
    On success, set Generation Status = Shopify-sync.
    On failure, keep Approved and write error if possible.
    """
```

---

## File Structure

Add:

```text
saree_image_sync.py
```

Reuse existing project modules if available:

```text
baserow_client.py
shopify_client.py
utils.py
```

Do not break:

```text
main.py
```

The fabric sync/enrichment flow must keep working.

---

## Output Reports

Create:

```text
output/saree_shopify_image_sync_preview.csv
output/saree_shopify_image_sync_report.csv
output/saree_shopify_image_sync_failed.csv
output/final_saree_shopify_image_sync_report.txt
```

---

## Report Columns

Each report row must include:

```text
Baserow table id
Baserow table name
Baserow row id
Product Code
Generation Status before
SHOPIFY Notes
Front image present yes/no
Side image present yes/no
Back image present yes/no
Close Up image present yes/no
Shopify product found yes/no
Shopify product title
Shopify product id
Shopify action
Images uploaded count
Duplicates skipped count
Main image set yes/no
New Generation Status
Warning
Error
```

---

## Logging

Create log file:

```text
logs/saree_image_sync.log
```

Log:

```text
Loaded table
Resolved fields
Rows loaded
Eligible approved rows
Rows skipped
Shopify SKU search result
Images planned
Images uploaded
Duplicates skipped
Main image set
Baserow status updated
Failures
Final summary
```

Never log tokens.

---

## Dry Run Behavior

When:

```env
DRY_RUN=true
```

The script must:

1. Read Baserow tables.
2. Discover fields dynamically.
3. Find eligible rows.
4. Validate generated image URLs.
5. Search Shopify product by SKU if Shopify token exists.
6. Build media sync plan.
7. Write preview CSV.
8. Do not update Shopify.
9. Do not update Baserow.

---

## Live Behavior

When:

```env
DRY_RUN=false
```

The script must:

1. Read eligible Baserow rows.
2. Search Shopify product by SKU.
3. Add generated images.
4. Set generated front image as main/first media.
5. Keep old mannequin images.
6. Update Baserow `Generation Status = Shopify-sync`.
7. Continue on row-level failure.
8. Write report and failed CSV.

---

## Run Commands

### Setup

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### Dry Run: 5 Rows

```powershell
$env:DRY_RUN='true'
$env:MAX_PRODUCTS='5'
python saree_image_sync.py
```

Expected:

```text
Only rows where Generation Status = Approved and SHOPIFY Notes = Approved are included.
Shopify products are found by SKU.
No Shopify writes.
No Baserow writes.
Preview report created.
```

### Live Test: 5 Rows

```powershell
$env:DRY_RUN='false'
$env:MAX_PRODUCTS='5'
python saree_image_sync.py
```

Expected:

```text
5 eligible approved saree rows processed.
Existing Shopify products found by SKU.
Generated images added to Shopify.
Generated front image becomes main image.
Old mannequin images remain.
Baserow Generation Status updated to Shopify-sync.
No new Shopify products created.
```

### Full Run After Manual Approval

```powershell
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
python saree_image_sync.py
```

---

## Final Summary Output

At the end, print:

```text
Tables scanned
Tables skipped
Rows loaded
Eligible rows
Rows skipped
Shopify products found
Shopify products not found
Products with duplicate SKU matches
Images uploaded
Duplicate images skipped
Main images set
Baserow rows updated
Rows failed
Preview/report path
Failed CSV path
```

---

## Acceptance Criteria

1. Existing fabric sync in `main.py` remains untouched and working.
2. New task is implemented in `saree_image_sync.py`.
3. Field IDs may differ table-by-table, but processing still works by dynamic field name discovery.
4. Only rows where both `Generation Status` and `SHOPIFY Notes` are `Approved` are processed.
5. `Product Code` is used to find Shopify product by SKU.
6. No new Shopify products are created.
7. No duplicate Shopify products are created.
8. Generated Front/Side/Back/Close Up images are uploaded in correct order.
9. Generated Front View becomes main/first product image.
10. Old mannequin images remain.
11. Baserow `Generation Status` becomes `Shopify-sync` only after Shopify image sync success.
12. Failed rows remain `Approved`.
13. Failed rows are logged clearly.
14. Dry run writes preview and performs no Shopify/Baserow writes.
15. Live 5-product test works before full run.
16. Reports are created under `output/`.
17. Logs are created under `logs/`.
18. Secrets are never logged or committed.
