# Hugging Face Setup Instructions

This document describes how to set up the Hugging Face dataset repository
for hosting the compiled VIN-decode database.

## 1. Create the Dataset Repository

### Option A: Via Hugging Face UI
1. Go to https://huggingface.co/datasets/new
2. Repository name: `vin-decode-mcp/vpic-database` (or your org name)
3. Visibility: Public
4. License: Other (we'll add attribution in the README)
5. Click "Create dataset"

### Option B: Via CLI
```bash
pip install huggingface_hub
huggingface-cli login  # Follow the prompts
huggingface-cli repo create vin-decode-mcp/vpic-database --type dataset --private
huggingface-cli upload vin-decode-mcp/vpic-database . --include "*.md"
```

## 2. Dataset README

Create a `README.md` in the dataset repo:

```markdown
---
license: other
tags:
  - vin
  - nhtsa
  - vpic
  - vehicle
  - database
---

# NHTSA vPIC Curated Vehicle Database

Curated vehicle database derived from [NHTSA's vPIC](https://vpic.nhtsa.dot.gov/)
data, optimized for VIN decoding via the [vin-decode-mcp](https://github.com/vin-decode-mcp) server.

## Source

Data sourced from the National Highway Traffic Safety Administration (NHTSA),
an agency of the United States Department of Transportation. NHTSA is a
government agency and the services provided are free for use by the public
as part of their Open Data initiative. No API key or registration required.

**[Download NHTSA vPIC standalone databases](https://vpic.nhtsa.dot.gov/Downloads/)**

## What's Included

| Table | Description |
|---|---|
| `make` | Vehicle manufacturers (curated ~534 makes) |
| `model` | Vehicle models (curated ~9,175 models) |
| `vehicletype` | Vehicle type categories |
| `make_model` | Make/model/vehicle-type relationships |
| `model_years` | Production year ranges |
| `wmi` | World Manufacturer Identifiers (3 or 6 chars) |
| `wmi_vinschema` | WMI to VIN schema mapping |
| `vin_pattern` | VIN descriptor patterns for model resolution |
| `dataset_info` | Build metadata |

## Database Stats (v2026-08)

- **Size**: ~4.5 MB (with VIN decode tables)
- **Makes**: ~534
- **Models**: ~9,175
- **VIN Patterns**: ~88,140
- **WMIs**: ~1,710

## Coverage

US-market vehicles, model year 1981 and forward. Includes:
- Passenger Cars
- Trucks
- MPVs (Multipurpose Passenger Vehicles)
- Motorcycles
- Off-Road Vehicles

Excluded: Buses, Trailers, Low-Speed Vehicles, Incomplete Vehicles.

## Usage

Download the `.db` file and use with the MCP server:

```bash
export VIN_MCP_DB_PATH=/path/to/provenance_vpic.db
vin-decode-mcp
```

Or query directly:

```python
import sqlite3
conn = sqlite3.connect("provenance_vpic.db", uri=True)
conn.row_factory = sqlite3.Row

# List makes
makes = conn.execute("SELECT * FROM make ORDER BY name").fetchall()
```

## Refresh Frequency

This database is rebuilt from NHTSA's standalone databases approximately
every 6-12 months. Check the build timestamp in `dataset_info` for the
source vintage.

## Attribution

Vehicle data: [NHTSA vPIC](https://vpic.nhtsa.dot.gov/)
Curated database pipeline: [vin-decode-mcp](https://github.com/vin-decode-mcp)
```

## 3. Upload the Database

```bash
# From the main repo after building
huggingface-cli upload vin-decode-mcp/vpic-database tools/out/provenance_vpic.db

# Or with huggingface_hub Python API
from huggingface_hub import HfApi
api = HfApi()
api.upload_file(
    path_or_fileobj="tools/out/provenance_vpic.db",
    path_in_repo="provenance_vpic.db",
    repo_id="vin-decode-mcp/vpic-database",
)
```

## 4. Update the Main README

After setting up the HF repo, update `README.md` in the main repo:

```markdown
## Database

The compiled database is hosted on Hugging Face:
https://huggingface.co/datasets/vin-decode-mcp/vpic-database

Download directly:
https://huggingface.co/datasets/vin-decode-mcp/vpic-database/resolve/main/provenance_vpic.db
```

## 5. GitHub Actions

The `.github/workflows/rebuild-db.yml` workflow automatically:
1. Downloads the latest NHTSA vPIC standalone database
2. Converts to SQLite
3. Runs the curated pare-down pipeline
4. Uploads to Hugging Face (if `HUGGING_FACE_TOKEN` secret is set)

To enable automatic uploads:
1. Go to your GitHub repo → Settings → Secrets and variables → Actions
2. Add a secret `HUGGING_FACE_TOKEN` with your Hugging Face token
   (generate at https://huggingface.co/settings/tokens)
3. Set `HUGGING_FACE_REPO` in the workflow to your repo name

## 6. Alternative: GitHub Releases

If you prefer to host on GitHub instead of Hugging Face:
1. Create a GitHub release
2. Attach the `.db` file as a release asset
3. Update the README to link to the release download URL

```bash
# Manual release
git tag v0.1.0
git push origin v0.1.0
gh release create v0.1.0 tools/out/provenance_vpic.db \
  --title "v0.1.0 - VIN decode database" \
  --notes "Database built from NHTSA vPIC 2026-08"
```
