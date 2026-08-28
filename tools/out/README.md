---
license: other
tags:
  - vin
  - nhtsa
  - vpic
  - vehicle
  - database
tables:
  - make
  - model
  - make_model
  - wmi
  - pattern
  - vehicletype
  - wmi_vinschema
  - vinexception
  - vinschema
  - manufacturer
  - country
  - drivetype
  - fueltype
  - bodystyle
  - transmission
  - enginemodel
  - enginemodelpattern
  - vspecschemapattern
  - vPIC_sqlite.db
configs:
  - config_name: make
    data_files:
      - split: train
        path: make.parquet
  - config_name: model
    data_files:
      - split: train
        path: model.parquet
  - config_name: make_model
    data_files:
      - split: train
        path: make_model.parquet
  - config_name: wmi
    data_files:
      - split: train
        path: wmi.parquet
  - config_name: pattern
    data_files:
      - split: train
        path: pattern.parquet
  - config_name: vehicletype
    data_files:
      - split: train
        path: vehicletype.parquet
  - config_name: wmi_vinschema
    data_files:
      - split: train
        path: wmi_vinschema.parquet
  - config_name: vinexception
    data_files:
      - split: train
        path: vinexception.parquet
  - config_name: vinschema
    data_files:
      - split: train
        path: vinschema.parquet
  - config_name: manufacturer
    data_files:
      - split: train
        path: manufacturer.parquet
  - config_name: country
    data_files:
      - split: train
        path: country.parquet
  - config_name: drivetype
    data_files:
      - split: train
        path: drivetype.parquet
  - config_name: fueltype
    data_files:
      - split: train
        path: fueltype.parquet
  - config_name: bodystyle
    data_files:
      - split: train
        path: bodystyle.parquet
  - config_name: transmission
    data_files:
      - split: train
        path: transmission.parquet
  - config_name: enginemodel
    data_files:
      - split: train
        path: enginemodel.parquet
  - config_name: enginemodelpattern
    data_files:
      - split: train
        path: enginemodelpattern.parquet
  - config_name: vspecschemapattern
    data_files:
      - split: train
        path: vspecschemapattern.parquet
---

# NHTSA vPIC Curated Vehicle Database

Curated vehicle database derived from [NHTSA's vPIC](https://vpic.nhtsa.dot.gov/)
data, optimized for VIN decoding via the [vin-decode-mcp](https://github.com/joakes90/vin-decode-mcp) server.

## Source

Data sourced from the National Highway Traffic Safety Administration (NHTSA),
an agency of the United States Department of Transportation. NHTSA is a
government agency and the services provided are free for use by the public
as part of their Open Data initiative. No API key or registration required.

**[Download NHTSA vPIC standalone databases](https://vpic.nhtsa.dot.gov/Downloads/)**

## What's Included

This is the **full vPIC SQLite database** with all NHTSA vehicle specification
tables, including:

| Category | Description |
|---|---|
| `make` | Vehicle manufacturers (~12,328) |
| `model` | Vehicle models (~31,869) |
| `vehicletype` | Vehicle type categories |
| `make_model` | Make/model relationships |
| `wmi` | World Manufacturer Identifiers (~12,971) |
| `wmi_vinschema` | WMI to VIN schema mapping |
| `pattern` | VIN descriptor patterns (~1.67M) |
| `vinexception` | VIN exception rules (~18,805) |
| `vinschema` | VIN schema definitions |
| `model_years` | Production year ranges |
| `engine_*` | Engine specifications (model, model, pattern, etc.) |
| `body_*` | Body specifications (style, cab, type, etc.) |
| `fuel_*` | Fuel specifications (type, delivery, tank, etc.) |
| `safety_*` | Safety equipment (airbags, brakes, ABS, etc.) |
| `battery_*` | Battery/electrification specs |
| `transmission` | Transmission types |
| `drivetype` | Drive types (FWD, RWD, AWD, etc.) |

## Database Stats

- **Size**: ~627 MB
- **Format**: SQLite (`.db`)
- **Source vintage**: 2026-08

## Dataset Viewer

This dataset is available in the [Dataset Viewer](https://huggingface.co/docs/hub/datasets-viewer) with individual tables available as separate configurations (config dropdown at top right). Key tables are also exported as [Parquet](https://parquet.apache.org/) for fast browser-based browsing.

### Available Tables (Parquet exports)

| Table | Config | Rows | Parquet Size |
|---|---|---|---|
| **make** | `make` | 12,328 | 0.3 MB |
| **model** | `model` | 31,869 | 0.6 MB |
| **make_model** | `make_model` | 31,869 | 0.6 MB |
| **wmi** | `wmi` | 12,971 | 0.6 MB |
| **pattern** | `pattern` | 1,674,161 | 28.6 MB |
| **vehicletype** | `vehicletype` | 9 | <1 MB |
| **wmi_vinschema** | `wmi_vinschema` | 41,708 | 0.7 MB |
| **vinexception** | `vinexception` | 18,805 | 0.2 MB |
| **vinschema** | `vinschema` | 25,149 | 1.4 MB |
| **manufacturer** | `manufacturer` | 22,893 | 0.5 MB |
| **country** | `country` | 199 | <1 MB |
| **drivetype** | `drivetype` | 23 | <1 MB |
| **fueltype** | `fueltype` | 14 | <1 MB |
| **bodystyle** | `bodystyle` | 71 | <1 MB |
| **transmission** | `transmission` | 12 | <1 MB |
| **enginemodel** | `enginemodel` | 346 | <1 MB |
| **enginemodelpattern** | `enginemodelpattern` | 2,510 | <1 MB |
| **vspecschemapattern** | `vspecschemapattern` | 16,754 | 0.1 MB |

## Usage

Download the `.db` file and use with the MCP server:

```bash
export VIN_MCP_DB_PATH=/path/to/vPIC_sqlite.db
vin-decode-mcp
```

Or query directly:

```python
import sqlite3
conn = sqlite3.connect("vPIC_sqlite.db", uri=True)
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
Curated database pipeline: [vin-decode-mcp](https://github.com/joakes90/vin-decode-mcp)
