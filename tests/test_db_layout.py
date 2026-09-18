"""Data-block layout arithmetic and data-block source parsing.

The offsets asserted here are the ones STEP 7 and TIA produce for standard
(non-optimized) block access. They are hand-derived from the documented S7
rules, so a regression in the walker shows up as a concrete wrong byte rather
than as a vague mismatch on real hardware.
"""

from __future__ import annotations

import pytest

from snap7_gateway.plc.db_layout import (
    DbSourceError,
    MemberNode,
    compute_layout,
    layout_to_symbols,
    parse_db_export,
    parse_db_source,
    parse_tia_db_xml,
)
from snap7_gateway.plc.s7types import map_declared_type

SCL_SOURCE = """DATA_BLOCK "Recipes"
{ S7_Optimized_Access := 'FALSE' }
VERSION : 0.1
NON_RETAIN
   STRUCT
      A : Bool;   // first bit
      B : Bool;   // second bit
      C : Byte;   // a byte
      D : Int := 5;   // an integer
      E : Bool;
      F : Real;   // temperature
      G : String[10];   // part name
      H : Bool;
      I : DInt;   // counter
   END_STRUCT;
BEGIN
   D := 5;
END_DATA_BLOCK
"""

NESTED_SOURCE = """DATA_BLOCK "Machine"
{ S7_Optimized_Access := 'FALSE' }
   STRUCT
      Enabled : Bool;   // line enabled
      Settings : Struct
         Speed : Real;   // rpm
         Name : String[4];
      END_STRUCT;
      Count : Int;
      Values : Array[0..9] of Int;   // trend buffer
   END_STRUCT;
BEGIN
END_DATA_BLOCK
"""

TIA_XML = """<?xml version="1.0" encoding="utf-8"?>
<Document>
  <SW.Blocks.GlobalDB ID="0">
    <AttributeList>
      <Interface>
        <Sections xmlns="http://www.siemens.com/automation/Openness/SW/Interface/v5">
          <Section Name="Static">
            <Member Name="Recipe_No" Datatype="Int">
              <Comment><MultiLanguageText Lang="en-US">Active recipe number</MultiLanguageText></Comment>
            </Member>
            <Member Name="Running" Datatype="Bool">
              <Comment><MultiLanguageText Lang="en-US">Line running</MultiLanguageText></Comment>
            </Member>
            <Member Name="Settings" Datatype="Struct">
              <Member Name="Speed" Datatype="Real">
                <Comment><MultiLanguageText Lang="en-US">Setpoint rpm</MultiLanguageText></Comment>
              </Member>
            </Member>
          </Section>
        </Sections>
      </Interface>
      <Name>Recipes</Name>
      <Number>12</Number>
    </AttributeList>
  </SW.Blocks.GlobalDB>
</Document>
"""


