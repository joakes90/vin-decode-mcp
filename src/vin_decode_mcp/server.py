"""VIN Decode MCP Server — fastMCP application.

Exposes tools for VIN decoding and vehicle data lookup, backed by a
curated NHTSA vPIC SQLite database.
"""

from __future__ import annotations

import sys

import structlog
from fastmcp import FastMCP

from .database import VinDatabase

# Send every log line to stderr, never stdout. structlog's default
# PrintLoggerFactory writes to stdout, which under the stdio transport *is*
# the JSON-RPC channel -- one `logger.error(...)` from a tool's except branch
# (a missing database is enough) lands a human-readable line in the middle of
# the message stream and the client fails to parse it. Configured here rather
# than in cli.py because `fastmcp run server.py` imports this module directly.
structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))

logger = structlog.get_logger(__name__)

# Create the MCP server
mcp = FastMCP(
    "vin-decode",
    instructions=(
        "This server decodes Vehicle Identification Numbers (VINs) and queries "
        "vehicle data from the NHTSA vPIC database. All data covers US-market "
        "vehicles, model year 1981 and forward. Specifications only — no title, "
        "accident, odometer, or theft history (those require NMVTIS/commercial data)."
    ),
)


def get_db() -> VinDatabase:
    """Lazily get the database connection."""
    return VinDatabase()


# ======================================================================
# Tool: decode_vin
# ======================================================================
@mcp.tool()
def decode_vin(vin: str, model_year: int | None = None) -> dict:
    """Decode a VIN to make, model, year, and vehicle type.

    Returns the decoded make, model, model year, and vehicle type for a
    17-character VIN. Uses the NHTSA vPIC pattern database for make/model
    resolution. Model year is computed from VIN position 10 unless you
    provide model_year explicitly.

    Partial VINs (shorter than 17 characters) may still decode if the
    WMI (positions 1-3) matches a known manufacturer.

    Args:
        vin: Vehicle Identification Number (17 chars, or shorter for partial).
        model_year: Optional explicit model year to improve decode accuracy.
            If omitted, computed from VIN position 10 + position 7.

    Returns:
        dict with keys: vin, make, model, year, vehicle_type, wmi,
        wmi_length, confidence.

    Examples:
        >>> decode_vin("1HGCM82633A004352")
        {'vin': '1HGCM82633A004352', 'make': 'Honda', 'model': 'Accord', ...}
        >>> decode_vin("WP0AA2A96KS106147")
        {'vin': 'WP0AA2A96KS106147', 'make': 'Porsche', 'model': '911', ...}
    """
    db = get_db()
    try:
        return db.decode_vin(vin, model_year)
    except FileNotFoundError as e:
        logger.error("decode_vin", error=str(e))
        return {"error": str(e), "make": None, "model": None, "year": None}
    except Exception as e:  # noqa: BLE001
        logger.error("decode_vin", error=str(e), vin=vin)
        return {"error": str(e), "make": None, "model": None, "year": None}


# ======================================================================
# Tool: decode_partial_vin
# ======================================================================
@mcp.tool()
def decode_partial_vin(pattern: str, limit: int = 20) -> list[dict]:
    """Decode a partial VIN pattern with wildcards.

    Supports * as a wildcard for exactly one character (matching the vPIC
    pattern format). Useful for matching a VIN you don't have the full
    17 characters of, or for pattern-based lookups.

    Args:
        pattern: Partial VIN with * wildcards (e.g. "5UXWX7C5*BA").
        limit: Maximum number of results (default 20).

    Returns:
        List of matching decode results sorted by confidence.

    Examples:
        >>> decode_partial_vin("1HGCM826*3A")
        [{'make': 'Honda', 'model': 'Accord', 'year': 2003, ...}]
        >>> decode_partial_vin("5UXWX7C5*BA")
        [{'make': 'BMW', 'model': 'X3', 'year': 2011, ...}]
    """
    db = get_db()
    try:
        return db.decode_partial_vin(pattern, limit)
    except Exception as e:  # noqa: BLE001
        logger.error("decode_partial_vin", error=str(e), pattern=pattern)
        return [{"error": str(e)}]


