"""Server-side validation must reject anything a hostile client can post."""

from __future__ import annotations

import pytest

from snap7_gateway.core.validation import (
    validate_area_address,
    validate_connection,
    validate_settings,
    validate_tag,
    validate_username,
)

GOOD_CONNECTION = {
    "name": "CNC Line 3",
    "host": "10.20.30.40",
    "rack": "0",
    "slot": "2",
    "tcp_port": "102",
    "connection_type": "PG",
    "timeout_ms": "3000",
    "poll_interval_ms": "1000",
    "exposure_mode": "whitelist",
    "enabled": "on",
}


class TestConnectionValidation:
    def test_a_good_form_is_accepted(self) -> None:
        values, errors = validate_connection(GOOD_CONNECTION)
        assert errors == {}
        assert values["name"] == "CNC Line 3"
        assert values["rack"] == 0 and values["tcp_port"] == 102

    def test_write_access_defaults_to_off(self) -> None:
        values, _ = validate_connection(GOOD_CONNECTION)
        assert values["write_enabled"] is False

    @pytest.mark.parametrize(
        "host",
        ["", "   ", "10.0.0.1; rm -rf /", "<script>alert(1)</script>", "a" * 300,
         "10.0.0.1 10.0.0.2", "../../etc/passwd"],
    )
    def test_bad_hosts_are_rejected(self, host: str) -> None:
        _, errors = validate_connection({**GOOD_CONNECTION, "host": host})
        assert "host" in errors

    @pytest.mark.parametrize("host", ["10.0.0.1", "plc-line3", "plc.factory.local", "::1"])
    def test_good_hosts_are_accepted(self, host: str) -> None:
        _, errors = validate_connection({**GOOD_CONNECTION, "host": host})
        assert "host" not in errors

    @pytest.mark.parametrize(
        "field,value",
        [("rack", "8"), ("rack", "-1"), ("slot", "32"), ("tcp_port", "0"),
         ("tcp_port", "70000"), ("timeout_ms", "10"), ("poll_interval_ms", "0"),
         ("rack", "two"), ("tcp_port", "102.5")],
    )
    def test_out_of_range_numbers_are_rejected(self, field: str, value: str) -> None:
        _, errors = validate_connection({**GOOD_CONNECTION, field: value})
        assert field in errors

    def test_duplicate_names_are_rejected(self) -> None:
        _, errors = validate_connection(GOOD_CONNECTION, existing_names={"cnc line 3"})
        assert "name" in errors

    @pytest.mark.parametrize("name", ["", " ", "a" * 65, "-leading", "semi;colon", "quote'name"])
    def test_bad_names_are_rejected(self, name: str) -> None:
        _, errors = validate_connection({**GOOD_CONNECTION, "name": name})
        assert "name" in errors

    def test_unknown_connection_type_is_rejected(self) -> None:
        _, errors = validate_connection({**GOOD_CONNECTION, "connection_type": "MAGIC"})
        assert "connection_type" in errors

    def test_unknown_exposure_mode_is_rejected(self) -> None:
        _, errors = validate_connection({**GOOD_CONNECTION, "exposure_mode": "everything"})
        assert "exposure_mode" in errors

    def test_missing_optional_numbers_fall_back_to_defaults(self) -> None:
        values, errors = validate_connection({"name": "X", "host": "10.0.0.1"})
        assert errors == {}
        assert values["tcp_port"] == 102 and values["poll_interval_ms"] == 1000


class TestTagValidation:
    GOOD = {
        "name": "SpindleOn",
        "area_type": "DB",
        "db_number": "10",
        "byte_offset": "4",
        "bit_offset": "2",
        "data_type": "BOOL",
        "length": "1",
    }

    def test_good_tag(self) -> None:
        values, errors = validate_tag(self.GOOD)
        assert errors == {}
        assert values["data_type"] == "BOOL"

    def test_bit_offset_only_applies_to_bool(self) -> None:
        _, errors = validate_tag({**self.GOOD, "data_type": "INT", "bit_offset": "3"})
        assert "bit_offset" in errors

    def test_bit_offset_range(self) -> None:
        _, errors = validate_tag({**self.GOOD, "bit_offset": "8"})
        assert "bit_offset" in errors

    def test_length_only_applies_to_string(self) -> None:
        _, errors = validate_tag({**self.GOOD, "data_type": "INT", "length": "20"})
        assert "length" in errors

    def test_string_length_is_range_checked(self) -> None:
        _, errors = validate_tag(
            {**self.GOOD, "data_type": "STRING", "bit_offset": "0", "length": "999"}
        )
        assert "length" in errors

    def test_unknown_data_type_is_rejected(self) -> None:
        _, errors = validate_tag({**self.GOOD, "data_type": "FLOAT"})
        assert "data_type" in errors

    def test_db_number_required_for_db_area(self) -> None:
        _, errors = validate_tag({**self.GOOD, "db_number": ""})
        assert "db_number" in errors

    def test_non_db_area_needs_no_db_number(self) -> None:
        values, errors = validate_tag(
            {**self.GOOD, "area_type": "M", "db_number": "", "data_type": "BYTE",
             "bit_offset": "0"}
        )
        assert errors == {}
        assert values["db_number"] == 0

    def test_duplicate_tag_names_are_rejected(self) -> None:
        _, errors = validate_tag(self.GOOD, existing_names={"spindleon"})
        assert "name" in errors


class TestAreaAddress:
    def test_size_is_range_checked(self) -> None:
        _, errors = validate_area_address(
            {"area_type": "DB", "db_number": "1", "size": "99999999"}, require_size=True
        )
        assert "size" in errors

    def test_unknown_area_rejected(self) -> None:
        _, errors = validate_area_address({"area_type": "XX"})
        assert "area_type" in errors


class TestSettingsValidation:
    def test_unknown_keys_are_silently_ignored(self) -> None:
        clean, errors = validate_settings({"password_hash": "pwned", "vplc_port": "1102"})
        assert errors == {}
        assert clean == {"vplc_port": "1102"}

    def test_numeric_bounds(self) -> None:
        _, errors = validate_settings({"vplc_port": "70000"})
        assert "vplc_port" in errors
        _, errors = validate_settings({"password_min_length": "4"})
        assert "password_min_length" in errors

    def test_booleans_are_normalised(self) -> None:
        clean, _ = validate_settings({"vplc_enabled": "on", "https_enabled": ""})
        assert clean["vplc_enabled"] == "true"
        assert clean["https_enabled"] == "false"

    def test_choice_fields(self) -> None:
        clean, errors = validate_settings({"locale": "th", "log_level": "debug"})
        assert errors == {}
        assert clean["locale"] == "th" and clean["log_level"] == "DEBUG"
        _, errors = validate_settings({"locale": "de"})
        assert "locale" in errors

    def test_bind_address_validated(self) -> None:
        clean, errors = validate_settings({"vplc_bind_ip": "0.0.0.0"})
        assert errors == {} and clean["vplc_bind_ip"] == "0.0.0.0"
        _, errors = validate_settings({"vplc_bind_ip": "not a host!"})
        assert "vplc_bind_ip" in errors


class TestUsernameValidation:
    @pytest.mark.parametrize("username", ["ab", "a" * 33, "has space", "semi;colon", ""])
    def test_bad_usernames(self, username: str) -> None:
        assert validate_username(username) is not None

    def test_good_username(self) -> None:
        assert validate_username("line3.operator") is None

    def test_duplicates_rejected(self) -> None:
        assert validate_username("admin", existing={"admin"}) is not None
