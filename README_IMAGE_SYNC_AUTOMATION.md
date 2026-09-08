# Janardhana Silk House Image Sync Automation

## Scope

Automatic Baserow to Shopify image sync is split by product family:

- Sarees: `saree_image_sync.py`
- Accessories: `accessories_image_sync.py`
- Fabric: `fabric_image_sync.py`
- Suits: `suit_image_sync.py`

## Universal Rules

- Sync only rows where `Generation Status = Approved` and `SHOPIFY Notes = Approved`.
- Match Shopify product by `Product Code = Shopify variant SKU`.
- Do not read, check, update, or fallback to the plain `SHOPIFY` field.
- Do not create products.
- Do not change title, description, price, inventory, variants, collections, tags, vendor, product type, or product status.
- Keep old mannequin/product media.
- Skip duplicate generated media by existing alt text or source URL.
- Update only `Generation Status = Shopify-sync` after Shopify media is attached and READY.
- If Shopify SKU is missing, keep the Baserow row approved and write it to the missing SKU CSV.

## Saree Image Order

Saree generated image order:

1. `Generated Front View`
2. `Back View`
3. `Side View`
4. `Close Up View`

The script resolves `Generated Front View` by `generated_front_field_id` first from `config/saree_catalog_field_map.json`, then reorders Shopify media so the generated front image becomes the first/main image.

## Image Compression

Images larger than 20 MB are re-encoded through Pillow before staged Shopify upload:

- Convert to RGB.
- Remove alpha channel.
- Resize only when the largest side is above `4472` pixels.
- Start JPEG quality at `92`.
- Reduce quality down to `75` until the file is below 20 MB.

Helper: `utils/image_prepare.py`

## Commands

Run one-time saree front-image fix:

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1
$env:DRY_RUN='false'
$env:FIX_SAREE_FRONT_IMAGE_ONLY='true'
python saree_image_sync.py
```

Run all saree catalogs:

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
$env:PROCESS_ALL_SAREE_CATALOGS='true'
python saree_image_sync.py
```

Run accessories:

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
$env:PROCESS_ACCESSORIES_SYNC='true'
python accessories_image_sync.py
```

Run fabric:

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
$env:PROCESS_FABRIC_SYNC='true'
python fabric_image_sync.py
```

Run automatic order manually:

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'

$env:PROCESS_ALL_SAREE_CATALOGS='true'
python saree_image_sync.py

$env:PROCESS_ACCESSORIES_SYNC='true'
python accessories_image_sync.py

$env:PROCESS_FABRIC_SYNC='true'
python fabric_image_sync.py
```

## Reports

Saree front fix:

- `output/final_saree_front_image_fix_report.txt`
- `output/saree_front_image_fix_report.csv`
- `output/saree_front_image_missing.csv`
- `output/saree_front_image_duplicates_skipped.csv`
- `output/saree_front_image_compressed.csv`
- `logs/saree_front_image_fix.log`

Accessories:

- `output/final_accessories_shopify_image_sync_report.txt`
- `output/accessories_shopify_image_sync_report.csv`
- `output/accessories_shopify_missing_skus.csv`
- `output/accessories_shopify_image_failures.csv`
- `output/accessories_shopify_duplicates_skipped.csv`
- `output/accessories_shopify_compressed_images.csv`
- `logs/accessories_image_sync.log`

Fabric:

- `output/final_fabric_shopify_image_sync_report.txt`
- `output/fabric_shopify_image_sync_report.csv`
- `output/fabric_shopify_missing_skus.csv`
- `output/fabric_shopify_image_failures.csv`
- `output/fabric_shopify_duplicates_skipped.csv`
- `output/fabric_shopify_compressed_images.csv`
- `logs/fabric_image_sync.log`

## Scheduling

Use Windows Task Scheduler.

- Interval: every 30 minutes.
- Run order: Saree first, then Accessories, then Fabric.
- Failure alert: CSV/log files only.
- Product creation: disabled; match by SKU only.
