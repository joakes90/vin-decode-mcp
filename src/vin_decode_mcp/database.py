"""Database layer for VIN Decode MCP.

Manages a read-only SQLite connection to the curated vPIC database.
All queries are parameterized and safe. The connection is opened with
file:...?mode=ro so writes are physically impossible at the OS level.

Database schema (produced by tools/build_db.py):

    make(id, name)
    model(id, name)
    vehicletype(id, name)
    make_model(makeid, modelid, vehicletypeid)
    model_years(makeid, modelid, year_from, year_to)
    wmi(wmi, makeid)                        -- wmi is 3 or 6 chars
    wmi_vinschema(wmi, vinschemaid, year_from, year_to)
    vin_pattern(vinschemaid, keys, modelid) -- keys has '*' wildcards
    dataset_info(key, value)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# VIN position 10's model-year code. 30 codes on a 30-year cycle:
# skip I, O, Q, U, Z (too easily confused with 1, 0, 2).
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"

# Default database path — will be overridden by CLI env var.
_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "provenance_vpic.db"


_db_path: Path | None = None


def set_default_db_path(path: Path) -> None:
    """Set the default database path (called by CLI before server starts)."""
    global _db_path
    _db_path = path


class VinDatabase:
    """Read-only wrapper around the curated vPIC SQLite database."""

    SCHEMA_SQL = """
