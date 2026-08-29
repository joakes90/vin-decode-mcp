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
import os
import re
import sqlite3
import subprocess
import zipfile
from pathlib import Path

COPY_END_MARKER = "\\."  # PostgreSQL COPY terminator in plain SQL dumps

# PostgreSQL COPY *text* format (the default) is not CSV: fields are tab
# separated, never quoted, and backslash-escaped. Parsing it with csv.reader
# silently corrupts the data in two ways -- a field containing a double quote
# gets swallowed by CSV quote handling, and the NULL marker \N arrives as the
# literal two-character string instead of NULL.
#
# The \N bug is not cosmetic. In the 2026-08 vintage it left 1,799,178 cells
# across 51 columns holding '\N', including every NULL in wmi_vinschema.yearto,
# which makes year ranges compare str against int downstream. Anything reading
# this database then has to defend itself with `NOT LIKE '\N'` filters, which
# is how the curated build ended up dropping most of its WMIs.
#
# See PostgreSQL docs, COPY: "Text Format".
_COPY_ESCAPES = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
}

NULL_MARKER = "\\N"


def decode_copy_field(field: str) -> str | None:
    r"""Decode one PostgreSQL COPY text-format field.

    `\N` (and only an unescaped `\N` occupying the whole field) is NULL. A
    literal backslash-N in the data is dumped as `\\N` and decodes to the
    two-character string, so the two are never ambiguous.
    """
    if field == NULL_MARKER:
        return None
    if "\\" not in field:
        return field

    out: list[str] = []
    i = 0
    n = len(field)
    while i < n:
        ch = field[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = field[i + 1]
        if nxt in _COPY_ESCAPES:
            out.append(_COPY_ESCAPES[nxt])
            i += 2
        elif nxt == "x":
            # \xNN hex escape (1-2 hex digits)
            hex_digits = ""
            j = i + 2
            while j < n and len(hex_digits) < 2 and field[j] in "0123456789abcdefABCDEF":
                hex_digits += field[j]
                j += 1
            if hex_digits:
                out.append(chr(int(hex_digits, 16)))
                i = j
            else:
                out.append(nxt)
                i += 2
        elif nxt in "01234567":
            # \NNN octal escape (1-3 octal digits)
            oct_digits = ""
            j = i + 1
            while j < n and len(oct_digits) < 3 and field[j] in "01234567":
                oct_digits += field[j]
                j += 1
            out.append(chr(int(oct_digits, 8)))
            i = j
        else:
            # Unknown escape: PostgreSQL emits the character itself.
            out.append(nxt)
            i += 2
    return "".join(out)


def parse_copy_row(line: str) -> list[str | None]:
    """Split one COPY text-format line into decoded fields.

    Tab is the delimiter and is never escaped inside a field (an embedded tab
    is dumped as `\\t`), so a plain split is correct and unambiguous.
    """
    return [decode_copy_field(f) for f in line.split("\t")]


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

    Strategy: Parse the SQL dump, extract CREATE TABLE statements,
    convert PG syntax to SQLite, create tables, then load data from COPY blocks.
    """
    print(f"Converting SQL dump: {sql_path} -> {output_path}")

    text = sql_path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")

    # --- Step 1: Extract COPY (data) blocks ---
    copy_blocks = []
    in_copy = False
    current_table = None
    current_data = []
    current_columns = None

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("COPY "):
            # Finalize previous COPY block if any
            if in_copy and current_table:
                copy_blocks.append(
                    {
                        "table": current_table,
                        "columns": current_columns,
                        "data": "\n".join(current_data),
                    }
                )
                current_data = []

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
            if stripped == COPY_END_MARKER:
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
                # Deliberately the raw line, not `stripped`: leading and
                # trailing whitespace is data inside a COPY block, and an
                # all-whitespace field is not the same as an empty one.
                current_data.append(line)

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

    # --- Step 2: Extract and convert schema ---
    if output_path.exists():
        output_path.unlink()

    conn = sqlite3.connect(output_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Remove COPY blocks to get schema text
    schema_lines = []
    in_copy = False
    for line in lines:
        if line.strip().startswith("COPY "):
            in_copy = True
            continue
        if in_copy and line.strip() == COPY_END_MARKER:
            in_copy = False
            continue
        schema_lines.append(line)

    schema_text = "\n".join(schema_lines)
    schema_text = convert_pg_to_sqlite_schema(schema_text)

    # Create tables one at a time for reliability
    table_re = re.compile(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["\w]+\s*\(([^;]+)\)',
        re.IGNORECASE | re.DOTALL,
    )

    tables_created = 0
    for match in table_re.finditer(schema_text):
        # Extract table name from the full CREATE TABLE statement
        full_match = re.match(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["\w]+',
            match.group(0),
            re.IGNORECASE,
        )
        table_name = full_match.group(0).split()[-1].strip('"')
        table_def = match.group(1).strip()

        # Skip empty or residue tables
        if not table_def.strip() or "REMOVED" in table_def:
            continue

        # Clean up column-level PG-specific syntax
        table_def = re.sub(r"\bON\s+CONFLICT[^;)]+", "", table_def, flags=re.IGNORECASE)
        table_def = re.sub(r"\bDEFERRABLE\b", "", table_def, flags=re.IGNORECASE)
        table_def = re.sub(r"\bNOT\s+DEFERRABLE\b", "", table_def, flags=re.IGNORECASE)
        table_def = re.sub(
            r"\bINITIALLY\s+(DEFERRED|IMMEDIATE)\b", "", table_def, flags=re.IGNORECASE
        )

        # Remove GENERATED ALWAYS/BY DEFAULT AS ... STORED (computed columns)
        table_def = re.sub(
            r"\s*GENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s*\(.*?\)\s+STORED",
            "",
            table_def,
            flags=re.IGNORECASE | re.DOTALL,
        )

        # Convert PostgreSQL array types to TEXT
        # Match multi-word type names with optional size, followed by []
        table_def = re.sub(
            r"\b(?:character\s+varying|varchar|text|integer|bigint|smallint|real|double\s+precision|boolean|timestamp(?:\s+without\s+time\s+zone)?|timestamp(?:\s+with\s+time\s+zone)?|timestamptz|date|uuid|numeric|decimal|inet|cidr|macaddr)\s*\(\d*\)\s*\[\]",
            "TEXT",
            table_def,
            flags=re.IGNORECASE,
        )
        # Handle remaining array notation
        table_def = re.sub(r"\[\]", " TEXT", table_def)
        table_def = re.sub(r"\[\d+\]", "", table_def)

        create_sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({table_def})'
        try:
            conn.execute(create_sql)
            tables_created += 1
        except sqlite3.Error as e:
            print(f"  ⚠ Failed to create table '{table_name}': {e}")

    print(f"  ✓ Created {tables_created} tables")

    # --- Step 3: Load data from COPY blocks ---
    loaded_rows = 0
    for block in copy_blocks:
        table = block["table"]
        table_name = table.split(".")[-1] if "." in table else table

        if not block["columns"]:
            continue

        # PostgreSQL COPY text format — tab separated, backslash escaped,
        # `\N` for NULL. Not CSV; see decode_copy_field above.
        n_cols = len(block["columns"])
        rows = []
        for line in block["data"].split("\n"):
            if not line or line.strip() == COPY_END_MARKER:
                continue
            row = parse_copy_row(line)
            if len(row) != n_cols:
                raise ValueError(
                    f"{table_name}: COPY row has {len(row)} fields, expected "
                    f"{n_cols} ({block['columns']}). Line: {line[:120]!r}"
                )
            rows.append(row)

        if not rows:
            continue

        columns_str = ", ".join(block["columns"])
        placeholders = ", ".join("?" for _ in block["columns"])
        insert_sql = f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders})"

        # A table that fails to load is a corrupt build, not a warning. The
        # previous code printed and carried on, which is how a database with
        # missing tables reached Hugging Face.
        try:
            conn.executemany(insert_sql, rows)
        except sqlite3.Error as e:
            conn.close()
            raise RuntimeError(
                f"Failed to load {len(rows):,} rows into {table_name}: {e}"
            ) from e
        loaded_rows += len(rows)
        if len(rows) > 1000:
            conn.commit()

    conn.commit()

    # --- Step 4: Add indexes ---
    # Handle PG indexes: CREATE INDEX name ON table USING btree (cols) INCLUDE (...)
    index_re = re.compile(
        r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+["\w]+\s+ON\s+["\w]+(?:\s+USING\s+\w+)?\s*\(([^)]+)\)(?:\s+INCLUDE\s*\([^)]*\))?',
        re.IGNORECASE,
    )
    indexes_created = 0
    for match in index_re.finditer(schema_text):
        # Extract index name
        idx_match = re.match(
            r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+["\w]+',
            match.group(0),
            re.IGNORECASE,
        )
        idx_name = idx_match.group(0).split()[-1].strip('"')
        # Extract table name
        tbl_match = re.search(r"ON\s+[\w\"]+", match.group(0), re.IGNORECASE)
        table_ref = tbl_match.group(0).split()[-1].strip('"') if tbl_match else ""
        columns = match.group(1).strip()
        if table_ref:
            try:
                conn.execute(
                    f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table_ref}" ({columns})'
                )
                indexes_created += 1
            except sqlite3.Error:
                pass
    print(f"  ✓ Created {indexes_created} indexes")

    conn.commit()

    verify_no_null_markers(conn)

    conn.close()

    print(f"  ✓ Loaded {loaded_rows:,} rows total")
    print(f"  ✓ Output: {output_path} ({output_path.stat().st_size / 1_048_576:.1f} MB)")


def verify_no_null_markers(conn: sqlite3.Connection) -> None:
    r"""Fail the build if any cell holds the literal string `\N`.

    A surviving `\N` means COPY decoding regressed and NULLs are being stored
    as text. Downstream that silently collapses joins -- it is what reduced the
    curated `wmi` table from ~1,700 rows to 173 -- so it must stop the build
    here rather than surface as a VIN that decodes to a year and nothing else.
    """
    offenders = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        for col in [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]:
            n = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" = ?', (NULL_MARKER,)
            ).fetchone()[0]
            if n:
                offenders.append((table, col, n))

    if offenders:
        detail = ", ".join(f"{t}.{c} ({n:,})" for t, c, n in offenders[:8])
        more = "" if len(offenders) <= 8 else f" and {len(offenders) - 8} more columns"
        conn.close()
        raise RuntimeError(
            rf"COPY decoding failed: literal '\N' stored instead of NULL in {detail}{more}. "
            r"NULLs must be decoded (see decode_copy_field)."
        )
    print(rf"  ✓ Verified: no literal '\N' NULL markers in {len(tables)} tables")


def strip_pg_meta_commands(text: str) -> str:
    """Remove PostgreSQL backslash meta-commands like \\restrict, \\set, etc."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("\\") and not stripped.startswith("--"):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def strip_multiline_blocks(text: str) -> str:
    """Remove multi-line PostgreSQL blocks: CREATE TYPE, CREATE FUNCTION, CREATE SEQUENCE, etc.

    Tracks parentheses and braces depth to know when multi-line blocks end.
    """
    result = []
    lines = text.split("\n")
    skip = False
    skip_kind = None
    brace_depth = 0
    paren_depth = 0

    for line in lines:
        stripped = line.strip()

        if skip:
            # Track depth to know when block ends
            for ch in stripped:
                if ch == "(":
                    paren_depth += 1
                elif ch == ")":
                    paren_depth -= 1
                elif ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1

            # Check if block is ending
            if skip_kind in ("TYPE", "FUNCTION", "TRIGGER", "DO"):
                if paren_depth <= 0 and brace_depth <= 0 and ";" in stripped:
                    skip = False
            elif skip_kind in ("SEQUENCE", "ALTER SEQUENCE", "CREATE SCHEMA"):
                if ";" in stripped:
                    skip = False
            continue

        # Check for blocks to skip
        if re.match(r"CREATE\s+TYPE\b", stripped, re.IGNORECASE):
            skip = True
            skip_kind = "TYPE"
            paren_depth = stripped.count("(") - stripped.count(")")
            brace_depth = stripped.count("{") - stripped.count("}")
            if paren_depth <= 0 and ";" in stripped:
                skip = False
            result.append(f"-- REMOVED: {stripped[:80]}")
        elif re.match(r"CREATE\s+FUNCTION\b", stripped, re.IGNORECASE):
            skip = True
            skip_kind = "FUNCTION"
            paren_depth = stripped.count("(") - stripped.count(")")
            brace_depth = stripped.count("{") - stripped.count("}")
            if paren_depth <= 0 and ";" in stripped:
                skip = False
            result.append(f"-- REMOVED: {stripped[:80]}")
        elif re.match(r"CREATE\s+SEQUENCE\b", stripped, re.IGNORECASE):
            skip = True
            skip_kind = "SEQUENCE"
            result.append(f"-- REMOVED: {stripped}")
        elif re.match(r"ALTER\s+SEQUENCE\b", stripped, re.IGNORECASE):
            skip = True
            skip_kind = "ALTER SEQUENCE"
            result.append(f"-- REMOVED: {stripped}")
        elif re.match(r"CREATE\s+SCHEMA\b", stripped, re.IGNORECASE):
            if ";" in stripped:
                result.append(f"-- REMOVED: {stripped}")
            else:
                skip = True
                skip_kind = "CREATE SCHEMA"
        elif stripped.startswith("DO $$") or stripped.startswith("DO $func$"):
            skip = True
            skip_kind = "DO"
            paren_depth = 0
            brace_depth = 0
            result.append(f"-- REMOVED: {stripped[:80]}")
        elif stripped.startswith("COMMENT ON"):
            result.append(f"-- REMOVED: {stripped}")
        elif re.match(r"SET\s+\S+\s*=", stripped, re.IGNORECASE):
            result.append(f"-- REMOVED: {stripped}")
        elif re.match(r"SELECT\s+pg_catalog\.set_config", stripped, re.IGNORECASE):
            result.append(f"-- REMOVED: {stripped}")
        else:
            result.append(line)

    return "\n".join(result)


def convert_pg_to_sqlite_schema(schema_text: str) -> str:
    """Convert PostgreSQL-specific syntax to SQLite-compatible SQL.

    Handles:
    - Strips backslash meta-commands (\\restrict, \\set, etc.)
    - Removes multi-line blocks (CREATE TYPE, CREATE FUNCTION, CREATE SEQUENCE, etc.)
    - SERIAL -> INTEGER, NUMERIC -> REAL, UUID -> TEXT, etc.
    - DEFAULT nextval(...) -> removed
    - Schema-qualified names -> unqualified
    """

    # Step 1: Strip backslash meta-commands
    schema_text = strip_pg_meta_commands(schema_text)

    # Step 2: Remove multi-line blocks
    schema_text = strip_multiline_blocks(schema_text)

    # Step 3: Regex-based conversions for remaining PG-specific syntax

    # Remove pg_catalog references
    schema_text = re.sub(r"pg_catalog\.\w+", "", schema_text, flags=re.IGNORECASE)

    # Remove schema prefix from table references
    schema_text = re.sub(r"\bvpic\.", "", schema_text, flags=re.IGNORECASE)

    # Convert SERIAL types
    schema_text = re.sub(r"\bBIGSERIAL\b", "INTEGER", schema_text)
    schema_text = re.sub(r"\bSERIAL\b", "INTEGER", schema_text)
    schema_text = re.sub(r"\bSMALLSERIAL\b", "INTEGER", schema_text)

    # Convert NUMERIC/DECIMAL
    schema_text = re.sub(r"\bNUMERIC\b", "REAL", schema_text)
    schema_text = re.sub(r"\bDECIMAL\b", "REAL", schema_text)

    # Convert UUID
    schema_text = re.sub(r"\bUUID\b", "TEXT", schema_text)

    # Convert TIMESTAMPTZ
    schema_text = re.sub(r"\bTIMESTAMPTZ\b", "TEXT", schema_text)

    # Convert inet/cidr/macaddr
    schema_text = re.sub(r"\b(inet|cidr|macaddr)\b", "TEXT", schema_text)

    # Convert boolean
    schema_text = re.sub(r"\bBOOLEAN\b", "INTEGER", schema_text)

    # Convert character varying / varchar
    schema_text = re.sub(
        r"\bCHARACTER\s+VARYING\b", "TEXT", schema_text, flags=re.IGNORECASE
    )
    schema_text = re.sub(r"\bVARCHAR\b", "TEXT", schema_text, flags=re.IGNORECASE)

    # Remove DEFAULT nextval(...)
    schema_text = re.sub(r"\bDEFAULT\s+nextval\([^)]*\)", "", schema_text)

    # Remove column-level OPTIONS
    schema_text = re.sub(r"\bOPTIONS\s*\([^)]*\)", "", schema_text)

    # Remove WITH (...) clauses (PG table options)
    schema_text = re.sub(r"\bWITH\s*\([^)]*\)", "", schema_text, flags=re.IGNORECASE)

    # Remove GENERATED ... AS IDENTITY (PG 10+ identity columns)
    schema_text = re.sub(
        r"\bGENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY",
        "",
        schema_text,
        flags=re.IGNORECASE,
    )

    return schema_text


def convert_from_custom(custom_path: Path, output_path: Path) -> None:
    """Convert a PostgreSQL .custom backup to SQLite.

    Uses pg_restore to render the archive as plain SQL, then converts it.
    Only `pg_restore` is needed -- no PostgreSQL server, no database. NHTSA
    distributes the archive zipped, so a .zip is unpacked first; pg_restore
    cannot read a zip container.
    """
    import tempfile

    print(f"Converting custom backup: {custom_path} -> {output_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        archive = custom_path

        if zipfile.is_zipfile(custom_path):
            with zipfile.ZipFile(custom_path) as zf:
                members = [n for n in zf.namelist() if not n.endswith("/")]
                if not members:
                    raise RuntimeError(f"{custom_path} is an empty zip archive")
                # NHTSA ships a single .custom member.
                member = next(
                    (m for m in members if m.lower().endswith(".custom")), members[0]
                )
                print(f"  Unpacking {member} from {custom_path.name}")
                archive = Path(zf.extract(member, path=tmp))

        sql_file = tmp / "dump.sql"
        result = subprocess.run(
            [
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(sql_file),
                str(archive),
            ],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"pg_restore failed ({result.returncode}): {result.stderr[:500]}\n"
                "Install PostgreSQL client tools (no server needed): "
                "`brew install libpq` or `apt-get install postgresql-client`."
            )
        if not sql_file.exists():
            raise RuntimeError("pg_restore reported success but produced no SQL dump")

        convert_from_sql(sql_file, output_path)


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
        # Live connection: dump to SQL then convert.
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            sql_file = Path(tmpdir) / "dump.sql"
            result = subprocess.run(
                [
                    "pg_dump",
                    "--no-owner",
                    "--no-privileges",
                    "--schema=vpic",
                    # Schema included on purpose: the converter builds its
                    # tables from the CREATE TABLE statements, so a
                    # --data-only dump yields a database with no tables.
                    "--file",
                    str(sql_file),
                    "-h", args.pg_host,
                    "-p", str(args.pg_port),
                    "-U", args.pg_user,
                    "-d", args.pg_db,
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"pg_dump failed ({result.returncode}): {result.stderr[:500]}"
                )
            if not sql_file.exists():
                raise RuntimeError("pg_dump reported success but produced no SQL dump")
            convert_from_sql(sql_file, args.output)


if __name__ == "__main__":
    main()
