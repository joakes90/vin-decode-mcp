r"""Tests for the PostgreSQL COPY decoder in tools/convert_to_sqlite.py.

This is where the shipped database broke. PostgreSQL's COPY *text* format is
not CSV, and parsing it as CSV stored the NULL marker `\N` as a literal
two-character string in 1,799,178 cells. Downstream that collapsed the curated
`wmi` table from ~1,718 rows to 173, so most VINs matched no manufacturer and
decoded to a model year and nothing else.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from convert_to_sqlite import (  # noqa: E402
    convert_from_sql,
    decode_copy_field,
    parse_copy_row,
    verify_no_null_markers,
)


class TestDecodeCopyField:
    def test_null_marker_becomes_none(self):
        assert decode_copy_field(r"\N") is None

    def test_escaped_backslash_n_is_not_null(self):
        r"""`\\N` is a literal backslash-N in the data, not a NULL."""
        assert decode_copy_field(r"\\N") == r"\N"

    def test_empty_string_is_not_null(self):
        assert decode_copy_field("") == ""

    def test_plain_value_passes_through(self):
        assert decode_copy_field("Honda") == "Honda"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (r"DAEW\r\n", "DAEW\r\n"),
            (r"a\tb", "a\tb"),
            (r"C:\\path", r"C:\path"),
            (r"\x41", "A"),
            (r"\101", "A"),
        ],
    )
    def test_backslash_escapes_are_decoded(self, raw, expected):
        assert decode_copy_field(raw) == expected

    def test_double_quotes_are_literal(self):
        """COPY text format has no quoting; csv.reader used to eat these."""
        assert decode_copy_field('say "hi"') == 'say "hi"'


class TestParseCopyRow:
    def test_mixed_row(self):
        assert parse_copy_row('1904\t\\N\t2226\tsay "hi"\ta\\tb') == [
            "1904",
            None,
            "2226",
            'say "hi"',
            "a\tb",
        ]

    def test_trailing_empty_field_is_kept(self):
        assert parse_copy_row("a\tb\t") == ["a", "b", ""]


class TestConvertFromSql:
    """End-to-end: a miniature pg_dump through the converter."""

    DUMP = """CREATE TABLE vpic.wmi (
    id integer NOT NULL,
    wmi character varying(6) NOT NULL,
    makeid integer,
    vehicletypeid integer
);
COPY vpic.wmi (id, wmi, makeid, vehicletypeid) FROM stdin;
1\t1HG\t474\t2
2\tJN1\t\\N\t2
\\.
"""

    @pytest.fixture
    def built(self, tmp_path):
        sql = tmp_path / "mini.sql"
        sql.write_text(self.DUMP)
        out = tmp_path / "mini.db"
        convert_from_sql(sql, out)
        conn = sqlite3.connect(out)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()

    def test_null_is_stored_as_real_null(self, built):
        row = built.execute("SELECT makeid FROM wmi WHERE wmi = 'JN1'").fetchone()
        assert row["makeid"] is None

    def test_non_null_keeps_integer_affinity(self, built):
        row = built.execute(
            "SELECT typeof(makeid) AS t, makeid FROM wmi WHERE wmi = '1HG'"
        ).fetchone()
        assert row["t"] == "integer"
        assert row["makeid"] == 474

    def test_no_literal_null_markers_survive(self, built):
        n = built.execute(
            r"SELECT COUNT(*) FROM wmi WHERE makeid = '\N' OR vehicletypeid = '\N'"
        ).fetchone()[0]
        assert n == 0


class TestVerifyNoNullMarkers:
    """The build gate that stops a corrupt database from shipping again."""

    def test_passes_on_clean_database(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.execute("INSERT INTO t VALUES (NULL)")
        verify_no_null_markers(conn)  # must not raise

    def test_raises_when_a_null_marker_survived(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.execute(r"INSERT INTO t VALUES ('\N')")
        with pytest.raises(RuntimeError, match=r"COPY decoding failed"):
            verify_no_null_markers(conn)