class TestLayoutRules:
    """Each rule on its own, so a failure names the rule that broke."""

    def _offsets(self, members: list[tuple[str, str]]) -> dict[str, tuple[int, int]]:
        layout = compute_layout([MemberNode(name=n, datatype=t) for n, t in members])
        return {m.path: (m.byte_offset, m.bit_offset) for m in layout.members}

    def test_bools_pack_eight_to_a_byte(self) -> None:
        offsets = self._offsets([(f"B{i}", "Bool") for i in range(10)])
        assert offsets["B0"] == (0, 0)
        assert offsets["B7"] == (0, 7)
        assert offsets["B8"] == (1, 0)
        assert offsets["B9"] == (1, 1)

    def test_a_byte_after_bools_starts_on_the_next_byte(self) -> None:
        offsets = self._offsets([("Flag", "Bool"), ("Value", "Byte")])
        assert offsets["Flag"] == (0, 0)
        assert offsets["Value"] == (1, 0)

    def test_words_align_to_an_even_byte(self) -> None:
        offsets = self._offsets([("B", "Byte"), ("W", "Int")])
        assert offsets["B"] == (0, 0)
        assert offsets["W"] == (2, 0)

    def test_real_aligns_to_two_not_to_four(self) -> None:
        """S7 alignment is word-based: a REAL after an INT sits at byte 2."""
        offsets = self._offsets([("I", "Int"), ("R", "Real")])
        assert offsets["R"] == (2, 0)

    def test_string_occupies_length_plus_two(self) -> None:
        layout = compute_layout(
            [MemberNode("S", "String[10]"), MemberNode("After", "Byte")]
        )
        members = {m.path: m for m in layout.members}
        assert members["S"].size_bytes == 12
        assert members["After"].byte_offset == 12

    def test_block_length_is_rounded_up_to_an_even_number(self) -> None:
        layout = compute_layout([MemberNode("Only", "Byte")])
        assert layout.total_bytes == 2

    def test_nested_struct_is_word_aligned_and_padded(self) -> None:
        layout = compute_layout(
            [
                MemberNode("Flag", "Bool"),
                MemberNode("S", "Struct", children=[MemberNode("Inner", "Byte")]),
                MemberNode("After", "Byte"),
            ]
        )
        members = {m.path: m for m in layout.members}
        assert members["Flag"].byte_offset == 0
        assert members["S"].byte_offset == 2      # aligned up from the partial byte
        assert members["S.Inner"].byte_offset == 2
        assert members["S"].size_bytes == 2       # padded to an even size
        assert members["After"].byte_offset == 4

    def test_a_struct_is_listed_before_its_children(self) -> None:
        layout = compute_layout(
            [MemberNode("S", "Struct", children=[MemberNode("Inner", "Byte")])]
        )
        assert [m.path for m in layout.members] == ["S", "S.Inner"]

    def test_array_size_covers_every_element(self) -> None:
        layout = compute_layout(
            [MemberNode("Values", "Array[0..9] of Int"), MemberNode("After", "Int")]
        )
        members = {m.path: m for m in layout.members}
        assert members["Values"].size_bytes == 20
        assert members["After"].byte_offset == 20

    def test_bool_array_packs_and_pads_to_a_word(self) -> None:
        info = map_declared_type("Array[0..15] of Bool")
        assert info.size_bytes == 2

    def test_an_unknown_type_is_flagged_as_poisoning_later_offsets(self) -> None:
        layout = compute_layout([MemberNode("X", "Frobnicate"), MemberNode("Y", "Int")])
        assert any("offsets after this member may be wrong" in w for w in layout.warnings)


class TestSclSource:
    def test_the_full_layout_matches_siemens(self) -> None:
        layout = parse_db_source(SCL_SOURCE)
        offsets = {m.path: (m.byte_offset, m.bit_offset) for m in layout.members}
        assert offsets == {
            "A": (0, 0), "B": (0, 1), "C": (1, 0), "D": (2, 0), "E": (4, 0),
            "F": (6, 0), "G": (10, 0), "H": (22, 0), "I": (24, 0),
        }
        assert layout.total_bytes == 28

    def test_comments_are_kept(self) -> None:
        members = {m.path: m for m in parse_db_source(SCL_SOURCE).members}
        assert members["F"].comment == "temperature"
        assert members["D"].comment == "an integer"

    def test_initial_values_are_ignored(self) -> None:
        members = {m.path: m for m in parse_db_source(SCL_SOURCE).members}
        assert members["D"].data_type == "INT"

    def test_nested_structs_and_arrays(self) -> None:
        layout = parse_db_source(NESTED_SOURCE)
        members = {m.path: m for m in layout.members}
        assert members["Settings.Speed"].byte_offset == 2
        assert members["Settings.Name"].byte_offset == 6
        assert members["Count"].byte_offset == 12
        assert members["Values"].byte_offset == 14
        assert not members["Values"].supported  # arrays are listed, not imported

    def test_optimized_access_is_refused_with_the_fix(self) -> None:
        source = SCL_SOURCE.replace("'FALSE'", "'TRUE'")
        with pytest.raises(DbSourceError) as excinfo:
            parse_db_source(source)
        message = str(excinfo.value)
        assert "optimized block access" in message
        assert "Clear 'Optimized block access'" in message

    def test_a_source_without_a_struct_is_refused(self) -> None:
        with pytest.raises(DbSourceError, match="no STRUCT"):
            parse_db_source("DATA_BLOCK DB 10\nBEGIN\nEND_DATA_BLOCK\n")

    def test_classic_awl_db_number(self) -> None:
        source = "DATA_BLOCK DB 42\nSTRUCT\n  V : INT;\nEND_STRUCT;\nBEGIN\nEND_DATA_BLOCK\n"
        assert parse_db_source(source).db_number == 42

    def test_a_missing_db_number_is_a_warning_not_a_failure(self) -> None:
        layout = parse_db_source(SCL_SOURCE)
        assert layout.db_number == 0
        assert any("does not state a DB number" in w for w in layout.warnings)


