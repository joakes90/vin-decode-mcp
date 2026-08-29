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

    def test_honda_prefix(self, test_db: VinDatabase):
        """First 11 characters of 1HGCM82633A004352 -> position 10 is '3' = 2003."""
        results = test_db.decode_partial_vin("1HGCM82633A")
        assert len(results) >= 1
        assert results[0]["make"] == "Honda"
        assert results[0]["model"] == "Accord"
        assert results[0]["year"] == 2003

    def test_honda_pattern_with_wildcard(self, test_db: VinDatabase):
        """A '*' stands in for one unknown character and must still match."""
        results = test_db.decode_partial_vin("1HGCM826*3A")
        assert len(results) >= 1
        assert results[0]["make"] == "Honda"
        assert results[0]["model"] == "Accord"

    def test_year_comes_from_vin_position_10(self, test_db: VinDatabase):
        """Not from the last character of whatever the caller typed.

        Reading the last character turned "1HGCM826*BA" into 2010 (and made
        the pattern match by luck). Position 10 is 'B' and position 7 is a
        digit, so the model year is 1981 -- outside the Accord schema, which
        is why no model comes back.
        """
        results = test_db.decode_partial_vin("1HGCM826*BA")
        assert results == [
            {
                "pattern": "1HGCM826*BA",
                "make": "Honda",
                "model": None,
                "year": 1981,
                "vehicle_type": None,
                "confidence": "wmi_only",
            }
        ]

    def test_shared_wmi_is_not_guessed(self, test_db: VinDatabase):
        """JN1 maps to Nissan and Infiniti; with no model match, name neither."""
        assert test_db.decode_partial_vin("JN1ZZZZZ*ZZ") == []

    def test_too_short(self, test_db: VinDatabase):
        results = test_db.decode_partial_vin("1HG")
        assert results == []

    def test_unknown_wmi(self, test_db: VinDatabase):
        results = test_db.decode_partial_vin("ZZZ00000000000000")
        assert results == []


class TestRegressions:
    """Regressions for the bugs that shipped a database decoding year-only.

    Each test here fails against the code as it was before the fix.
    """

    def test_model_with_null_vehicle_type_still_decodes(self, test_db: VinDatabase):
        """A model whose vehicletypeid is NULL must still resolve.

        `_resolve_model` used to inner-join `vehicletype`, which silently
        discarded every make_model row with a NULL type -- 291 of them in the
        real build. Those VINs degraded to make-only.
        """
        result = test_db.decode_vin("WBA9U9U95CF256789")
        assert result["make"] == "BMW"
        assert result["model"] == "Untyped Coupe"
        assert result["confidence"] == "full"
        assert result["vehicle_type"] is None

    def test_multi_typed_model_picks_deterministically(self, test_db: VinDatabase):
        """Accord is typed both Passenger Car and MPV; the answer must be stable."""
        first = test_db.decode_vin("1HGCM82633A004352")["vehicle_type"]
        assert first == "Passenger Car"  # lowest vehicletype id wins, not MPV
        for _ in range(5):
            assert test_db.decode_vin("1HGCM82633A004352")["vehicle_type"] == first

    def test_result_shape_is_identical_on_every_path(self, test_db: VinDatabase):
        """Callers must never have to probe for a key.

        `wmi_length` used to be present only on the success path.
        """
        expected = {
            "vin",
            "make",
            "model",
            "year",
            "vehicle_type",
            "wmi",
            "wmi_length",
            "confidence",
        }
        for vin, want_confidence in [
            ("SHORT", "invalid_vin"),
            ("ZZZ99999999999999", "no_wmi_match"),
            ("1HGCM82633A004352", "full"),
        ]:
            result = test_db.decode_vin(vin)
            assert set(result) == expected, f"{vin} returned {sorted(result)}"
            assert result["confidence"] == want_confidence

    def test_shared_wmi_resolves_make_from_the_matched_model(self, test_db: VinDatabase):
        """JN1 is both Nissan and Infiniti; the model is what disambiguates."""
        assert test_db.decode_vin("JN1AZ4EH6BM551234")["make"] == "Nissan"
        assert test_db.decode_vin("JN1AGDHC5LM254321")["make"] == "Infiniti"
