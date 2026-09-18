"""Reading symbol/comment exports, and importing them into the tag table.

Fixtures are shaped like the real thing: STEP 7 writes fixed-width columns with
an address that contains spaces, TIA writes a header row and percent-prefixed
addresses, and both contain rows the gateway must refuse rather than guess at.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from snap7_gateway.core.tag_import import MAX_IMPORT_TAGS, apply_import, plan_import
from snap7_gateway.db.models import AreaType
from snap7_gateway.plc.symbols import (
    SymbolImportError,
    parse_asc,
    parse_delimited,
    parse_sdf,
    parse_symbol_export,
    parse_xlsx,
    sanitize_name,
)

from .test_discovery import make_connection

SDF_EXPORT = '''"Motor_Start","E      0.0","BOOL","Start pushbutton line 3"
"Motor_Stop","E      0.1","BOOL","Stop pushbutton"
"Motor_Run","A      4.0","BOOL","Motor contactor K1"
"Speed_SP","MW    20","INT","Speed setpoint in rpm"
"Temp_Act","MD    24","REAL","Actual temperature degC"
"Recipe_No","DB10.DBW    0","INT","Active recipe number"
"Part_Name","DB10.DBB    4","STRING[20]","Part identifier"
"Cycle_Timer","T      5","TIMER","Cycle time"
"Data_Recipes","DB    10","DB 10","Recipe data block"
'''

ASC_EXPORT = """126,Motor_Start                     E      0.0 BOOL     Start pushbutton line 3
127,Speed_SP                        MW    20   INT      Speed setpoint in rpm
128,M1_Pump_Fault                   M     10.3 BOOL     Pump 1 fault
129,Recipe_No                       DB10.DBW   0 INT    Active recipe number
130,Temp_Act                        MD    24   REAL     Actual temperature
"""

TIA_CSV = """Name,Path,Data Type,Logical Address,Comment
Motor_Start,Default tag table,Bool,%I0.0,Start pushbutton
Speed_SP,Default tag table,Int,%MW20,Speed setpoint
Temp_Act,Default tag table,Real,%MD24,Actual temperature
Valve/Open,Default tag table,Bool,%Q1.3,Valve open command
Cycle_Time,Default tag table,Time,%MD30,Cycle duration
"""


def build_xlsx(rows: list[list[str]]) -> bytes:
    """A minimal TIA-shaped workbook, written with the standard library only."""
    shared: list[str] = []
    index: dict[str, int] = {}

    def sid(value: str) -> int:
        if value not in index:
            index[value] = len(shared)
            shared.append(value)
        return index[value]

    body = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(ord("A") + column)}{row_index}" t="s"><v>{sid(value)}</v></c>'
            for column, value in enumerate(row)
        )
        body.append(f'<row r="{row_index}">{cells}</row>')
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = (
        f'<?xml version="1.0"?><worksheet xmlns="{namespace}">'
        f'<sheetData>{"".join(body)}</sheetData></worksheet>'
    )
    strings = (
        f'<?xml version="1.0"?><sst xmlns="{namespace}" count="{len(shared)}">'
        + "".join(f"<si><t>{s}</t></si>" for s in shared)
        + "</sst>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/sharedStrings.xml", strings)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


class TestSdf:
    def test_reads_every_usable_row(self) -> None:
        result = parse_sdf(SDF_EXPORT)
        names = {e.name for e in result.entries}
        assert names == {
            "Motor_Start", "Motor_Stop", "Motor_Run", "Speed_SP",
            "Temp_Act", "Recipe_No", "Part_Name",
        }

    def test_german_mnemonics_map_to_english_areas(self) -> None:
        entries = {e.name: e for e in parse_sdf(SDF_EXPORT).entries}
        assert entries["Motor_Start"].area_type == AreaType.INPUT   # E -> I
        assert entries["Motor_Run"].area_type == AreaType.OUTPUT    # A -> Q

    def test_comments_are_kept(self) -> None:
        entries = {e.name: e for e in parse_sdf(SDF_EXPORT).entries}
        assert entries["Motor_Start"].comment == "Start pushbutton line 3"

    def test_declared_type_beats_the_address_form(self) -> None:
        """MW20 implies WORD, but the table says INT - the table wins."""
        entries = {e.name: e for e in parse_sdf(SDF_EXPORT).entries}
        assert entries["Speed_SP"].data_type == "INT"

    def test_string_length_is_carried_through(self) -> None:
        entries = {e.name: e for e in parse_sdf(SDF_EXPORT).entries}
        assert entries["Part_Name"].data_type == "STRING"
        assert entries["Part_Name"].length == 20

    def test_data_block_addresses(self) -> None:
        entries = {e.name: e for e in parse_sdf(SDF_EXPORT).entries}
        recipe = entries["Recipe_No"]
        assert (recipe.area_type, recipe.db_number, recipe.byte_offset) == (AreaType.DB, 10, 0)

    def test_timers_and_block_names_are_skipped_with_a_reason(self) -> None:
        result = parse_sdf(SDF_EXPORT)
        reasons = {s.name: s.reason for s in result.skipped}
        assert "timers are not mirrored" in reasons["Cycle_Timer"]
        assert "names a block" in reasons["Data_Recipes"]


class TestAsc:
    def test_fixed_width_columns(self) -> None:
        entries = {e.name: e for e in parse_asc(ASC_EXPORT).entries}
        assert len(entries) == 5
        assert entries["Speed_SP"].byte_offset == 20
        assert entries["Temp_Act"].data_type == "REAL"

    def test_a_name_that_looks_like_an_address_is_not_mistaken_for_one(self) -> None:
        """'M1_Pump_Fault' must not parse as merker byte 1."""
        entries = {e.name: e for e in parse_asc(ASC_EXPORT).entries}
        fault = entries["M1_Pump_Fault"]
        assert (fault.area_type, fault.byte_offset, fault.bit_offset) == (AreaType.MERKER, 10, 3)

    def test_comments_survive_the_column_split(self) -> None:
        entries = {e.name: e for e in parse_asc(ASC_EXPORT).entries}
        assert entries["Motor_Start"].comment == "Start pushbutton line 3"

    def test_leading_ordinal_is_dropped(self) -> None:
        assert all(not e.name[0].isdigit() for e in parse_asc(ASC_EXPORT).entries)


class TestDelimited:
    def test_tia_csv(self) -> None:
        result = parse_delimited(TIA_CSV)
        assert len(result.entries) == 5

    def test_illegal_characters_in_a_name_are_replaced_and_reported(self) -> None:
        entries = {e.name: e for e in parse_delimited(TIA_CSV).entries}
        assert "Valve_Open" in entries
        assert entries["Valve_Open"].original_name == "Valve/Open"
        assert entries["Valve_Open"].renamed

    def test_a_raw_view_carries_an_honest_note(self) -> None:
        entries = {e.name: e for e in parse_delimited(TIA_CSV).entries}
        cycle = entries["Cycle_Time"]
        assert cycle.data_type == "DINT"
        assert "milliseconds" in cycle.note
        assert "milliseconds" in str(cycle.as_tag_values()["description"])

    def test_semicolon_separated_is_handled(self) -> None:
        text = TIA_CSV.replace(",", ";")
        assert len(parse_delimited(text).entries) == 5

    def test_missing_header_is_an_error_with_advice(self) -> None:
        with pytest.raises(SymbolImportError, match="no header row"):
            parse_delimited("a,b,c\n1,2,3\n")


class TestXlsx:
    HEADER = ["Name", "Path", "Data Type", "Logical Address", "Comment"]

    def test_reads_a_tag_table(self) -> None:
        data = build_xlsx(
            [
                self.HEADER,
                ["Motor_Run", "Default tag table", "Bool", "%Q4.0", "Motor contactor"],
                ["Level_mm", "Default tag table", "Int", "%MW100", "Tank level"],
            ]
        )
        entries = {e.name: e for e in parse_xlsx(data).entries}
        assert entries["Motor_Run"].bit_offset == 0
        assert entries["Motor_Run"].area_type == AreaType.OUTPUT
        assert entries["Level_mm"].byte_offset == 100

    def test_a_bad_row_is_skipped_not_fatal(self) -> None:
        data = build_xlsx(
            [
                self.HEADER,
                ["Good", "t", "Bool", "%Q4.0", ""],
                ["Bad", "t", "Bool", "not-an-address", ""],
            ]
        )
        result = parse_xlsx(data)
        assert len(result.entries) == 1 and len(result.skipped) == 1
        assert "not a recognised S7 address" in result.skipped[0].reason

    def test_a_non_zip_is_refused_clearly(self) -> None:
        with pytest.raises(SymbolImportError, match="bad zip archive"):
            parse_xlsx(b"this is not a workbook")

    def test_a_workbook_without_headers_is_refused(self) -> None:
        with pytest.raises(SymbolImportError, match="no header row"):
            parse_xlsx(build_xlsx([["a", "b"], ["1", "2"]]))


class TestDispatcher:
    def test_extension_chooses_the_reader(self) -> None:
        assert "sdf" in parse_symbol_export("plc.sdf", SDF_EXPORT.encode()).format
        assert "asc" in parse_symbol_export("plc.asc", ASC_EXPORT.encode()).format
        assert "delimited" in parse_symbol_export("plc.csv", TIA_CSV.encode()).format

    def test_content_is_sniffed_when_the_extension_is_unknown(self) -> None:
        assert "sdf" in parse_symbol_export("plc.dat", SDF_EXPORT.encode()).format
        assert "delimited" in parse_symbol_export("plc.dat", TIA_CSV.encode()).format

    def test_xlsx_is_detected_from_the_zip_magic(self) -> None:
        data = build_xlsx([TestXlsx.HEADER, ["A", "t", "Bool", "%Q0.0", ""]])
        assert "xlsx" in parse_symbol_export("no-extension", data).format

    def test_windows_1252_export_decodes(self) -> None:
        text = '"Temp_C","MD    24","REAL","Temperatur in \xb0C"\n'
        result = parse_symbol_export("plc.sdf", text.encode("cp1252"))
        assert "°C" in result.entries[0].comment

    def test_empty_file_is_refused(self) -> None:
        with pytest.raises(SymbolImportError, match="empty"):
            parse_symbol_export("plc.sdf", b"")

    def test_oversized_file_is_refused(self) -> None:
        with pytest.raises(SymbolImportError, match="limit"):
            parse_symbol_export("plc.sdf", b"x" * (17 * 1024 * 1024))


class TestSanitizeName:
    @pytest.mark.parametrize(
        "raw,expected",
        [("Motor_Start", "Motor_Start"), ("Valve/Open", "Valve_Open"),
         ('"Quoted"', "Quoted"), ("Läge#1", "L_ge_1"), ("", "Tag")],
    )
    def test_characters(self, raw: str, expected: str) -> None:
        assert sanitize_name(raw) == expected

    def test_length_is_capped(self) -> None:
        assert len(sanitize_name("x" * 200)) == 64

    def test_collisions_get_a_suffix(self) -> None:
        taken: set[str] = set()
        assert sanitize_name("Speed", taken=taken) == "Speed"
        assert sanitize_name("Speed", taken=taken) == "Speed_2"
        assert sanitize_name("Speed", taken=taken) == "Speed_3"

    def test_a_leading_symbol_is_prefixed(self) -> None:
        assert sanitize_name("_hidden").startswith("T")


class TestImportPlanning:
    def _connection(self, db):
        return make_connection(db)

    def test_a_clean_file_is_all_creates(self, db) -> None:
        connection = self._connection(db)
        plan = plan_import(db, connection.id, parse_sdf(SDF_EXPORT))
        assert plan.creates == 7
        assert plan.rejects == 0
        assert len(plan.file_skipped) == 2
        assert not plan.applied

    def test_planning_writes_nothing(self, db) -> None:
        connection = self._connection(db)
        plan_import(db, connection.id, parse_sdf(SDF_EXPORT))
        assert db.list_tags(connection.id) == []

    def test_apply_creates_the_tags(self, db) -> None:
        connection = self._connection(db)
        plan = plan_import(db, connection.id, parse_sdf(SDF_EXPORT))
        apply_import(db, connection, plan, actor="tester")
        tags = {t.name: t for t in db.list_tags(connection.id)}
        assert len(tags) == 7
        assert tags["Speed_SP"].address == "MW20"
        assert tags["Temp_Act"].address == "MD24"
        assert tags["Recipe_No"].address == "DB10.DBW0"
        assert tags["Motor_Start"].description == "Start pushbutton line 3"

    def test_import_is_recorded_in_the_audit_trail(self, db) -> None:
        connection = self._connection(db)
        plan = plan_import(db, connection.id, parse_sdf(SDF_EXPORT))
        apply_import(db, connection, plan, actor="tester", ip="10.0.0.9")
        entry = db.list_audit()[0]
        assert entry.action == "tags.import"
        assert entry.username == "tester"
        assert "created" in (entry.detail or "")

    def test_re_importing_changes_nothing_by_default(self, db) -> None:
        connection = self._connection(db)
        first = plan_import(db, connection.id, parse_sdf(SDF_EXPORT))
        apply_import(db, connection, first, actor="tester")

        second = plan_import(db, connection.id, parse_sdf(SDF_EXPORT))
        assert second.creates == 0
        assert second.skips == 7
        assert "already exists" in second.of_action("skip")[0].reason

    def test_overwrite_only_updates_what_actually_changed(self, db) -> None:
        connection = self._connection(db)
        apply_import(
            db, connection, plan_import(db, connection.id, parse_sdf(SDF_EXPORT)), actor="t"
        )
        changed = SDF_EXPORT.replace('"MW    20","INT"', '"MW    30","INT"')
        plan = plan_import(db, connection.id, parse_sdf(changed), overwrite=True)
        assert plan.updates == 1
        assert plan.skips == 6
        apply_import(db, connection, plan, actor="t")
        tags = {t.name: t for t in db.list_tags(connection.id)}
        assert tags["Speed_SP"].byte_offset == 30

    def test_duplicate_names_inside_one_file_are_rejected(self, db) -> None:
        connection = self._connection(db)
        text = '"Speed","MW    20","INT",""\n"Speed","MW    22","INT",""\n'
        plan = plan_import(db, connection.id, parse_sdf(text))
        # The parser renames the second to Speed_2, so both are importable and
        # neither silently overwrites the other.
        assert plan.creates == 2
        assert {p.entry.name for p in plan.of_action("create")} == {"Speed", "Speed_2"}

    def test_entries_are_validated_with_the_same_rules_as_the_form(self, db) -> None:
        connection = self._connection(db)
        plan = plan_import(db, connection.id, parse_sdf(SDF_EXPORT))
        for item in plan.planned:
            values = item.entry.as_tag_values()
            assert values["data_type"]
            assert isinstance(values["byte_offset"], int)

    def test_the_import_size_limit_is_reported(self, db) -> None:
        connection = self._connection(db)
        rows = "".join(
            f'"Tag_{i}","MW    {i * 2}","INT",""\n' for i in range(MAX_IMPORT_TAGS + 10)
        )
        plan = plan_import(db, connection.id, parse_sdf(rows))
        assert plan.writes > MAX_IMPORT_TAGS
        assert any("limit for one import" in e for e in plan.errors)
        with pytest.raises(ValueError, match="Refusing|refusing"):
            apply_import(db, connection, plan, actor="t")

    def test_a_failing_row_does_not_abort_the_rest(self, db) -> None:
        connection = self._connection(db)
        plan = plan_import(db, connection.id, parse_sdf(SDF_EXPORT))
        # Take the name of one planned row before applying, so the insert collides.
        db.create_tag(
            connection.id,
            {"name": plan.of_action("create")[0].entry.name, "area_type": "M",
             "db_number": 0, "byte_offset": 0, "bit_offset": 0, "data_type": "BYTE",
             "length": 1, "description": None},
        )
        apply_import(db, connection, plan, actor="t")
        assert len(db.list_tags(connection.id)) == 7  # 6 imported + the pre-existing one
        assert plan.rejects == 1
        assert "database rejected" in plan.of_action("reject")[0].reason
