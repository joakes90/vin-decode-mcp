"""Smoke tests against the real curated database, when one is present.

The unit tests run on a hand-built fixture whose IDs are internally consistent
by construction. That is exactly the property the shipped database lost, so a
green fixture suite proves nothing about the artifact users actually download.
These tests run only when a real database is available and are skipped
otherwise, so CI without the 4.5 MB file stays green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vin_decode_mcp.database import VinDatabase

REAL_DB = Path(__file__).parent.parent / "curated_vpic.db"

pytestmark = pytest.mark.skipif(
    not REAL_DB.exists(),
    reason=f"no curated database at {REAL_DB}; run tools/vpic_pare_down.py first",
)

# make, model and vehicle type for VINs spanning domestic, JDM, Korean and
# European manufacturers, 3- and 6-character WMIs, and a shared WMI.
CANARIES = [
    ("1HGCM82633A004352", "Honda", "Accord", 2003),
    ("WP0AA2A96KS106147", "Porsche", "911", 2019),
    ("JN1AZ4EH6BM551234", "Nissan", "370Z", 2011),
    ("WBA3A5C55CF256789", "BMW", "328i", 2012),
    ("1FTFW1ET5DFC10312", "Ford", "F-150", 2013),
    ("2T1BURHE0JC123456", "Toyota", "Corolla", 2018),
    ("KMHD35LE0EU123456", "Hyundai", "Elantra", 2014),
]


@pytest.fixture(scope="module")
def real_db():
    db = VinDatabase(REAL_DB)
    yield db
    db.close()


@pytest.mark.parametrize(("vin", "make", "model", "year"), CANARIES)
def test_canary_vins_decode_fully(real_db, vin, make, model, year):
    result = real_db.decode_vin(vin)
    assert result["confidence"] == "full", result
    assert result["make"] == make
    assert result["model"] == model
    assert result["year"] == year


def test_pattern_model_ids_resolve(real_db):
    """Every vin_pattern.modelid must name a real model.

    The shipped build renumbered `model.id` while copying vPIC's native model
    ids into `vin_pattern`, so pattern modelid 911 resolved to a model called
    'LA677'. Nothing decoded.
    """
    orphans = real_db.conn.execute(
        "SELECT COUNT(*) FROM vin_pattern p "
        "LEFT JOIN model m ON m.id = p.modelid WHERE m.id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_every_pattern_model_reaches_a_make(real_db):
    """decode_vin joins patterns through make_model; unreachable rows decode to nothing."""
    unreachable = real_db.conn.execute(
        "SELECT COUNT(*) FROM vin_pattern p WHERE NOT EXISTS "
        "(SELECT 1 FROM make_model mm WHERE mm.modelid = p.modelid)"
    ).fetchone()[0]
    assert unreachable == 0


def test_wmi_table_is_populated(real_db):
    """The broken build had 173 WMIs because it ignored vPIC's wmi_make table."""
    n = real_db.conn.execute("SELECT COUNT(*) FROM wmi").fetchone()[0]
    assert n > 1_000, f"only {n} WMIs; most VINs will not match a manufacturer"


def test_makes_are_curated(real_db):
    """Curation should land near 534; 12k means the filter never ran."""
    n = real_db.conn.execute("SELECT COUNT(*) FROM make").fetchone()[0]
    assert 400 < n < 800, f"{n} makes is outside the curated range"
