"""CLI entry point for VIN Decode MCP Server.

Usage:
    vin-decode-mcp              # stdio transport (default)
    vin-decode-mcp --transport http --port 8765  # HTTP transport
    vin-decode-mcp --db-path /path/to/db.db      # custom database

Environment:
    VIN_MCP_DB_PATH      Path to the SQLite database (overrides --db-path)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .database import set_default_db_path
from .server import mcp


def resolve_db_path(args_db: Path | None) -> Path:
    """Resolve database path: env var > CLI arg > default location.

    If the database doesn't exist, return the default path —
    the database layer will suggest downloading from Hugging Face.
    """
    # 1. Environment variable
    env_path = Path(os.environ.get("VIN_MCP_DB_PATH", "")).expanduser()
    if env_path.exists():
        return env_path
    env_default = Path(__file__).parent.parent.parent / "provenance_vpic.db"

    if args_db and args_db.exists():
        return args_db

    if env_default.exists():
        return env_default

    # 2. Alternative locations
    alt_paths = [
        Path.home() / "vin-decode-mcp.db",
    ]
    for alt in alt_paths:
        if alt.exists():
            return alt

    return env_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VIN Decode MCP Server — Decode VINs and query vehicle data"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="HTTP port (only for --transport http, default: 8765)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to the SQLite database file",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Resolve the database path and set it globally before server starts
    db_path = resolve_db_path(args.db_path)
    set_default_db_path(db_path)

    if args.transport == "http":
        mcp.run(transport="http", host="0.0.0.0", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
