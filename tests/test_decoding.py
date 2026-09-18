"""Unit tests for S7 data-type decoding against recorded byte buffers.

No PLC is involved: every case is a hand-written buffer with a known meaning,
which is what makes this the regression net for the decoding rules.
"""

from __future__ import annotations

import math

import pytest

from snap7_gateway.plc.decoding import (
    MAX_STRING_LENGTH,
    DataType,
    DecodeError,
    apply_bit,
    decode,
    encode,
    format_value,
    size_of,
)


class TestBool:
    # 0b1010_0101 - bits 0, 2, 5 and 7 set.
    BUFFER = bytes([0b10100101])

    @pytest.mark.parametrize(
        "bit,expected",
        [(0, True), (1, False), (2, True), (3, False), (4, False), (5, True), (6, False), (7, True)],
    )
    def test_every_bit(self, bit: int, expected: bool) -> None:
        assert decode(self.BUFFER, DataType.BOOL, 0, bit) is expected

    def test_bit_out_of_range(self) -> None:
        with pytest.raises(DecodeError, match="bit index"):
            decode(self.BUFFER, DataType.BOOL, 0, 8)

    def test_offset_applies(self) -> None:
        buffer = bytes([0x00, 0b00001000])
        assert decode(buffer, "BOOL", 1, 3) is True
        assert decode(buffer, "BOOL", 0, 3) is False


class TestIntegers:
    def test_byte(self) -> None:
        assert decode(bytes([0x00, 0xFF]), DataType.BYTE, 1) == 255

    def test_word_is_unsigned_big_endian(self) -> None:
        assert decode(bytes([0xFF, 0xFE]), DataType.WORD, 0) == 65534

    def test_int_is_signed_big_endian(self) -> None:
        assert decode(bytes([0xFF, 0xFE]), DataType.INT, 0) == -2
        assert decode(bytes([0x7F, 0xFF]), DataType.INT, 0) == 32767
        assert decode(bytes([0x80, 0x00]), DataType.INT, 0) == -32768

    def test_dint_is_signed(self) -> None:
        assert decode(bytes([0xFF, 0xFF, 0xFF, 0xFF]), DataType.DINT, 0) == -1
        assert decode(bytes([0x00, 0x00, 0x00, 0x05]), DataType.DINT, 0) == 5
        assert decode(bytes([0x80, 0x00, 0x00, 0x00]), DataType.DINT, 0) == -2147483648

    def test_dword_is_unsigned(self) -> None:
        assert decode(bytes([0xFF, 0xFF, 0xFF, 0xFF]), DataType.DWORD, 0) == 4294967295

    def test_uint_and_udint(self) -> None:
        assert decode(bytes([0xFF, 0xFF]), DataType.UINT, 0) == 65535
        assert decode(bytes([0xFF, 0xFF, 0xFF, 0xFF]), DataType.UDINT, 0) == 4294967295


