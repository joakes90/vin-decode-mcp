#!/usr/bin/env python3
"""
vpic_pare_down.py -- M2.3 spike prototype.

Pares NHTSA's vPIC "lite" SQLite conversion (~333 MB, 97 tables) down to the
small set of picker tables Provenance actually needs at vehicle-entry time:
make, model, make/model pairs tagged with vehicle type, and model year ranges.

Stdlib only. See tools/README.md for the full stage-by-stage explanation,
the refresh workflow, and where the decision record lives (this repo does not
carry prose docs -- see README).

Usage:
    python3 tools/vpic_pare_down.py \
        --source /Users/justin/Devel/db/sqlite_conversion/vpic_lite.db \
        --output tools/out/curated_vpic.db

Idempotent: re-running against the same source produces the same output
(modulo the build_timestamp stamp and diff_report.txt, which record the run).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_VERSION = "1.2.0"
SOURCE_VINTAGE = "2026-07"

# ---------------------------------------------------------------------------
# Stage 1: config
# ---------------------------------------------------------------------------
# vPIC vehicletype ids we keep. Owner decision 2026-08-08: motorcycles are IN.
#   1  Motorcycle
#   2  Passenger Car
#   3  Truck
#   7  Multipurpose Passenger Vehicle (MPV)
#   13 Off-Road Vehicle
# Excluded on purpose: 5 Bus, 6 Trailer, 9 Low Speed Vehicle (LSV),
# 10 Incomplete Vehicle -- not vehicles a Provenance owner is tracking.
# Changing what's kept is a one-line edit here (or a --keep-types run without
# editing anything); nothing else in the script needs to know the id values.
KEEP_VEHICLE_TYPES: set[int] = {1, 2, 3, 7, 13}

# vPIC's id for the Motorcycle vehicle type. Motorcycle-only makes get extra
# curation (see tools/curation.json): vPIC registers every chopper shop that
# ever filed a WMI, so the raw motorcycle make list is ~1,700 entries of which
# a few dozen are manufacturers a picker should offer.
MOTORCYCLE_TYPE_ID = 1

# elementid in vPIC's `pattern` table whose `attributeid` carries the model id
# (as text) that a given VIN pattern decodes to. 28 = "Model" (see `element`).
MODEL_ELEMENT_ID = 28

# Reserved id range for overlay-curated rows -- guaranteed never to collide
# with a real vPIC id (vPIC ids are all positive).
OVERLAY_ID_START = -1

# ---------------------------------------------------------------------------
# VIN decode (added 1.2.0, M2.4). The picker tables above answer "what can the
# user choose?"; these three answer "what is this VIN?" so the form can fill
# make/model/year in from a scanned or typed VIN.
#
# Deliberately NOT a full vPIC decode. We emit only what is needed to reach
# make and model -- the WMI-to-make map, the year-scoped WMI-to-schema map,
# and the Model element's patterns. Engine, plant, body class and the rest of
# vPIC's ~150 elements are excluded: they would take the output from ~3.5 MB
# to the 15-25 MB the M2.3 spike estimated for a full decode, for data no
# current screen shows. M6's export may want the plant/engine subset later --
# that is a deliberate future addition, not an oversight (see ADR-0018).
#
# Model year is NOT in here and never will be: it is computable from VIN
# position 10 plus the position-7 rule, so shipping a table for it would be
# storing what an algorithm already knows.
# ---------------------------------------------------------------------------
# vPIC WMIs come in two lengths, and a decoder has to try both:
#   3 chars -- VIN positions 1-3, manufacturers building >=1000 vehicles/year
#   6 chars -- VIN positions 1-3 + 12-14, low-volume manufacturers, who share
#              a positions-1-3 prefix and are only distinguishable further in.
# In the 2026-07 source, 9,765 of 12,925 WMIs are the 6-char kind, so treating
# WMIs as 3 characters would misattribute most of the low-volume marques that
# a collector-focused app cares about most.
WMI_SHORT_LENGTH = 3
WMI_LONG_LENGTH = 6

DEFAULT_SOURCE = Path("/Users/justin/Devel/db/sqlite_conversion/vpic_lite.db")
TOOLS_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = TOOLS_DIR / "out" / "curated_vpic.db"
DEFAULT_OVERLAY = TOOLS_DIR / "overlay.json"
DEFAULT_CURATION = TOOLS_DIR / "curation.json"

# Sanity ranges for stage-2 validation: (table, min_expected, max_expected).
# These are loose bounds on the vintage this script was built against
# (2026-07); a wildly different count is a signal the source schema or
# vintage changed in a way worth a human look, not necessarily a bug.
EXPECTED_COUNTS = {
    "make": (8_000, 20_000),
    "model": (20_000, 45_000),
    "make_model": (20_000, 45_000),
    "wmi": (8_000, 20_000),
    "wmi_make": (8_000, 25_000),
    "wmi_vinschema": (20_000, 70_000),
    "pattern": (1_000_000, 2_500_000),
    "vehicletype": (5, 20),
    "element": (50, 300),
}

REQUIRED_TABLES = list(EXPECTED_COUNTS.keys())


# ---------------------------------------------------------------------------
# Small data holders
# ---------------------------------------------------------------------------
@dataclass
class DerivedData:
    makes: dict[int, str] = field(default_factory=dict)  # id -> name
    models: dict[int, str] = field(default_factory=dict)  # id -> name
    vehicletypes: dict[int, str] = field(default_factory=dict)  # id -> name
    # (makeid, modelid, vehicletypeid_or_None) -- vehicletypeid None means
    # "no pattern coverage for this model", still kept, never dropped.
    make_model_rows: set[tuple[int, int, int | None]] = field(default_factory=set)
    # (makeid, modelid) -> list[(year_from, year_to_or_None)], already merged.
    model_years: dict[tuple[int, int], list[tuple[int, int | None]]] = field(
        default_factory=dict
    )
    # --- VIN decode (empty unless --vin-decode ran) ---------------------
    # (wmi, makeid) -- wmi is 3 or 6 characters, see WMI_*_LENGTH.
    wmi_rows: set[tuple[str, int]] = field(default_factory=set)
    # (wmi, vinschemaid, year_from, year_to_or_None)
    wmi_schema_rows: set[tuple[str, int, int, int | None]] = field(default_factory=set)
    # (vinschemaid, keys, modelid) -- keys is a variable-length VDS pattern.
    vin_pattern_rows: set[tuple[int, str, int]] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Stage 2: validate source
# ---------------------------------------------------------------------------
def connect_source(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"FATAL: source database not found at {path}")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def validate_source(conn: sqlite3.Connection, source_path: Path) -> None:
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = [t for t in REQUIRED_TABLES if t not in existing]
    if missing:
        raise SystemExit(
            f"FATAL: source database {source_path} is missing required "
            f"table(s): {', '.join(missing)}"
        )

    print(f"Source: {source_path} ({source_path.stat().st_size / 1_048_576:.1f} MB)")
    print(f"{'table':<16}{'rows':>12}   expected range")
    out_of_range = []
    for table, (lo, hi) in EXPECTED_COUNTS.items():
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count == 0:
            raise SystemExit(
                f"FATAL: source table '{table}' is empty -- refusing to derive "
                f"a picker dataset from it."
            )
        flag = ""
        if not (lo <= count <= hi):
            flag = "  <-- outside expected range, check vintage/schema"
            out_of_range.append(table)
        print(f"{table:<16}{count:>12,}   [{lo:,}, {hi:,}]{flag}")

    if out_of_range:
        print(
            f"WARNING: {', '.join(out_of_range)} outside the expected count "
            f"range. Not fatal, but worth a human look before trusting the "
            f"derived output."
        )
    print()


# ---------------------------------------------------------------------------
# Scratch indexes: the source ships with *zero* indexes (verified against the
# 2026-07 lite conversion), so a naive correlated-subquery join over the 1.6M
# row `pattern` table is a full scan per outer row. We copy just the rows we
# need (elementid=28, the "Model" element) into an in-memory scratch table
# and index it there instead of mutating the 333 MB source file on disk.
# ---------------------------------------------------------------------------
def build_scratch(conn: sqlite3.Connection) -> None:
    conn.execute("ATTACH DATABASE ':memory:' AS scratch")
    conn.execute(
        """
        CREATE TABLE scratch.pattern28 AS
        SELECT vinschemaid, CAST(attributeid AS INTEGER) AS modelid
        FROM pattern
        WHERE elementid = ?
        """,
        (MODEL_ELEMENT_ID,),
    )
    conn.execute("CREATE INDEX scratch.idx_pattern28_modelid ON pattern28(modelid)")
    conn.execute(
        "CREATE INDEX scratch.idx_pattern28_vinschemaid ON pattern28(vinschemaid)"
    )

    # model_vtype_year: every (modelid, vehicletypeid, yearfrom, yearto) a
    # VIN pattern implies -- deliberately ACROSS ALL vehicle types, not just
    # kept ones. A model whose only known types fall outside the kept set is
    # positively known to be something we exclude (a CBR600RR is a motorcycle
    # whether or not motorcycles are kept) and must be dropped outright; only
    # models with no pattern coverage anywhere stay as type-NULL unknowns.
    # Filtering here instead produced exactly that bug: excluded-type models
    # degraded to NULL and leaked into every build.
    conn.execute(
        """
        CREATE TABLE scratch.model_vtype_year AS
        SELECT DISTINCT
            p.modelid   AS modelid,
            w.vehicletypeid AS vehicletypeid,
            wv.yearfrom AS yearfrom,
            wv.yearto   AS yearto
        FROM pattern28 p
        JOIN wmi_vinschema wv ON wv.vinschemaid = p.vinschemaid
        JOIN wmi w ON w.id = wv.wmiid
        """
    )
    conn.execute("CREATE INDEX scratch.idx_mvy_modelid ON model_vtype_year(modelid)")


# ---------------------------------------------------------------------------
# Stage 3: kept makes (vehicle-type filter + curation)
# ---------------------------------------------------------------------------
def load_curation(path: Path) -> dict:
    if not path.exists():
        print(f"No curation file at {path}; keeping every make the type filter keeps.")
        return {
            "motorcycle_min_models": 0,
            "force_keep_makes": [],
            "force_drop_makes": [],
        }
    with path.open() as f:
        return json.load(f)


def determine_kept_makes(
    conn: sqlite3.Connection, curation: dict
) -> dict[int, str]:
    """Makes linked to a kept vehicle type, minus curation-driven removals.

    Motorcycle-only makes additionally have to clear `motorcycle_min_models`
    (or be force-kept by name): vPIC's motorcycle make list is ~1,700 entries,
    of which >1,000 are one-model custom shops. Mixed makes (Honda, BMW) are
    never subject to the threshold -- their motorcycles ride on the make's car
    presence. `force_drop_makes` removes a make by name from any build.
    """
    placeholders = ",".join("?" for _ in KEEP_VEHICLE_TYPES)
    rows = conn.execute(
        f"""
        SELECT
            m.id,
            m.name,
            SUM(CASE WHEN w.vehicletypeid != ? THEN 1 ELSE 0 END) AS nonmoto_wmis
        FROM make m
        JOIN wmi_make wm ON wm.makeid = m.id
        JOIN wmi w ON w.id = wm.wmiid
        WHERE w.vehicletypeid IN ({placeholders})
        GROUP BY m.id, m.name
        """,
        [MOTORCYCLE_TYPE_ID, *KEEP_VEHICLE_TYPES],
    ).fetchall()

    model_counts = {
        r[0]: r[1]
        for r in conn.execute("SELECT makeid, COUNT(*) FROM make_model GROUP BY makeid")
    }

    threshold = curation.get("motorcycle_min_models", 0)
    force_keep = {n.lower() for n in curation.get("force_keep_makes", [])}
    force_drop = {n.lower() for n in curation.get("force_drop_makes", [])}
    matched_keep: set[str] = set()
    matched_drop: set[str] = set()

    kept: dict[int, str] = {}
    n_dropped_threshold = 0
    n_forced_keep = 0
    for r in rows:
        name_lower = r["name"].lower()
        if name_lower in force_drop:
            matched_drop.add(name_lower)
            continue
        is_moto_only = r["nonmoto_wmis"] == 0
        if is_moto_only:
            if name_lower in force_keep:
                matched_keep.add(name_lower)
                n_forced_keep += 1
            elif model_counts.get(r["id"], 0) < threshold:
                n_dropped_threshold += 1
                continue
        kept[r["id"]] = r["name"]

    if MOTORCYCLE_TYPE_ID in KEEP_VEHICLE_TYPES:
        print(
            f"Motorcycle curation: {n_dropped_threshold} motorcycle-only makes "
            f"dropped below the {threshold}-model threshold, "
            f"{n_forced_keep} kept via force_keep_makes."
        )
    kept_names_lower = {n.lower() for n in kept.values()}
    for name in force_keep - matched_keep - kept_names_lower:
        # Not needed as a motorcycle rescue AND not in the output at all --
        # a typo, or the dataset changed. (A make that survives on its own,
        # like the mixed moto+off-road CAN-AM, is fine and stays silent.)
        print(
            f"WARNING: force_keep_makes entry {name!r} is not in the output "
            f"-- typo, or the dataset changed."
        )
    for name in force_drop - matched_drop:
        print(
            f"WARNING: force_drop_makes entry {name!r} matched no make the "
            f"type filter kept -- typo, or the dataset changed."
        )
    return kept


# ---------------------------------------------------------------------------
# Stage 4: derive picker tables
# ---------------------------------------------------------------------------
def merge_year_ranges(
    ranges: list[tuple[int, int | None]],
) -> list[tuple[int, int | None]]:
    """Merge overlapping/adjacent (year_from, year_to) ranges.

    year_to of None means open-ended (still in production / no end pinned).
    An open-ended range absorbs anything after it once merged, since "still
    ongoing" already covers any later start year.
    """
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: r[0])
    merged: list[list[int | None]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        cur_start, cur_end = merged[-1]
        if cur_end is None:
            # Current range is already open-ended; it swallows everything
            # that starts after it.
            continue
        if start <= cur_end + 1:
            # Overlapping or adjacent (e.g. ...1988] + [1989... merges).
            if end is None:
                merged[-1][1] = None
            else:
                merged[-1][1] = max(cur_end, end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def derive_picker_data(
    conn: sqlite3.Connection, kept_makes: dict[int, str]
) -> DerivedData:
    data = DerivedData(makes=dict(kept_makes))

    vt_rows = conn.execute(
        f"SELECT id, name FROM vehicletype WHERE id IN "
        f"({','.join(str(i) for i in KEEP_VEHICLE_TYPES)})"
    ).fetchall()
    data.vehicletypes = {r["id"]: r["name"] for r in vt_rows}

    kept_make_ids = set(kept_makes.keys())
    if not kept_make_ids:
        return data

    placeholders = ",".join("?" for _ in kept_make_ids)
    mm_rows = conn.execute(
        f"""
        SELECT mm.makeid, mm.modelid, mo.name AS model_name
        FROM make_model mm
        JOIN model mo ON mo.id = mm.modelid
        WHERE mm.makeid IN ({placeholders})
        """,
        list(kept_make_ids),
    ).fetchall()

    # modelid -> list of (vehicletypeid, yearfrom, yearto) from the scratch
    # join table, built once and reused across every make/model pair.
    type_year_by_model: dict[int, list[tuple[int, int, int | None]]] = {}
    for row in conn.execute(
        "SELECT modelid, vehicletypeid, yearfrom, yearto FROM scratch.model_vtype_year"
    ):
        type_year_by_model.setdefault(row["modelid"], []).append(
            (row["vehicletypeid"], row["yearfrom"], row["yearto"])
        )

    raw_years: dict[tuple[int, int], list[tuple[int, int | None]]] = {}

    for row in mm_rows:
        makeid, modelid, model_name = row["makeid"], row["modelid"], row["model_name"]
        entries = type_year_by_model.get(modelid)
        if not entries:
            # No pattern coverage for this model anywhere -- genuinely
            # unknown type. Keep the pair tagged NULL rather than guess.
            data.models[modelid] = model_name
            data.make_model_rows.add((makeid, modelid, None))
            continue
        kept_entries = [e for e in entries if e[0] in KEEP_VEHICLE_TYPES]
        if not kept_entries:
            # Positively typed, and every known type is excluded (e.g. a
            # motorcycle model under Honda in a cars-only build). Dropped
            # outright -- NULL is reserved for "unknown", not "excluded".
            continue
        data.models[modelid] = model_name
        seen_types = set()
        for vehicletypeid, yearfrom, yearto in kept_entries:
            seen_types.add(vehicletypeid)
            raw_years.setdefault((makeid, modelid), []).append((yearfrom, yearto))
        for vehicletypeid in seen_types:
            data.make_model_rows.add((makeid, modelid, vehicletypeid))

    for key, ranges in raw_years.items():
        data.model_years[key] = merge_year_ranges(ranges)

    return data


# ---------------------------------------------------------------------------
# Stage 4b: derive VIN decode tables (added 1.2.0, M2.4)
# ---------------------------------------------------------------------------
def derive_vin_data(conn: sqlite3.Connection, data: DerivedData) -> None:
    """Emit the WMI and pattern rows needed to decode a VIN to make + model.

    Mutates `data` in place. Scoped hard to what the picker already contains:
    a WMI whose make was curated out is dropped, and a pattern whose model was
    dropped (or is an excluded vehicle type) is dropped too. **The decoder can
    therefore never produce a make or model the picker cannot also offer** --
    without that rule a scanned VIN could fill the form with a value the user
    is then unable to re-select after clearing it, which reads as the app
    corrupting its own field.
    """
    kept_make_ids = set(data.makes)
    kept_model_ids = set(data.models)
    if not kept_make_ids:
        return

    # vPIC carries the WMI->make link twice: `wmi.makeid` and the `wmi_make`
    # join table. They mostly agree but neither is a superset of the other in
    # the 2026-07 conversion, so take the union and let the set dedupe it --
    # a missed WMI is a VIN that silently fails to decode.
    wmi_to_make: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT id, wmi, makeid FROM wmi WHERE makeid IS NOT NULL"
    ):
        if row["makeid"] in kept_make_ids:
            wmi_to_make.setdefault(row["id"], set()).add(row["makeid"])
    for row in conn.execute("SELECT wmiid, makeid FROM wmi_make"):
        if row["makeid"] in kept_make_ids:
            wmi_to_make.setdefault(row["wmiid"], set()).add(row["makeid"])

    wmi_text: dict[int, str] = {}
    for row in conn.execute("SELECT id, wmi FROM wmi"):
        wmi_text[row["id"]] = row["wmi"]

    skipped_length = 0
    for wmiid, makeids in wmi_to_make.items():
        text = wmi_text.get(wmiid)
        if text is None:
            continue
        if len(text) not in (WMI_SHORT_LENGTH, WMI_LONG_LENGTH):
            # Neither shape a decoder knows how to key on. Rare and always
            # malformed source data; counted rather than silently ignored.
            skipped_length += 1
            continue
        for makeid in makeids:
            data.wmi_rows.add((text, makeid))

    kept_wmi_ids = {
        wmiid
        for wmiid in wmi_to_make
        if len(wmi_text.get(wmiid, "")) in (WMI_SHORT_LENGTH, WMI_LONG_LENGTH)
    }
    if not kept_wmi_ids:
        return

    schema_ids: set[int] = set()
    for row in conn.execute(
        "SELECT wmiid, vinschemaid, yearfrom, yearto FROM wmi_vinschema"
    ):
        if row["wmiid"] not in kept_wmi_ids:
            continue
        text = wmi_text[row["wmiid"]]
        schema_ids.add(row["vinschemaid"])
        data.wmi_schema_rows.add(
            (text, row["vinschemaid"], row["yearfrom"], row["yearto"])
        )

    # Read from `pattern` directly rather than scratch.pattern28: the scratch
    # table dropped `keys`, which the picker never needed and the decoder is
    # entirely built on. Scoping to the surviving schemas keeps this cheap
    # without widening the scratch table for picker-only builds.
    dropped_model = 0
    if not schema_ids:
        return

    # A scratch table rather than `IN (?, ?, ...)`. The kept schema set is
    # ~10,100 ids on the 2026-07 vintage, and one host parameter apiece blows
    # straight past SQLITE_MAX_VARIABLE_NUMBER, which is **999** on any SQLite
    # older than 3.32. It happens to work on the machine this was written on
    # and fails with "too many SQL variables" on one with an older bundled
    # SQLite -- exactly the portability trap the "stdlib only, nothing to
    # install" promise in the README must not spring on someone.
    conn.execute("DROP TABLE IF EXISTS scratch.kept_schemas")
    conn.execute("CREATE TABLE scratch.kept_schemas (vinschemaid INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO scratch.kept_schemas (vinschemaid) VALUES (?)",
        ((sid,) for sid in schema_ids),
    )
    for row in conn.execute(
        """
        SELECT p.vinschemaid, p.keys, CAST(p.attributeid AS INTEGER) AS modelid
        FROM pattern p
        JOIN scratch.kept_schemas ks ON ks.vinschemaid = p.vinschemaid
        WHERE p.elementid = ?
        """,
        (MODEL_ELEMENT_ID,),
    ):
        if row["modelid"] not in kept_model_ids:
            dropped_model += 1
            continue
        data.vin_pattern_rows.add((row["vinschemaid"], row["keys"], row["modelid"]))

    print(
        f"VIN decode: {len(data.wmi_rows)} WMI->make rows, "
        f"{len(data.wmi_schema_rows)} WMI->schema rows, "
        f"{len(data.vin_pattern_rows)} model patterns"
        + (f" ({skipped_length} WMIs skipped on length)" if skipped_length else "")
        + (f", {dropped_model} patterns dropped to curated-out models" if dropped_model else "")
    )


# ---------------------------------------------------------------------------
# Stage 5: overlay hook
# ---------------------------------------------------------------------------
def load_overlay(path: Path) -> list[dict]:
    if not path.exists():
        print(f"No overlay file at {path}; skipping overlay merge.")
        return []
    with path.open() as f:
        payload = json.load(f)
    entries = payload.get("entries", [])
    if entries:
        print(
            f"Overlay: {len(entries)} curated entr{'y' if len(entries)==1 else 'ies'} in {path}"
        )
    return entries


def apply_overlay(data: DerivedData, entries: list[dict]) -> None:
    if not entries:
        return

    name_to_makeid = {name.lower(): mid for mid, name in data.makes.items()}
    # model names are unique within a make in vPIC (verified against the
    # 2026-07 conversion); key overlay model lookups the same way.
    model_name_to_id_by_make: dict[int, dict[str, int]] = {}
    for makeid, modelid, _ in data.make_model_rows:
        model_name_to_id_by_make.setdefault(makeid, {})[
            data.models[modelid].lower()
        ] = modelid

    vt_name_to_id = {name.lower(): vid for vid, name in data.vehicletypes.items()}

    next_id = OVERLAY_ID_START
    for entry in entries:
        make_name = entry["make"]
        model_name = entry["model"]
        year_from = entry["year_from"]
        year_to = entry.get("year_to")
        vt_name = entry["vehicletype"]

        vt_id = vt_name_to_id.get(vt_name.lower())
        if vt_id is None:
            raise SystemExit(
                f"FATAL: overlay entry {entry!r} names vehicletype "
                f"{vt_name!r}, which is not one of the kept types "
                f"{sorted(data.vehicletypes.values())}."
            )

        makeid = name_to_makeid.get(make_name.lower())
        if makeid is None:
            makeid = next_id
            next_id -= 1
            data.makes[makeid] = make_name
            name_to_makeid[make_name.lower()] = makeid
            model_name_to_id_by_make[makeid] = {}

        model_lookup = model_name_to_id_by_make.setdefault(makeid, {})
        modelid = model_lookup.get(model_name.lower())
        if modelid is None:
            modelid = next_id
            next_id -= 1
            data.models[modelid] = model_name
            model_lookup[model_name.lower()] = modelid

        data.make_model_rows.add((makeid, modelid, vt_id))
        key = (makeid, modelid)
        existing = data.model_years.get(key, [])
        data.model_years[key] = merge_year_ranges(existing + [(year_from, year_to)])


# ---------------------------------------------------------------------------
# Stage 6: build output db, stamp, diff
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE make (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE model (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE vehicletype (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
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
CREATE TABLE dataset_info (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX idx_make_name ON make(name);
CREATE INDEX idx_model_name ON model(name);
CREATE INDEX idx_make_model_makeid ON make_model(makeid);
CREATE INDEX idx_make_model_modelid ON make_model(modelid);
CREATE INDEX idx_model_years_makeid_modelid ON model_years(makeid, modelid);
"""

