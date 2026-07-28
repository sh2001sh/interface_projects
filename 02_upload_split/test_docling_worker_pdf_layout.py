from __future__ import annotations

import unittest
from pathlib import Path

from docling_worker import (
    _drop_residual_table_fragments,
    _infer_layout_split_index,
    _merge_short_text_fragments_into_cross_page_tables,
    _split_layout_lines_by_index,
    _table_rows_to_blocks,
    process_document_with_pages,
)


PROJECT_ROOT = Path(__file__).resolve().parent


class DoclingWorkerPdfLayoutTests(unittest.TestCase):
    def test_layout_split_index_separates_side_by_side_text_lines(self) -> None:
        lines = [
            "J13.2C3 (Continued)             J13.2C4 (Continued)",
            "SEARCH LIGHT STATUS          1  EA FREQUENCY BAND J          2",
            "LOW LIGHT LEVEL TELEVISION   1  EA FREQUENCY BAND K          2",
            "DIFAR/CODAR STATUS           1  EA FREQUENCY BAND M          2",
        ]

        split_index = _infer_layout_split_index(lines)

        self.assertIsNotNone(split_index)
        left_lines, right_lines = _split_layout_lines_by_index(lines, int(split_index))
        self.assertTrue(left_lines[0].startswith("J13.2C3"))
        self.assertTrue(right_lines[0].startswith("J13.2C4"))
        self.assertIn("SEARCH LIGHT STATUS", left_lines[1])
        self.assertIn("EA FREQUENCY BAND J", right_lines[1])

    def test_layout_split_index_keeps_left_restart_marker_with_left_column(self) -> None:
        lines = [
            "          MODE IV INTERROGATOR STATUS  1",
            "                      J13.2C4             COMPUTER STATUS              3",
            "          DATA ELEMENT__________________# BITS CONSOLE STATUS          5",
            "          WORD FORMAT                  2  SPARE                       35",
        ]

        split_index = _infer_layout_split_index(lines)

        self.assertIsNotNone(split_index)
        left_lines, right_lines = _split_layout_lines_by_index(lines, int(split_index))
        self.assertIn("J13.2C4", left_lines)
        self.assertNotIn("J13.2C4", right_lines)
        self.assertIn("COMPUTER STATUS 3", right_lines)
        self.assertIn("DATA ELEMENT__________________#", left_lines)
        self.assertIn("BITS CONSOLE STATUS 5", right_lines)

    def test_j28_field_coding_pages_are_merged_as_table_block(self) -> None:
        sample_path = PROJECT_ROOT.parent / "04_semantic_chunk" / "runtime" / "tmp" / "side_by_side_tables" / "j28_3870_3876.pdf"
        if not sample_path.exists():
            self.skipTest(f"missing fixture: {sample_path}")

        result = process_document_with_pages(str(sample_path), page_batch_size=20)
        blocks = result["blocks"]
        first_table = next(block for block in blocks if block.get("type") == "table")
        metadata = first_table.get("metadata") or {}

        self.assertEqual(first_table["page_num"], 1)
        self.assertEqual(metadata.get("merged_pages"), [1, 2, 3])
        self.assertEqual(metadata.get("row_count"), 29)
        self.assertIn("FIELD CODING FOR J28.2(0)I", first_table["content"])
        self.assertNotIn("FIELD CODING FOR J28.2(0)I    (SHEET   2) DFI", first_table["content"])

    def test_drop_residual_table_fragments_removes_short_text_under_merged_table(self) -> None:
        blocks = [
            {
                "page_num": 6,
                "type": "table",
                "content": "FIELD CODING FOR J12.0I (SHEET 1)",
                "metadata": {"merged_pages": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15]},
                "order": 62,
            },
            {
                "page_num": 8,
                "type": "text",
                "content": "10011 M",
                "metadata": {"page_layout": "single"},
                "order": 156,
            },
            {
                "page_num": 20,
                "type": "table",
                "content": "WORD MAP: J12.0C2 TARGET DATA CONTINUATION WORD",
                "metadata": {"merged_pages": [20]},
                "order": 276,
            },
            {
                "page_num": 20,
                "type": "text",
                "content": ": : SPARE : : :",
                "metadata": {"page_layout": "single"},
                "order": 295,
            },
        ]

        filtered = _drop_residual_table_fragments(blocks)

        self.assertEqual(len(filtered), 2)
        self.assertTrue(all(block["type"] == "table" for block in filtered))

    def test_drop_residual_table_fragments_keeps_normal_summary_text(self) -> None:
        blocks = [
            {
                "page_num": 2,
                "type": "text",
                "content": "J12.0C6 DATA ELEMENT__________________# BITS WORD FORMAT 2 CONTINUATION WORD LABEL 5 LASER ILLUMINATOR CODE 16",
                "metadata": {"page_layout": "single"},
                "order": 8,
            }
        ]

        filtered = _drop_residual_table_fragments(blocks)

        self.assertEqual(filtered, blocks)

    def test_drop_residual_table_fragments_removes_short_parenthetical_annotation_under_table(self) -> None:
        blocks = [
            {
                "page_num": 17,
                "type": "table",
                "content": "WORD NUMBER: J12.0C1",
                "metadata": {"merged_pages": [17]},
                "order": 238,
            },
            {
                "page_num": 17,
                "type": "text",
                "content": "(# DMPIS)",
                "metadata": {"page_layout": "single"},
                "order": 230,
            },
        ]

        filtered = _drop_residual_table_fragments(blocks)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["type"], "table")

    def test_merge_short_text_fragments_absorbs_duplicate_title_and_table_tail(self) -> None:
        blocks = [
            {
                "page_num": 5,
                "type": "text",
                "content": "WORD NUMBER: J12.0I",
                "metadata": {"page_layout": "single"},
                "order": 18,
            },
            {
                "page_num": 5,
                "type": "table",
                "content": "WORD NUMBER: J12.0I\nREFERENCE DFI/DUI",
                "metadata": {
                    "title": "WORD NUMBER: J12.0I",
                    "title_candidates": ["WORD NUMBER: J12.0I", "----------------"],
                    "merged_pages": [5],
                },
                "order": 19,
            },
            {
                "page_num": 5,
                "type": "text",
                "content": "COMPLIANCE (RRN R/C)",
                "metadata": {"page_layout": "single"},
                "order": 20,
            },
        ]

        merged = _merge_short_text_fragments_into_cross_page_tables(blocks)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["type"], "table")
        self.assertTrue(merged[0]["content"].startswith("WORD NUMBER: J12.0I"))
        self.assertTrue(merged[0]["content"].endswith("COMPLIANCE (RRN R/C)"))
        absorbed = (merged[0].get("metadata") or {}).get("absorbed_text_fragments") or []
        self.assertEqual([item["position"] for item in absorbed], ["prefix", "suffix"])

    def test_merge_short_text_fragments_preserves_non_table_section_titles(self) -> None:
        blocks = [
            {
                "page_num": 1,
                "type": "text",
                "content": "PURPOSE",
                "metadata": {"page_layout": "single"},
                "order": 1,
            },
            {
                "page_num": 1,
                "type": "text",
                "content": "The J12.0 Mission Assignment Message is used by C2 JUs.",
                "metadata": {"page_layout": "single"},
                "order": 2,
            },
            {
                "page_num": 1,
                "type": "table",
                "content": "DATA ELEMENT SUMMARY\nWORD FORMAT | 2",
                "metadata": {"title": "DATA ELEMENT SUMMARY", "merged_pages": [1]},
                "order": 3,
            },
        ]

        merged = _merge_short_text_fragments_into_cross_page_tables(blocks)

        self.assertEqual([block["type"] for block in merged], ["text", "text", "table"])
        self.assertEqual(merged[0]["content"], "PURPOSE")

    def test_merge_short_text_fragments_absorbs_cross_page_tail_fragment_on_covered_page(self) -> None:
        blocks = [
            {
                "page_num": 6,
                "type": "table",
                "content": "FIELD CODING FOR J12.0I (SHEET 1)\nDFI | DUI | DUI/DI NAME",
                "metadata": {
                    "title": "FIELD CODING FOR J12.0I (SHEET 1)",
                    "merged_pages": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
                    "merged_cross_page": True,
                },
                "order": 17,
            },
            {
                "page_num": 15,
                "type": "text",
                "content": "POWER.",
                "metadata": {"page_layout": "single"},
                "order": 18,
            },
            {
                "page_num": 16,
                "type": "table",
                "content": "WORD MAP: J12.0C1 TARGET POSITION CONTINUATION WORD",
                "metadata": {"title": "WORD MAP: J12.0C1 TARGET POSITION CONTINUATION WORD", "merged_pages": [16]},
                "order": 19,
            },
        ]

        merged = _merge_short_text_fragments_into_cross_page_tables(blocks)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["type"], "table")
        self.assertTrue(merged[0]["content"].endswith("POWER."))
        absorbed = (merged[0].get("metadata") or {}).get("absorbed_text_fragments") or []
        self.assertEqual(absorbed[-1]["content"], "POWER.")

    def test_cross_page_table_sample_stays_one_table_block(self) -> None:
        sample_path = Path("/nfs/615/tmp/pdf_regression_samples/cross-page-table-5308-5311.pdf")
        if not sample_path.exists():
            self.skipTest(f"missing fixture: {sample_path}")

        result = process_document_with_pages(str(sample_path), page_batch_size=20)
        blocks = result["blocks"]

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "table")
        self.assertEqual((blocks[0].get("metadata") or {}).get("merged_pages"), [1, 2, 3, 4])
        self.assertEqual((blocks[0].get("metadata") or {}).get("row_count"), 115)

    def test_table_rows_to_blocks_splits_side_by_side_sections_for_xlsx(self) -> None:
        rows = [
            ["J12.0I", "J12.0I", "J12.0C1", "J12.0C1"],
            ["DATA ELEMENT", "# BITS", "DATA ELEMENT", "# BITS"],
            ["WORD FORMAT", "2", "WORD FORMAT", "2"],
            ["LABEL, J-SERIES", "5", "CONTINUATION WORD LABEL", "5"],
            ["J12.0C2", "J12.0C2", "J12.0C3", "J12.0C3"],
            ["DATA ELEMENT", "# BITS", "DATA ELEMENT", "# BITS"],
            ["WORD FORMAT", "2", "WORD FORMAT", "2"],
        ]

        blocks = _table_rows_to_blocks(
            rows,
            page_num=1,
            block_type="table",
            metadata={"sheet_name": "Sheet1", "sheet_index": 1, "parser": "docling"},
            order=1,
        )

        self.assertEqual([block["type"] for block in blocks], ["table", "table", "table", "table"])
        self.assertEqual([(block.get("metadata") or {}).get("column_role") for block in blocks], ["left", "left", "right", "right"])
        self.assertTrue(blocks[0]["content"].splitlines()[0].startswith("J12.0I"))
        self.assertTrue(blocks[1]["content"].splitlines()[0].startswith("J12.0C2"))
        self.assertTrue(blocks[2]["content"].splitlines()[0].startswith("J12.0C1"))
        self.assertTrue(blocks[3]["content"].splitlines()[0].startswith("J12.0C3"))

    def test_table_rows_to_blocks_keeps_short_heading_as_text_for_xlsx(self) -> None:
        rows = [["PURPOSE", "PURPOSE", "PURPOSE", "PURPOSE"]]

        blocks = _table_rows_to_blocks(
            rows,
            page_num=1,
            block_type="table",
            metadata={"sheet_name": "Sheet1", "sheet_index": 1, "parser": "docling"},
            order=1,
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[0]["content"], "PURPOSE")


if __name__ == "__main__":
    unittest.main()
