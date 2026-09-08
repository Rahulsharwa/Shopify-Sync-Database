# Upload Saree → Shopify Product Creator

This is a separate product-creation pipeline. It does not change any existing
product or image sync script.

It reads Baserow table `Upload Saree` (`1076991`) and processes only rows where:

- `Generation Status = Approved`
- `SHOPIFY Notes = Approved`
- Product Title, Product Code, Price, and Front View are present

Before creation it checks exact Shopify SKU and handle matches, then checks an
exact title match. Any match is skipped and reported. New products default to
`ACTIVE`. The Front View is uploaded first and verified as the first
media item. On complete success only `Generation Status` is changed to
`Shopify-sync`; `SHOPIFY Notes` is never changed.

Before Baserow success writeback, the creator verifies Online Store publication,
tracked inventory with `available=1` and `on_hand=1` at the configured location,
DENY inventory policy, taxable status, and media readiness. Tags are clean
storefront tags only, with a maximum of six.

The product category resolves from Baserow `Category` when Shopify has an exact
taxonomy match; otherwise it uses Shopify taxonomy `Saris`
(`gid://shopify/TaxonomyCategory/aa-1-23-2-1`).

## Environment

Add these values to `.env`:

```env
BASEROW_API_BASE=https://api.baserow.io
BASEROW_TOKEN=
BASEROW_DATABASE_ID=419522
UPLOAD_SAREE_BASEROW_TABLE_ID=1076991

SHOPIFY_STORE_DOMAIN=
SHOPIFY_ADMIN_ACCESS_TOKEN=
SHOPIFY_API_VERSION=2026-04

OPENROUTER_API_KEY=
OPENROUTER_MODEL=openai/gpt-4.1-mini

DRY_RUN=true
MAX_PRODUCTS=5
CREATE_PRODUCTS_FROM_UPLOAD_SAREE=true
WRITE_BASEROW_COMMENTS=true
USE_OPENROUTER_IMAGE_INPUT=true

UPLOAD_SAREE_PRODUCT_STATUS=ACTIVE
UPLOAD_SAREE_PUBLISH_ONLINE_STORE=true
SHOPIFY_ONLINE_STORE_PUBLICATION_NAME=Online Store
VERIFY_PRODUCT_ONLINE_STORE_PUBLISHED=true
UPLOAD_SAREE_INVENTORY_TRACKED=true
UPLOAD_SAREE_INVENTORY_QUANTITY=1
UPLOAD_SAREE_INVENTORY_POLICY=DENY
SHOPIFY_LOCATION_NAME=Janardhana Silk House JC Road
ALLOW_LOCATION_FALLBACK=false
UPLOAD_SAREE_TAXABLE=true
UPLOAD_SAREE_MAX_TAGS=6
UPLOAD_SAREE_TAG_STYLE=clean
UPLOAD_SAREE_IMAGE_UPLOAD_MODE=original_only
UPLOAD_SAREE_DISABLE_COMPRESSION=true
UPLOAD_SAREE_DISABLE_RESIZE=true
UPLOAD_SAREE_DISABLE_REENCODE=true
UPLOAD_SAREE_KEEP_EXACT_ORIGINAL=true
UPLOAD_SAREE_FAIL_IF_ORIGINAL_UPLOAD_FAILS=true
UPLOAD_SAREE_ALLOW_COMPRESSION_FALLBACK=false
UPLOAD_SAREE_MAX_ORIGINAL_UPLOAD_MB=20
UPLOAD_SAREE_JPEG_QUALITY_START=98
UPLOAD_SAREE_JPEG_QUALITY_MIN=95
```

Live mode requires `OPENROUTER_API_KEY`. Dry run can operate without it and
uses conservative deterministic copy, clearly marked in the AI report.
The creator is pinned to table `1076991`; it deliberately ignores a different
shared `BASEROW_TABLE_ID` used by the older sync scripts.

## Baserow access diagnostic

This mode checks only Baserow field metadata and one-row read access. It does
not initialize Shopify or OpenRouter, create products, or update Baserow.
`BASEROW_TOKEN` is preferred; `BASEROW_API_TOKEN` is used only when
`BASEROW_TOKEN` is absent.

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1

$env:CHECK_UPLOAD_SAREE_BASEROW_ACCESS='true'
python create_shopify_products_from_upload_saree.py
```

Reports:

- `output/upload_saree_baserow_access_check.txt`
- `output/upload_saree_baserow_access_check.json`
- `logs/upload_saree_product_create.log`

Before a normal dry or live product run, disable the diagnostic flag:

```powershell
Remove-Item Env:\CHECK_UPLOAD_SAREE_BASEROW_ACCESS -ErrorAction SilentlyContinue
```

## Required first run

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1

$env:DRY_RUN='true'
$env:MAX_PRODUCTS='5'
$env:CREATE_PRODUCTS_FROM_UPLOAD_SAREE='true'
python create_shopify_products_from_upload_saree.py
```

Review:

- `output/upload_saree_product_create_preview.csv`
- `output/upload_saree_product_create_duplicates.csv`
- `output/upload_saree_product_create_missing_fields.csv`
- `output/upload_saree_product_create_ai_report.csv`
- `output/final_upload_saree_product_create_report.txt`