# Emitted only when VIN decode is on, so a --no-vin-decode build is byte-for-
# byte the picker-only dataset M2.3 shipped rather than that dataset plus five
# empty tables.
VIN_SCHEMA_SQL = """
CREATE TABLE wmi (
    wmi    TEXT NOT NULL,
    makeid INTEGER NOT NULL REFERENCES make(id)
);
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

CREATE INDEX idx_wmi_wmi ON wmi(wmi);
CREATE INDEX idx_wmi_vinschema_wmi ON wmi_vinschema(wmi);
CREATE INDEX idx_vin_pattern_vinschemaid ON vin_pattern(vinschemaid);
"""


def write_output_db(data: DerivedData, path: Path) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    if data.wmi_rows:
        conn.executescript(VIN_SCHEMA_SQL)

    conn.executemany("INSERT INTO make (id, name) VALUES (?, ?)", data.makes.items())
    conn.executemany("INSERT INTO model (id, name) VALUES (?, ?)", data.models.items())
    conn.executemany(
        "INSERT INTO vehicletype (id, name) VALUES (?, ?)", data.vehicletypes.items()
    )
    conn.executemany(
        "INSERT INTO make_model (makeid, modelid, vehicletypeid) VALUES (?, ?, ?)",
        data.make_model_rows,
    )
    year_rows = [
        (makeid, modelid, yf, yt)
        for (makeid, modelid), ranges in data.model_years.items()
        for yf, yt in ranges
    ]
    conn.executemany(
        "INSERT INTO model_years (makeid, modelid, year_from, year_to) VALUES (?, ?, ?, ?)",
        year_rows,
    )
    if data.wmi_rows:
        conn.executemany(
            "INSERT INTO wmi (wmi, makeid) VALUES (?, ?)", data.wmi_rows
        )
        conn.executemany(
            "INSERT INTO wmi_vinschema (wmi, vinschemaid, year_from, year_to) "
            "VALUES (?, ?, ?, ?)",
            data.wmi_schema_rows,
        )
        conn.executemany(
            "INSERT INTO vin_pattern (vinschemaid, keys, modelid) VALUES (?, ?, ?)",
            data.vin_pattern_rows,
        )
    conn.commit()
    conn.close()


