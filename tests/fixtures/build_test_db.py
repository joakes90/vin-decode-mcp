"""Build a minimal test database for unit tests.

Contains only the data needed to verify VIN decoding canaries and
basic lookup queries. Not representative of production data volume.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ── Insert test data ──────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "test_vpic.db"


def build() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Schema (subset of production)
    c.executescript("""
CREATE TABLE make (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE model (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE vehicletype (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE make_model (
    makeid INTEGER NOT NULL REFERENCES make(id),
    modelid INTEGER NOT NULL REFERENCES model(id),
    vehicletypeid INTEGER REFERENCES vehicletype(id)
);
CREATE TABLE model_years (
    makeid INTEGER NOT NULL REFERENCES make(id),
    modelid INTEGER NOT NULL REFERENCES model(id),
    year_from INTEGER NOT NULL,
    year_to INTEGER
);
CREATE TABLE wmi (wmi TEXT NOT NULL, makeid INTEGER NOT NULL REFERENCES make(id));
CREATE TABLE wmi_vinschema (
    wmi TEXT NOT NULL,
    vinschemaid INTEGER NOT NULL,
    year_from INTEGER NOT NULL,
    year_to INTEGER
);
CREATE TABLE vin_pattern (
    vinschemaid INTEGER NOT NULL,
    keys TEXT NOT NULL,
    modelid INTEGER NOT NULL REFERENCES model(id)
);
CREATE TABLE dataset_info (key TEXT PRIMARY KEY, value TEXT);
""")

    # ── Makes ────────────────────────────────────────────────────────────
    makes = [
        (1, "Honda"),
        (2, "Porsche"),
        (3, "Nissan"),
        (4, "Infiniti"),
        (5, "BMW"),
    ]
    c.executemany("INSERT INTO make VALUES (?, ?)", makes)

    # ── Vehicle types ────────────────────────────────────────────────────
    vtypes = [
        (2, "Passenger Car"),
        (1, "Motorcycle"),
        (3, "Truck"),
    ]
    c.executemany("INSERT INTO vehicletype VALUES (?, ?)", vtypes)

    # ── Models ───────────────────────────────────────────────────────────
    models = [
        (101, "Accord"),
        (102, "Civic"),
        (201, "911"),
        (301, "370Z"),
        (401, "Q50"),
        (501, "328i"),
        (502, "M3"),
    ]
    c.executemany("INSERT INTO model VALUES (?, ?)", models)

    # ── Make/Model links ─────────────────────────────────────────────────
    mm = [
        (1, 101, 2),  # Honda Accord → Passenger Car
        (1, 102, 2),  # Honda Civic → Passenger Car
        (2, 201, 2),  # Porsche 911 → Passenger Car
        (3, 301, 2),  # Nissan 370Z → Passenger Car
        (4, 401, 2),  # Infiniti Q50 → Passenger Car
        (5, 501, 2),  # BMW 328i → Passenger Car
        (5, 502, 2),  # BMW M3 → Passenger Car
    ]
    c.executemany("INSERT INTO make_model VALUES (?, ?, ?)", mm)

    # ── Model years ──────────────────────────────────────────────────────
    my = [
        (1, 101, 1976, 2025),
        (1, 102, 1972, 2025),
        (2, 201, 1981, None),  # open-ended
        (3, 301, 2009, 2025),
        (4, 401, 2014, 2025),
        (5, 501, 1990, 2025),
        (5, 502, 1986, 2025),
    ]
    c.executemany("INSERT INTO model_years VALUES (?, ?, ?, ?)", my)

    # ── WMIs ─────────────────────────────────────────────────────────────
    # Note: JN1 is shared by Nissan (3) and Infiniti (4)
    # Note: 6-char WMIs use positions 1-3 + 12-14 of a VIN
    wmis = [
        ("1HG", 1),  # Honda
        ("WP0AA2", 2),  # Porsche (6-char, positions 1-3 + 12-14 = AA2)
        ("WP0KS1", 2),  # Porsche (6-char from test VIN WP0AA2A96KS106147)
        ("WP0", 2),  # Porsche (3-char)
        ("JN1", 3),  # Nissan
        ("JN1", 4),  # Infiniti (same WMI!)
        ("WBA", 5),  # BMW
    ]
    c.executemany("INSERT INTO wmi VALUES (?, ?)", wmis)

    # ── VIN schemas ──────────────────────────────────────────────────────
    # vinschemaid 1 = Honda Accord pattern
    # vinschemaid 2 = Honda Civic pattern
    # vinschemaid 3 = Porsche 911 pattern
    # vinschemaid 4 = Nissan 370Z pattern
    # vinschemaid 5 = Infiniti Q50 pattern
    # vinschemaid 6 = BMW 3-series pattern
    schemas = [
        ("1HG", 1, 1998, 2025),  # Honda Accord
        ("1HG", 2, 2000, 2025),  # Honda Civic
        ("WP0AA2", 3, 1980, 2025),  # Porsche 911 (6-char WMI)
        ("WP0KS1", 3, 1980, 2025),  # Porsche 911 (6-char from test VIN)
        ("WP0", 3, 1980, 2025),  # Porsche 911 (3-char WMI)
        ("JN1", 4, 1980, 2025),  # Nissan 370Z
        ("JN1", 5, 2014, 2025),  # Infiniti Q50
        ("WBA", 6, 1980, 2025),  # BMW 3-series
    ]
    c.executemany("INSERT INTO wmi_vinschema VALUES (?, ?, ?, ?)", schemas)

    # ── VIN patterns ─────────────────────────────────────────────────────
    # keys is the VDS portion (VIN positions 4-9) with * wildcards
    # * means exactly one character in vPIC pattern format
    #
    # Honda Accord (1HGCM82633A004352): VDS = CM8263
    # Pattern: CM8*** means C, M, 8, then any 3 chars
    c.executemany(
        "INSERT INTO vin_pattern VALUES (?, ?, ?)",
        [
            (1, "CM8***", 101),  # Honda Accord
            (2, "C***00", 102),  # Honda Civic
            (3, "A2A96*", 201),  # Porsche 911
            (4, "AZ4EH*", 301),  # Nissan 370Z
            (5, "AGDHC*", 401),  # Infiniti Q50
            (6, "3A5C5*", 501),  # BMW 328i
            (6, "WTAFM*", 502),  # BMW M3
        ],
    )

    # ── Dataset info ─────────────────────────────────────────────────────
    c.executemany(
        "INSERT INTO dataset_info VALUES (?, ?)",
        [
            ("source_file", "test_vpic.db"),
            ("source_vintage", "test"),
            ("build_timestamp", "2025-01-01T00:00:00+00:00"),
            ("script_version", "0.1.0"),
            ("has_vin_decode", "1"),
            ("kept_vehicle_types", "[1,2,3]"),
        ],
    )

    conn.commit()
    conn.close()
    print(f"Test database created: {DB_PATH} ({DB_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    build()
