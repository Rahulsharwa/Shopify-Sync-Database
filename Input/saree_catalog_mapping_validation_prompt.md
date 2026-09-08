# Saree Catalog Field Mapping + Codex Validation Prompt

**Project:** Janardhana Silk House Saree Generate Shopify Image Sync

**Purpose:** Save catalog field IDs centrally and validate Baserow connectivity only.
**Important:** This prompt is for **saving IDs and validation only**. It must **not run Shopify sync**, **not upload images**, and **not update Baserow rows**.

---

## Core Sync Rule

Only these two fields are allowed for approval/status logic:

1. `Generation Status`
2. `SHOPIFY Notes`

Do **not** read, check, update, or fallback to the `SHOPIFY` field.

After successful future image sync, update only:

```text
Generation Status = Shopify-sync
```

Do not touch the `SHOPIFY` field.

---

## Catalog Field Mapping Given So Far

| # | Catalog | Table ID | Generation Status Field ID | SHOPIFY Notes Field ID |
|---:|---|---:|---:|---:|
| 1 | Kanjivaram Silks | 948083 | 8253052 | 8253055 |
| 2 | Pure Silk Sarees | 935204 | 8123033 | 8123036 |
| 3 | Tussar Silk Saree | 948245 | 8254631 | 8254634 |
| 4 | South Weaves – South Silk Sarees | 935205 | 8123050 | 8123053 |
| 5 | Soft Silk Sarees | 935207 | 8123084 | 8123087 |
| 6 | Patola & Orissa Silk Sarees | 935208 | 8123101 | 8123104 |
| 7 | Paithani Silk Sarees | 935206 | 8123067 | 8123070 |
| 8 | Printed Pure Silk Sarees | 935203 | 8123016 | 8123019 |
| 9 | Cotton Silk Sarees | 935215 | 8123220 | 8123223 |
| 10 | Banarasi Silk Sarees | 935210 | 8123135 | 8123138 |
| 11 | Banarasi Georgette Silk Sarees | 935209 | 8123118 | 8123121 |
| 12 | Banarasi Kora Silk Saree | 935211 | 8123152 | 8123155 |
| 13 | Gadwal Handloom | 935213 | 8123186 | 8123189 |
| 14 | Jamawar Silk Sarees | 935214 | 8123203 | 8123206 |
| 15 | Cotton Saree | 935216 | 8123237 | 8123240 |
| 16 | Linen & Kota Silk Sarees | 935217 | 8123254 | 8123257 |
| 17 | Art Silk Sarees | 935218 | 8123271 | 8123274 |

> Pending final confirmation: `Bandhani Silk Saree (935212)` mapping is not included in this locked config unless its `SHOPIFY Notes` field ID is confirmed.

---

# Codex Prompt

Paste the following prompt into Codex.

