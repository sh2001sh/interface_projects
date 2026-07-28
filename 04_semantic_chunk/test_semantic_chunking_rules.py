from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
API_APP_PATH = PROJECT_ROOT / "app.py"


def _load_api_module():
    project_root_text = str(PROJECT_ROOT)
    if project_root_text not in sys.path:
        sys.path.insert(0, project_root_text)
    spec = importlib.util.spec_from_file_location("interface_project_04_semantic_chunk_app", API_APP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {API_APP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _block(module, block_id: int, page_num: int, content: str, block_type: str = "text", metadata=None):
    metadata = metadata or {}
    return module.Block(
        block_id=block_id,
        project_id="proj_test",
        file_name="demo.pdf",
        page_num=page_num,
        content=content,
        cleaned_content=content,
        block_type=block_type,
        page_range=metadata.get("merged_pages"),
        metadata=metadata or {},
    )


class SemanticChunkRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_api_module()

    def test_rule_chunking_merges_same_protocol_layout_without_literal_title_match(self):
        blocks = [
            _block(
                self.module,
                1,
                10,
                "WORD MAP\nWORD NUMBER: J12.0\nFIELD_A bit range 1->VALUE_A",
                metadata={"protocol": "J12.0"},
            ),
            _block(
                self.module,
                2,
                11,
                "WORD DESCRIPTION\nTITLE: Mission Assignment\nFIELD_A formula = VALUE_A * 2",
                metadata={"word_number": "J12.0C1"},
            ),
        ]

        chunks = self.module.rule_semantic_chunking(blocks, max_token_size=1024, use_llm_fallback=False)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["block_ids"], [1, 2])

    def test_rule_chunking_splits_when_structure_switches_and_evidence_drops(self):
        blocks = [
            _block(
                self.module,
                1,
                20,
                "WORD MAP\nWORD NUMBER: J12.0\nFIELD_A -> TARGET_A\nFIELD_B -> TARGET_B",
                metadata={"protocol": "J12.0"},
            ),
            _block(
                self.module,
                2,
                21,
                "General overview paragraph about operational context and narrative background.",
            ),
        ]

        chunks = self.module.rule_semantic_chunking(blocks, max_token_size=1024, use_llm_fallback=False)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["block_ids"], [1])
        self.assertEqual(chunks[1]["block_ids"], [2])

    def test_rule_chunking_drops_low_value_text_fragments(self):
        blocks = [
            _block(self.module, 1, 1, "WORD MAP\nWORD NUMBER: J12.0\nFIELD_A -> TARGET_A"),
            _block(self.module, 2, 1, ":\n:\n--->"),
            _block(self.module, 3, 2, "WORD DESCRIPTION\nWORD NUMBER: J12.0\nFIELD_A formula = SRC_A * 2"),
        ]

        chunks = self.module.rule_semantic_chunking(blocks, max_token_size=1024, use_llm_fallback=False)
        chunk_block_ids = [block_id for chunk in chunks for block_id in chunk["block_ids"]]

        self.assertIn(1, chunk_block_ids)
        self.assertIn(3, chunk_block_ids)
        self.assertNotIn(2, chunk_block_ids)

    def test_rule_chunking_keeps_pdf_table_blocks_independent(self):
        blocks = [
            _block(
                self.module,
                1,
                18,
                "FIELD CODING FOR J12.0C1 (SHEET 1)\nDFI | DUI | DUI/DI NAME",
                block_type="table",
                metadata={"parser": "docling_pdf_layout", "merged_pages": [18, 19]},
            ),
            _block(
                self.module,
                2,
                20,
                "WORD MAP: J12.0C2 TARGET DATA CONTINUATION WORD\nWORD NUMBER: J12.0C2",
                block_type="table",
                metadata={"parser": "docling_pdf_layout"},
            ),
        ]

        chunks = self.module.rule_semantic_chunking(blocks, max_token_size=4096, use_llm_fallback=False)

        self.assertEqual([chunk["block_ids"] for chunk in chunks], [[1], [2]])

    def test_semantic_split_prefers_weak_boundary_over_token_limit_only(self):
        blocks = [
            _block(self.module, 1, 1, "WORD MAP\nWORD NUMBER: J12.0\nFIELD_A -> TARGET_A\n" + ("A" * 210)),
            _block(self.module, 2, 2, "WORD DESCRIPTION\nWORD NUMBER: J12.0\nFIELD_A formula = SRC_A * 2\n" + ("B" * 210)),
            _block(self.module, 3, 5, "Narrative appendix text unrelated to the protocol field mapping.\n" + ("C" * 420)),
        ]

        chunks = self.module.semantic_split_chunk(blocks, max_token_size=260, semantic_type="field_definition")

        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["block_ids"], [1, 2])
        self.assertEqual(chunks[1]["block_ids"], [3])

    def test_block_serialization_exposes_page_range_for_merged_tables(self):
        block = _block(
            self.module,
            40497,
            8,
            "TABLE A",
            block_type="table",
            metadata={"merged_pages": [8, 9, 10], "end_page": 10},
        )

        payload = self.module._serialize_block(block)

        self.assertEqual(payload["page_num"], 8)
        self.assertEqual(payload["page_range"], [8, 9, 10])

    def test_block_serialization_keeps_single_page_range_for_regular_blocks(self):
        block = _block(self.module, 40498, 16, "TABLE B", block_type="table")

        payload = self.module._serialize_block(block)

        self.assertEqual(payload["page_num"], 16)
        self.assertEqual(payload["page_range"], [16])


if __name__ == "__main__":
    unittest.main()