class TestTiaXml:
    def test_reads_number_name_members_and_comments(self) -> None:
        layout = parse_tia_db_xml(TIA_XML)
        assert layout.db_number == 12
        assert layout.db_name == "Recipes"
        members = {m.path: m for m in layout.members}
        assert members["Recipe_No"].comment == "Active recipe number"
        assert members["Settings.Speed"].comment == "Setpoint rpm"

    def test_offsets_are_computed_from_declaration_order(self) -> None:
        members = {m.path: m for m in parse_tia_db_xml(TIA_XML).members}
        assert members["Recipe_No"].byte_offset == 0
        assert (members["Running"].byte_offset, members["Running"].bit_offset) == (2, 0)
        assert members["Settings.Speed"].byte_offset == 4

    def test_bad_xml_is_refused(self) -> None:
        with pytest.raises(DbSourceError, match="not valid XML"):
            parse_tia_db_xml("<Document><unclosed>")

    def test_xml_without_members_is_refused_with_advice(self) -> None:
        with pytest.raises(DbSourceError, match="Generate source from blocks"):
            parse_tia_db_xml("<Document><Other/></Document>")


class TestDispatcherAndConversion:
    def test_extension_and_content_both_route_correctly(self) -> None:
        assert parse_db_export("db12.xml", TIA_XML.encode()).db_number == 12
        assert parse_db_export("recipes.db", SCL_SOURCE.encode()).total_bytes == 28
        assert parse_db_export("no-extension", TIA_XML.encode()).db_number == 12

    def test_conversion_to_symbols(self) -> None:
        layout = parse_db_source(NESTED_SOURCE)
        symbols = layout_to_symbols(layout, db_number=12, prefix="Machine.")
        names = {e.name: e for e in symbols.entries}
        assert names["Machine.Enabled"].address_text == "DB12.DBX0.0"
        assert names["Machine.Settings.Speed"].address_text == "DB12.DBD2"
        assert names["Machine.Settings.Speed"].data_type == "REAL"
        assert names["Machine.Count"].address_text == "DB12.DBW12"
        reasons = {s.name: s.reason for s in symbols.skipped}
        assert "structures have no single value" in reasons["Machine.Settings"]
        assert "ARRAY" in reasons["Machine.Values"]

    def test_conversion_without_a_db_number_refuses_to_guess(self) -> None:
        symbols = layout_to_symbols(parse_db_source(SCL_SOURCE))
        assert symbols.entries == []
        assert any("no DB number" in e for e in symbols.errors)

    def test_string_length_survives_conversion(self) -> None:
        symbols = layout_to_symbols(parse_db_source(SCL_SOURCE), db_number=5)
        part = next(e for e in symbols.entries if e.name == "G")
        assert part.data_type == "STRING" and part.length == 10