CREATE TABLE make (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE model (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE vehicletype (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE make_model (
    makeid        INTEGER NOT NULL REFERENCES make(id),
    modelid       INTEGER NOT NULL REFERENCES model(id),
    vehicletypeid INTEGER REFERENCES vehicletype(id)
);
CREATE TABLE model_years (
    makeid    INTEGER NOT NULL REFERENCES make(id),
    modelid   INTEGER NOT NULL REFERENCES model(id),
    year_from INTEGER NOT NULL,
    year_to   INTEGER
);
CREATE TABLE wmi (wmi TEXT NOT NULL, makeid INTEGER NOT NULL REFERENCES make(id));
CREATE TABLE wmi_vinschema (
    wmi         TEXT NOT NULL,
    vinschemaid INTEGER NOT NULL,
    year_from   INTEGER NOT NULL,
    year_to     INTEGER
);
CREATE TABLE vin_pattern (
    vinschemaid INTEGER NOT NULL,
    keys        TEXT NOT NULL,
    modelid     INTEGER NOT NULL REFERENCES model(id)
);
CREATE TABLE dataset_info (key TEXT PRIMARY KEY, value TEXT);
"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = _db_path if _db_path is not None else _DEFAULT_DB_PATH
        if isinstance(db_path, str):
            db_path = Path(db_path)
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        """Lazy-read-only connection."""
        if self._conn is None:
            if not self.db_path.exists():
                raise FileNotFoundError(
                    f"VIN database not found at {self.db_path}. "
                    f"Download it from the project releases or Hugging Face."
                )
            uri = f"file:{self.db_path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> VinDatabase:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Schema & metadata
    # ------------------------------------------------------------------
    def get_schema_ddl(self) -> str:
        """Return the full DDL for documentation purposes."""
        return self.SCHEMA_SQL

    def get_dataset_info(self) -> dict[str, str]:
        """Return dataset metadata from the dataset_info table."""
        cursor = self.conn.execute("SELECT key, value FROM dataset_info")
        return {row["key"]: row["value"] for row in cursor}

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------
    def _make_id_to_name(self, make_ids: list[int]) -> dict[int, str]:
        if not make_ids:
            return {}
        qmarks = ",".join("?" for _ in make_ids)
        rows = self.conn.execute(
            f"SELECT id, name FROM make WHERE id IN ({qmarks})", make_ids
        ).fetchall()
        return {r["id"]: r["name"] for r in rows}

    def _model_id_to_name(self, model_ids: list[int]) -> dict[int, str]:
        if not model_ids:
            return {}
        qmarks = ",".join("?" for _ in model_ids)
        rows = self.conn.execute(
            f"SELECT id, name FROM model WHERE id IN ({qmarks})", model_ids
        ).fetchall()
        return {r["id"]: r["name"] for r in rows}

    def _make_name_to_id(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT id, name FROM make")
        return {r["name"].lower(): r["id"] for r in rows}

    def _model_name_for(self, makeid: int, model_name: str) -> int | None:
        """Resolve (makeid, model_name) -> modelid."""
        row = self.conn.execute(
            """SELECT mm.modelid
               FROM make_model mm
               JOIN model mo ON mo.id = mm.modelid
               WHERE mm.makeid = ? AND LOWER(mo.name) = LOWER(?)""",
            (makeid, model_name),
        ).fetchone()
        return row["modelid"] if row else None

    # ------------------------------------------------------------------
    # VIN decode (reference implementation from vpic_pare_down.py)
    # ------------------------------------------------------------------
    def decode_vin(self, vin: str, model_year: int | None = None) -> dict[str, Any]:
        """Decode a VIN to make, model, year, and vehicle type.

        Args:
            vin: 17-character VIN string (or shorter for partial decode).
            model_year: Optional explicit model year. If omitted, computed
                from VIN position 10 + position 7 disambiguation.

        Returns:
            dict with keys: vin, make (str | None), model (str | None),
            year (int | None), vehicle_type (str | None), wmi (str | None),
            confidence (str).
        """
        vin = vin.strip().upper()
        if len(vin) < 11:
            return {
                "vin": vin,
                "make": None,
                "model": None,
                "year": None,
                "vehicle_type": None,
                "wmi": None,
                "confidence": "invalid_vin",
            }

        year = model_year if model_year is not None else self._model_year_from_vin(vin)
        wmi, make_ids, wmi_len = self._resolve_wmi(vin)
        if wmi is None:
            return {
                "vin": vin,
                "make": None,
                "model": None,
                "year": year,
                "vehicle_type": None,
                "wmi": None,
                "confidence": "no_wmi_match",
            }

        make_names = self._make_id_to_name(make_ids) if make_ids else {}

        model_name, vehicle_type_id, resolved_make = self._resolve_model(vin, wmi, make_ids, year)

        # Use resolved make from SQL match (handles multi-make WMI disambiguation)
        make_name = resolved_make
        if make_name is None and len(make_ids) == 1:
            make_name = make_names.get(make_ids[0])

        vehicle_type = None
        if vehicle_type_id is not None:
            row = self.conn.execute(
                "SELECT name FROM vehicletype WHERE id = ?", (vehicle_type_id,)
            ).fetchone()
            if row:
                vehicle_type = row["name"]

        if make_name and model_name:
            confidence = "full"
        elif make_name:
            confidence = "make_only"
        else:
            confidence = "no_match"

        return {
            "vin": vin,
            "make": make_name,
            "model": model_name,
            "year": year,
            "vehicle_type": vehicle_type,
            "wmi": wmi,
            "wmi_length": wmi_len,
            "confidence": confidence,
        }

    def _model_year_from_vin(self, vin: str) -> int | None:
        """Compute model year from VIN positions 10 and 7.

        Position 10 is the year code letter/digit.
        Position 7 breaks ambiguity: digit => 1980-2009, letter => 2010-2039.
        """
        if len(vin) < 10:
            return None
        code = vin[9].upper()
        index = _YEAR_CODES.find(code)
        if index < 0:
            return None
        base = 2010 if vin[6].isalpha() else 1980
        return base + index

    def _resolve_wmi(self, vin: str) -> tuple[str | None, list[int], int | None]:
        """Resolve WMI from VIN. Try 6-char first, fall back to 3-char.

        Returns (wmi_string, make_ids, wmi_length).
        Multiple makes can share a WMI (e.g. JN1 = Nissan + Infiniti).
        """
        # 6-char WMI: positions 1-3 + 12-14 (low-volume manufacturers)
        wmi_6 = vin[0:3] + vin[11:14] if len(vin) >= 14 else None

        for wmi in (wmi_6, vin[0:3]):
            if wmi is None:
                continue
            rows = self.conn.execute(
                "SELECT DISTINCT makeid FROM wmi WHERE wmi = ?", (wmi,)
            ).fetchall()
            if rows:
                make_ids = [r["makeid"] for r in rows]
                return wmi, make_ids, len(wmi)

        return None, [], None

    def _resolve_model(
        self,
        vin: str,
        wmi: str,
        make_ids: list[int],
        year: int | None,
    ) -> tuple[str | None, int | None, str | None]:
        """Resolve model, vehicle type, and make from VIN pattern matching.

        The WMI alone does not identify a make — JN1 covers Nissan and Infiniti.
        The matched model disambiguates, so make and model are resolved together.

        Returns: (model_name, vehicle_type_id, make_name)

        vPIC's '*' in keys means exactly one character (GLOB '?'), and keys may
        contain character classes like [EF]. We match by translating '*' to '?'
        and doing a prefix GLOB match. Longer keys are more specific and win.
        """
        vds = vin[3:17]  # Vehicle Descriptor Section

        # Build IN clause for make IDs
        if make_ids:
            qmarks_in = ",".join("?" for _ in make_ids)
            params: list = [wmi, vds]
        else:
            qmarks_in = None
            params = [wmi, vds]

        if year is not None:
            params.extend([year, year])

        if make_ids:
            # Build param list matching SQL placeholder order
            # SQL: ws.wmi=?, mm.makeid IN (?), year=?, vds=?
            params = (
                [wmi, make_ids[0], year, year, vds]
                if len(make_ids) == 1
                else [wmi, *make_ids, year, year, vds]
            )

            # Pattern match against all candidate makes for this WMI
            sql = f"""
                SELECT mk.id AS makeid, mk.name AS make,
                       mo.id AS modelid, mo.name AS model,
                       vt.id AS vehicle_type_id,
                       LENGTH(p.keys) AS width
                FROM wmi_vinschema ws
                JOIN vin_pattern p ON p.vinschemaid = ws.vinschemaid
                JOIN model mo ON mo.id = p.modelid
                JOIN make_model mm ON mm.modelid = p.modelid
                JOIN make mk ON mk.id = mm.makeid
                JOIN vehicletype vt ON vt.id = mm.vehicletypeid
                WHERE ws.wmi = ?
                  AND mm.makeid IN ({qmarks_in})
                  AND (? IS NULL OR ? BETWEEN ws.year_from AND COALESCE(ws.year_to, 9999))
                  AND SUBSTR(?, 1) GLOB REPLACE(p.keys, '*', '?') || '*'
                ORDER BY width DESC
                LIMIT 1
            """
        else:
            params = [wmi, year, year, vds]
            sql = """
                SELECT mk.id AS makeid, mk.name AS make,
                       mo.id AS modelid, mo.name AS model,
                       vt.id AS vehicle_type_id,
                       LENGTH(p.keys) AS width
                FROM wmi_vinschema ws
                JOIN vin_pattern p ON p.vinschemaid = ws.vinschemaid
                JOIN model mo ON mo.id = p.modelid
                JOIN make_model mm ON mm.modelid = p.modelid
                JOIN make mk ON mk.id = mm.makeid
                JOIN vehicletype vt ON vt.id = mm.vehicletypeid
                WHERE ws.wmi = ?
                  AND (? IS NULL OR ? BETWEEN ws.year_from AND COALESCE(ws.year_to, 9999))
                  AND SUBSTR(?, 1) GLOB REPLACE(p.keys, '*', '?') || '*'
                ORDER BY width DESC
                LIMIT 1
            """

        row = self.conn.execute(sql, params).fetchone()
        if row:
            return row["model"], row["vehicle_type_id"], row["make"]

        # No model matched — make is still worth returning if unambiguous
        if len(make_ids) == 1:
            row = self.conn.execute("SELECT name FROM make WHERE id = ?", (make_ids[0],)).fetchone()
            if row:
                return None, None, row["name"]

        return None, None, None

    # ------------------------------------------------------------------
    # Bulk queries
    # ------------------------------------------------------------------
    def get_all_makes(self, limit: int = 10_000) -> list[dict]:
        """Return all makes sorted by name."""
        rows = self.conn.execute("SELECT id, name FROM make ORDER BY name").fetchmany(limit)
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def get_models_for_make(
        self, make_name: str, vehicle_type: str | None = None, limit: int = 2_000
    ) -> list[dict]:
        """Return models for a make, optionally filtered by vehicle type."""
        row = self.conn.execute(
            "SELECT id FROM make WHERE LOWER(name) = LOWER(?)", (make_name,)
        ).fetchone()
        if not row:
            return []

        if vehicle_type:
            vtype_row = self.conn.execute(
                "SELECT id FROM vehicletype WHERE LOWER(name) = LOWER(?)",
                (vehicle_type,),
            ).fetchone()
            if not vtype_row:
                return []
            vtype_id = vtype_row["id"]
            rows = self.conn.execute(
                """SELECT DISTINCT mo.id, mo.name
                   FROM make_model mm
                   JOIN model mo ON mo.id = mm.modelid
                   JOIN vehicletype vt ON vt.id = mm.vehicletypeid
                   WHERE mm.makeid = ? AND vt.id = ?
                   ORDER BY mo.name
                   LIMIT ?""",
                (row["id"], vtype_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT DISTINCT mo.id, mo.name
                   FROM make_model mm
                   JOIN model mo ON mo.id = mm.modelid
                   WHERE mm.makeid = ?
                   ORDER BY mo.name
                   LIMIT ?""",
                (row["id"], limit),
            ).fetchall()

        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def get_model_years(self, make_name: str, model_name: str) -> dict | None:
        """Return year range for a make/model pair."""
        make_row = self.conn.execute(
            "SELECT id FROM make WHERE LOWER(name) = LOWER(?)", (make_name,)
        ).fetchone()
        if not make_row:
            return None

        model_row = self.conn.execute(
            """SELECT mm.modelid
               FROM make_model mm
               JOIN model mo ON mo.id = mm.modelid
               WHERE mm.makeid = ? AND LOWER(mo.name) = LOWER(?)
               LIMIT 1""",
            (make_row["id"], model_name),
        ).fetchone()
        if not model_row:
            return None

        ranges = self.conn.execute(
            "SELECT year_from, year_to FROM model_years "
            "WHERE makeid = ? AND modelid = ? ORDER BY year_from",
            (make_row["id"], model_row["modelid"]),
        ).fetchall()

        if not ranges:
            return None

        year_from = ranges[0]["year_from"]
        year_to = ranges[-1]["year_to"]  # last range's end (NULL = open-ended)

        return {"year_from": year_from, "year_to": year_to}

    def get_wmi_info(self, wmi: str) -> dict | None:
        """Return WMI lookup info."""
        wmi = wmi.strip().upper()
        row = self.conn.execute("SELECT wmi, makeid FROM wmi WHERE wmi = ?", (wmi,)).fetchone()
        if not row:
            return None
        make_row = self.conn.execute(
            "SELECT name FROM make WHERE id = ?", (row["makeid"],)
        ).fetchone()
        return {
            "wmi": row["wmi"],
            "make": make_row["name"] if make_row else None,
            "makeid": row["makeid"],
        }

    def get_vehicle_types(self) -> list[dict]:
        """Return all vehicle types."""
        rows = self.conn.execute("SELECT id, name FROM vehicletype ORDER BY name").fetchall()
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def get_make_vehicle_types(self, make_name: str) -> list[str]:
        """Return distinct vehicle types for a make."""
        make_row = self.conn.execute(
            "SELECT id FROM make WHERE LOWER(name) = LOWER(?)", (make_name,)
        ).fetchone()
        if not make_row:
            return []
        rows = self.conn.execute(
            """SELECT DISTINCT vt.name
               FROM make_model mm
               JOIN vehicletype vt ON vt.id = mm.vehicletypeid
               WHERE mm.makeid = ? AND vt.name IS NOT NULL
               ORDER BY vt.name""",
            (make_row["id"],),
        ).fetchall()
        return [r["name"] for r in rows]

    def decode_partial_vin(self, pattern: str, limit: int = 100) -> list[dict]:
        """Decode a partial VIN pattern (with * wildcards).

        Args:
            pattern: VIN pattern with * as wildcard (e.g. "1HGCM826*BA").
            limit: Maximum results.

        Returns:
            List of matching VIN decode results.
        """
        pattern = pattern.strip().upper()
        if len(pattern) < 11:
            return []

        # Try to match WMI first (positions 1-3, possibly 6)
        vds_partial = pattern[3:11] if len(pattern) >= 11 else pattern[3:]

        make_name = None
        year = None

        # Attempt WMI match
        wmi = pattern[0:3]
        if len(pattern) >= 14:
            # Try 6-char WMI: positions 1-3 + 12-14
            wmi = pattern[0:3] + pattern[11:14]

        wmi_rows = self.conn.execute("SELECT wmi, makeid FROM wmi WHERE wmi = ?", (wmi,)).fetchall()

        if wmi_rows:
            # Handle multi-make WMIs (e.g. JN1 = Nissan + Infiniti)
            all_make_ids = [r["makeid"] for r in wmi_rows]
            qmarks = ",".join("?" for _ in all_make_ids)
            makes = self.conn.execute(
                f"SELECT id, name FROM make WHERE id IN ({qmarks})", all_make_ids
            ).fetchall()
            make_name = makes[0]["name"] if makes else None

            # Try to match model from partial VDS
            if vds_partial:
                # Check if pattern ends with a year char
                last_char = pattern[-1] if pattern else ""
                if last_char and last_char in _YEAR_CODES and len(pattern) >= 10:
                    try:
                        year_idx = _YEAR_CODES.index(last_char)
                        if len(pattern) > 10 and pattern[-2] in _YEAR_CODES:
                            year = 2010 + year_idx
                        else:
                            year = 1980 + year_idx
                    except ValueError:
                        pass

                year_clause = "? BETWEEN ws.year_from AND COALESCE(ws.year_to, 9999)"
                year_params = [year]
            else:
                year_clause = "1=1"
                year_params = []

            rows = self.conn.execute(
                f"""SELECT mo.name AS model, vt.name AS vehicle_type,
                          LENGTH(p.keys) AS width
                   FROM wmi_vinschema ws
                   JOIN vin_pattern p ON p.vinschemaid = ws.vinschemaid
                   JOIN model mo ON mo.id = p.modelid
                   LEFT JOIN vehicletype vt ON vt.id = (
                       SELECT mm2.vehicletypeid FROM make_model mm2
                       WHERE mm2.modelid = p.modelid LIMIT 1
                   )
                   WHERE ws.wmi = ?
                     AND {year_clause}
                     AND SUBSTR(?, 1) GLOB REPLACE(p.keys, '*', '?') || '*'
                   ORDER BY width DESC
                   LIMIT ?""",
                [wmi, *year_params, vds_partial, limit],
            ).fetchall()

            if rows:
                # Return top matches
                results = []
                seen_models = set()
                for r in rows:
                    if r["model"] not in seen_models and len(results) < limit:
                        results.append(
                            {
                                "pattern": pattern,
                                "make": make_name,
                                "model": r["model"],
                                "year": year,
                                "vehicle_type": r["vehicle_type"],
                                "confidence": "partial_match",
                            }
                        )
                        seen_models.add(r["model"])
                return results

        # Fallback: return WMI info only
        if make_name:
            return [
                {
                    "pattern": pattern,
                    "make": make_name,
                    "model": None,
                    "year": year,
                    "vehicle_type": None,
                    "confidence": "wmi_only",
                }
            ]

        return []

    def get_make_names(self) -> list[str]:
        """Return all make names for auto-completion."""
        rows = self.conn.execute("SELECT name FROM make ORDER BY name").fetchall()
        return [r["name"] for r in rows]
