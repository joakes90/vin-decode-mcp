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


DEFAULT_DB_FILENAME = "provenance_vpic.db"
HF_REPO_ID = "joakes90/vpic-database"


def _download_db_from_hf(target: Path) -> bool:
    """Download the latest database from Hugging Face.

    Returns True if download succeeded, False otherwise.
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print(
            f"vin-decode-mcp: auto-download requires huggingface_hub. "
            f"Install with: pip install huggingface_hub\n"
            f"Or set VIN_MCP_DB_PATH to point to an existing database."
        )
        return False

    try:
        print(f"vin-decode-mcp: Downloading database from Hugging Face...")
        src_path = hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=DEFAULT_DB_FILENAME,
            repo_type="dataset",
        )
        # Copy to target location
        import shutil
        shutil.copy2(src_path, target)
        print(f"vin-decode-mcp: Database saved to {target}")
        return True
    except Exception as e:
        print(f"vin-decode-mcp: Failed to download database: {e}")
        return False


def resolve_db_path(args_db: Path | None) -> Path:
    """Resolve database path: env var > CLI arg > default location.

    If the database doesn't exist at any location, attempts to download
    from Hugging Face. Falls back gracefully if download fails.
    """
    # 1. Environment variable
    env_val = os.environ.get("VIN_MCP_DB_PATH", "").strip()
    if env_val:
        env_path = Path(env_val).expanduser()
        if env_path.exists():
            return env_path

    env_default = Path(__file__).parent.parent.parent / DEFAULT_DB_FILENAME

    if args_db and args_db.exists():
        return args_db

    if env_default.exists():
        return env_default

    # 2. Home directory fallback
    home_path = Path.home() / "vin-decode-mcp.db"
    if home_path.exists():
        return home_path

    # 3. Try to download from Hugging Face
    if _download_db_from_hf(env_default):
        return env_default

    # 4. Return env path (will trigger FileNotFoundError in the database layer)
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
        mcp.run(transport="http", host="0.0.0.0", port=args.port, show_banner=False)
    else:
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
