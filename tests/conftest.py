"""Test fixtures for VIN Decode MCP."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the src/ directory is importable
src_dir = Path(__file__).parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from vin_decode_mcp.database import VinDatabase  # noqa: E402

FIXTURE_DB = Path(__file__).parent / "fixtures" / "test_vpic.db"


@pytest.fixture(autouse=True)
def _ensure_test_db():
    """Build the test database before any tests run."""
    if not FIXTURE_DB.exists():
        from fixtures import build_test_db

        build_test_db.build()


@pytest.fixture
def test_db() -> VinDatabase:
    """Return a VinDatabase instance pointing at the test database."""
    return VinDatabase(FIXTURE_DB)


@pytest.fixture
def db(test_db: VinDatabase):
    """Yield and close the database connection."""
    yield test_db
    test_db.close()