def stamp_dataset_info(
    path: Path, source_path: Path, curation: dict, has_vin_decode: bool
) -> None:
    conn = sqlite3.connect(path)
    info = {
        "source_file": source_path.name,
        "source_vintage": SOURCE_VINTAGE,
        "build_timestamp": datetime.now(timezone.utc).isoformat(),
        "script_version": SCRIPT_VERSION,
        # So the app can decide whether VIN auto-fill is available by reading
        # one row, rather than probing sqlite_master for table existence.
        "has_vin_decode": "1" if has_vin_decode else "0",
        "kept_vehicle_types": json.dumps(sorted(KEEP_VEHICLE_TYPES)),
        "motorcycle_min_models": str(curation.get("motorcycle_min_models", 0)),
        "force_keep_makes": json.dumps(curation.get("force_keep_makes", [])),
        "force_drop_makes": json.dumps(curation.get("force_drop_makes", [])),
    }
    conn.executemany(
        "INSERT OR REPLACE INTO dataset_info (key, value) VALUES (?, ?)",
        info.items(),
    )
    conn.commit()
    conn.close()


def diff_against_previous(
    new_path: Path, previous_path: Path, report_path: Path
) -> None:
    lines: list[str] = []
    lines.append(
        f"vPIC pare-down diff report -- {datetime.now(timezone.utc).isoformat()}"
    )
    lines.append(f"previous: {previous_path}")
    lines.append(f"new:      {new_path}")
    lines.append("")

    if not previous_path.exists():
        lines.append("No previous output at this path -- first build, nothing to diff.")
        text = "\n".join(lines)
        print(text)
        report_path.write_text(text + "\n")
        return

    old_conn = sqlite3.connect(f"file:{previous_path}?mode=ro", uri=True)
    new_conn = sqlite3.connect(f"file:{new_path}?mode=ro", uri=True)

    def names_by_id(conn: sqlite3.Connection, table: str) -> dict[int, str]:
        return {r[0]: r[1] for r in conn.execute(f"SELECT id, name FROM {table}")}

    for table in ("make", "model"):
        old = names_by_id(old_conn, table)
        new = names_by_id(new_conn, table)
        added = sorted(new.keys() - old.keys())
        removed = sorted(old.keys() - new.keys())
        lines.append(
            f"{table}: {len(old)} -> {len(new)} ({len(added)} added, {len(removed)} removed)"
        )
        if added:
            sample = ", ".join(f"{new[i]}({i})" for i in added[:20])
            more = "" if len(added) <= 20 else f", ... +{len(added) - 20} more"
            lines.append(f"  added: {sample}{more}")
        if removed:
            sample = ", ".join(f"{old[i]}({i})" for i in removed[:20])
            more = "" if len(removed) <= 20 else f", ... +{len(removed) - 20} more"
            lines.append(f"  removed: {sample}{more}")

    old_mm = old_conn.execute("SELECT COUNT(*) FROM make_model").fetchone()[0]
    new_mm = new_conn.execute("SELECT COUNT(*) FROM make_model").fetchone()[0]
    lines.append(f"make_model rows: {old_mm} -> {new_mm}")

    old_my = old_conn.execute("SELECT COUNT(*) FROM model_years").fetchone()[0]
    new_my = new_conn.execute("SELECT COUNT(*) FROM model_years").fetchone()[0]
    lines.append(f"model_years rows: {old_my} -> {new_my}")

    old_conn.close()
    new_conn.close()

    text = "\n".join(lines)
    print(text)
    report_path.write_text(text + "\n")