# ======================================================================
# Tool: get_all_makes
# ======================================================================
@mcp.tool()
def get_all_makes() -> list[dict]:
    """List all vehicle makes registered in vPIC.

    Returns every make currently in the curated database. This includes
    makes from all vehicle types (cars, trucks, motorcycles, etc.) that
    passed the curation filter.

    Returns:
        List of dicts with keys: id, name. Sorted alphabetically by name.
    """
    db = get_db()
    try:
        return db.get_all_makes()
    except Exception as e:  # noqa: BLE001
        logger.error("get_all_makes", error=str(e))
        return [{"error": str(e)}]


# ======================================================================
# Tool: get_models_for_make
# ======================================================================
@mcp.tool()
def get_models_for_make(make: str, vehicle_type: str | None = None) -> list[dict]:
    """List all models for a given make.

    Optionally filter by vehicle type to narrow results (e.g. get only
    car models or only motorcycle models for a make that produces both).

    Args:
        make: Make name (e.g. "Honda"). Case-insensitive. Use get_all_makes()
            to list valid names; numeric make IDs are not accepted here.
        vehicle_type: Optional vehicle type filter
            ("Passenger Car", "Motorcycle", "Truck", etc.).

    Returns:
        List of dicts with keys: id, name. Sorted alphabetically.
    """
    db = get_db()
    try:
        return db.get_models_for_make(make, vehicle_type)
    except Exception as e:  # noqa: BLE001
        logger.error("get_models_for_make", error=str(e), make=make)
        return [{"error": str(e)}]


# ======================================================================
# Tool: get_model_years
# ======================================================================
@mcp.tool()
def get_model_years(make: str, model: str) -> dict | None:
    """Get the model year range for a make/model pair.

    Args:
        make: Make name (e.g. "Porsche"). Case-insensitive.
        model: Model name (e.g. "911"). Case-insensitive.

    Returns:
        dict with keys: year_from, year_to. year_to is None if the model
        is still in production. None if the make/model is not found.

    Examples:
        >>> get_model_years("Porsche", "911")
        {'year_from': 1981, 'year_to': None}  # still in production
        >>> get_model_years("Honda", "Accord")
        {'year_from': 1981, 'year_to': None}  # vPIC starts at 1981
    """
    db = get_db()
    try:
        return db.get_model_years(make, model)
    except Exception as e:  # noqa: BLE001
        logger.error("get_model_years", error=str(e))
        return {"error": str(e)}


# ======================================================================
# Tool: get_wmi_info
# ======================================================================
@mcp.tool()
def get_wmi_info(wmi: str) -> dict | None:
    """Decode a World Manufacturer Identifier (WMI).

    Returns manufacturer information for a 3- or 6-character WMI code
    (VIN positions 1-3, optionally + positions 12-14 for low-volume
    manufacturers).

    Args:
        wmi: WMI code (3 chars for high-volume, 6 for low-volume
            manufacturers).

    Returns:
        dict with keys: wmi, make, makeid. None if not found.

    Examples:
        >>> get_wmi_info("1HG")
        {'wmi': '1HG', 'make': 'Honda', 'makeid': 474}
        >>> get_wmi_info("1A9841")  # 6-char low-volume
        {'wmi': '1A9841', 'make': 'AC Propulsion', 'makeid': 771}
    """
    db = get_db()
    try:
        return db.get_wmi_info(wmi)
    except Exception as e:  # noqa: BLE001
        logger.error("get_wmi_info", error=str(e), wmi=wmi)
        return {"error": str(e)}


# ======================================================================
# Tool: get_vehicle_types
# ======================================================================
@mcp.tool()
def get_vehicle_types() -> list[dict]:
    """List all available vehicle types.

    Returns the vehicle types in the curated dataset (e.g. Passenger Car,
    Motorcycle, Truck, MPV, Off-Road Vehicle).

    Returns:
        List of dicts with keys: id, name.
    """
    db = get_db()
    try:
        return db.get_vehicle_types()
    except Exception as e:  # noqa: BLE001
        logger.error("get_vehicle_types", error=str(e))
        return [{"error": str(e)}]


