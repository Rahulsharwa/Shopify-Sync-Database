# Men Accessories -> Shopify Product Create

Separate workflow for creating Shopify tie products from the Baserow `Men Accessories` table.

## Source

- Database ID: `419522`
- Table: `Men Accessories`
- Table ID: `936098`
- Main image: `Generated Front View` (`8133053`)
- Reference image: `Tie Image` (`8133049`), not used as storefront main image
- Approval fields: `Generation Status`, `SHOPIFY Notes`

## Safety Rules

- Creates only when `CREATE_PRODUCTS_FROM_MEN_ACCESSORIES=true`.
- Full sync without `MAX_PRODUCTS` requires `CONFIRM_MEN_ACCESSORIES_FULL_SYNC=true`.
- Duplicate checks run before create: SKU, generated handle, exact title.
- Baserow `Generation Status` is updated to `Shopify-sync` only after product, media, inventory, publication, and persistence verification pass.
- `SHOPIFY Notes` is never modified.
- Main image is downloaded from Baserow original `file_obj["url"]`, resized to `2304 x 4096`, staged uploaded, verified `READY`, and set first.

## Audit Only

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1

$env:CHECK_MEN_ACCESSORIES_BASEROW_ACCESS='true'
python create_shopify_products_from_men_accessories.py
```

## Dry Run

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1

Remove-Item Env:\CHECK_MEN_ACCESSORIES_BASEROW_ACCESS -ErrorAction SilentlyContinue
$env:DRY_RUN='true'
$env:MAX_PRODUCTS='5'
$env:CREATE_PRODUCTS_FROM_MEN_ACCESSORIES='true'
python create_shopify_products_from_men_accessories.py
```

## Five-Product Live Canary

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1

Remove-Item Env:\CHECK_MEN_ACCESSORIES_BASEROW_ACCESS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
$env:MAX_PRODUCTS='5'
$env:MAX_SUCCESSFUL_PRODUCTS='5'
$env:CREATE_PRODUCTS_FROM_MEN_ACCESSORIES='true'
$env:MEN_ACCESSORIES_PRODUCT_STATUS='ACTIVE'
$env:MEN_ACCESSORIES_PUBLISH_ONLINE_STORE='true'
$env:SHOPIFY_ONLINE_STORE_PUBLICATION_NAME='Online Store'
$env:VERIFY_PRODUCT_ONLINE_STORE_PUBLISHED='true'
$env:MEN_ACCESSORIES_INVENTORY_TRACKED='true'
$env:MEN_ACCESSORIES_INVENTORY_QUANTITY='1'
$env:MEN_ACCESSORIES_INVENTORY_POLICY='DENY'
$env:MEN_ACCESSORIES_LOCATION_NAME='Janardhana Silk House JC Road'
$env:MEN_ACCESSORIES_TAXABLE='true'
$env:MEN_ACCESSORIES_MAX_TAGS='6'
$env:MEN_ACCESSORIES_TAG_STYLE='clean'
$env:MEN_ACCESSORIES_RESIZE_BEFORE_SHOPIFY='true'
$env:MEN_ACCESSORIES_TARGET_WIDTH='2304'
$env:MEN_ACCESSORIES_TARGET_HEIGHT='4096'
$env:MEN_ACCESSORIES_RESIZE_FILTER='LANCZOS'
$env:MEN_ACCESSORIES_RESIZE_FORMAT='preserve'
$env:MEN_ACCESSORIES_JPEG_EXPORT_QUALITY='95'
$env:MEN_ACCESSORIES_REQUIRE_TARGET_DIMENSIONS='true'
$env:MEN_ACCESSORIES_RESIZE_FIELDS='Generated Front View'
python create_shopify_products_from_men_accessories.py
```

## Full Sync After Canary Approval

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1

Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
$env:CONFIRM_MEN_ACCESSORIES_FULL_SYNC='true'
$env:CREATE_PRODUCTS_FROM_MEN_ACCESSORIES='true'
python create_shopify_products_from_men_accessories.py
```

## Reports

New products retain the `Men Accessories` collection and are also assigned to
the exact Shopify collection `New Arrivals`. This is enabled by default with
`ADD_NEW_PRODUCTS_TO_NEW_ARRIVALS=true`; the exact collection title is set by
`SHOPIFY_NEW_ARRIVALS_COLLECTION_NAME`.

- `output/men_accessories_baserow_field_audit.json`
- `output/men_accessories_baserow_field_audit.txt`
- `output/men_accessories_reference_product_audit.json`
- `output/men_accessories_reference_product_audit.txt`
- `output/men_accessories_product_create_preview.csv`
- `output/men_accessories_product_create_report.csv`
- `output/men_accessories_product_create_failed.csv`
- `output/men_accessories_product_create_duplicates.csv`
- `output/men_accessories_product_create_missing_fields.csv`
- `output/men_accessories_product_create_ai_report.csv`
- `output/men_accessories_product_create_image_failures.csv`
- `output/men_accessories_product_persistence_check.csv`
- `output/final_men_accessories_product_create_report.txt`
- `logs/men_accessories_product_create.log`