# ---------------------------------------------------------------------------
# Stage 7: verification report
# ---------------------------------------------------------------------------
def run_verification(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print("\n=== Verification report ===")

    n_makes = conn.execute("SELECT COUNT(*) FROM make").fetchone()[0]
    n_models = conn.execute("SELECT COUNT(*) FROM model").fetchone()[0]
    print(f"kept makes:  {n_makes:,}")
    print(f"kept models: {n_models:,}")

    porsche_911 = conn.execute("""
        SELECT my.year_from, my.year_to
        FROM model_years my
        JOIN model mo ON mo.id = my.modelid
        JOIN make mk ON mk.id = my.makeid
        WHERE mk.name = 'Porsche' AND mo.name = '911'
        ORDER BY my.year_from
        """).fetchall()
    if porsche_911:
        first = porsche_911[0]
        ok = first["year_from"] == 1981
        print(
            f"Porsche 911 year ranges: {[(r['year_from'], r['year_to']) for r in porsche_911]}"
            f"  {'OK (starts 1981)' if ok else 'UNEXPECTED -- does not start 1981'}"
        )
    else:
        print("Porsche 911: NOT FOUND -- unexpected")

    bmw_motorcycle = conn.execute("""
        SELECT DISTINCT mo.name
        FROM make_model mm
        JOIN model mo ON mo.id = mm.modelid
        JOIN make mk ON mk.id = mm.makeid
        JOIN vehicletype vt ON vt.id = mm.vehicletypeid
        WHERE mk.name = 'BMW' AND vt.name = 'Motorcycle'
        ORDER BY mo.name
        LIMIT 8
        """).fetchall()
    bmw_car = conn.execute("""
        SELECT COUNT(DISTINCT mm.modelid) c
        FROM make_model mm
        JOIN make mk ON mk.id = mm.makeid
        JOIN vehicletype vt ON vt.id = mm.vehicletypeid
        WHERE mk.name = 'BMW' AND vt.name = 'Passenger Car'
        """).fetchone()["c"]
    print(
        f"BMW: {bmw_car} car-typed model(s), "
        f"{len(bmw_motorcycle)}+ motorcycle-typed model(s) "
        f"(sample: {[r['name'] for r in bmw_motorcycle]})"
    )

    ferrari_count = conn.execute("""
        SELECT COUNT(DISTINCT mm.modelid) c
        FROM make_model mm
        JOIN make mk ON mk.id = mm.makeid
        WHERE mk.name = 'Ferrari'
        """).fetchone()["c"]
    print(f"Ferrari model count: {ferrari_count} (expected 66)")

    skyline = conn.execute("""
        SELECT COUNT(*) c
        FROM make_model mm
        JOIN make mk ON mk.id = mm.makeid
        JOIN model mo ON mo.id = mm.modelid
        WHERE mk.name = 'Nissan' AND mo.name LIKE '%Skyline%'
        """).fetchone()["c"]
    print(
        f"Nissan Skyline rows: {skyline} "
        f"({'OK, absent as expected (grey-import gap)' if skyline == 0 else 'UNEXPECTED -- present'})"
    )

    null_typed = conn.execute(
        "SELECT COUNT(*) FROM make_model WHERE vehicletypeid IS NULL"
    ).fetchone()[0]
    print(f"type-NULL make_model rows (no pattern coverage anywhere): {null_typed:,}")

    kept_type_names = {
        r[0] for r in conn.execute("SELECT name FROM vehicletype").fetchall()
    }
    if "Motorcycle" in kept_type_names:
        # Motorcycle curation spot checks: majors present, custom shops gone.
        for brand in ("Harley-Davidson", "Ducati", "APRILIA", "Royal Enfield", "CAN-AM"):
            present = conn.execute(
                "SELECT COUNT(*) FROM make WHERE name = ?", (brand,)
            ).fetchone()[0]
            print(f"motorcycle major {brand!r}: {'present' if present else 'MISSING -- unexpected'}")
        for shop in ("Custom Choppers by Otis", "Crazy Dago Customs"):
            present = conn.execute(
                "SELECT COUNT(*) FROM make WHERE name = ?", (shop,)
            ).fetchone()[0]
            print(f"custom shop {shop!r}: {'absent, OK' if not present else 'PRESENT -- curation failed'}")
        honda_moto = conn.execute("""
            SELECT COUNT(*) c FROM make_model mm
            JOIN make mk ON mk.id = mm.makeid
            JOIN vehicletype vt ON vt.id = mm.vehicletypeid
            WHERE mk.name = 'Honda' AND vt.name = 'Motorcycle'
            """).fetchone()["c"]
        print(f"Honda motorcycle-typed models: {honda_moto} (expected ~300)")
    else:
        # Leak canary: in a build that excludes motorcycles, positively-typed
        # bikes must be gone entirely, not degraded to type-NULL rows.
        leaked = conn.execute("""
            SELECT COUNT(*) c FROM make_model mm
            JOIN model mo ON mo.id = mm.modelid
            WHERE mo.name IN ('CBR600RR', 'S 1000 RR', 'Gold Wing')
            """).fetchone()["c"]
        print(
            f"motorcycle leak canary (CBR600RR / S 1000 RR / Gold Wing): {leaked} rows "
            f"({'OK, fully removed' if leaked == 0 else 'LEAK -- excluded-type models present'})"
        )

    has_vin = conn.execute(
        "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name='vin_pattern'"
    ).fetchone()["c"]
    if has_vin:
        print()
        print("--- VIN decode ---")
        for table in ("wmi", "wmi_vinschema", "vin_pattern"):
            n = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            print(f"{table:<16}{n:>10,} rows")
        for vin, expected in VIN_DECODE_CANARIES:
            make, model = decode_vin(conn, vin)
            got = f"{make or '-'} / {model or '-'}"
            ok = (make, model) == expected
            print(
                f"  {vin}  ->  {got:<24} "
                f"({'OK' if ok else f'MISMATCH, expected {expected[0]} / {expected[1]}'})"
            )

    conn.close()
    size = path.stat().st_size
    budget = 5_000_000 if has_vin else 2_000_000
    print(
        f"output db size: {size:,} bytes ({size / 1_048_576:.2f} MB) "
        f"({'OK, under' if size < budget else 'LARGER than'} the "
        f"{budget / 1_000_000:.0f} MB budget for this build)"
    )
    print("=== end verification report ===\n")


# ---------------------------------------------------------------------------
# Reference VIN decoder (added 1.2.0, M2.4)
#
# This is not shipped -- the app has a Swift port. It exists so the *pipeline*
# can prove the tables it just emitted actually decode real VINs, and so the
# matching rules have one written-down authority the Swift side can be diffed
# against when it disagrees with reality. Keep the two in step.
# ---------------------------------------------------------------------------
# VIN position 10's model-year code. 30 codes on a 30-year cycle: the letters
# skip I, O, Q, U and Z (too easily confused with 1, 0 and 2), and 0 is never
# used. Position 7 breaks the ambiguity -- a digit there means the earlier
# turn of the cycle, a letter means the later one. This is why a 1997 and a
# 2027 car do not collide.
YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"

# Known-good VINs spanning the cases that actually break: a mainstream US
# domestic (3-char WMI), a low-volume European (6-char WMI), a JDM import, and
# a German make whose model names are trim-granular.
VIN_DECODE_CANARIES = [
    ("1HGCM82633A004352", ("Honda", "Accord")),
    ("WP0AA2A96KS106147", ("Porsche", "911")),
    ("JN1AZ4EH6BM551234", ("Nissan", "370Z")),
    ("WBA3A5C55CF256789", ("BMW", "328i")),
]


def model_year_from_vin(vin: str) -> int | None:
    """Model year from VIN positions 10 and 7. No table, pure arithmetic."""
    if len(vin) < 10:
        return None
    code = vin[9].upper()
    index = YEAR_CODES.find(code)
    if index < 0:
        return None
    # Position 7 alphabetic => 2010-2039, numeric => 1980-2009.
    base = 2010 if vin[6].isalpha() else 1980
    return base + index


def decode_vin(conn: sqlite3.Connection, vin: str) -> tuple[str | None, str | None]:
    """Decode a VIN to (make, model) against a built output database."""
    vin = vin.strip().upper()
    if len(vin) < 11:
        return (None, None)
    year = model_year_from_vin(vin)

    # 6-char WMIs first: low-volume manufacturers share their first three
    # characters with someone else, so a 3-char hit is only trustworthy once
    # the 6-char lookup has missed.
    candidates = [vin[0:3] + vin[11:14], vin[0:3]]
    wmi = None
    make_ids: list[int] = []
    for candidate in candidates:
        rows = conn.execute(
            "SELECT DISTINCT makeid FROM wmi WHERE wmi = ?", (candidate,)
        ).fetchall()
        if rows:
            wmi = candidate
            make_ids = [r["makeid"] for r in rows]
            break
    if wmi is None:
        return (None, None)

    # vPIC's `keys` wildcard '*' means exactly one character, which is GLOB's
    # '?', not GLOB's '*'. Appending a real GLOB '*' then makes the whole thing
    # a prefix match, which is what handles keys of differing widths --
    # including character classes like 'AZ4[EF]', where the string is seven
    # characters long but matches only four VIN positions.
    #
    # The make comes back from this join too, not from the WMI lookup above.
    # A WMI is not unique to a make -- JN1 covers both Nissan and Infiniti,
    # and picking the first row returned decodes a 370Z as an Infiniti. The
    # matched *model* is what disambiguates, so make and model are resolved
    # together or not at all.
    make_placeholders = ",".join("?" for _ in make_ids)
    rows = conn.execute(
        f"""
        SELECT mk.name AS make, mo.name AS model, LENGTH(p.keys) AS width
        FROM wmi_vinschema ws
        JOIN vin_pattern p ON p.vinschemaid = ws.vinschemaid
        JOIN model mo ON mo.id = p.modelid
        JOIN make_model mm ON mm.modelid = p.modelid
        JOIN make mk ON mk.id = mm.makeid
        WHERE ws.wmi = ?
          AND mm.makeid IN ({make_placeholders})
          AND (? IS NULL OR ? BETWEEN ws.year_from AND COALESCE(ws.year_to, 9999))
          AND SUBSTR(?, 4) GLOB REPLACE(p.keys, '*', '?') || '*'
        ORDER BY width DESC
        """,
        (wmi, *make_ids, year, year, vin),
    ).fetchall()
    if rows:
        return (rows[0]["make"], rows[0]["model"])

    # No model matched. The make is still worth returning when the WMI is
    # unambiguous -- a half-filled form beats an empty one -- but a guess
    # between several makes is worse than nothing.
    if len(make_ids) == 1:
        row = conn.execute(
            "SELECT name FROM make WHERE id = ?", (make_ids[0],)
        ).fetchone()
        if row is not None:
            return (row["name"], None)
    return (None, None)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overlay", type=Path, default=DEFAULT_OVERLAY)
    parser.add_argument("--curation", type=Path, default=DEFAULT_CURATION)
    parser.add_argument(
        "--keep-types",
        type=str,
        default=None,
        help="Comma-separated vehicletype ids to keep, overriding the default "
        f"({','.join(str(i) for i in sorted(KEEP_VEHICLE_TYPES))}). "
        "For experimenting with variant builds without editing the script.",
    )
    parser.add_argument(
        "--no-vin-decode",
        action="store_true",
        help="Omit the VIN decode tables (wmi, wmi_vinschema, vin_pattern), "
        "producing the picker-only dataset M2.3 shipped. Costs about 2.5 MB "
        "of the built database; the app's VIN auto-fill stops working past "
        "model year, which it computes without any table.",
    )
    return parser.parse_args()


def main() -> None:
    global KEEP_VEHICLE_TYPES
    args = parse_args()
    if args.keep_types is not None:
        KEEP_VEHICLE_TYPES = {int(t) for t in args.keep_types.split(",") if t.strip()}
        print(f"Keep-types override: {sorted(KEEP_VEHICLE_TYPES)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    conn = connect_source(args.source)
    validate_source(conn, args.source)
    build_scratch(conn)

    curation = load_curation(args.curation)
    kept_makes = determine_kept_makes(conn, curation)
    print(
        f"Kept makes (type filter + curation): {len(kept_makes)}"
    )

    data = derive_picker_data(conn, kept_makes)
    if args.no_vin_decode:
        print("VIN decode: skipped (--no-vin-decode).")
    else:
        derive_vin_data(conn, data)
    conn.close()

    overlay_entries = load_overlay(args.overlay)
    apply_overlay(data, overlay_entries)

    tmp_path = args.output.with_suffix(args.output.suffix + ".tmp")
    write_output_db(data, tmp_path)
    stamp_dataset_info(tmp_path, args.source, curation, bool(data.wmi_rows))

    report_path = args.output.parent / "diff_report.txt"
    diff_against_previous(tmp_path, args.output, report_path)

    tmp_path.replace(args.output)
    print(f"\nWrote {args.output}")

    run_verification(args.output)


if __name__ == "__main__":
    main()