# ======================================================================
# Tool: get_make_vehicle_types
# ======================================================================
@mcp.tool()
def get_make_vehicle_types(make: str) -> list[str]:
    """List the vehicle types a given make produces.

    Useful for understanding what categories a manufacturer covers
    (e.g. BMW produces "Motorcycle", "Passenger Car", "Truck").

    Args:
        make: Make name (e.g. "BMW"). Case-insensitive.

    Returns:
        List of vehicle type name strings.
    """
    db = get_db()
    try:
        return db.get_make_vehicle_types(make)
    except Exception as e:  # noqa: BLE001
        logger.error("get_make_vehicle_types", error=str(e), make=make)
        return []


# ======================================================================
# Resource: vin_mcp://schema
# ======================================================================
@mcp.resource("vin-mcp://schema")
def get_schema() -> str:
    """Database schema and column descriptions.

    This resource exposes the full DDL for the VIN database and
    documentation for each column. Useful for LLMs that need to
    understand the data structure.

    Tables:
        make — Vehicle makes (manufacturers)
        model — Vehicle models
        vehicletype — Vehicle type categories
        make_model — Linking table: which models belong to which makes, and under which vehicle type
        model_years — Production year ranges for each make/model pair
        wmi — World Manufacturer Identifiers (3 or 6 characters)
        wmi_vinschema — WMI to VIN schema mapping with year ranges
        vin_pattern — VIN descriptor patterns used for make/model resolution
        dataset_info — Metadata about this database version and build
    """
    db = get_db()
    try:
        ddl = db.get_schema_ddl()
        descriptions = {
            "make": "id (int), name (text) — Vehicle manufacturers",
            "model": "id (int), name (text) — Vehicle model names",
            "vehicletype": "id (int), name (text) — Vehicle type categories (Passenger Car, Motorcycle, etc.)",
            "make_model": "makeid, modelid, vehicletypeid — Junction table linking makes to models with vehicle type context",
            "model_years": "makeid, modelid, year_from, year_to — Production year ranges (year_to NULL = still in production)",
            "wmi": "wmi (3 or 6 chars), makeid — World Manufacturer Identifiers",
            "wmi_vinschema": "wmi, vinschemaid, year_from, year_to — WMI to VIN schema mapping with year ranges",
            "vin_pattern": "vinschemaid, keys (pattern with * wildcards), modelid — VIN descriptor patterns for model resolution",
            "dataset_info": "key, value — Build metadata (source, vintage, timestamps)",
        }
        section = "\n".join(f"  {table}: {desc}" for table, desc in descriptions.items())
        return f"Database Schema:\n\n{ddl}\n\nColumn Descriptions:\n{section}"
    except Exception as e:  # noqa: BLE001
        logger.error("get_schema", error=str(e))
        return f"Error loading schema: {e}"


# ======================================================================
# Resource: vin_mcp://info
# ======================================================================
@mcp.resource("vin-mcp://info")
def get_info() -> str:
    """Dataset information and build metadata.

    Returns the source file, vintage date, build timestamp, script version,
    and dataset statistics for this database instance.
    """
    db = get_db()
    try:
        info = db.get_dataset_info()
        lines = [
            "VIN Decode Database Information",
            "=" * 40,
        ]
        for key, value in info.items():
            lines.append(f"{key}: {value}")
        # Add some stats
        n_makes = db.conn.execute("SELECT COUNT(*) FROM make").fetchone()[0]
        n_models = db.conn.execute("SELECT COUNT(*) FROM model").fetchone()[0]
        n_patterns = db.conn.execute("SELECT COUNT(*) FROM vin_pattern").fetchone()[0]
        lines.append("")
        lines.append("Database contents:")
        lines.append(f"  Makes: {n_makes:,}")
        lines.append(f"  Models: {n_models:,}")
        lines.append(f"  VIN patterns: {n_patterns:,}")
        lines.append(f"  Database: {db.db_path}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.error("get_info", error=str(e))
        return f"Error loading info: {e}"
