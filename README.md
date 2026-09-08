# Shopify Sync Database

Production-oriented Python tools for synchronizing Baserow catalog data and media with Shopify. The project supports fabric product enrichment, saree/suit/accessory image synchronization, Upload Saree product creation, Upload Saree existing-product media completion, and Men Accessories product creation/reconciliation.

Products are matched by exact SKU/Product Code wherever an existing Shopify product is required. Every write workflow supports a dry-run or a capped test before an uncapped live run. Runtime reports are written to `output/` and logs to `logs/`; neither directory's generated contents are committed.

## Safety rules

- Keep credentials only in `.env` or `Input/.env`. These files are ignored by Git.
- Never paste access tokens into source, reports, commits, or console commands.
- Run `DRY_RUN=true` with `MAX_PRODUCTS=5` before a new or changed live workflow.
- Remove `MAX_PRODUCTS` only after reviewing the capped test.
- Enable only the mode needed for the current run; clear stale mode variables first.
- Product-creation modes perform duplicate checks by SKU, handle, and exact title.
- Existing-media modes never create products or change merchandising data.
- Review `output/` and `logs/` after every live run. Failed Baserow rows should remain eligible for a safe retry.

## Requirements and setup

- Windows PowerShell
- Python 3.10 or newer
- A Baserow database token with access to the required tables and write access to status/report fields
- A Shopify Admin API token with the required product, media, inventory, publication, and collection scopes
- An OpenRouter API key for AI-assisted new-product descriptions

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Fill in `.env` locally. Common variables include `BASEROW_API_BASE`, `BASEROW_TOKEN`, `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_ACCESS_TOKEN`, `SHOPIFY_API_VERSION`, and `OPENROUTER_API_KEY`. Do not commit `.env`.

## Tests

