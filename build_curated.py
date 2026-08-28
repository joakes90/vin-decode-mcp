#!/usr/bin/env python3
"""Build the curated provenance_vpic.db from the raw vPIC_sqlite.db."""

import sqlite3
from pathlib import Path

SRC = Path("/Users/justin/Devel/Python/VIN_MCP/tools/out/vPIC_sqlite.db")
DST = Path("/Users/justin/Devel/Python/VIN_MCP/tools/out/provenance_vpic.db")

# Read source
src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
src.row_factory = sqlite3.Row

# Create destination with MCP server schema
dst = sqlite3.connect(str(DST))
dst.executescript("""
CREATE TABLE make (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE model (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE vehicletype (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE make_model (makeid INTEGER NOT NULL REFERENCES make(id), modelid INTEGER NOT NULL REFERENCES model(id), vehicletypeid INTEGER REFERENCES vehicletype(id));
CREATE TABLE model_years (makeid INTEGER NOT NULL REFERENCES make(id), modelid INTEGER NOT NULL REFERENCES model(id), year_from INTEGER NOT NULL, year_to INTEGER);
CREATE TABLE wmi (wmi TEXT NOT NULL, makeid INTEGER NOT NULL REFERENCES make(id));
CREATE TABLE wmi_vinschema (wmi TEXT NOT NULL, vinschemaid INTEGER NOT NULL, year_from INTEGER NOT NULL, year_to INTEGER);
CREATE TABLE vin_pattern (vinschemaid INTEGER NOT NULL, keys TEXT NOT NULL, modelid INTEGER NOT NULL REFERENCES model(id));
CREATE TABLE dataset_info (key TEXT PRIMARY KEY, value TEXT);
""")

# --- Make ---
print("Building make table...")
makes = {}
for row in src.execute("SELECT id, codename AS name FROM make WHERE codename IS NOT NULL AND codename != ''").fetchall():
    # Filter out known junk
    name = row["name"].strip()
    if name and len(name) > 1 and name not in ("", " "):
        # Skip some obviously wrong entries
        if "Company" not in name and "Manufacturing" not in name or name in ("Toyota", "Honda", "Ford", "GM", "Nissan", "Hyundai", "Kia", "BMW", "Mercedes-Benz", "Volkswagen", "Audi", "Porsche", "Subaru", "Mazda", "Mitsubishi", "Suzuki", "Daihatsu", "Lexus", "Infiniti", "Acura", "Buick", "Cadillac", "Chevrolet", "Dodge", "Chrysler", "Jeep", "Ram", "Lincoln", "GMC", "Tesla", "Volvo", "Land Rover", "Jaguar", "Ferrari", "Lamborghini", "Maserati", "Bentley", "Rolls-Royce", "Aston Martin", "McLaren", "Lotus", "Alfa Romeo", "Fiat", "Peugeot", "Renault", "Citroen", "Opel", "Skoda", "Seat", "Mini", "Morgan", "Ariel", "TVR", "Koenigsegg", "Pagani", "Bugatti", "Ducati", "Honda", "Yamaha", "Kawasaki", "Suzuki", "BMW Motorrad"):
            madeid = len(makes) + 1
            makes[name] = madeid
            dst.execute("INSERT INTO make VALUES (?, ?)", (madeid, name))

# Actually let me just take all unique codenames from make table
print("Retrying make table with better logic...")
dst.executescript("""
DROP TABLE IF EXISTS make;
CREATE TABLE make (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
DROP TABLE IF EXISTS model;
CREATE TABLE model (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
DROP TABLE IF EXISTS vehicletype;
CREATE TABLE vehicletype (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
DROP TABLE IF EXISTS make_model;
CREATE TABLE make_model (makeid INTEGER NOT NULL REFERENCES make(id), modelid INTEGER NOT NULL REFERENCES model(id), vehicletypeid INTEGER REFERENCES vehicletype(id));
DROP TABLE IF EXISTS model_years;
CREATE TABLE model_years (makeid INTEGER NOT NULL REFERENCES make(id), modelid INTEGER NOT NULL REFERENCES model(id), year_from INTEGER NOT NULL, year_to INTEGER);
DROP TABLE IF EXISTS wmi;
CREATE TABLE wmi (wmi TEXT NOT NULL, makeid INTEGER NOT NULL REFERENCES make(id));
DROP TABLE IF EXISTS wmi_vinschema;
CREATE TABLE wmi_vinschema (wmi TEXT NOT NULL, vinschemaid INTEGER NOT NULL, year_from INTEGER NOT NULL, year_to INTEGER);
DROP TABLE IF EXISTS vin_pattern;
CREATE TABLE vin_pattern (vinschemaid INTEGER NOT NULL, keys TEXT NOT NULL, modelid INTEGER NOT NULL REFERENCES model(id));
DROP TABLE IF EXISTS dataset_info;
CREATE TABLE dataset_info (key TEXT PRIMARY KEY, value TEXT);
""")

# Simpler approach: just use make names directly
print("Building make table...")
make_map = {}  # name -> id
makeid = 1
for row in src.execute("SELECT DISTINCT codename FROM make WHERE codename IS NOT NULL AND codename != '' ORDER BY codename").fetchall():
    name = row[0].strip()
    if name:
        make_map[name] = makeid
        dst.execute("INSERT INTO make VALUES (?, ?)", (makeid, name))
        makeid += 1

print(f"  Made {len(make_map)} makes")

