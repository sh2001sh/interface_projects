from __future__ import annotations

import unittest
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from document_parsing import (
    ParsedElement,
    _extract_embedded_title_row,
    _merge_cross_page_tables,
    _promote_text_tables,
    _reorder_reading_order,
    _strip_page_furniture,
    _supplement_native_layout_tables,
    _suppress_duplicate_table_text,
)
from document_parsing_pdf import _title_candidate_score


class DocumentParsingTests(unittest.TestCase):
    def test_title_candidate_score_prefers_structured_section_title_over_long_paragraph(self) -> None:
        paragraph_score = _title_candidate_score(
            "The J12.0 Mission Assignment Message is used by C2 JUs to assign missions and designate targets.",
            source="neighbor",
            item_top_ratio=0.15,
            item_center_ratio=0.50,
            table_top_ratio=0.25,
            table_center_ratio=0.50,
        )
        section_score = _title_candidate_score(
            "DATA ELEMENT SUMMARY",
            source="neighbor",
            item_top_ratio=0.22,
            item_center_ratio=0.22,
            table_top_ratio=0.25,
            table_center_ratio=0.48,
        )

        self.assertGreater(section_score, paragraph_score)

    def test_strip_page_furniture_removes_repeated_headers_and_page_numbers(self) -> None:
        elements = [
            ParsedElement(kind="text", page_num=1, text="MIL-STD-6016D\nAPPENDIX M\nM.1.2 Body", top_ratio=0.03, bottom_ratio=0.80),
            ParsedElement(kind="text", page_num=1, text="M-62", top_ratio=0.90, bottom_ratio=0.03),
            ParsedElement(kind="text", page_num=2, text="MIL-STD-6016D\nAPPENDIX M\nM.1.3 Body", top_ratio=0.03, bottom_ratio=0.80),
            ParsedElement(kind="text", page_num=2, text="M-63", top_ratio=0.90, bottom_ratio=0.03),
        ]

        cleaned = _strip_page_furniture(elements)

        self.assertEqual([item.text for item in cleaned], ["M.1.2 Body", "M.1.3 Body"])

    def test_strip_page_furniture_removes_sheet_appendix_header(self) -> None:
        elements = [
            ParsedElement(kind="text", page_num=7051, text="(Sheet 3 of 4) APPENDIX C\nBody paragraph", top_ratio=0.04, bottom_ratio=0.70),
        ]

        cleaned = _strip_page_furniture(elements)

        self.assertEqual([item.text for item in cleaned], ["Body paragraph"])

    def test_strip_page_furniture_keeps_table_title_like_header(self) -> None:
        elements = [
            ParsedElement(
                kind="text",
                page_num=2,
                text="TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 1 of 3)",
                top_ratio=0.06,
                bottom_ratio=0.84,
            )
        ]

        cleaned = _strip_page_furniture(elements)

        self.assertEqual([item.text for item in cleaned], ["TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 1 of 3)"])

    def test_strip_page_furniture_removes_variant_page_footer_by_canonical_repeat(self) -> None:
        elements = [
            ParsedElement(kind="text", page_num=10, text="Body line one", top_ratio=0.18, bottom_ratio=0.60),
            ParsedElement(kind="text", page_num=10, text="Page 10 of 400", top_ratio=0.94, bottom_ratio=0.02),
            ParsedElement(kind="text", page_num=11, text="Body line two", top_ratio=0.18, bottom_ratio=0.60),
            ParsedElement(kind="text", page_num=11, text="Page 11 of 400", top_ratio=0.94, bottom_ratio=0.02),
            ParsedElement(kind="text", page_num=12, text="Body line three", top_ratio=0.18, bottom_ratio=0.60),
            ParsedElement(kind="text", page_num=12, text="Page 12 of 400", top_ratio=0.94, bottom_ratio=0.02),
        ]

        cleaned = _strip_page_furniture(elements)

        self.assertEqual([item.text for item in cleaned], ["Body line one", "Body line two", "Body line three"])

    def test_strip_page_furniture_removes_blank_page_and_punctuation_noise(self) -> None:
        elements = [
            ParsedElement(kind="text", page_num=1, text="--------------------- THIS PAGE INTENTIONALLY LEFT BLANK", top_ratio=0.42, bottom_ratio=0.45),
            ParsedElement(kind="text", page_num=2, text=":\n:\n--->", top_ratio=0.40, bottom_ratio=0.42),
            ParsedElement(kind="text", page_num=3, text="PURPOSE\nThe message purpose is defined by the protocol.", top_ratio=0.20, bottom_ratio=0.50),
        ]

        cleaned = _strip_page_furniture(elements)

        self.assertEqual([item.text for item in cleaned], ["PURPOSE\nThe message purpose is defined by the protocol."])

    def test_reorder_reading_order_splits_repeated_section_heads_within_single_column_block(self) -> None:
        elements = [
            ParsedElement(
                kind="text",
                page_num=2,
                text=(
                    "J13.2C4 (Continued)\n"
                    "BITS DATA ELEMENT__________________# BITS\n"
                    "EA FREQUENCY BAND J 2\n"
                    "EA FREQUENCY BAND K 2\n"
                    "J13.2C5\n"
                    "DATA ELEMENT__________________# BITS\n"
                    "WORD FORMAT 2\n"
                    "LINK 14 STATUS 2\n"
                    "J13.2C6\n"
                    "DATA ELEMENT__________________# BITS\n"
                    "WORD FORMAT 2\n"
                    "LINK 16 CONTROL STATUS 2"
                ),
                top_ratio=0.18,
                bottom_ratio=0.14,
                left_ratio=0.56,
                right_ratio=0.90,
                center_ratio=0.73,
                width_ratio=0.34,
                height_ratio=0.68,
                column_role="right",
                metadata={"page_layout": "double", "column_role": "right"},
            )
        ]

        reordered = _reorder_reading_order(elements)

        self.assertEqual(len(reordered), 3)
        self.assertEqual([item.column_role for item in reordered], ["right", "right", "right"])
        self.assertTrue(reordered[0].text.startswith("J13.2C4"))
        self.assertTrue(reordered[1].text.startswith("J13.2C5"))
        self.assertTrue(reordered[2].text.startswith("J13.2C6"))
        self.assertTrue(all(item.metadata.get("split_from_column_text") for item in reordered))

    def test_reorder_reading_order_keeps_left_column_restart_separate_from_right_column_continuation(self) -> None:
        elements = [
            ParsedElement(
                kind="text",
                page_num=2,
                text=(
                    "J13.2C3 (Continued)\n"
                    "DATA ELEMENT__________________#\n"
                    "RECORDER STATUS, 4 1\n"
                    "SONAR STATUS 1\n"
                    "J13.2C4\n"
                    "DATA ELEMENT__________________# BITS\n"
                    "WORD FORMAT 2\n"
                    "ES FREQUENCY BAND A 2"
                ),
                top_ratio=0.18,
                bottom_ratio=0.14,
                left_ratio=0.10,
                right_ratio=0.44,
                center_ratio=0.27,
                width_ratio=0.34,
                height_ratio=0.68,
                column_role="left",
                metadata={"page_layout": "mixed", "column_role": "left"},
            ),
            ParsedElement(
                kind="text",
                page_num=2,
                text=(
                    "J13.2C4 (Continued)\n"
                    "BITS DATA ELEMENT__________________# BITS\n"
                    "EA FREQUENCY BAND J 2\n"
                    "J13.2C5\n"
                    "DATA ELEMENT__________________# BITS\n"
                    "MODE IV INTERROGATOR STATUS 1\n"
                    "COMPUTER STATUS 3\n"
                    "CONSOLE STATUS 5\n"
                    "J13.2C6\n"
                    "DATA ELEMENT__________________# BITS\n"
                    "LINK 16 CONTROL STATUS 2"
                ),
                top_ratio=0.18,
                bottom_ratio=0.14,
                left_ratio=0.56,
                right_ratio=0.90,
                center_ratio=0.73,
                width_ratio=0.34,
                height_ratio=0.68,
                column_role="right",
                metadata={"page_layout": "mixed", "column_role": "right"},
            ),
        ]

        reordered = _reorder_reading_order(elements)

        self.assertEqual(len(reordered), 5)
        self.assertEqual([item.column_role for item in reordered], ["left", "left", "right", "right", "right"])
        self.assertTrue(reordered[0].text.startswith("J13.2C3"))
        self.assertTrue(reordered[1].text.startswith("J13.2C4\nDATA ELEMENT"))
        self.assertTrue(reordered[2].text.startswith("J13.2C4 (Continued)"))
        self.assertTrue(reordered[3].text.startswith("J13.2C5"))
        self.assertIn("COMPUTER STATUS 3", reordered[3].text)
        self.assertTrue(reordered[4].text.startswith("J13.2C6"))

    def test_reorder_reading_order_splits_independent_tables_within_same_column(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="DATA J12.0I",
                rows=[
                    ["DATA", "J12.0I"],
                    ["ELEMENT__________________# WORD FORMAT", "BITS"],
                    ["LABEL, J-SERIES", "5"],
                    ["J12.0C1 DATA", "ELEMENT__________________# BITS"],
                    ["WORD FORMAT", "2"],
                    ["CONTINUATION WORD LABEL", "5"],
                ],
                top_ratio=0.20,
                bottom_ratio=0.20,
                left_ratio=0.08,
                right_ratio=0.44,
                center_ratio=0.26,
                width_ratio=0.36,
                height_ratio=0.60,
                column_role="left",
                metadata={"column_role": "left"},
            ),
            ParsedElement(
                kind="table",
                page_num=1,
                text="J12.0C2 (Continued) ELEMENT__________________# BITS",
                rows=[
                    ["J12.0C2", "(Continued)", "ELEMENT__________________# BITS"],
                    ["DATA 2", "SURFACE SPECIFIC TYPE(12) *", ""],
                    ["", "J12.0C3", ""],
                    ["", "DATA ELEMENT__________________#", "BITS"],
                    ["", "WORD FORMAT", "2"],
                    ["", "J12.0C4", ""],
                    ["DATA", "ELEMENT__________________#", "BITS"],
                    ["", "WORD FORMAT", "2"],
                ],
                top_ratio=0.20,
                bottom_ratio=0.20,
                left_ratio=0.56,
                right_ratio=0.92,
                center_ratio=0.74,
                width_ratio=0.36,
                height_ratio=0.60,
                column_role="right",
                metadata={"column_role": "right"},
            ),
        ]

        reordered = _reorder_reading_order(elements)

        tables = [item for item in reordered if item.kind == "table"]
        self.assertEqual(len(tables), 5)
        self.assertEqual([item.column_role for item in tables], ["left", "left", "right", "right", "right"])
        self.assertTrue(tables[0].text.startswith("DATA J12.0I"))
        self.assertTrue(tables[1].text.startswith("J12.0C1"))
        self.assertTrue(tables[2].text.startswith("J12.0C2"))
        self.assertTrue(tables[3].text.startswith("J12.0C3"))
        self.assertTrue(tables[4].text.startswith("J12.0C4"))

    def test_merge_cross_page_tables_merges_same_table_and_dedupes_header(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=2,
                text="TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 1 of 3)",
                rows=[
                    ["Mission Assignment", "Value", "Transmit Table"],
                    ["Engage", "5", "Table 5-4-J12.0-1"],
                ],
                metadata={"title": "TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 1 of 3)", "col_count": 3, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=3,
                text="TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 2 of 3)",
                rows=[
                    ["Mission Assignment", "Value", "Transmit Table"],
                    ["Attack", "41", "Table 5-4-J12.0-3"],
                ],
                metadata={"title": "TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 2 of 3)", "col_count": 3, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0].metadata["merged_cross_page"])
        self.assertEqual(merged[0].metadata["merged_pages"], [2, 3])
        self.assertEqual(merged[0].metadata["end_page"], 3)
        self.assertEqual(
            merged[0].rows,
            [
                ["Mission Assignment", "Value", "Transmit Table"],
                ["Engage", "5", "Table 5-4-J12.0-1"],
                ["Attack", "41", "Table 5-4-J12.0-3"],
            ],
        )

    def test_merge_cross_page_tables_merges_same_table_across_missing_intermediate_sheet(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=6,
                text="FIELD CODING FOR J12.0I (SHEET 1)",
                rows=[["A", "B"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 1)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=7,
                text="FIELD CODING FOR J12.0I (SHEET 2)",
                rows=[["C", "D"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 2)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=8,
                text="FIELD CODING FOR J12.0I (SHEET 3)",
                rows=[["E", "F"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 3)", "col_count": 2, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables([elements[0], elements[2]])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata["merged_pages"], [6, 8])
        self.assertEqual(merged[0].metadata["end_page"], 8)

    def test_merge_cross_page_tables_merges_same_title_fragments_on_same_page(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=8,
                text="FIELD CODING FOR J12.0I (SHEET 3)",
                rows=[["A", "B"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 3)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=8,
                text="FIELD CODING FOR J12.0I (SHEET 3)",
                rows=[["C", "D"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 3)", "col_count": 2, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata["merged_pages"], [8])
        self.assertEqual(merged[0].metadata["end_page"], 8)

    def test_merge_cross_page_tables_merges_long_sheet_sequence(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=6,
                text="FIELD CODING FOR J12.0I (SHEET 1)",
                rows=[["S1"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 1)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=7,
                text="FIELD CODING FOR J12.0I (SHEET 2)",
                rows=[["S2"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 2)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=8,
                text="FIELD CODING FOR J12.0I (SHEET 3)",
                rows=[["S3"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 3)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=9,
                text="FIELD CODING FOR J12.0I (SHEET 4)",
                rows=[["S4"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 4)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=10,
                text="FIELD CODING FOR J12.0I (SHEET 5)",
                rows=[["S5"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 5)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=11,
                text="FIELD CODING FOR J12.0I (SHEET 6)",
                rows=[["S6"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 6)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=12,
                text="FIELD CODING FOR J12.0I (SHEET 7)",
                rows=[["S7"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 7)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=13,
                text="FIELD CODING FOR J12.0I (SHEET 8)",
                rows=[["S8"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 8)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=14,
                text="FIELD CODING FOR J12.0I (SHEET 9)",
                rows=[["S9"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 9)", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=15,
                text="FIELD CODING FOR J12.0I (SHEET 10)",
                rows=[["S10"]],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 10)", "col_count": 2, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata["merged_pages"], [6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
        self.assertEqual(merged[0].metadata["end_page"], 15)

    def test_merge_cross_page_tables_does_not_merge_unrelated_tables(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=10,
                text="TABLE A.1",
                rows=[["A", "B"], ["1", "2"]],
                metadata={"title": "TABLE A.1", "col_count": 2, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=11,
                text="TABLE A.2",
                rows=[["A", "B"], ["3", "4"]],
                metadata={"title": "TABLE A.2", "col_count": 2, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 2)
        self.assertTrue(all(item.metadata["merged_cross_page"] is False for item in merged))

    def test_merge_cross_page_tables_merges_full_sheet_sequence(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=5308,
                text="TABLE A.4-9-J3.0-R. C2 Space Surveillance Function (Sheet 1 of 4)",
                rows=[["H1", "H2", "H3"], ["r1", "a", "b"]],
                metadata={"title": "TABLE A.4-9-J3.0-R. C2 Space Surveillance Function (Sheet 1 of 4)", "col_count": 3, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=5309,
                text="TABLE A.4-9-J3.0-R. C2 Space Surveillance Function (Sheet 2 of 4)",
                rows=[["H1", "H2", "H3"], ["r2", "c", "d"]],
                metadata={"title": "TABLE A.4-9-J3.0-R. C2 Space Surveillance Function (Sheet 2 of 4)", "col_count": 3, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=5310,
                text="TABLE A.4-9-J3.0-R. C2 Space Surveillance Function (Sheet 3 of 4)",
                rows=[["H1", "H2", "H3"], ["r3", "e", "f"]],
                metadata={"title": "TABLE A.4-9-J3.0-R. C2 Space Surveillance Function (Sheet 3 of 4)", "col_count": 3, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=5311,
                text="TABLE A.4-9-J3.0-R. C2 Space Surveillance Function (Sheet 4 of 4)",
                rows=[["H1", "H2", "H3"], ["r4", "g", "h"]],
                metadata={"title": "TABLE A.4-9-J3.0-R. C2 Space Surveillance Function (Sheet 4 of 4)", "col_count": 3, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata["merged_pages"], [5308, 5309, 5310, 5311])
        self.assertEqual(
            merged[0].rows,
            [
                ["H1", "H2", "H3"],
                ["r1", "a", "b"],
                ["r2", "c", "d"],
                ["r3", "e", "f"],
                ["r4", "g", "h"],
            ],
        )

    def test_merge_cross_page_tables_inherits_title_when_second_page_is_weak(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=2051,
                text="TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 1 of 2)",
                rows=[["H1", "H2", "H3"], ["r1", "a", "b"]],
                metadata={"title": "TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 1 of 2)", "col_count": 3, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=2052,
                text="------------------------",
                rows=[["H1", "H2", "H3"], ["r2", "c", "d"]],
                metadata={"title": "------------------------", "col_count": 3, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0].text,
            "TABLE M.1.2-1. J12.0 Mission Assignment Transmit Tables (Sheet 1 of 2)",
        )
        self.assertEqual(merged[0].metadata["merged_pages"], [2051, 2052])

    def test_merge_cross_page_tables_uses_schema_and_geometry_when_second_title_is_generic(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=101,
                text="FIELD CODING FOR J10.2I (SHEET 9)",
                rows=[
                    ["DFI", "DUI", "NAME", "CODE"],
                    ["394", "009", "WEAPON ENGAGEMENT STATUS", "(CONTINUED)"],
                ],
                top_ratio=0.22,
                bottom_ratio=0.04,
                left_ratio=0.10,
                right_ratio=0.90,
                center_ratio=0.50,
                width_ratio=0.80,
                metadata={"title": "FIELD CODING FOR J10.2I (SHEET 9)", "col_count": 4, "header_row_count": 1},
            ),
            ParsedElement(
                kind="table",
                page_num=102,
                text="CONTINUED",
                rows=[
                    ["DFI", "DUI", "NAME", "CODE"],
                    ["395", "010", "WEAPON CONTROL ORDER", "1111"],
                ],
                top_ratio=0.05,
                bottom_ratio=0.18,
                left_ratio=0.11,
                right_ratio=0.89,
                center_ratio=0.50,
                width_ratio=0.78,
                metadata={"title": "CONTINUED", "col_count": 4, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata["merged_pages"], [101, 102])
        self.assertEqual(merged[0].rows[1], ["394", "009", "WEAPON ENGAGEMENT STATUS", "(CONTINUED)"])
        self.assertEqual(merged[0].rows[2], ["395", "010", "WEAPON CONTROL ORDER", "1111"])

    def test_merge_cross_page_tables_handles_embedded_row_title_and_sheet_number_without_of(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="FIELD CODING FOR J10.2I (SHEET 9)",
                rows=[
                    ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"],
                    ["394", "009", "WEAPON ENGAGEMENT STATUS", "(CONTINUED)", ""],
                ],
                metadata={"title": "FIELD CODING FOR J10.2I (SHEET 9)", "col_count": 5, "header_row_count": 0},
            ),
            ParsedElement(
                kind="table",
                page_num=2,
                text="FIELD CODING FOR J10.2I (SHEET 10)",
                rows=[
                    ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"],
                    ["394", "009", "WEAPON ENGAGEMENT STATUS", "(CONTINUED)", ""],
                ],
                metadata={"title": "FIELD CODING FOR J10.2I (SHEET 10)", "col_count": 5, "header_row_count": 0},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata["merged_pages"], [1, 2])
        self.assertEqual(merged[0].metadata["title"], "FIELD CODING FOR J10.2I (SHEET 9)")
        self.assertEqual(merged[0].metadata["header_row_count"], 1)
        self.assertEqual(merged[0].rows[0], ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"])

    def test_merge_cross_page_tables_skips_short_bridge_text_between_table_segments(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="FIELD CODING FOR J10.2I (SHEET 9)",
                rows=[
                    ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"],
                    ["394", "009", "WEAPON ENGAGEMENT STATUS", "(CONTINUED)", ""],
                ],
                metadata={"title": "FIELD CODING FOR J10.2I (SHEET 9)", "col_count": 5, "header_row_count": 1},
            ),
            ParsedElement(kind="text", page_num=1, text="THE TARGET UNTIL ORDERED OR THE", metadata={"label": "text"}, top_ratio=0.91, bottom_ratio=0.03),
            ParsedElement(
                kind="table",
                page_num=2,
                text="FIELD CODING FOR J10.2I (SHEET 10)",
                rows=[
                    ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"],
                    ["394", "009", "WEAPON ENGAGEMENT STATUS", "(CONTINUED)", ""],
                ],
                metadata={"title": "FIELD CODING FOR J10.2I (SHEET 10)", "col_count": 5, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].kind, "table")
        self.assertEqual(merged[0].metadata["merged_pages"], [1, 2])
        self.assertEqual(merged[1].kind, "text")
        self.assertEqual(merged[1].text, "THE TARGET UNTIL ORDERED OR THE")

    def test_merge_cross_page_tables_does_not_merge_local_region_table(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=8,
                text="FIELD CODING FOR J12.0I (SHEET 3)",
                rows=[["CODING", "CHARACTER"], ["00000", "0"]],
                metadata={
                    "title": "FIELD CODING FOR J12.0I (SHEET 3)",
                    "col_count": 2,
                    "header_row_count": 1,
                    "region_role": "local_right",
                },
            ),
            ParsedElement(
                kind="table",
                page_num=9,
                text="FIELD CODING FOR J12.0I (SHEET 4)",
                rows=[
                    ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"],
                    ["769", "010", "TRACK NUMBER, OBJECTIVE", "(CONTINUED)", ""],
                ],
                metadata={"title": "FIELD CODING FOR J12.0I (SHEET 4)", "col_count": 5, "header_row_count": 1},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].metadata["merged_pages"], [8])
        self.assertEqual(merged[1].metadata["merged_pages"], [9])

    def test_merge_cross_page_tables_does_not_merge_different_column_roles(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=20,
                text="TABLE A.1 (Sheet 1 of 2)",
                rows=[["H1", "H2"], ["left-1", "x"]],
                column_role="left",
                metadata={"title": "TABLE A.1 (Sheet 1 of 2)", "col_count": 2, "header_row_count": 1, "column_role": "left"},
            ),
            ParsedElement(
                kind="table",
                page_num=21,
                text="TABLE A.1 (Sheet 2 of 2)",
                rows=[["H1", "H2"], ["right-1", "y"]],
                column_role="right",
                metadata={"title": "TABLE A.1 (Sheet 2 of 2)", "col_count": 2, "header_row_count": 1, "column_role": "right"},
            ),
        ]

        merged = _merge_cross_page_tables(elements)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].metadata["merged_pages"], [20])
        self.assertEqual(merged[1].metadata["merged_pages"], [21])

    def test_suppress_duplicate_table_text_removes_text_inside_table_region(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="FIELD CODING FOR J28.2(0)I (SHEET 1)",
                rows=[
                    ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE"],
                    ["1850", "001", "TEXT MESSAGE", "0"],
                ],
                top_ratio=0.16,
                bottom_ratio=0.08,
                left_ratio=0.08,
                right_ratio=0.92,
                metadata={"title": "FIELD CODING FOR J28.2(0)I (SHEET 1)"},
            ),
            ParsedElement(
                kind="text",
                page_num=1,
                text="TEXT MESSAGE 0",
                top_ratio=0.32,
                bottom_ratio=0.62,
                left_ratio=0.44,
                right_ratio=0.58,
            ),
        ]

        cleaned = _suppress_duplicate_table_text(elements)

        self.assertEqual([item.kind for item in cleaned], ["table"])

    def test_suppress_duplicate_table_text_removes_short_header_fragment(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="WORD NUMBER: J28.2(0)E0",
                rows=[
                    ["REFERENCE DFI/DUI", "BIT POSITION", "# BITS"],
                    ["1851", "003", "CHARACTER #3"],
                ],
                top_ratio=0.26,
                bottom_ratio=0.20,
                left_ratio=0.22,
                right_ratio=0.73,
                metadata={"title": "WORD NUMBER: J28.2(0)E0"},
            ),
            ParsedElement(
                kind="text",
                page_num=1,
                text="REFERENCE BIT #",
                top_ratio=0.27,
                bottom_ratio=0.70,
                left_ratio=0.09,
                right_ratio=0.55,
            ),
        ]

        cleaned = _suppress_duplicate_table_text(elements)

        self.assertEqual([item.kind for item in cleaned], ["table"])

    def test_suppress_duplicate_table_text_keeps_unrelated_text_outside_table_region(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="WORD DESCRIPTION",
                rows=[["WORD", "BIT", "DESCRIPTION"], ["1", "0", "MESSAGE ID"]],
                top_ratio=0.20,
                bottom_ratio=0.15,
                left_ratio=0.10,
                right_ratio=0.90,
                metadata={"title": "WORD DESCRIPTION"},
            ),
            ParsedElement(
                kind="text",
                page_num=1,
                text="Operational notes for this message remain outside the table.",
                top_ratio=0.78,
                bottom_ratio=0.10,
                left_ratio=0.12,
                right_ratio=0.88,
            ),
        ]

        cleaned = _suppress_duplicate_table_text(elements)

        self.assertEqual([item.text for item in cleaned], ["WORD DESCRIPTION", "Operational notes for this message remain outside the table."])

    def test_suppress_duplicate_table_text_keeps_table_title_near_table(self) -> None:
        elements = [
            ParsedElement(
                kind="text",
                page_num=1,
                text="TABLE A.4-9-J3.0-R. C2 Space Surveillance Function",
                top_ratio=0.10,
                bottom_ratio=0.86,
                left_ratio=0.18,
                right_ratio=0.82,
            ),
            ParsedElement(
                kind="table",
                page_num=1,
                text="",
                rows=[["H1", "H2"], ["C2 Space Surveillance Function", "Value"]],
                top_ratio=0.15,
                bottom_ratio=0.20,
                left_ratio=0.10,
                right_ratio=0.90,
                metadata={"title": ""},
            ),
        ]

        cleaned = _suppress_duplicate_table_text(elements)

        self.assertEqual([item.kind for item in cleaned], ["text", "table"])

    def test_promote_text_tables_converts_field_coding_text_to_table(self) -> None:
        elements = [
            ParsedElement(
                kind="text",
                page_num=2,
                text=(
                    "FIELD CODING FOR J28.2(0)I (SHEET 2) "
                    "DFI DUI DUI/DI NAME DI BIT CODE DUI/DI EXPLANATION "
                    "1851 001 CHARACTER #1 (CONTINUED) ------------ GENERAL APPLICATION USE. "
                    "1700 001 OPERATOR/WARFARE AREA SPECIFIES THE INTENDED INTERNAL CONTROLLER."
                ),
                top_ratio=0.15,
                bottom_ratio=0.10,
                left_ratio=0.09,
                right_ratio=0.82,
                metadata={"label": "text"},
            )
        ]

        promoted = _promote_text_tables(elements)

        self.assertEqual(promoted[0].kind, "table")
        self.assertEqual(promoted[0].text, "FIELD CODING FOR J28.2(0)I (SHEET 2)")
        self.assertTrue(promoted[0].metadata["promoted_from_text"])
        self.assertEqual(promoted[0].rows[0], ["DFI", "DUI", "DUI/DI NAME", "DI BIT CODE", "DUI/DI EXPLANATION"])
        self.assertEqual(promoted[0].rows[1][:3], ["1851", "001", "CHARACTER #1 (CONTINUED)"])
        self.assertEqual(promoted[0].rows[2][:3], ["1700", "001", "OPERATOR/WARFARE AREA"])

    def test_extract_embedded_title_row_accepts_split_field_coding_title_cells(self) -> None:
        rows = [
            ["", "", "", "FIELD CODING", "FOR", "J12.0I", "(SHEET 4)", ""],
            ["DFI", "DUI", "DUI/DI NAME", "DI BIT", "CODE", "", "DUI/DI EXPLANATION", ""],
            ["769", "010", "TRACK NUMBER, OBJECTIVE", "(CONTINUED)", "", "", "", ""],
        ]

        title, remaining_rows = _extract_embedded_title_row(rows)

        self.assertEqual(title, "FIELD CODING FOR J12.0I (SHEET 4)")
        self.assertEqual(remaining_rows[0], rows[1])

    def test_promote_text_tables_keeps_regular_text(self) -> None:
        elements = [
            ParsedElement(
                kind="text",
                page_num=1,
                text="This paragraph mentions DFI and DUI but does not contain a structured table header.",
            )
        ]

        promoted = _promote_text_tables(elements)

        self.assertEqual(promoted[0].kind, "text")
        self.assertEqual(promoted[0].text, elements[0].text)

    def test_supplement_native_layout_tables_replaces_missed_field_coding_text(self) -> None:
        sample_path = PROJECT_ROOT / "runtime" / "tmp" / "side_by_side_tables" / "j28_3870_3876.pdf"
        if not sample_path.exists():
            self.skipTest(f"missing fixture: {sample_path}")
        elements = [
            ParsedElement(
                kind="text",
                page_num=2,
                text="FIELD CODING FOR J28.2(0)I (SHEET 2) DFI DUI DUI/DI NAME DI BIT CODE DUI/DI EXPLANATION",
                top_ratio=0.15,
                bottom_ratio=0.10,
                left_ratio=0.09,
                right_ratio=0.82,
            )
        ]

        supplemented = _supplement_native_layout_tables(str(sample_path), elements)

        page_two_tables = [item for item in supplemented if item.page_num == 2 and item.kind == "table"]
        page_two_texts = [item for item in supplemented if item.page_num == 2 and item.kind == "text"]
        self.assertEqual(len(page_two_tables), 1)
        self.assertEqual(page_two_texts, [])
        self.assertTrue(page_two_tables[0].metadata["native_text_fallback"])
        self.assertEqual(page_two_tables[0].metadata["native_text_fallback_source"], "field_coding")
        self.assertGreaterEqual(len(page_two_tables[0].rows), 2)

    def test_supplement_native_layout_tables_skips_pages_with_docling_table(self) -> None:
        sample_path = PROJECT_ROOT / "runtime" / "tmp" / "side_by_side_tables" / "j28_3870_3876.pdf"
        if not sample_path.exists():
            self.skipTest(f"missing fixture: {sample_path}")
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="FIELD CODING FOR J28.2(0)I (SHEET 1)",
                rows=[["DFI", "DUI"], ["1743", "001"]],
                top_ratio=0.16,
                bottom_ratio=0.08,
                left_ratio=0.17,
                right_ratio=0.83,
            )
        ]

        supplemented = _supplement_native_layout_tables(str(sample_path), elements)

        page_one_tables = [item for item in supplemented if item.page_num == 1 and item.kind == "table"]
        self.assertEqual(len(page_one_tables), 1)
        self.assertFalse(page_one_tables[0].metadata.get("native_text_fallback", False))

    def test_reorder_reading_order_prefers_left_column_before_right_column(self) -> None:
        elements = [
            ParsedElement(kind="text", page_num=1, text="Right top", top_ratio=0.10, left_ratio=0.56, right_ratio=0.88, center_ratio=0.72, width_ratio=0.32),
            ParsedElement(kind="text", page_num=1, text="Left bottom", top_ratio=0.22, left_ratio=0.10, right_ratio=0.42, center_ratio=0.26, width_ratio=0.32),
            ParsedElement(kind="text", page_num=1, text="Left top", top_ratio=0.08, left_ratio=0.10, right_ratio=0.42, center_ratio=0.26, width_ratio=0.32),
            ParsedElement(kind="text", page_num=1, text="Right bottom", top_ratio=0.24, left_ratio=0.56, right_ratio=0.88, center_ratio=0.72, width_ratio=0.32),
        ]

        reordered = _reorder_reading_order(elements)

        self.assertEqual([item.text for item in reordered], ["Left top", "Left bottom", "Right top", "Right bottom"])

    def test_reorder_reading_order_keeps_single_column_top_to_bottom(self) -> None:
        elements = [
            ParsedElement(kind="text", page_num=1, text="Section 2", top_ratio=0.30, left_ratio=0.12, right_ratio=0.82, center_ratio=0.47, width_ratio=0.70),
            ParsedElement(kind="text", page_num=1, text="Section 1", top_ratio=0.10, left_ratio=0.12, right_ratio=0.82, center_ratio=0.47, width_ratio=0.70),
            ParsedElement(kind="text", page_num=1, text="Section 3", top_ratio=0.50, left_ratio=0.12, right_ratio=0.82, center_ratio=0.47, width_ratio=0.70),
        ]

        reordered = _reorder_reading_order(elements)

        self.assertEqual([item.text for item in reordered], ["Section 1", "Section 2", "Section 3"])

    def test_reorder_reading_order_handles_mixed_layout_with_full_width_anchor(self) -> None:
        elements = [
            ParsedElement(kind="text", page_num=1, text="Intro full width", top_ratio=0.04, left_ratio=0.08, right_ratio=0.92, center_ratio=0.50, width_ratio=0.84),
            ParsedElement(kind="text", page_num=1, text="Right col 1", top_ratio=0.16, left_ratio=0.58, right_ratio=0.88, center_ratio=0.73, width_ratio=0.30),
            ParsedElement(kind="text", page_num=1, text="Left col 1", top_ratio=0.14, left_ratio=0.10, right_ratio=0.42, center_ratio=0.26, width_ratio=0.32),
            ParsedElement(kind="text", page_num=1, text="Left col 2", top_ratio=0.25, left_ratio=0.10, right_ratio=0.42, center_ratio=0.26, width_ratio=0.32),
            ParsedElement(kind="text", page_num=1, text="Right col 2", top_ratio=0.27, left_ratio=0.58, right_ratio=0.88, center_ratio=0.73, width_ratio=0.30),
            ParsedElement(kind="text", page_num=1, text="Closing full width", top_ratio=0.40, left_ratio=0.08, right_ratio=0.92, center_ratio=0.50, width_ratio=0.84),
        ]

        reordered = _reorder_reading_order(elements)

        self.assertEqual(
            [item.text for item in reordered],
            ["Intro full width", "Left col 1", "Left col 2", "Right col 1", "Right col 2", "Closing full width"],
        )

    def test_reorder_reading_order_marks_side_by_side_tables_as_local_regions(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="Left table",
                rows=[["H1", "H2"], ["L", "1"]],
                top_ratio=0.12,
                bottom_ratio=0.42,
                left_ratio=0.08,
                right_ratio=0.42,
                center_ratio=0.25,
                width_ratio=0.34,
            ),
            ParsedElement(
                kind="table",
                page_num=1,
                text="Right table",
                rows=[["H1", "H2"], ["R", "1"]],
                top_ratio=0.13,
                bottom_ratio=0.41,
                left_ratio=0.58,
                right_ratio=0.90,
                center_ratio=0.74,
                width_ratio=0.32,
            ),
        ]

        reordered = _reorder_reading_order(elements)

        self.assertEqual([item.metadata.get("region_role") for item in reordered], ["local_left", "local_right"])
        self.assertEqual([item.text for item in reordered], ["Left table", "Right table"])

    def test_reorder_reading_order_splits_wide_table_into_left_and_right_tables(self) -> None:
        elements = [
            ParsedElement(
                kind="table",
                page_num=1,
                text="DATA ELEMENT SUMMARY",
                rows=[
                    ["DATA", "J13.2I", "BITS", "J13.2C1", "BITS"],
                    ["WORD", "FORMAT", "2", "AIR SPECIFIC TYPE", "12"],
                    ["LABEL", "J-SERIES", "5", "TYPE OF STORES, 1", "8"],
                ],
                top_ratio=0.20,
                bottom_ratio=0.15,
                left_ratio=0.12,
                right_ratio=0.86,
                center_ratio=0.49,
                width_ratio=0.74,
                height_ratio=0.55,
                metadata={"title": "DATA ELEMENT SUMMARY", "col_count": 5, "row_count": 3, "header_row_count": 1},
            ),
        ]

        reordered = _reorder_reading_order(elements)

        self.assertEqual(len(reordered), 2)
        self.assertEqual([item.column_role for item in reordered], ["left", "right"])
        self.assertEqual([item.metadata.get("region_role") for item in reordered], ["local_left", "local_right"])
        self.assertTrue(all(item.metadata.get("split_from_wide_table") for item in reordered))

    def test_reorder_reading_order_splits_wide_code_block_into_columns(self) -> None:
        elements = [
            ParsedElement(
                kind="text",
                page_num=2,
                text=(
                    "J13.2C3 (Continued)        J13.2C4 (Continued)\n"
                    "SEARCH LIGHT STATUS 1      EA FREQUENCY BAND J 2\n"
                    "LOW LIGHT LEVEL TV 1       EA FREQUENCY BAND K 2\n"
                    "DIFAR/CODAR STATUS 1       EA FREQUENCY BAND L 2\n"
                ),
                top_ratio=0.15,
                bottom_ratio=0.12,
                left_ratio=0.12,
                right_ratio=0.86,
                center_ratio=0.49,
                width_ratio=0.74,
                height_ratio=0.50,
                label="code",
                metadata={"label": "code"},
            ),
        ]

        reordered = _reorder_reading_order(elements)

        self.assertEqual(len(reordered), 2)
        self.assertEqual([item.column_role for item in reordered], ["left", "right"])
        self.assertTrue(reordered[0].text.startswith("J13.2C3"))
        self.assertTrue(reordered[1].text.startswith("J13.2C4"))
        self.assertTrue(all(item.metadata.get("split_from_wide_text") for item in reordered))


if __name__ == "__main__":
    unittest.main()
