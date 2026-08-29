#!/bin/bash
# rebuild.sh — Download NHTSA vPIC data, convert to SQLite, and build the
# curated VIN-decode database.
#
# Usage:
#   bash tools/rebuild.sh
#
# Prerequisites:
#   - PostgreSQL installed (pg_restore, psql, createdb)
#   - Python 3.11+
#   - Internet connection to download NHTSA data
#
# Environment variables (optional):
#   PG_HOST      PostgreSQL host (default: localhost)
#   PG_PORT      PostgreSQL port (default: 5432)
#   PG_USER      PostgreSQL username (default: postgres)
#   PG_DB        Temp database name (default: vpic_temp)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-postgres}"
PG_DB="${PG_DB:-vpic_temp}"

VINTAGE="${1:-}"  # e.g. "2026_08" — extract from NHTSA download URL

echo "=============================================="
echo "NHTSA vPIC → SQLite → Curated DB Pipeline"
echo "=============================================="
echo "  PG:        ${PG_HOST}:${PG_PORT}/${PG_DB}"
echo "  Project:   ${PROJECT_DIR}"
echo ""

# ── Step 1: Download ──────────────────────────────────────────────────
DL_DIR="${PROJECT_DIR}/tools/tmp_download"
mkdir -p "${DL_DIR}"

# Try to infer the latest vintage from the download URL
# NHTSA URLs follow the pattern: vPICList_lite_YYYY_MM.custom.zip
# Try the latest known vintage first, fall back gracefully
DOWNLOAD_URLS=(
    "https://vpic.nhtsa.dot.gov/downloads/vPICList_lite_2026_08.custom.zip"
    "https://vpic.nhtsa.dot.gov/downloads/vPICList_lite_2026_07.custom.zip"
    "https://vpic.nhtsa.dot.gov/downloads/vPICList_lite_2026_06.custom.zip"
)

DL_FILE=""
for url in "${DOWNLOAD_URLS[@]}"; do
    echo "Trying: ${url}"
    if curl -sf -o "${DL_DIR}/vpic.custom.zip" "${url}" 2>/dev/null; then
        DL_FILE="${DL_DIR}/vpic.custom.zip"
        echo "  ✓ Downloaded successfully"
        break
    else
        echo "  ✗ Failed"
    fi
done

if [ -z "${DL_FILE}" ]; then
    echo ""
    echo "ERROR: Could not download from any URL."
    echo "Try downloading manually from: https://vpic.nhtsa.dot.gov/Downloads/"
    echo "Then place the .custom.zip file at: ${DL_DIR}/vpic.custom.zip"
    echo "And re-run: bash ${0}"
    exit 1
fi

# ── Step 2: Restore to PostgreSQL ─────────────────────────────────────
echo ""
echo "Step 2: Restoring to PostgreSQL..."

# Drop existing temp database if it exists
psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -tc "SELECT 1 FROM pg_database WHERE datname='${PG_DB}'" | grep -q 1 && \
    dropdb -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" "${PG_DB}"

createdb -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" "${PG_DB}"

pg_restore \
    --host "${PG_HOST}" \
    --port "${PG_PORT}" \
    --username "${PG_USER}" \
    --dbname "${PG_DB}" \
    --no-owner \
    --no-privileges \
    --verbose \
    "${DL_FILE}"

echo "  ✓ PostgreSQL database restored"

# ── Step 3: Dump to plain SQL ─────────────────────────────────────────
echo ""
echo "Step 3: Dumping to plain SQL..."

SQL_FILE="${DL_DIR}/vpic_dump.sql"
psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" \
    --no-password \
    -f - > "${SQL_FILE}" <<'EOSQL'
-- Dump the vpic schema
SELECT 'DUMPING SCHEMA';
\dn+
SELECT schemaname, tablename FROM pg_tables WHERE schemaname = 'vpic' ORDER BY tablename;
EOSQL

# Actually use pg_dump for a proper SQL dump
psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" \
    --no-password \
    -c "COPY (SELECT 1) TO STDOUT" 2>/dev/null  # Quick connectivity check

# Use pg_dump to extract the vpic schema data
pg_dump -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d "${PG_DB}" \
    --no-owner --no-privileges --schema=vpic \
    --data-only > "${SQL_FILE}" 2>/dev/null

