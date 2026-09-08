$ErrorActionPreference = "Continue"

$ProjectRoot = "C:\Users\rahul\Documents\Fabrics\shopify-sync"
Set-Location $ProjectRoot

if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . ".\.venv\Scripts\Activate.ps1"
}

Remove-Item Env:\MAX_PRODUCTS -ErrorAction SilentlyContinue
$env:DRY_RUN = "false"

$env:PROCESS_ALL_SAREE_CATALOGS = "true"
python saree_image_sync.py

Remove-Item Env:\PROCESS_ALL_SAREE_CATALOGS -ErrorAction SilentlyContinue
$env:PROCESS_ACCESSORIES_SYNC = "true"
python accessories_image_sync.py

Remove-Item Env:\PROCESS_ACCESSORIES_SYNC -ErrorAction SilentlyContinue
$env:PROCESS_FABRIC_SYNC = "true"
python fabric_image_sync.py

Remove-Item Env:\PROCESS_FABRIC_SYNC -ErrorAction SilentlyContinue