# --- Vehicletype ---
print("Building vehicletype table...")
vt_map = {}
vtid = 1
# Use the vehicletype table from source
for row in src.execute("SELECT DISTINCT element FROM element WHERE element IS NOT NULL AND element != '' ORDER BY element LIMIT 10").fetchall():
    name = row[0].strip()
    if name:
        vt_map[name] = vtid
        dst.execute("INSERT INTO vehicletype VALUES (?, ?)", (vtid, name))
        vtid += 1

# If vehicletype table exists, use it
vt_src = src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vehicletype'").fetchone()
if vt_src:
    dst.execute("DELETE FROM vehicletype")
    vt_map = {}
    vtid = 1
    for row in src.execute("SELECT DISTINCT elementname FROM vehicletype WHERE elementname IS NOT NULL AND elementname != '' ORDER BY elementname").fetchall():
        name = row[0].strip()
        if name:
            vt_map[name] = vtid
            dst.execute("INSERT INTO vehicletype VALUES (?, ?)", (vtid, name))
            vtid += 1
    print(f"  Made {len(vt_map)} vehicle types")

# --- Model ---
print("Building model table...")
model_map = {}  # (make_name, model_name) -> (modelid, makeid)
modelid = 1
# Use make_model table to get make+model combinations
for row in src.execute("""
    SELECT DISTINCT m.codename AS make_name, modl.codename AS model_name
    FROM make_model mm
    JOIN make m ON mm.makeid = m.id
    JOIN model modl ON mm.modelid = modl.id
    WHERE m.codename IS NOT NULL AND modl.codename IS NOT NULL
    ORDER BY m.codename, modl.codename
""").fetchall():
    make_name = row[0].strip()
    model_name = row[1].strip()
    if make_name in make_map and model_name:
        key = (make_name, model_name)
        if key not in model_map:
            model_map[key] = (modelid, make_map[make_name])
            dst.execute("INSERT INTO model VALUES (?, ?)", (modelid, model_name))
            modelid += 1

print(f"  Made {len(model_map)} models")

# --- Make_Model ---
print("Building make_model table...")
mm_count = 0
for (make_name, model_name), (modelid, makeid) in model_map.items():
    dst.execute("INSERT INTO make_model VALUES (?, ?, ?)", (makeid, modelid, None))
    mm_count += 1
print(f"  Made {mm_count} make_model entries")

# --- Model_Years ---
print("Building model_years table...")
my_count = 0
for (make_name, model_name), (modelid, makeid) in model_map.items():
    # Try to find year info from vehiclespecschema_year
    years = src.execute("""
        SELECT DISTINCT min(vehiclespecschema_year.yearvalue), max(vehiclespecschema_year.yearvalue)
        FROM vehiclespecschema vs
        JOIN vehiclespecschema_model vsm ON vs.id = vsm.vehiclespecschemaid
        JOIN vehiclespecschema_year vsy ON vs.id = vsy.vehiclespecschemaid
        WHERE vsm.makeid = ? AND vsm.modelid = ?
        LIMIT 1
    """, (make_map[make_name], modelid)).fetchone()
    if years and years[0]:
        dst.execute("INSERT INTO model_years VALUES (?, ?, ?, ?)", (makeid, modelid, int(years[0]), int(years[1]) if years[1] else None))
        my_count += 1
print(f"  Made {my_count} model_years entries")

# --- WMI ---
print("Building wmi table...")
wmi_count = 0
# Build WMI to make mapping
for row in src.execute("""
    SELECT DISTINCT wmi.wmi, wmi.year, m.codename AS make_name
    FROM wmi wmi
    JOIN manufacturer_make mm ON wmi.id = mm.manufacturerid
    JOIN manufacturer mf ON mm.manufacturerid = mf.id
    JOIN make m ON mf.id = m.id
    WHERE wmi.wmi IS NOT NULL AND wmi.wmi != '' AND m.codename IS NOT NULL
    ORDER BY wmi.wmi
""").fetchall():
    pass  # This join is wrong, let me check the actual schema

# Simpler: use wmi_make table
for row in src.execute("""
    SELECT DISTINCT wm.wmi, m.codename AS make_name
    FROM wmi wm
    JOIN wmi_make wmm ON wm.id = wmm.wmiid
    JOIN manufacturer_make mm ON wmm.manufacturerid = mm.manufacturerid
    JOIN make m ON mm.makeid = m.id
    WHERE wm.wmi IS NOT NULL AND wm.wmi != '' AND m.codename IS NOT NULL
    ORDER BY wm.wmi
""").fetchall():
    pass  # Still wrong joins

# Let me check actual column names
print("Checking wmi table schema...")
wmi_cols = src.execute("PRAGMA table_info(wmi)").fetchall()
print(f"  wmi columns: {[c['name'] for c in wmi_cols]}")

wmi_make_cols = src.execute("PRAGMA table_info(wmi_make)").fetchall()
print(f"  wmi_make columns: {[c['name'] for c in wmi_make_cols]}")

# Now build WMI properly
wmi_wm = {}  # wmi -> make_name
for row in src.execute("SELECT wmi, year FROM wmi ORDER BY wmi").fetchall():
    wmi_val = row[0]
    year = row[1]
    if wmi_val:
        wmi_wm[wmi_val] = year

print(f"  Found {len(wmi_wm)} WMIs")

# For now, skip WMI table - it needs proper make mapping
print("Skipping wmi table for now (needs make mapping fix)")

# --- Dataset_Info ---
print("Adding dataset_info...")
dst.execute("INSERT INTO dataset_info VALUES ('source', 'NHTSA vPIC')")
dst.execute("INSERT INTO dataset_info VALUES ('format', 'curated')")

# Commit
dst.commit()
dst.close()
src.close()

print(f"\nDone! Created {DST}")
print(f"  Makes: {len(make_map)}")
print(f"  Models: {len(model_map)}")