Run the focused automated test suite before a production run:

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1
python -m pytest -q
```

## Fabric product sync (`main.py`)

Start with a five-product preview, then a capped live run:

```powershell
$env:DRY_RUN='true';  $env:MAX_PRODUCTS='5'; python main.py
$env:DRY_RUN='false'; $env:MAX_PRODUCTS='5'; python main.py
```

After review, run all remaining eligible rows:

```powershell
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
python main.py
```

## Saree catalog image sync (`saree_image_sync.py`)

The image sync updates existing Shopify products only. It matches Product Code to Shopify SKU, preserves existing media, uploads missing generated images, and writes the configured Baserow status only after success.

All configured saree catalogs:

```powershell
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
$env:SYNC_ALL_SAREE_GENERATE_CATEGORIES='true'
python saree_image_sync.py
```

Use a capped preview by setting `DRY_RUN='true'` and `MAX_PRODUCTS='5'`. Single-catalog test variables are `PROCESS_SINGLE_CATALOG`, `CATALOG_NAME`, `CATALOG_TABLE_ID`, `GENERATION_STATUS_FIELD_ID`, and `SHOPIFY_NOTES_FIELD_ID`.

## Suit catalog image sync (`suit_image_sync.py`)

```powershell
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN='false'
$env:PROCESS_SUIT_CATALOGS='true'
python suit_image_sync.py
```

For a capped validation, set `DRY_RUN='true'` and `MAX_PRODUCTS='5'`. Catalog and field mappings are stored under `config/`.

## Accessories and fabric image sync

```powershell
$env:DRY_RUN='true'; $env:MAX_PRODUCTS='5'; $env:PROCESS_ACCESSORIES_SYNC='true'; python accessories_image_sync.py
$env:DRY_RUN='true'; $env:MAX_PRODUCTS='5'; $env:PROCESS_FABRIC_SYNC='true'; python fabric_image_sync.py
```

Change `DRY_RUN` to `false` for the capped live test. After review, remove `MAX_PRODUCTS` for the full run.

## Upload Saree access diagnostic

This read-only mode checks Baserow table `1076991`. It does not call Shopify or OpenRouter and does not update Baserow.

```powershell
$env:CHECK_UPLOAD_SAREE_BASEROW_ACCESS='true'
python create_shopify_products_from_upload_saree.py
Remove-Item Env:\CHECK_UPLOAD_SAREE_BASEROW_ACCESS -ErrorAction SilentlyContinue
```

## Upload Saree new-product creation

This mode creates only new products from approved Upload Saree rows. It validates required fields, prevents duplicate SKU/handle/title products, uploads configured media, publishes ACTIVE products, sets inventory, assigns collections, and updates Baserow only after full success.

Safe five-product preview:

```powershell
Remove-Item Env:\CHECK_UPLOAD_SAREE_BASEROW_ACCESS -ErrorAction SilentlyContinue
$env:PRODUCT_CREATE_SOURCE='upload_saree'
$env:CREATE_PRODUCTS_FROM_UPLOAD_SAREE='true'
$env:DRY_RUN='true'
$env:MAX_PRODUCTS='5'
python create_shopify_products_from_upload_saree.py
```

Five-product live test: keep the same variables and change `DRY_RUN` to `false`. For the approved full run:

```powershell
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:PRODUCT_CREATE_SOURCE='upload_saree'
$env:CREATE_PRODUCTS_FROM_UPLOAD_SAREE='true'
$env:CONFIRM_UPLOAD_SAREE_FULL_SYNC='true'
$env:DRY_RUN='false'
$env:ADD_NEW_PRODUCTS_TO_NEW_ARRIVALS='true'
$env:SHOPIFY_NEW_ARRIVALS_COLLECTION_NAME='New Arrivals'
$env:UPLOAD_SAREE_PRODUCT_STATUS='ACTIVE'
$env:UPLOAD_SAREE_PUBLISH_ONLINE_STORE='true'
$env:VERIFY_PRODUCT_ONLINE_STORE_PUBLISHED='true'
$env:UPLOAD_SAREE_INVENTORY_TRACKED='true'
$env:UPLOAD_SAREE_INVENTORY_QUANTITY='1'
$env:UPLOAD_SAREE_INVENTORY_POLICY='DENY'
$env:SHOPIFY_LOCATION_NAME='Janardhana Silk House JC Road'
$env:UPLOAD_SAREE_TAXABLE='true'
$env:UPLOAD_SAREE_MAX_TAGS='6'
$env:UPLOAD_SAREE_TAG_STYLE='clean'
$env:UPLOAD_SAREE_BLOCK_THUMBNAIL_URLS='true'
python create_shopify_products_from_upload_saree.py
```

## Upload Saree existing-product media completion

This mutually exclusive mode finds exactly one existing Shopify product by SKU and uploads only missing configured media. It does not create products or modify title, description, price, inventory, tags, collections, status, or publishing.

Preview first:

```powershell
Remove-Item Env:\CREATE_PRODUCTS_FROM_UPLOAD_SAREE -ErrorAction SilentlyContinue
$env:SYNC_UPLOAD_SAREE_EXISTING_MEDIA='true'
$env:DRY_RUN='true'
$env:MAX_PRODUCTS='5'
python create_shopify_products_from_upload_saree.py
```

Full live media completion after reviewing the preview:

```powershell
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
Remove-Item Env:\CREATE_PRODUCTS_FROM_UPLOAD_SAREE -ErrorAction SilentlyContinue
$env:SYNC_UPLOAD_SAREE_EXISTING_MEDIA='true'
$env:DRY_RUN='false'
python create_shopify_products_from_upload_saree.py
```

## Men Accessories product creation and reconciliation

The existing entry point routes `PRODUCT_CREATE_SOURCE=men_accessories` to the Men Accessories implementation. It uploads all configured original images, prevents duplicate products/media, assigns the required collections, and uses the existing OpenRouter description path.

```powershell
Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:PRODUCT_CREATE_SOURCE='men_accessories'
$env:DRY_RUN='false'
$env:MEN_ACCESSORIES_COLLECTION_NAME="Men's Ties"
$env:ADD_NEW_PRODUCTS_TO_NEW_ARRIVALS='true'
$env:SHOPIFY_NEW_ARRIVALS_COLLECTION_NAME='New Arrivals'
$env:MEN_ACCESSORIES_MEDIA_COMPLETION_EXISTING='true'
$env:MEN_ACCESSORIES_NEVER_DUPLICATE_MEDIA='true'
python create_shopify_products_from_upload_saree.py
```

To reconcile already-synced Men Accessories products instead of creating new ones:

```powershell
$env:PRODUCT_CREATE_SOURCE='men_accessories'
$env:RECONCILE_MEN_ACCESSORIES_SHOPIFY_SYNC='true'
$env:DRY_RUN='false'
$env:MEN_ACCESSORIES_REQUIRED_COLLECTION_1_ID='339913605318'
$env:MEN_ACCESSORIES_REQUIRED_COLLECTION_2_ID='339549880518'
python create_shopify_products_from_upload_saree.py
```

## Scheduled image sync

`run_image_sync_automation.ps1` runs the configured image workflows in sequence. Configure Windows Task Scheduler to call it every 30 minutes with the project directory as the working directory. Review the script and perform manual dry-runs before enabling the scheduled task.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\rahul\Documents\Fabrics\shopify-sync\run_image_sync_automation.ps1
```

## Reports and troubleshooting

- Product and media reports: `output/`
- Operational logs: `logs/`
- Catalog/table mappings: `config/`
- Workflow-specific details: `README_UPLOAD_SAREE_PRODUCT_CREATE.md`, `README_MEN_ACCESSORIES_PRODUCT_CREATE.md`, and `README_IMAGE_SYNC_AUTOMATION.md`

A non-zero exit can mean one or more rows failed while other rows completed successfully. Inspect the final text report, failed CSV, and log before retrying. Correct missing Baserow data or permissions first; duplicate-safe workflows can then be rerun.

## Git publication

Before committing, confirm `.env`, runtime output, logs, virtual environments, caches, and temporary media are excluded:

```powershell
git status --short
git check-ignore -v .env Input/.env
```

Then commit and push from this project directory:

```powershell
git init
git add .
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/Rahulsharwa/Shopify-Sync-Database.git
git push -u origin main
```
