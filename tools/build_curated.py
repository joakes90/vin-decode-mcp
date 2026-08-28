#!/usr/bin/env python3
"""Build curated provenance_vpic.db from full vPIC SQLite DB."""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

FULL = Path("tools/out/vPIC_sqlite.db")
OUT = Path("tools/out/provenance_vpic.db")

print("Building curated vPIC database (fixed modelid)...\n")

src = sqlite3.connect(f"file:{FULL}?mode=ro", uri=True)
src.row_factory = sqlite3.Row

if OUT.exists():
    OUT.unlink()
dst = sqlite3.connect(str(OUT))
dst.execute("PRAGMA journal_mode=WAL")

schema = open("src/vin_decode_mcp/database.py").read()
schema_code = schema.split('SCHEMA_SQL = """')[1].split('"""')[0]
dst.executescript(schema_code)
dst.commit()

# ===================================================================
# STEP 1: make
# ===================================================================
src_makes = {}
for r in src.execute(
    "SELECT id, name FROM make WHERE name != '' AND name NOT LIKE '\\N'"
):
    src_makes[r[0]] = r[1]

makes_with_data = set()
for r in src.execute("SELECT DISTINCT makeid FROM make_model"):
    makes_with_data.add(r["makeid"])
for r in src.execute("SELECT DISTINCT makeid FROM wmi"):
    makes_with_data.add(r["makeid"])

curated_make = {}
for i, (src_id, name) in enumerate(sorted(src_makes.items())):
    if src_id not in makes_with_data:
        continue
    curated_make[src_id] = i + 1
    dst.execute("INSERT INTO make VALUES (?, ?)", (i + 1, name))
print(f"  ✅  {len(curated_make)} makes")

# ===================================================================
# STEP 2: vehicletype
# ===================================================================
VT = {
    1: "Motorcycle",
    2: "Passenger Car",
    3: "Truck",
    7: "Multipurpose Passenger Vehicle (MPV)",
    13: "Off-Road Vehicle",
}
for i, (vt_id, vt_name) in enumerate(sorted(VT.items())):
    dst.execute("INSERT INTO vehicletype VALUES (?, ?)", (i + 1, vt_name))
print(f"  ✅  {len(VT)} vehicle types")

# ===================================================================
# STEP 3: model — src_mid -> dst_id (1-indexed)
# ===================================================================
kept_ids = list(curated_make.keys())
qmarks = ",".join("?" for _ in kept_ids)

src_models = {}  # src_modelid -> dst_modelid
for i, r in enumerate(
    src.execute(
        f"""
    SELECT DISTINCT mm.modelid, mo.name
    FROM make_model mm
    JOIN model mo ON mo.id = mm.modelid
    WHERE mm.makeid IN ({qmarks})
    ORDER BY mo.name
""",
        kept_ids,
    ).fetchall()
):
    src_models[r[0]] = i + 1
    dst.execute("INSERT INTO model VALUES (?, ?)", (i + 1, r[1]))
print(f"  ✅  {len(src_models)} models")

# ===================================================================
# STEP 4: make_model — use dst IDs not names
# ===================================================================
vtype_count = 0
for r in src.execute(
    f"""
    SELECT ms.makeid, vm.modelid, ms.vehicletypeid
    FROM vehiclespecschema ms
    JOIN vehiclespecschema_model vm ON vm.vehiclespecschemaid = ms.id
    WHERE ms.makeid IN ({qmarks})
""",
    kept_ids,
).fetchall():
    dst_make = curated_make.get(r[0])
    dst_model = src_models.get(r[1])
    if dst_make is None or dst_model is None:
        continue
    vtype_src = r[2]
    if vtype_src not in VT:
        continue
    vtype_dst = list(VT.keys()).index(vtype_src) + 1

    vtype_count += 1
    dst.execute(
        "INSERT OR IGNORE INTO make_model VALUES (?, ?, ?)",
        (dst_make, dst_model, vtype_dst),
    )
print(f"  ✅  {vtype_count} make/model/type pairs")

# ===================================================================
# STEP 5: model_years
# ===================================================================
dst.execute(
    "CREATE TEMPORARY TABLE my_temp (makeid INTEGER, modelid INTEGER, year INTEGER)"
)

for r in src.execute(
    f"""
    SELECT ms.makeid, vm.modelid, mys.year
    FROM vehiclespecschema ms
    JOIN vehiclespecschema_model vm ON vm.vehiclespecschemaid = ms.id
    JOIN vehiclespecschema_year mys ON mys.vehiclespecschemaid = ms.id
    WHERE ms.makeid IN ({qmarks})
""",
    kept_ids,
).fetchall():
    dst_make = curated_make.get(r[0])
    dst_model = src_models.get(r[1])
    if dst_make is None or dst_model is None:
        continue
    dst.execute("INSERT INTO my_temp VALUES (?, ?, ?)", (dst_make, dst_model, r[2]))