```text
Update `saree_image_sync.py` and project config so the catalog field IDs are saved centrally.

Do not run the sync.
Do not upload images.
Do not update Shopify.
Do not update Baserow rows.
Do not modify product data.
Do not touch the `SHOPIFY` field.

Goal:
Only save/confirm catalog mapping and connection-readiness for all currently provided catalog IDs.

Important:
Sync logic must check only these two fields:
1. `Generation Status`
2. `SHOPIFY Notes`

Do not read, check, update, or fallback to the `SHOPIFY` field.

After successful future sync, the only status field to update will be:

Generation Status = Shopify-sync

Do not touch the `SHOPIFY` field.

## Create/update config file

Create or update:

config/saree_catalog_field_map.json

Save this exact mapping:

{
  "Kanjivaram Silks": {
    "table_id": 948083,
    "generation_status_field_id": 8253052,
    "shopify_notes_field_id": 8253055
  },
  "Pure Silk Sarees": {
    "table_id": 935204,
    "generation_status_field_id": 8123033,
    "shopify_notes_field_id": 8123036
  },
  "Tussar Silk Saree": {
    "table_id": 948245,
    "generation_status_field_id": 8254631,
    "shopify_notes_field_id": 8254634
  },
  "South Weaves – South Silk Sarees": {
    "table_id": 935205,
    "generation_status_field_id": 8123050,
    "shopify_notes_field_id": 8123053
  },
  "Soft Silk Sarees": {
    "table_id": 935207,
    "generation_status_field_id": 8123084,
    "shopify_notes_field_id": 8123087
  },
  "Patola & Orissa Silk Sarees": {
    "table_id": 935208,
    "generation_status_field_id": 8123101,
    "shopify_notes_field_id": 8123104
  },
  "Paithani Silk Sarees": {
    "table_id": 935206,
    "generation_status_field_id": 8123067,
    "shopify_notes_field_id": 8123070
  },
  "Printed Pure Silk Sarees": {
    "table_id": 935203,
    "generation_status_field_id": 8123016,
    "shopify_notes_field_id": 8123019
  },
  "Cotton Silk Sarees": {
    "table_id": 935215,
    "generation_status_field_id": 8123220,
    "shopify_notes_field_id": 8123223
  },
  "Banarasi Silk Sarees": {
    "table_id": 935210,
    "generation_status_field_id": 8123135,
    "shopify_notes_field_id": 8123138
  },
  "Banarasi Georgette Silk Sarees": {
    "table_id": 935209,
    "generation_status_field_id": 8123118,
    "shopify_notes_field_id": 8123121
  },
  "Banarasi Kora Silk Saree": {
    "table_id": 935211,
    "generation_status_field_id": 8123152,
    "shopify_notes_field_id": 8123155
  },
  "Gadwal Handloom": {
    "table_id": 935213,
    "generation_status_field_id": 8123186,
    "shopify_notes_field_id": 8123189
  },
  "Jamawar Silk Sarees": {
    "table_id": 935214,
    "generation_status_field_id": 8123203,
    "shopify_notes_field_id": 8123206
  },
  "Cotton Saree": {
    "table_id": 935216,
    "generation_status_field_id": 8123237,
    "shopify_notes_field_id": 8123240
  },
  "Linen & Kota Silk Sarees": {
    "table_id": 935217,
    "generation_status_field_id": 8123254,
    "shopify_notes_field_id": 8123257
  },
  "Art Silk Sarees": {
    "table_id": 935218,
    "generation_status_field_id": 8123271,
    "shopify_notes_field_id": 8123274
  }
}

## Add validation-only mode

Add env support:

VALIDATE_CATALOG_MAPPING_ONLY=true

When `VALIDATE_CATALOG_MAPPING_ONLY=true`:

1. Load `config/saree_catalog_field_map.json`
2. For each catalog:
   - connect to the Baserow table
   - fetch field metadata only
   - confirm table exists
   - confirm `Generation Status` field ID exists
   - confirm `SHOPIFY Notes` field ID exists
   - confirm field names match expected meaning
   - confirm `Generation Status` has `Approved`
   - confirm whether `Generation Status` has `Shopify-sync`
3. Do not fetch rows.
4. Do not upload media.
5. Do not update Shopify.
6. Do not update Baserow rows.
7. Do not read, write, or fallback to the `SHOPIFY` field.

## Output validation report

Create:

output/saree_catalog_field_mapping_validation.csv
output/saree_catalog_field_mapping_validation.txt
logs/saree_catalog_field_mapping_validation.log

CSV columns:

Catalog name
Table ID
Table found yes/no
Generation Status field ID
Generation Status field found yes/no
Generation Status field name
Approved option found yes/no
Shopify-sync option found yes/no
SHOPIFY Notes field ID
SHOPIFY Notes field found yes/no
SHOPIFY Notes field name
Valid yes/no
Warning
Error

Final text report must show:

Total catalogs in mapping
Valid catalogs
Invalid catalogs
Catalogs missing Shopify-sync option
Catalogs missing Approved option
Catalogs with missing SHOPIFY Notes field
Catalogs with missing Generation Status field

## Required command, validation only

Use this command:

cd C:\Users\rahul\Documents\Fabrics\shopify-sync
.venv\Scripts\Activate.ps1

$env:VALIDATE_CATALOG_MAPPING_ONLY='true'
python saree_image_sync.py

## Acceptance criteria

1. Mapping file is created or updated successfully.
2. No Shopify media upload runs.
3. No Shopify product changes happen.
4. No Baserow row updates happen.
5. The script only validates table/field connectivity.
6. `SHOPIFY` field is ignored completely.
7. Final validation report confirms which catalogs are connected and ready.
8. Report clearly shows which catalogs are missing `Shopify-sync` inside `Generation Status`.
```

---

## Single-line Validation Command

```powershell
cd C:\Users\rahul\Documents\Fabrics\shopify-sync; .venv\Scripts\Activate.ps1; $env:VALIDATE_CATALOG_MAPPING_ONLY='true'; python saree_image_sync.py
```