Only after approving that preview, run the five-product live test:

```powershell
$env:DRY_RUN='false'
$env:MAX_PRODUCTS='5'
$env:CREATE_PRODUCTS_FROM_UPLOAD_SAREE='true'
python create_shopify_products_from_upload_saree.py
```

Do not remove `MAX_PRODUCTS` until the five created Draft products have been
reviewed in Shopify.

## Original-quality image policy

Images at or below 20 MB use the original Baserow `file_obj["url"]` without
Pillow, resize, conversion, or compression. If Shopify rejects that URL, the
same original bytes are staged. Compression is disabled by default. An image
over 20 MB fails unless `UPLOAD_SAREE_ALLOW_COMPRESSION_FALLBACK=true` is
explicitly set; that exceptional path starts at JPEG quality 98 and does not
go below 95 unless aggressive compression is also explicitly enabled.

Run the read-only quality audit before another product-creation run:

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1

Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='true'
$env:AUDIT_UPLOAD_SAREE_IMAGE_UPLOAD_SOURCE='true'
$env:UPLOAD_SAREE_IMAGE_UPLOAD_MODE='original_only'
$env:UPLOAD_SAREE_DISABLE_COMPRESSION='true'
$env:UPLOAD_SAREE_DISABLE_RESIZE='true'
$env:UPLOAD_SAREE_DISABLE_REENCODE='true'
$env:UPLOAD_SAREE_KEEP_EXACT_ORIGINAL='true'
$env:UPLOAD_SAREE_ALLOW_COMPRESSION_FALLBACK='false'
python create_shopify_products_from_upload_saree.py
```

Repair no more than five already-created products before visual review:

```powershell
$env:DRY_RUN='false'
$env:MAX_PRODUCTS='5'
$env:REPAIR_UPLOAD_SAREE_LOW_QUALITY_MEDIA='true'
Remove-Item Env:\AUDIT_UPLOAD_SAREE_IMAGE_UPLOAD_SOURCE -ErrorAction SilentlyContinue
$env:UPLOAD_SAREE_IMAGE_UPLOAD_MODE='original_only'
$env:UPLOAD_SAREE_DISABLE_COMPRESSION='true'
$env:UPLOAD_SAREE_DISABLE_RESIZE='true'
$env:UPLOAD_SAREE_DISABLE_REENCODE='true'
$env:UPLOAD_SAREE_KEEP_EXACT_ORIGINAL='true'
$env:UPLOAD_SAREE_FAIL_IF_ORIGINAL_UPLOAD_FAILS='true'
$env:UPLOAD_SAREE_ALLOW_COMPRESSION_FALLBACK='false'
python create_shopify_products_from_upload_saree.py
```

Do not run unlimited product creation until the five repaired/new products
pass visual review in Shopify.

New products retain their mapped/category collection and are also assigned to
the exact Shopify collection `New Arrivals`. This is enabled by default with
`ADD_NEW_PRODUCTS_TO_NEW_ARRIVALS=true` and can only be disabled explicitly.
Override the exact title with `SHOPIFY_NEW_ARRIVALS_COLLECTION_NAME`.

## Safe correction modes

Fix up to five products confirmed by their Upload Saree row, `Shopify-sync`
status, SKU, and expected source handle:

```powershell
$env:DRY_RUN='false'
$env:MAX_PRODUCTS='5'
$env:FIX_UPLOAD_SAREE_CREATED_PRODUCTS='true'
python create_shopify_products_from_upload_saree.py
```

Fix one explicitly identified product without changing its title, description,
price, images, or collections:

```powershell
$env:DRY_RUN='false'
$env:FIX_UPLOAD_SAREE_PRODUCT_PUBLISHING='true'
$env:FIX_SHOPIFY_PRODUCT_LEGACY_ID='8991578161350'
$env:FIX_INVENTORY='true'
$env:FIX_TAGS='true'
python create_shopify_products_from_upload_saree.py
```

Fix reports:

- `output/upload_saree_created_products_fix_report.csv`
- `output/final_upload_saree_created_products_fix_report.txt`
- `output/upload_saree_product_publish_fix_report.csv`
- `output/final_upload_saree_product_publish_fix_report.txt`

## Existing-product optional media enrichment

Match Upload Saree rows to existing Shopify products by exact SKU and add only
missing `Side View`, `Back View`, `Close-Up`, and `BlouseGrid` media. This mode
never creates products, reorders media, or changes product attributes. It checks
both `Approved` and `Shopify-sync` rows whose `SHOPIFY Notes` is `Approved`.

```powershell
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='true'
$env:SYNC_UPLOAD_SAREE_EXISTING_MEDIA='true'
$env:CREATE_PRODUCTS_FROM_UPLOAD_SAREE='false'
python create_shopify_products_from_upload_saree.py
```

After reviewing the preview, change `DRY_RUN` to `false`. Reports are written to
`output/upload_saree_existing_media_sync_report.csv`,
`output/upload_saree_existing_media_sync_failed.csv`, and
`output/final_upload_saree_existing_media_sync_report.txt`; the dedicated log is
`logs/upload_saree_existing_media_sync.log`.
