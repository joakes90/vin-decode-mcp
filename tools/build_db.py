#!/usr/bin/env python3
"""
build_db.py — Orchestrate the vPIC database rebuild pipeline.

This script orchestrates the data pipeline for building the curated
VIN-decode SQLite database from NHTSA's vPIC source data.

Workflow:
    1. Download NHTSA's PostgreSQL standalone database (.custom or .plain)
    2. Restore to a local PostgreSQL database
    3. Dump to plain SQL
    4. Convert PostgreSQL syntax to SQLite
    5. Run the curated pare-down pipeline (vpic_pare_down.py)
    6. Verify and diff against previous build

Usage:
    python3 tools/build_db.py                    # Full rebuild (default)
    python3 tools/build_db.py --verify only      # Verify existing database
    python3 tools/build_db.py --dry-run          # Show what would happen

Environment:
    NHTSA_PG_USERNAME  PostgreSQL username for restoration (default: postgres)
    NHTSA_DB_NAME      Name for the temp database (default: vpic_temp)
    NHTSA_DB_HOST      PostgreSQL host (default: localhost)
    NHTSA_DB_PORT      PostgreSQL port (default: 5432)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TOOLS_DIR.parent
VPIC_SCRIPT = TOOLS_DIR / "vpic_pare_down.py"
DEFAULT_SOURCE = PROJECT_DIR / "tools" / "out" / "vpic_lite.db"
DEFAULT_OUTPUT = PROJECT_DIR / "tools" / "out" / "curated_vpic.db"
DEFAULT_OVERLAY = TOOLS_DIR / "overlay.json"
DEFAULT_CURATION = TOOLS_DIR / "curation.json"
CONVERTER = TOOLS_DIR / "convert_to_sqlite.py"

# ── Defaults ───────────────────────────────────────────────────────────
NHTSA_DOWNLOAD_URL = "https://vpic.nhtsa.dot.gov/downloads/vPICList_lite_{}.custom.zip"
PG_HOST = "localhost"
PG_PORT = "5432"
PG_USER = "postgres"
PG_DB = "vpic_temp"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the curated vPIC database for VIN decoding",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"Source SQLite file (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--overlay",
        type=Path,
        default=DEFAULT_OVERLAY,
        help=f"Overlay JSON (default: {DEFAULT_OVERLAY})",
    )
    parser.add_argument(
        "--curation",
        type=Path,
        default=DEFAULT_CURATION,
        help=f"Curation JSON (default: {DEFAULT_CURATION})",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run verification only (no build)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without doing anything",
    )
    parser.add_argument(
        "--keep-types",
        type=str,
        default=None,
        help="Comma-separated vehicle type IDs to keep (e.g. 2,7)",
    )
    parser.add_argument(
        "--no-vin-decode",
        action="store_true",
        help="Skip VIN decode tables (~1 MB smaller)",
    )
    return parser.parse_args()


def check_pg_available() -> bool:
    """Check if PostgreSQL tools are available."""
    return shutil.which("pg_restore") is not None and shutil.which("psql") is not None


def check_python_script() -> Path:
    """Check the pare-down script exists."""
    if not VPIC_SCRIPT.exists():
        print(f"FATAL: Pare-down script not found at {VPIC_SCRIPT}")
        sys.exit(1)
    return VPIC_SCRIPT


def run_build(args: argparse.Namespace) -> None:
    """Run the vpic_pare_down.py pipeline."""
    cmd = [
        sys.executable,
        str(VPIC_SCRIPT),
        "--source", str(args.source),
        "--output", str(args.output),
        "--overlay", str(args.overlay),
        "--curation", str(args.curation),
    ]
    if args.keep_types:
        cmd.extend(["--keep-types", args.keep_types])
    if args.no_vin_decode:
        cmd.append("--no-vin-decode")

    print(f"\n{'='*60}")
    print(f"Building database: {args.output}")
    print(f"{'='*60}\n")

    result = subprocess.run(cmd, cwd=str(PROJECT_DIR), capture_output=False)
    if result.returncode != 0:
        print(f"\nFAILED: vpic_pare_down.py exited with code {result.returncode}")
        sys.exit(1)

    print(f"\n✓ Database built successfully: {args.output}")
    size_mb = args.output.stat().st_size / 1_048_576
    print(f"  Size: {size_mb:.2f} MB")
    print(f"  Built: {datetime.now(timezone.utc).isoformat()}")


def verify(args: argparse.Namespace) -> None:
    """Run verification only."""
    if not args.output.exists():
        print(f"FATAL: Output database not found at {args.output}")
        sys.exit(1)

    print(f"\nVerifying: {args.output}")
    print(f"{'='*60}\n")

    # Run verification by importing and calling the script's function
    cmd = [
        sys.executable,
        str(VPIC_SCRIPT),
        "--source", str(args.source),
        "--output", str(args.output),
        "--overlay", str(args.overlay),
        "--curation", str(args.curation),
    ]

    # We can't easily call verify() standalone, so just run the full build
    # which includes verification at the end. For pure verification, the user
    # should run with --dry-run or check the existing database manually.
    print("To verify the database, run the full build which includes verification.")
    print(f"  python3 {VPIC_SCRIPT} --source {args.source} --output {args.output}")
    print(f"\nQuick stats:")
    import sqlite3
    conn = sqlite3.connect(f"file:{args.output}?mode=ro", uri=True)
    for table in ("make", "model", "make_model", "model_years",
                  "wmi", "wmi_vinschema", "vin_pattern"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count:,}")
    conn.close()


def main() -> None:
    args = parse_args()

    if args.verify:
        verify(args)
        return

    if args.dry_run:
        print("Dry run — here's what would happen:")
        print(f"  Source: {args.source}")
        print(f"  Output: {args.output}")
        print(f"  Pare-down script: {VPIC_SCRIPT}")
        return

    # Check prerequisites
    check_python_script()

    if not args.source.exists():
        print(f"\n{'='*60}")
        print("SOURCE DATABASE NOT FOUND")
        print(f"{'='*60}\n")
        print(f"Expected source: {args.source}")
        print()
        print("To get the source database:")
        print()
        print("  1. Download NHTSA's PostgreSQL standalone database:")
        print("     wget https://vpic.nhtsa.dot.gov/downloads/vPICList_lite_YYYY_MM.custom.zip")
        print()
        print("  2. Restore to local PostgreSQL:")
        print(f"     pg_restore --host {PG_HOST} --port {PG_PORT} "
              f"--username {PG_USER} --dbname {PG_DB} --no-owner --no-privileges file.custom")
        print()
        print("  3. Convert to SQLite (requires PostgreSQL installed):")
        print(f"     python3 {CONVERTER}")
        print()
        print("  4. Then run this script:")
        print(f"     python3 {VPIC_SCRIPT}")
        print()
        print("Or use the convenience script:")
        print(f"  bash {TOOLS_DIR / 'rebuild.sh'}")
        print()
        sys.exit(1)

    run_build(args)


if __name__ == "__main__":
    main()
