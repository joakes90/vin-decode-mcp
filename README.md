# vin-decode-mcp

**Decode VINs and query vehicle data from a curated NHTSA vPIC database — powered by the Model Context Protocol.**

A standalone, offline-capable MCP server for LLMs to decode Vehicle Identification Numbers (VINs) and look up makes, models, and vehicle specifications using data from [NHTSA's vPIC](https://vpic.nhtsa.dot.gov/).

```
pip install vin-decode-mcp
vin-decode-mcp  # Start the MCP server
```

## Why?

- **Offline**: Works without internet access. The curated SQLite database (~4.5 MB) is self-contained.
- **No rate limits**: Unlike calling the vPIC API directly, local queries are unlimited.
- **Fast**: Pattern matching against the SQLite database takes microseconds.
- **LLM-native**: Tools with rich docstrings, schema resources, and structured JSON output.
- **Open data**: NHTSA vPIC is US government open data — free, no API key required.

## Data Coverage

- US-market vehicles, model year **1981 and forward**
- **536 makes**, **9,284 models**, **88,267 VIN patterns** (2026-08 vintage)
- Passenger Cars, Trucks, MPVs, Motorcycles, Off-Road Vehicles
- Excludes: Buses, Trailers, Low-Speed Vehicles, Incomplete Vehicles

> **Specifications only** — this database does not include title, accident, odometer,
> or theft history (those require NMVTIS/commercial data sources).

## Quick Start

### Installation

```bash
pip install vin-decode-mcp
```

Or from source:

```bash
git clone https://github.com/<org>/vin-decode-mcp.git
cd vin-decode-mcp
pip install -e .
```

### Running

```bash
# Default: stdio transport (for Claude Desktop, Cursor, etc.)
vin-decode-mcp

# HTTP transport
vin-decode-mcp --transport http --port 8765
```

### Using with Claude Desktop

Create a dedicated venv so the binary lands where you can reference it:

```bash
python3 -m venv ~/.local/venvs/vin-decode
source ~/.local/venvs/vin-decode/bin/activate
pip install vin-decode-mcp
deactivate
```

Add to `~/.config/claude-desktop/config.json` (or `~/Library/Application Support/claude-desktop/config.json` on macOS):

```json
{
  "mcpServers": {
    "vin-decode": {
      "command": "~/.local/venvs/vin-decode/bin/vin-decode-mcp"
    }
  }
}
```

Replace the path with wherever you put the venv. Restart Claude Desktop. The model can now use VIN decoding tools in conversations.

> **Note:** Claude Desktop spawns processes with a minimal `$PATH` that doesn't include conda environments or virtualenvs, so always use the **absolute path** to the binary — just putting `"vin-decode-mcp"` won't work.

## Available Tools

| Tool | Description |
|------|-------------|
| `decode_vin(vin, model_year?)` | Decode a VIN → make, model, year, vehicle type |
| `decode_partial_vin(pattern, limit?)` | Match a partial VIN with `*` wildcards |
| `get_all_makes()` | List all vehicle makes |
| `get_models_for_make(make, vehicle_type?)` | List models for a make |
| `get_model_years(make, model)` | Get production year range |
| `get_wmi_info(wmi)` | Decode a WMI → manufacturer info |
| `get_vehicle_types()` | List available vehicle types |
| `get_make_vehicle_types(make)` | List vehicle types for a make |

### Examples

```
>>> decode_vin("1HGCM82633A004352")
{
  "vin": "1HGCM82633A004352",
  "make": "Honda",
  "model": "Accord",
  "year": 2003,
  "vehicle_type": "Passenger Car",
  "wmi": "1HG",
  "confidence": "full"
}

>>> get_model_years("Porsche", "911")
{"year_from": 1981, "year_to": null}

>>> decode_partial_vin("5UXWX7C5*BA")
[{"make": "BMW", "model": "X3", "year": 2011,
  "vehicle_type": "Passenger Car", "confidence": "partial_match"}]
```

## Database

### Download

The compiled database is hosted on Hugging Face:

**Dataset**: https://huggingface.co/datasets/joakes90/vpic-database
**Direct download**: https://huggingface.co/datasets/joakes90/vpic-database/resolve/main/curated_vpic.db

### Custom Database Path

```bash
# Set via environment variable
export VIN_MCP_DB_PATH=/path/to/curated_vpic.db
vin-decode-mcp

# Or via CLI flag
vin-decode-mcp --db-path /path/to/curated_vpic.db
```

### Rebuilding

The database is rebuilt from NHTSA's standalone PostgreSQL databases approximately every 6-12 months:

```bash
# Requires PostgreSQL installed (pg_restore, psql)
bash tools/rebuild.sh

# Or step by step:
# 1. Download NHTSA data: https://vpic.nhtsa.dot.gov/Downloads/
# 2. Convert to SQLite
python3 tools/convert_to_sqlite.py --input dump.sql --output tools/out/vpic_lite.db
# 3. Build curated database
python3 tools/build_db.py --source tools/out/vpic_lite.db --output tools/out/curated_vpic.db
```

See [`docs/hf-setup.md`](docs/hf-setup.md) for Hugging Face setup instructions.

## Data Source & Attribution

Vehicle data sourced from [NHTSA's vPIC](https://vpic.nhtsa.dot.gov/) — the National
Highway Traffic Safety Administration's Vehicle Product Information Catalog and
Vehicle Listing. NHTSA is a United States government agency.

- **Data license**: US Government work (public domain)
- **API**: No key or registration required
- **Refresh frequency**: ~6-12 months
- **Report errors**: Contact the NHTSA Manufacturer Helpdesk at manufacturerinfo@dot.gov or 1-888-399-3277

## Architecture

```
User / LLM Agent
       │
       ▼  MCP (stdio / HTTP)
┌──────────────────┐
│  vin-decode-mcp  │  pip install vin-decode-mcp
│  (FastMCP server)│  env: VIN_MCP_DB_PATH=/path/to/curated_vpic.db
└────────┬─────────┘
         │  sqlite3 (mode=ro)
         ▼
┌──────────────────────┐
│  curated_vpic.db     │  ~4.5 MB, curated
│    (Hugging Face)    │  makes + models + WMI + VIN patterns
└──────────────────────┘
         ▲
         │  rebuilds from
┌──────────────────┐
│ NHTSA vPIC PG DB │  69 MB, official
│ (NHTSA website)  │  refreshed 2x/year
└──────────────────┘
```

## Project Structure

```
vin-decode-mcp/
├── src/vin_decode_mcp/
│   ├── __init__.py              # Package init
│   ├── server.py                # FastMCP server with all tools
│   ├── database.py              # SQLite layer + VIN decoder
│   └── cli.py                   # CLI entry point
├── tools/
│   ├── build_db.py              # Pipeline orchestrator
│   ├── convert_to_sqlite.py     # PG → SQLite converter (COPY text format)
│   ├── vpic_pare_down.py        # Curated pare-down + VIN decode tables
│   ├── rebuild.sh               # Full rebuild script
│   ├── curation.json            # Make/model curation rules
│   ├── overlay.json             # Grey-import classic additions
│   └── README.md                # Rebuild instructions
├── tests/
│   ├── conftest.py              # Test fixtures
│   ├── test_decode.py           # VIN decode canary + regression tests
│   ├── test_server.py           # Bulk lookup tests
│   ├── test_convert.py          # PostgreSQL COPY decoding tests
│   ├── test_real_db.py          # Smoke tests against the curated DB
│   └── fixtures/
│       ├── build_test_db.py     # Test database builder
│       └── test_vpic.db         # Minimal test database
├── .github/workflows/
│   ├── ci.yml                   # CI: test + lint
│   └── rebuild-db.yml           # Scheduled DB rebuild
├── docs/
│   └── hf-setup.md              # Hugging Face setup guide
├── pyproject.toml
├── LICENSE
└── README.md
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -v

# Lint
python -m ruff check src/ tests/

# Format
python -m ruff format src/ tests/
```

## Comparison with Other Solutions

| | **vin-decode-mcp** | **NHTSA vPIC API** | **vin-mcp (NLMA)** |
|---|---|---|---|
| **Transport** | Local SQLite | HTTP REST | HTTP REST |
| **Offline** | ✅ | ❌ | ❌ |
| **Rate limited** | No | Yes | Yes |
| **Data size** | ~4.5 MB | N/A | N/A |
| **VIN fields** | Make + Model + Year | ~130 fields | ~130 fields |
| **Makes/Models** | ✅ 536/9,284 | ✅ Full catalog | ✅ Full catalog |
| **Install** | `pip install` | None | `pip install` |

## License

**MIT License** — Code is MIT. Data is US Government public domain.

See [`LICENSE`](LICENSE) for details.

## Contributing

Contributions welcome! Please:

1. Fork and create a feature branch
2. Add tests for new functionality
3. Ensure CI passes
4. Submit a pull request

For major changes, open an issue first to discuss the approach.
