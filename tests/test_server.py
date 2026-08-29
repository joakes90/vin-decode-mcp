"""Tests for bulk lookup tools."""

from vin_decode_mcp.database import VinDatabase


class TestAllMakes:
    def test_returns_sorted_list(self, db: VinDatabase):
        makes = db.get_all_makes()
        assert len(makes) >= 5
        names = [m["name"] for m in makes]
        assert names == sorted(names)

    def test_honda_present(self, db: VinDatabase):
        makes = db.get_all_makes()
        names = {m["name"] for m in makes}
        assert "Honda" in names
        assert "Porsche" in names

    def test_id_type(self, db: VinDatabase):
        makes = db.get_all_makes()
        for m in makes:
            assert isinstance(m["id"], int)
            assert isinstance(m["name"], str)


class TestModelsForMake:
    def test_honda_models(self, db: VinDatabase):
        models = db.get_models_for_make("Honda")
        names = [m["name"] for m in models]
        assert "Accord" in names
        assert "Civic" in names

    def test_bmw_filter_car(self, db: VinDatabase):
        models = db.get_models_for_make("BMW", vehicle_type="Passenger Car")
        names = [m["name"] for m in models]
        assert "328i" in names
        assert "M3" in names

    def test_unknown_make(self, db: VinDatabase):
        models = db.get_models_for_make("NonExistent")
        assert models == []

    def test_case_insensitive(self, db: VinDatabase):
        models = db.get_models_for_make("honda")
        assert len(models) > 0


class TestModelYears:
    def test_porsche_911(self, db: VinDatabase):
        result = db.get_model_years("Porsche", "911")
        assert result is not None
        assert result["year_from"] == 1981
        assert result["year_to"] is None  # open-ended

    def test_honda_accord(self, db: VinDatabase):
        result = db.get_model_years("Honda", "Accord")
        assert result is not None
        assert result["year_from"] == 1976
        assert result["year_to"] == 2025

    def test_unknown_make(self, db: VinDatabase):
        result = db.get_model_years("NonExistent", "Unknown")
        assert result is None

    def test_case_insensitive(self, db: VinDatabase):
        result = db.get_model_years("porsche", "911")
        assert result is not None


class TestWmiInfo:
    def test_3char_wmi(self, db: VinDatabase):
        result = db.get_wmi_info("1HG")
        assert result is not None
        assert result["make"] == "Honda"

    def test_6char_wmi(self, db: VinDatabase):
        result = db.get_wmi_info("WP0AA2")
        assert result is not None
        assert result["make"] == "Porsche"

    def test_unknown_wmi(self, db: VinDatabase):
        result = db.get_wmi_info("ZZZ")
        assert result is None


class TestVehicleTypes:
    def test_returns_types(self, db: VinDatabase):
        vtypes = db.get_vehicle_types()
        names = [v["name"] for v in vtypes]
        assert "Passenger Car" in names
        assert "Motorcycle" in names

    def test_has_ids(self, db: VinDatabase):
        vtypes = db.get_vehicle_types()
        for v in vtypes:
            assert isinstance(v["id"], int)


class TestMakeVehicleTypes:
    def test_bmw_types(self, db: VinDatabase):
        types = db.get_make_vehicle_types("BMW")
        assert "Passenger Car" in types

    def test_honda_types(self, db: VinDatabase):
        types = db.get_make_vehicle_types("Honda")
        assert "Passenger Car" in types

    def test_unknown_make(self, db: VinDatabase):
        types = db.get_make_vehicle_types("NonExistent")
        assert types == []


class TestSchemaAndInfo:
    def test_schema_ddl(self, db: VinDatabase):
        ddl = db.get_schema_ddl()
        assert "CREATE TABLE" in ddl
        assert "make" in ddl
        assert "wmi" in ddl

    def test_dataset_info(self, db: VinDatabase):
        info = db.get_dataset_info()
        assert "source_file" in info
        assert "source_vintage" in info


class TestLoggingNeverTouchesStdout:
    """Under the stdio transport, stdout *is* the JSON-RPC channel.

    structlog's default PrintLoggerFactory writes to stdout, so an unguarded
    `logger.error(...)` in a tool's except branch corrupts the message stream
    and the client fails to parse it.
    """

    def test_error_path_writes_nothing_to_stdout(self, capsys, monkeypatch, tmp_path):
        from vin_decode_mcp import server

        monkeypatch.setattr(server, "get_db", lambda: VinDatabase(tmp_path / "missing.db"))
        result = server.decode_vin("1HGCM82633A004352")

        assert "error" in result
        assert capsys.readouterr().out == ""