if [ ! -s "${SQL_FILE}" ]; then
    echo "  ✗ pg_dump produced empty file"
    echo "  Falling back to csvs..."
    # If pg_dump fails, we'll convert via Python
    echo "pg_dump not available or failed"
    HAS_PG_DUMP=0
else
    echo "  ✓ SQL dump: $(wc -l < "${SQL_FILE}" | tr -d ' ') lines"
    HAS_PG_DUMP=1
fi

# ── Step 4: Convert PG SQL → SQLite ───────────────────────────────────
echo ""
echo "Step 4: Converting PostgreSQL → SQLite..."

LITE_DB="${PROJECT_DIR}/tools/out/vpic_lite.db"
mkdir -p "${PROJECT_DIR}/tools/out"

if [ "${HAS_PG_DUMP:-1}" -eq 1 ] && [ -s "${SQL_FILE}" ]; then
    # Use the Python converter if available
    if [ -f "${PROJECT_DIR}/tools/convert_to_sqlite.py" ]; then
        python3 "${PROJECT_DIR}/tools/convert_to_sqlite.py" \
            --input "${SQL_FILE}" \
            --output "${LITE_DB}"
        echo "  ✓ SQLite: ${LITE_DB}"
    else
        # Direct conversion via Python
        python3 -c "
import sqlite3
import re
from pathlib import Path

sql_file = Path('${SQL_FILE}')
lite_db = Path('${LITE_DB}')

conn = sqlite3.connect(lite_db)
text = sql_file.read_text(encoding='utf-8', errors='replace')

# Convert PG-specific syntax
text = re.sub(r\"[^:]:\\s*(\\w+)\", r\"\\1\", text)  # Remove colons before identifiers
text = re.sub(r'\\bSERIAL\\b', 'INTEGER', text)
text = re.sub(r\"DEFAULT nextval\\('([^']+)'\\)\", r\"\", text)
text = re.sub(r\"\\bTEXT\\b\", 'TEXT', text)
text = re.sub(r\"\\bNUMERIC\\b\", 'REAL', text)
text = re.sub(r\"\\bUUID\\b\", 'TEXT', text)

# Handle COPY statements - convert to INSERT
# This is a simplification; for production, use proper CSV export

conn.executescript(text)
conn.commit()
conn.close()
print(f'Converted to SQLite: {lite_db}')
"
    fi
else
    echo "  ⚠ pg_dump not available or empty. Using fallback."
    echo "  The convert_to_sqlite.py script handles this case."
    if [ -f "${PROJECT_DIR}/tools/convert_to_sqlite.py" ]; then
        python3 "${PROJECT_DIR}/tools/convert_to_sqlite.py" \
            --pg-db "${PG_DB}" \
            --pg-host "${PG_HOST}" \
            --pg-port "${PG_PORT}" \
            --pg-user "${PG_USER}" \
            --output "${LITE_DB}"
    fi
fi

echo "  ✓ SQLite database: ${LITE_DB}"

# ── Step 5: Run curated pare-down ─────────────────────────────────────
echo ""
echo "Step 5: Running curated pare-down pipeline..."

# --output MUST NOT be the source: vpic_pare_down.py unlinks the output path
# before writing, so pointing both at LITE_DB deletes the converted source
# and the build then fails with nothing to read.
CURATED_DB="${PROJECT_DIR}/tools/out/curated_vpic.db"

python3 "${SCRIPT_DIR}/vpic_pare_down.py" \
    --source "${LITE_DB}" \
    --output "${CURATED_DB}" \
    --overlay "${SCRIPT_DIR}/overlay.json" \
    --curation "${SCRIPT_DIR}/curation.json"

echo ""
echo "=============================================="
echo "Pipeline complete!"
echo "=============================================="
echo ""
echo "Source (full vPIC):  ${LITE_DB}"
echo "Curated (shipped):   ${CURATED_DB} ($(ls -lh "${CURATED_DB}" 2>/dev/null | awk '{print $5}'))"
echo ""
echo "To use with the MCP server:"
echo "  export VIN_MCP_DB_PATH=${CURATED_DB}"
echo "  vin-decode-mcp"
echo ""
echo "To clean up the temp database:"
echo "  dropdb -h ${PG_HOST} -p ${PG_PORT} -U ${PG_USER} ${PG_DB}"
echo "  rm -rf ${DL_DIR}"