dst.commit()
for r in dst.execute(
    """
    SELECT makeid, modelid, MIN(year) as year_from, MAX(year) as year_to
    FROM my_temp GROUP BY makeid, modelid
"""
).fetchall():
    dst.execute("INSERT INTO model_years VALUES (?, ?, ?, ?)", (r[0], r[1], r[2], r[3]))
dst.execute("DROP TABLE my_temp")
print(
    f"  ✅  {dst.execute('SELECT COUNT(*) FROM model_years').fetchone()[0]} model year ranges"
)

# ===================================================================
# STEP 6: wmi
# ===================================================================
wmi_count = 0
for r in src.execute(
    f"""
    SELECT w.wmi, w.makeid
    FROM wmi w
    WHERE w.makeid IN ({qmarks})
    AND w.wmi IS NOT NULL AND w.wmi != '' AND w.wmi NOT LIKE '\\N'
""",
    kept_ids,
).fetchall():
    dst_make = curated_make.get(r[1])
    if dst_make is None:
        continue
    wmi_count += 1
    dst.execute("INSERT OR IGNORE INTO wmi VALUES (?, ?)", (r[0], dst_make))
print(f"  ✅  {wmi_count} WMIs")

# ===================================================================
# STEP 7: wmi_vinschema + vin_pattern
# ===================================================================
src_wmiid_to_str = {}
for r in src.execute(
    "SELECT id, wmi FROM wmi WHERE wmi != '' AND wmi NOT LIKE '\\N'"
).fetchall():
    src_wmiid_to_str[r[0]] = r[1]

wmiid_makeid = {}
for r in src.execute(
    "SELECT id, makeid FROM wmi WHERE wmi != '' AND wmi NOT LIKE '\\N'"
).fetchall():
    wmiid_makeid[r[0]] = r[1]

ws_count = 0
for r in src.execute(
    "SELECT wmiid, vinschemaid, yearfrom, yearto FROM wmi_vinschema WHERE yearfrom IS NOT NULL AND yearto IS NOT NULL"
).fetchall():
    make_id = wmiid_makeid.get(r[0])
    if make_id not in curated_make:
        continue
    wmi_str = src_wmiid_to_str.get(r[0], "")
    if not wmi_str:
        continue
    ws_count += 1
    dst.execute("INSERT INTO wmi_vinschema VALUES (?, ?, ?, ?)", (wmi_str, r[1], r[2], r[3]))
print(f"  ✅  {ws_count} wmi_vinschema")

vin_pattern_count = 0
for r in src.execute(
    """
    SELECT p.vinschemaid, p.keys, p.attributeid
    FROM pattern p
    WHERE p.elementid = 28
    AND p.keys IS NOT NULL AND p.keys != '' AND p.keys NOT LIKE '\\N'
    AND p.vinschemaid IN (SELECT DISTINCT vinschemaid FROM wmi_vinschema)
"""
).fetchall():
    try:
        model_id = int(r[2])
        # model_id here is the vPIC src model id, map to dst
        dst_model = src_models.get(model_id)
        if dst_model:
            vin_pattern_count += 1
            dst.execute("INSERT OR IGNORE INTO vin_pattern VALUES (?, ?, ?)", (r[0], r[1], dst_model))
    except (ValueError, TypeError):
        pass
print(f"  ✅  {vin_pattern_count} vin_pattern")

# ===================================================================
# STEP 8: dataset_info
# ===================================================================
dst.execute(
    "INSERT INTO dataset_info VALUES (?, ?)",
    ("build_timestamp", datetime.now(timezone.utc).isoformat()),
)
dst.execute(
    "INSERT INTO dataset_info VALUES (?, ?)",
    ("source", "NHTSA vPIC full SQLite dump"),
)
dst.execute(
    "INSERT INTO dataset_info VALUES (?, ?)",
    ("source_version", "2026-08"),
)
dst.commit()

src.close()
dst.close()

size = OUT.stat().st_size / 1024 / 1024
print(f"\n  ✅  {OUT} ({size:.1f} MB)")

# Verify Honda
db_path = str(OUT)
src2 = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
print("\n  Verification:")
honda = src2.execute('SELECT id, name FROM make WHERE name="Honda"').fetchone()
print(f"    make(Honda): {honda}")
honda_mm = src2.execute(
    'SELECT COUNT(*) FROM make_model JOIN make m ON m.id=makeid WHERE m.name="Honda"'
).fetchone()[0]
print(f"    make_model(Honda): {honda_mm}")
civic = src2.execute('SELECT id, name FROM model WHERE name="Civic"').fetchone()
print(f"    model(Civic): {civic}")
if civic:
    civic_yrs = src2.execute(
        'SELECT year_from, year_to FROM model_years JOIN make m ON m.id=makeid JOIN model mo ON mo.id=modelid WHERE m.name="Honda" AND mo.name="Civic" ORDER BY year_from'
    ).fetchall()
    print(f"    model_years(Honda/Civic): {civic_yrs}")

print("\n✅ Build complete!")
