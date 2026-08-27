"""Tests for VIN decoding canaries and edge cases."""

from __future__ import annotations

from vin_decode_mcp.database import VinDatabase


class TestModelYearFromVin:
    """Test model year computation from VIN positions 10 and 7."""

    def test_honda_accord_2003(self, test_db: VinDatabase):
        year = test_db._model_year_from_vin("1HGCM82633A004352")
        assert year == 2003

    def test_porsche_911_2019(self, test_db: VinDatabase):
        year = test_db._model_year_from_vin("WP0AA2A96KS106147")
        assert year == 2019  # K = 2019

    def test_old_vin_1995(self, test_db: VinDatabase):
        year = test_db._model_year_from_vin("1G1AA11H8S7123456")
        assert year == 1995  # S = 1995 (numeric position 7 => 1980-2009)

    def test_future_vin_2028(self, test_db: VinDatabase):
        # Position 7 (index 6) = B (alpha → 2010 cycle). Position 10 (index 9) = W (index 18 → 2028)
        year = test_db._model_year_from_vin("1G1AAABH8W7123456")
        assert year == 2028

    def test_short_vin(self, test_db: VinDatabase):
        assert test_db._model_year_from_vin("12345") is None

    def test_invalid_code(self, test_db: VinDatabase):
        # I, O, Q are not used in position 10
        assert test_db._model_year_from_vin("1G1AA11H8I7123456") is None


class TestDecodeVin:
    """Test the main VIN decode function."""

    def test_honda_accord(self, test_db: VinDatabase):
        result = test_db.decode_vin("1HGCM82633A004352")
        assert result["make"] == "Honda"
        assert result["model"] == "Accord"
        assert result["year"] == 2003
        assert result["confidence"] == "full"
        assert result["wmi"] == "1HG"

    def test_porsche_911_6char_wmi(self, test_db: VinDatabase):
        # 3-char WMI WP0 → Porsche. VDS starts with A2A96 matching pattern.
        result = test_db.decode_vin("WP0A2A96XXK123456")
        assert result["make"] == "Porsche"
        assert result["model"] == "911"
        assert result["confidence"] == "full"
        assert result["wmi"] == "WP0"

    def test_porsche_6char_wmi(self, test_db: VinDatabase):
        """Verify 6-char WMI resolves correctly."""
        # Positions 12-14 (indices 11-13) = AA2 → 6-char WMI = WP0AA2
        result = test_db.decode_vin("WP0A2A96XXKAA2123")
        assert result["wmi"] == "WP0AA2"
        assert result["make"] == "Porsche"
        assert result["model"] == "911"

    def test_nissan_370z_jdm_shared_wmi(self, test_db: VinDatabase):
        """JN1 is shared by Nissan and Infiniti — model resolves it."""
        result = test_db.decode_vin("JN1AZ4EH6BM551234")
        assert result["make"] == "Nissan"
        assert result["model"] == "370Z"
        assert result["confidence"] == "full"

    def test_bmw_328i(self, test_db: VinDatabase):
        result = test_db.decode_vin("WBA3A5C55CF256789")
        assert result["make"] == "BMW"
        assert result["model"] == "328i"
        assert result["year"] == 2012
        assert result["confidence"] == "full"

    def test_short_vin(self, test_db: VinDatabase):
        result = test_db.decode_vin("12345")
        assert result["confidence"] == "invalid_vin"
        assert result["make"] is None

    def test_unknown_wmi(self, test_db: VinDatabase):
        result = test_db.decode_vin("ZZZABCDEF12345678")
        assert result["confidence"] == "no_wmi_match"
        assert result["make"] is None

    def test_short_vin_gives_make(self, test_db: VinDatabase):
        """A partial VIN that resolves to WMI but has no matching pattern."""
        # 11-char VIN: WMI matches Honda, but VDS 'XYZABCDEF' has no pattern match
        result = test_db.decode_vin("1HGXYZABCDE")
        assert result["make"] == "Honda"
        assert result["model"] is None
        assert result["confidence"] == "make_only"

    def test_explicit_year(self, test_db: VinDatabase):
        result = test_db.decode_vin("1HGCM82633A004352", model_year=2003)
        assert result["make"] == "Honda"
        assert result["model"] == "Accord"

    def test_infiniti_shared_wmi(self, test_db: VinDatabase):
        """JN1 is shared — Q50 pattern should resolve to Infiniti."""
        result = test_db.decode_vin("JN1AGDHC6EM123456")
        assert result["make"] == "Infiniti"
        assert result["model"] == "Q50"
        assert result["confidence"] == "full"


class TestPartialVin:
    """Test partial VIN pattern matching."""

    def test_honda_pattern(self, test_db: VinDatabase):
        results = test_db.decode_partial_vin("1HGCM826*BA")
        assert len(results) >= 1
        assert results[0]["make"] == "Honda"
        assert results[0]["model"] == "Accord"

    def test_too_short(self, test_db: VinDatabase):
        results = test_db.decode_partial_vin("1HG")
        assert results == []

    def test_unknown_wmi(self, test_db: VinDatabase):
        results = test_db.decode_partial_vin("ZZZ00000000000000")
        assert results == []
