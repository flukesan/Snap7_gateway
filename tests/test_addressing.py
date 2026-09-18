"""Parsing the address spellings that turn up in real STEP 7 / TIA exports."""

from __future__ import annotations

import pytest

from snap7_gateway.db.models import AreaType
from snap7_gateway.plc.addressing import AddressError, parse_address, try_parse_address


class TestBitAddresses:
    @pytest.mark.parametrize(
        "text,area,byte,bit",
        [
            ("I0.0", AreaType.INPUT, 0, 0),
            ("%I0.0", AreaType.INPUT, 0, 0),
            ("E 0.1", AreaType.INPUT, 0, 1),          # German: Eingang
            ("Q1.3", AreaType.OUTPUT, 1, 3),
            ("A 4.0", AreaType.OUTPUT, 4, 0),          # German: Ausgang
            ("M7.1", AreaType.MERKER, 7, 1),
            ("%M100.7", AreaType.MERKER, 100, 7),
            ("I    10.5", AreaType.INPUT, 10, 5),      # fixed-width padding
        ],
    )
    def test_forms(self, text: str, area: str, byte: int, bit: int) -> None:
        parsed = parse_address(text)
        assert (parsed.area_type, parsed.byte_offset, parsed.bit_offset) == (area, byte, bit)
        assert parsed.implied_type == "BOOL"
        assert not parsed.unsupported

    def test_bit_must_be_0_to_7(self) -> None:
        with pytest.raises(AddressError, match="bits are 0-7"):
            parse_address("M7.8")

    def test_bit_number_is_required(self) -> None:
        with pytest.raises(AddressError, match="no bit number"):
            parse_address("M7")


class TestWidthAddresses:
    @pytest.mark.parametrize(
        "text,area,byte,implied",
        [
            ("IB10", AreaType.INPUT, 10, "BYTE"),
            ("IW22", AreaType.INPUT, 22, "WORD"),
            ("ID24", AreaType.INPUT, 24, "DWORD"),
            ("QB0", AreaType.OUTPUT, 0, "BYTE"),
            ("MW20", AreaType.MERKER, 20, "WORD"),
            ("MD24", AreaType.MERKER, 24, "DWORD"),
            ("%MW20", AreaType.MERKER, 20, "WORD"),
            ("MW    20", AreaType.MERKER, 20, "WORD"),
            ("EW 22", AreaType.INPUT, 22, "WORD"),
            ("AD 8", AreaType.OUTPUT, 8, "DWORD"),
        ],
    )
    def test_forms(self, text: str, area: str, byte: int, implied: str) -> None:
        parsed = parse_address(text)
        assert (parsed.area_type, parsed.byte_offset, parsed.implied_type) == (area, byte, implied)
        assert parsed.bit_offset == 0

    def test_bit_on_a_word_address_is_rejected(self) -> None:
        with pytest.raises(AddressError, match="bit number on a non-bit"):
            parse_address("MW20.3")


class TestDataBlockAddresses:
    @pytest.mark.parametrize(
        "text,db,byte,bit,implied",
        [
            ("DB10.DBX4.2", 10, 4, 2, "BOOL"),
            ("DB10.DBB4", 10, 4, 0, "BYTE"),
            ("DB10.DBW4", 10, 4, 0, "WORD"),
            ("DB10.DBD8", 10, 8, 0, "DWORD"),
            ("%DB1.DBW0", 1, 0, 0, "WORD"),
            ("DB10.DBW    4", 10, 4, 0, "WORD"),
            ("DB 10 . DBX 4.2", 10, 4, 2, "BOOL"),
        ],
    )
    def test_forms(self, text: str, db: int, byte: int, bit: int, implied: str) -> None:
        parsed = parse_address(text)
        assert parsed.area_type == AreaType.DB
        assert (parsed.db_number, parsed.byte_offset, parsed.bit_offset) == (db, byte, bit)
        assert parsed.implied_type == implied

    def test_dbx_requires_a_bit(self) -> None:
        with pytest.raises(AddressError, match="no bit number"):
            parse_address("DB10.DBX4")


class TestUnsupportedAreas:
    """Timers, counters and block names parse, but are reported, not mapped."""

    @pytest.mark.parametrize("text", ["T5", "T 5", "C12", "Z12"])
    def test_timers_and_counters(self, text: str) -> None:
        parsed = parse_address(text)
        assert parsed.unsupported
        assert "not mirrored" in parsed.unsupported_reason

    @pytest.mark.parametrize("text", ["DB10", "FC1", "FB2", "OB1", "UDT5"])
    def test_block_names(self, text: str) -> None:
        parsed = parse_address(text)
        assert parsed.unsupported
        assert "names a block" in parsed.unsupported_reason


class TestRejections:
    @pytest.mark.parametrize(
        "text", ["", "   ", "hello", "Motor_Start", "DB10.DBQ4", "X1.0", "1.0", None]
    )
    def test_not_an_address(self, text) -> None:
        with pytest.raises(AddressError):
            parse_address(text)

    def test_try_parse_returns_none(self) -> None:
        assert try_parse_address("nonsense") is None
        assert try_parse_address("MW20") is not None


class TestCanonical:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("E 0.1", "I0.1"),
            ("A 4.0", "Q4.0"),
            ("MW    20", "MW20"),
            ("%MD24", "MD24"),
            ("DB10.DBW    4", "DB10.DBW4"),
            ("DB10.DBX4.2", "DB10.DBX4.2"),
        ],
    )
    def test_round_trips_to_english_mnemonics(self, text: str, expected: str) -> None:
        assert parse_address(text).canonical() == expected
