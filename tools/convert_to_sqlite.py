#!/usr/bin/env python3
"""
convert_to_sqlite.py — Convert NHTSA vPIC PostgreSQL data to SQLite.

Takes either:
  - A PostgreSQL plain SQL dump (--input file.sql)
  - A live PostgreSQL connection (--pg-db host/user/db)
  - A PostgreSQL .custom file (--custom file.custom)

And produces a SQLite database suitable for vpic_pare_down.py.

Usage:
    python3 convert_to_sqlite.py --input dump.sql --output vpic_lite.db
    python3 convert_to_sqlite.py --pg-db vpic --pg-user postgres --output vpic_lite.db
    python3 convert_to_sqlite.py --custom vPICList_lite.custom --output vpic_lite.db
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert NHTSA vPIC PostgreSQL data to SQLite",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path, help="PostgreSQL plain SQL dump file")
    group.add_argument("--custom", type=Path, help="PostgreSQL .custom backup file")
    group.add_argument("--pg-db", help="PostgreSQL database name (live connection)")
    parser.add_argument("--pg-host", default="localhost")
    parser.add_argument("--pg-port", type=int, default=5432)
    parser.add_argument("--pg-user", default=os.environ.get("PGUSER", "postgres"))
    parser.add_argument("--output", type=Path, required=True, help="Output SQLite file")
    return parser.parse_args()


def convert_from_sql(sql_path: Path, output_path: Path) -> None:
    """Convert a PostgreSQL plain SQL dump to SQLite.

    Strategy: Parse the SQL dump, extract CREATE TABLE and INSERT/COPY
    statements, convert PG syntax to SQLite, and execute.
    """
    print(f"Converting SQL dump: {sql_path} → {output_path}")

    # Extract COPY statements (PG bulk load) into separate files
    text = sql_path.read_text(encoding="utf-8", errors="replace")

    # Find COPY TABLE statements and extract the data
    copy_blocks = []
    lines = text.split("\n")
    in_copy = False
    current_table = None
    current_data = []
    current_columns = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith("COPY "):
            # Start of a COPY block
            if in_copy:
                copy_blocks.append(
                    {
                        "table": current_table,
                        "columns": current_columns,
                        "data": "\n".join(current_data),
                    }
                )
                current_data = []

            # Parse: COPY vpic.table_name (col1, col2) FROM stdin
            import re
            match = re.match(
                r"COPY\s+(\S+)\s*\(([^)]+)\)\s*FROM\s+stdin", stripped
            )
            if match:
                in_copy = True
                current_table = match.group(1)
                current_columns = [c.strip() for c in match.group(2).split(",")]
            else:
                in_copy = False

        elif in_copy:
            if stripped == "\." :
                # End of COPY
                if current_table:
                    copy_blocks.append(
                        {
                            "table": current_table,
                            "columns": current_columns,
                            "data": "\n".join(current_data),
                        }
                    )
                in_copy = False
                current_table = None
                current_columns = None
                current_data = []
            else:
                current_data.append(stripped)

    # Don't forget the last COPY block
    if in_copy and current_table:
        copy_blocks.append(
            {
                "table": current_table,
                "columns": current_columns,
                "data": "\n".join(current_data),
            }
        )

    print(f"  Found {len(copy_blocks)} COPY blocks")

    # Create the SQLite database
    if output_path.exists():
        output_path.unlink()

    conn = sqlite3.connect(output_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Schema only: extract CREATE TABLE statements from the original SQL
    # (without the data)
    schema_lines = []
    in_copy = False
    for line in lines:
        if line.strip().startswith("COPY "):
            in_copy = True
            continue
        if in_copy and line.strip() == "\.":
            in_copy = False
            continue
        if not in_copy and line.strip():
            schema_lines.append(line)

    schema_text = "\n".join(schema_lines)

    # Convert PG syntax to SQLite
    schema_text = convert_pg_to_sqlite_schema(schema_text)

    # Execute schema
    try:
        conn.executescript(schema_text)
    except sqlite3.Error as e:
        print(f"  ⚠ Schema creation had issues (non-fatal): {e}")
        # Try creating tables one at a time
        for stmt in schema_text.split(";"):
            stmt = stmt.strip()
            if stmt.startswith("CREATE TABLE"):
                try:
                    conn.execute(stmt)
                except sqlite3.Error:
                    pass

    # Load data from COPY blocks
    loaded_rows = 0
    for block in copy_blocks:
        table = block["table"]
        # Strip schema prefix if present
        table_name = table.split(".")[-1] if "." in table else table

        if not block["columns"]:
            continue

        # Parse CSV data
        reader = csv.reader(io.StringIO(block["data"]))
        rows = []
        for row in reader:
            if row and row[0] != "\.":
                rows.append(row)

        if not rows:
            continue

        columns_str = ", ".join(block["columns"])
        placeholders = ", ".join("?" for _ in block["columns"])
        insert_sql = f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders})"

        try:
            conn.executemany(insert_sql, rows)
            loaded_rows += len(rows)
            if len(rows) > 1000:
                conn.commit()
        except sqlite3.Error as e:
            print(f"  ⚠ Error loading {table_name}: {e} ({len(rows)} rows)")

    conn.commit()

    # Add indexes (rebuild from schema)
    index_re = r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)\s+ON\s+(\S+)\s*\(([^)]+)\)"
    for match in re.finditer(index_re, schema_text, re.IGNORECASE):
        idx_name = match.group(1)
        table_ref = match.group(2).split(".")[-1]
        columns = match.group(3)
        try:
            conn.execute(
                f"CREATE INDEX {idx_name} ON {table_ref} ({columns})"
            )
        except sqlite3.Error:
            pass

    conn.commit()
    conn.close()

    print(f"  ✓ Loaded {loaded_rows:,} rows total")
    print(f"  ✓ Output: {output_path} ({output_path.stat().st_size / 1_048_576:.1f} MB)")


def convert_pg_to_sqlite_schema(schema_text: str) -> str:
    """Convert PostgreSQL-specific syntax to SQLite-compatible SQL.

    Handles:
    - SERIAL → INTEGER
    - TEXT(255) → TEXT
    - NUMERIC → REAL
    - UUID → TEXT
    - DEFAULT nextval(...) → removed
    - Schema-qualified names → unqualified
    - COMMENT ON → ignored
    - DO $$ blocks → ignored
    """
    import re

    # Remove COMMENT ON statements
    schema_text = re.sub(
        r"COMMENT\s+ON\s+\S+\s+IS\s+'[^']*';", "", schema_text, flags=re.IGNORECASE
    )

    # Remove DO $$ ... $$ blocks
    schema_text = re.sub(
        r"DO\s+\$\$[^$]*\$\$;", "", schema_text, flags=re.IGNORECASE | re.DOTALL
    )

    # Remove SET statements
    schema_text = re.sub(
        r"SET\s+\S+\s*=.*;", "", schema_text, flags=re.IGNORECASE
    )

    # Remove pg_catalog references (they're catalog tables)
    schema_text = re.sub(
        r"pg_catalog\.\w+", "", schema_text, flags=re.IGNORECASE
    )

    # Remove schema prefix from table references
    schema_text = re.sub(
        r"\bvpic\.", "", schema_text, flags=re.IGNORECASE
    )

    # Convert SERIAL types
    schema_text = re.sub(r"\bBIGSERIAL\b", "INTEGER", schema_text)
    schema_text = re.sub(r"\bSERIAL\b", "INTEGER", schema_text)
    schema_text = re.sub(r"\bSMALLSERIAL\b", "INTEGER", schema_text)

    # Convert NUMERIC
    schema_text = re.sub(r"\bNUMERIC\b", "REAL", schema_text)
    schema_text = re.sub(r"\bDECIMAL\b", "REAL", schema_text)

    # Convert UUID
    schema_text = re.sub(r"\bUUID\b", "TEXT", schema_text)

    # Convert TIMESTAMPTZ
    schema_text = re.sub(r"\bTIMESTAMPTZ\b", "TEXT", schema_text)

    # Convert inet/ cidr
    schema_text = re.sub(r"\b(inet|cidr|macaddr)\b", "TEXT", schema_text)

    # Convert boolean
    schema_text = re.sub(r"\bBOOLEAN\b", "INTEGER", schema_text)

    # Remove DEFAULT nextval(...)
    schema_text = re.sub(
        r"\bDEFAULT\s+nextval\([^)]*\)", "", schema_text
    )

    # Remove column-level OPTIONS and other PG-specific stuff
    schema_text = re.sub(r"\bOPTIONS\s*\([^)]*\)", "", schema_text)

    # Remove WITH (...) clauses
    schema_text = re.sub(
        r"\bWITH\s*\([^)]*\)", "", schema_text, flags=re.IGNORECASE
    )

    return schema_text


def convert_from_custom(custom_path: Path, output_path: Path) -> None:
    """Convert a PostgreSQL .custom backup to SQLite.

    Uses pg_restore to extract to a temp DB, then dumps via psql,
    then converts the SQL dump.
    """
    import tempfile

    print(f"Converting custom backup: {custom_path} → {output_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = Path(tmpdir) / "vpic_sqlite.db"

        # Extract SQL from custom format using pg_restore --schema-only
        # then data
        pg_restore_cmd = [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "--dbname", f"vpic_{os.environ.get('PGUSER', 'postgres')}",
            str(custom_path),
        ]

        # This requires an active PostgreSQL server. Use subprocess approach.
        print("  Note: pg_restore requires an active PostgreSQL server.")
        print("  Using a hybrid approach: extract tables and convert.")

        # Alternative: use pg_restore to dump to SQL, then convert
        sql_file = Path(tmpdir) / "dump.sql"
        result = subprocess.run(
            ["pg_restore", "--no-owner", "--no-privileges", "--clean",
             "--if-exists", "--create", str(custom_path)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            print(f"  ✗ pg_restore failed: {result.stderr[:200]}")
            print("  Please ensure PostgreSQL is installed and running.")
            return

        # Read the generated SQL and convert
        if sql_file.exists():
            convert_from_sql(sql_file, output_path)
        else:
            print("  ✗ Could not generate SQL dump from custom backup")


def main() -> None:
    args = parse_args()

    if args.input:
        if not args.input.exists():
            print(f"FATAL: Input file not found: {args.input}")
            return
        convert_from_sql(args.input, args.output)

    elif args.custom:
        if not args.custom.exists():
            print(f"FATAL: Custom file not found: {args.custom}")
            return
        convert_from_custom(args.custom, args.output)

    elif args.pg_db:
        # Live connection: dump to SQL then convert
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            sql_file = Path(tmpdir) / "dump.sql"
            result = subprocess.run(
                [
                    "pg_dump",
                    "--no-owner",
                    "--no-privileges",
                    "--schema=vpic",
                    "--data-only",
                    "-h", args.pg_host,
                    "-p", str(args.pg_port),
                    "-U", args.pg_user,
                    "-d", args.pg_db,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and sql_file.exists():
                convert_from_sql(sql_file, args.output)
            else:
                print(f"✗ pg_dump failed: {result.stderr[:200]}")


if __name__ == "__main__":
    main()
