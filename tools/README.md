# Database Build Pipeline

This directory contains the tools for rebuilding the curated VIN-decode database
from NHTSA's vPIC data.

## What's Here

| File | Purpose |
|---|---|
| `build_db.py` | Python orchestrator — runs the full rebuild pipeline |
| `convert_to_sqlite.py` | PostgreSQL → SQLite converter |
| `rebuild.sh` | Shell script — download, convert, and build in one command |
| `curation.json` | Editorial rules for filtering makes/models (reproducible curation) |
| `overlay.json` | Grey-import / classic car additions missing from US-only vPIC data |

## Prerequisites

- **Python 3.11+** — for the Python pipeline tools
- **PostgreSQL** (with `pg_restore` and `pg_dump`) — required to process NHTSA's standalone database
- **Internet access** — to download the latest NHTSA vPIC data

## Quick Rebuild

```bash
bash tools/rebuild.sh
```

This will:
1. Download the latest NHTSA vPIC PostgreSQL standalone database
2. Restore to a local PostgreSQL instance
3. Convert to SQLite format
4. Run the curated pare-down pipeline
5. Output: `tools/out/curated_vpic.db` (~4.5 MB)

## Manual Build

```bash
# Step 1: Download from NHTSA
# https://vpic.nhtsa.dot.gov/Downloads/
# Download: vPICList_lite_YYYY_MM.custom.zip

# Step 2: Convert to SQLite
python3 tools/convert_to_sqlite.py \
    --custom vPICList_lite_2026_08.custom.zip \
    --output tools/out/vpic_lite.db

# Step 3: Build curated database
python3 tools/build_db.py \
    --source tools/out/vpic_lite.db \
    --output tools/out/curated_vpic.db
```

## Curation

The curated database filters ~1,700+ motorcycle-only makes down to ~534 meaningful
manufacturers using `tools/curation.json`:

- **motorcycle_min_models**: Minimum model count for motorcycle-only makes (default: 25)
- **force_keep_makes**: Brands always included regardless of threshold
- **force_drop_makes**: Brands always excluded

The overlay hook (`tools/overlay.json`) adds grey-import classics and JDM models
absent from the US-focused vPIC dataset.

## Database Refresh Schedule

NHTSA refreshes the vPIC standalone databases approximately every 6–12 months.
A GitHub Action (`.github/workflows/rebuild-db.yml`) automates this on a 6-month
schedule. Set `HUGGING_FACE_TOKEN` as a repository secret to auto-upload the
compiled database to Hugging Face after each rebuild.
