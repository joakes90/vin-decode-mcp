"""Database layer for VIN Decode MCP.

Manages a read-only SQLite connection to the curated vPIC database.
All queries are parameterized and safe. The connection is opened with
file:...?mode=ro so writes are physically impossible at the OS level.

Database schema (produced by tools/vpic_pare_down.py):

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
_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "curated_vpic.db"


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
            # check_same_thread=False: FastMCP dispatches tool calls on a
            # worker pool, so the connection outlives the thread that opened
            # it. Safe here because the database is opened read-only and
            # Python's sqlite3 serialises access.
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
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
            wmi_length (int | None), confidence (str). Every key is present on
            every path, including the failure paths, so callers never have to
            probe for them.
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
                "wmi_length": None,
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
                "wmi_length": None,
                "confidence": "no_wmi_match",
            }

        make_name, model_name, make_id, model_id = self._resolve_model(vin, wmi, make_ids, year)

        vehicle_type = self._vehicle_type_for(make_id, model_id)

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

    def _vehicle_type_for(self, make_id: int | None, model_id: int | None) -> str | None:
        """Vehicle type for a resolved make/model pair, or None if unknown.

        Kept out of the model-resolution query on purpose. `make_model` carries
        one row per (make, model, vehicletype), and a model that vPIC types
        more than one way (an Accord is both Passenger Car and MPV) multiplies
        the pattern join, letting an arbitrary row win the `LIMIT 1`. Worse,
        joining `vehicletype` there at all has to be an inner join to read the
        name, which silently discards every pair whose type is NULL -- 291 of
        them in the curated build, where NULL means "no pattern coverage
        anywhere", not "no such vehicle".

        Ties are broken by lowest vehicletype id, which orders vPIC's ids as
        Motorcycle < Passenger Car < Truck < MPV < Off-Road: the more specific
        body classes sort ahead of MPV, the catch-all that was previously
        winning at random.
        """
        if make_id is None or model_id is None:
            return None
        row = self.conn.execute(
            """SELECT vt.name
               FROM make_model mm
               JOIN vehicletype vt ON vt.id = mm.vehicletypeid
               WHERE mm.makeid = ? AND mm.modelid = ?
               ORDER BY vt.id
               LIMIT 1""",
            (make_id, model_id),
        ).fetchone()
        return row["name"] if row else None

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
    ) -> tuple[str | None, str | None, int | None, int | None]:
        """Resolve make and model together from VIN pattern matching.

        Returns: (make_name, model_name, make_id, model_id)

        A WMI is not unique to a make -- JN1 covers both Nissan and Infiniti,
        and picking the first row returned decodes a 370Z as an Infiniti. The
        matched *model* is what disambiguates, so make and model are resolved
        together or not at all.

        vPIC's '*' in `keys` means exactly one character, which is GLOB's '?',
        not GLOB's '*'. Appending a real GLOB '*' then makes the whole thing a
        prefix match, which is what handles keys of differing widths --
        including character classes like 'AZ4[EF]', where the string is seven
        characters long but matches only four VIN positions. Longer keys are
        more specific, so the widest match wins.
        """
        vds = vin[3:17]  # Vehicle Descriptor Section

        if not make_ids:
            return None, None, None, None

        make_placeholders = ",".join("?" for _ in make_ids)
        row = self.conn.execute(
            f"""
            SELECT mk.id AS makeid, mk.name AS make,
                   mo.id AS modelid, mo.name AS model,
                   LENGTH(p.keys) AS width
            FROM wmi_vinschema ws
            JOIN vin_pattern p ON p.vinschemaid = ws.vinschemaid
            JOIN model mo ON mo.id = p.modelid
            JOIN make_model mm ON mm.modelid = p.modelid
            JOIN make mk ON mk.id = mm.makeid
            WHERE ws.wmi = ?
              AND mm.makeid IN ({make_placeholders})
              AND (? IS NULL OR ? BETWEEN ws.year_from AND COALESCE(ws.year_to, 9999))
              AND ? GLOB REPLACE(p.keys, '*', '?') || '*'
            ORDER BY width DESC
            LIMIT 1
            """,
            (wmi, *make_ids, year, year, vds),
        ).fetchone()
        if row:
            return row["make"], row["model"], row["makeid"], row["modelid"]

        # No model matched. The make is still worth returning when the WMI is
        # unambiguous -- a half-filled answer beats an empty one -- but a guess
        # between several makes is worse than nothing.
        if len(make_ids) == 1:
            row = self.conn.execute(
                "SELECT id, name FROM make WHERE id = ?", (make_ids[0],)
            ).fetchone()
            if row:
                return row["name"], None, row["id"], None

        return None, None, None, None

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

        wmi, make_ids, _ = self._resolve_wmi(pattern)
        if wmi is None:
            return []

        # Same rule decode_vin uses: VIN position 10 is the year code and
        # position 7 selects the cycle. Reading the year off the *last*
        # character of whatever the caller typed instead read 'A' out of
        # "5UXWX7C5*BA" and produced 1980/2010 rather than 2011, so every
        # candidate then failed the schema year range and the whole lookup
        # degraded to WMI-only.
        year = self._model_year_from_vin(pattern)

        # The stored `keys` describe the VDS from position 4 onward and are
        # prefix-matched (note the trailing GLOB '*'), so passing the rest of
        # the partial VIN through is safe and more selective than truncating.
        # A caller's literal '*' lines up with the '?' that REPLACE puts in
        # the pattern, which is what makes wildcards work at all.
        vds_partial = pattern[3:]

        rows = self.conn.execute(
            f"""SELECT mk.name AS make, mk.id AS makeid,
                       mo.name AS model, mo.id AS modelid,
                       LENGTH(p.keys) AS width
               FROM wmi_vinschema ws
               JOIN vin_pattern p ON p.vinschemaid = ws.vinschemaid
               JOIN model mo ON mo.id = p.modelid
               JOIN make_model mm ON mm.modelid = p.modelid
               JOIN make mk ON mk.id = mm.makeid
               WHERE ws.wmi = ?
                 AND mm.makeid IN ({",".join("?" for _ in make_ids)})
                 AND (? IS NULL OR ? BETWEEN ws.year_from AND COALESCE(ws.year_to, 9999))
                 AND ? GLOB REPLACE(p.keys, '*', '?') || '*'
               ORDER BY width DESC""",
            (wmi, *make_ids, year, year, vds_partial),
        ).fetchall()

        results: list[dict] = []
        seen: set[tuple[int, int]] = set()
        for r in rows:
            key = (r["makeid"], r["modelid"])
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "pattern": pattern,
                    "make": r["make"],
                    "model": r["model"],
                    "year": year,
                    "vehicle_type": self._vehicle_type_for(r["makeid"], r["modelid"]),
                    "confidence": "partial_match",
                }
            )
            if len(results) >= limit:
                break
        if results:
            return results

        # No model matched. Naming the make is only honest when the WMI maps
        # to exactly one -- JN1 is both Nissan and Infiniti, and picking the
        # first row returned would report a guess as a fact.
        if len(make_ids) == 1:
            row = self.conn.execute("SELECT name FROM make WHERE id = ?", (make_ids[0],)).fetchone()
            if row:
                return [
                    {
                        "pattern": pattern,
                        "make": row["name"],
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
