"""CLI entry point for VIN Decode MCP Server.

Usage:
    vin-decode-mcp              # stdio transport (default)
    vin-decode-mcp --transport http --port 8765  # HTTP transport
    vin-decode-mcp --db-path /path/to/db.db      # custom database

Environment:
    VIN_MCP_DB_PATH      Path to the SQLite database (overrides --db-path)
    XDG_CACHE_HOME       Where an auto-downloaded database is cached
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .database import set_default_db_path
from .server import mcp

DEFAULT_DB_FILENAME = "curated_vpic.db"
HF_REPO_ID = "joakes90/vpic-database"


def _log(message: str) -> None:
    """Write a progress message to stderr.

    Never stdout. Under the default stdio transport, stdout *is* the JSON-RPC
    channel to the client -- a single stray line of human-readable text there
    corrupts the stream and the handshake fails. These messages fire on first
    run, during the Hugging Face download, which is exactly when a freshly
    configured client is starting the server for the first time.
    """
    print(f"vin-decode-mcp: {message}", file=sys.stderr, flush=True)


def _cache_db_path() -> Path:
    """Where an auto-downloaded database is cached.

    Not next to the installed package: for a wheel that resolves inside
    site-packages, which is often read-only and is wiped on upgrade.
    """
    cache_root = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(cache_root).expanduser() if cache_root else Path.home() / ".cache"
    return base / "vin-decode-mcp" / DEFAULT_DB_FILENAME


def _download_db_from_hf(target: Path) -> bool:
    """Download the curated database from Hugging Face into `target`.

    Returns True if the database is in place afterwards, False otherwise.
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        _log(
            "auto-download requires huggingface_hub. Install with: "
            "pip install huggingface_hub — or set VIN_MCP_DB_PATH to an "
            "existing database."
        )
        return False

    try:
        _log(f"downloading database from Hugging Face ({HF_REPO_ID})...")
        src_path = hf_hub_download(
            repo_id=HF_REPO_ID,
            filename=DEFAULT_DB_FILENAME,
            repo_type="dataset",
        )
        import shutil

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, target)
        _log(f"database saved to {target}")
        return True
    except Exception as e:  # noqa: BLE001 — any failure falls back to the error path
        _log(f"failed to download database: {e}")
        return False


def resolve_db_path(args_db: Path | None) -> Path:
    """Resolve the database path: VIN_MCP_DB_PATH > --db-path > cached copy.

    If no database exists at any of those, downloads one from Hugging Face
    into the cache directory. The returned path may still not exist, in which
    case the database layer raises a FileNotFoundError naming it.
    """
    env_val = os.environ.get("VIN_MCP_DB_PATH", "").strip()
    if env_val:
        env_path = Path(env_val).expanduser()
        if env_path.exists():
            return env_path
        # Explicitly configured and wrong: say so rather than silently
        # falling through to a different database than the one requested.
        _log(f"VIN_MCP_DB_PATH points at {env_path}, which does not exist")

    if args_db:
        if args_db.exists():
            return args_db
        _log(f"--db-path points at {args_db}, which does not exist")

    # A database sitting beside a source checkout, for development.
    repo_db = Path(__file__).parent.parent.parent / DEFAULT_DB_FILENAME
    if repo_db.exists():
        return repo_db

    cache_db = _cache_db_path()
    if cache_db.exists():
        return cache_db

    if _download_db_from_hf(cache_db):
        return cache_db

    return cache_db


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