class TestFloats:
    def test_real(self) -> None:
        # IEEE-754 single precision, big-endian: 100.0
        assert decode(bytes([0x42, 0xC8, 0x00, 0x00]), DataType.REAL, 0) == pytest.approx(100.0)
        # -0.5
        assert decode(bytes([0xBF, 0x00, 0x00, 0x00]), DataType.REAL, 0) == pytest.approx(-0.5)

    def test_real_nan_is_reported_not_raised(self) -> None:
        value = decode(bytes([0x7F, 0xC0, 0x00, 0x00]), DataType.REAL, 0)
        assert math.isnan(value)

    def test_lreal(self) -> None:
        buffer = bytes([0x40, 0x59, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        assert decode(buffer, DataType.LREAL, 0) == pytest.approx(100.0)


class TestString:
    def test_classic_siemens_layout(self) -> None:
        buffer = bytes([20, 5]) + b"HELLO" + bytes(15)
        assert decode(buffer, DataType.STRING, 0, length=20) == "HELLO"

    def test_empty_string(self) -> None:
        buffer = bytes([20, 0]) + bytes(20)
        assert decode(buffer, DataType.STRING, 0, length=20) == ""

    def test_length_header_is_clamped_not_trusted(self) -> None:
        """An uninitialised string must not read past the buffer."""
        buffer = bytes([255, 255]) + b"AB"
        assert decode(buffer, DataType.STRING, 0, length=2) == "AB"

    def test_offset(self) -> None:
        buffer = bytes(4) + bytes([10, 3]) + b"ABC" + bytes(7)
        assert decode(buffer, DataType.STRING, 4, length=10) == "ABC"

    def test_size_includes_two_header_bytes(self) -> None:
        assert size_of(DataType.STRING, 20) == 22

    def test_length_is_range_checked(self) -> None:
        with pytest.raises(DecodeError):
            size_of(DataType.STRING, 0)
        with pytest.raises(DecodeError):
            size_of(DataType.STRING, MAX_STRING_LENGTH + 1)


class TestChar:
    def test_char(self) -> None:
        assert decode(b"A", DataType.CHAR, 0) == "A"


class TestGuards:
    """A short or malformed buffer must raise DecodeError, never IndexError."""

    def test_short_buffer(self) -> None:
        with pytest.raises(DecodeError, match="needs 4 byte"):
            decode(bytes([0x00, 0x01]), DataType.DINT, 0)

    def test_offset_past_end(self) -> None:
        with pytest.raises(DecodeError):
            decode(bytes(4), DataType.INT, 10)

    def test_negative_offset(self) -> None:
        with pytest.raises(DecodeError, match=">= 0"):
            decode(bytes(4), DataType.INT, -1)

    def test_unknown_type(self) -> None:
        with pytest.raises(DecodeError, match="unsupported data type"):
            decode(bytes(4), "FLOAT64", 0)

    def test_empty_buffer(self) -> None:
        with pytest.raises(DecodeError):
            decode(b"", DataType.BYTE, 0)

    def test_type_names_are_case_insensitive(self) -> None:
        assert decode(bytes([0x00, 0x2A]), "int", 0) == 42


class TestRoundTrip:
    @pytest.mark.parametrize(
        "data_type,value",
        [
            (DataType.INT, -12345),
            (DataType.INT, 32767),
            (DataType.DINT, -2147483648),
            (DataType.WORD, 65535),
            (DataType.DWORD, 4294967295),
            (DataType.UINT, 1234),
            (DataType.UDINT, 999999),
            (DataType.BYTE, 200),
            (DataType.REAL, 3.5),
            (DataType.LREAL, 1234.5),
        ],
    )
    def test_encode_then_decode(self, data_type, value) -> None:
        assert decode(encode(value, data_type), data_type, 0) == pytest.approx(value)

    def test_string_round_trip(self) -> None:
        raw = encode("Line 3 OK", DataType.STRING, 16)
        assert len(raw) == size_of(DataType.STRING, 16)
        assert decode(raw, DataType.STRING, 0, length=16) == "Line 3 OK"

    def test_bool_encode_sets_only_the_target_bit(self) -> None:
        assert encode(True, DataType.BOOL, bit_source=3) == bytes([0b00001000])
        assert encode(False, DataType.BOOL, bit_source=3) == bytes([0])

    def test_out_of_range_is_rejected(self) -> None:
        with pytest.raises(DecodeError):
            encode(70000, DataType.INT)
        with pytest.raises(DecodeError):
            encode(256, DataType.BYTE)


class TestApplyBit:
    def test_set_and_clear(self) -> None:
        assert apply_bit(0b00000000, 2, True) == 0b00000100
        assert apply_bit(0b11111111, 2, False) == 0b11111011

    def test_preserves_other_bits(self) -> None:
        assert apply_bit(0b10100101, 1, True) == 0b10100111

    def test_range_checked(self) -> None:
        with pytest.raises(DecodeError):
            apply_bit(0, 9, True)


class TestFormatValue:
    def test_bool(self) -> None:
        assert format_value(True, DataType.BOOL) == "TRUE"
        assert format_value(False, DataType.BOOL) == "FALSE"

    def test_word_shows_hex(self) -> None:
        assert format_value(4660, DataType.WORD) == "4660 (0x1234)"

    def test_real_is_trimmed(self) -> None:
        assert format_value(3.14159265, DataType.REAL) == "3.14159"
