import sys
import unittest
from unittest import mock


sys.path.insert(0, "/nfs/615/interface_projects/05_generate_qa")

import app  # noqa: E402


class GenerateQAAllocationTests(unittest.TestCase):
    def test_allocate_unit_target_counts_preserves_total_budget(self) -> None:
        allocations = app._allocate_unit_target_counts(
            [
                {"unit_id": "u1", "allocation_weight": 4.5, "minimum_target": 1},
                {"unit_id": "u2", "allocation_weight": 1.2, "minimum_target": 1},
                {"unit_id": "u3", "allocation_weight": 0.8, "minimum_target": 1},
            ],
            average_count=3,
        )

        self.assertEqual(sum(allocations.values()), 9)
        self.assertGreater(allocations["u1"], allocations["u2"])
        self.assertGreaterEqual(allocations["u2"], allocations["u3"])

    def test_allocate_unit_target_counts_honors_requirement_minimum(self) -> None:
        allocations = app._allocate_unit_target_counts(
            [
                {"unit_id": "requirement", "allocation_weight": 2.5, "minimum_target": 2},
                {"unit_id": "normal", "allocation_weight": 1.0, "minimum_target": 1},
            ],
            average_count=1,
        )

        self.assertEqual(sum(allocations.values()), 3)
        self.assertGreaterEqual(allocations["requirement"], 2)
        self.assertGreaterEqual(allocations["requirement"], allocations["normal"])

    def test_allocate_unit_target_counts_respects_supported_capacity(self) -> None:
        allocations = app._allocate_unit_target_counts(
            [
                {"unit_id": "rich", "allocation_weight": 5.0, "minimum_target": 1, "supported_capacity": 6},
                {"unit_id": "thin", "allocation_weight": 4.0, "minimum_target": 1, "supported_capacity": 1},
                {"unit_id": "mid", "allocation_weight": 2.0, "minimum_target": 1, "supported_capacity": 3},
            ],
            average_count=3,
        )

        self.assertEqual(sum(allocations.values()), 9)
        self.assertLessEqual(allocations["thin"], 1)
        self.assertLessEqual(allocations["mid"], 3)
        self.assertGreater(allocations["rich"], allocations["thin"])

    def test_resolve_unit_target_window_allows_over_average_compensation(self) -> None:
        soft_target, max_target = app._resolve_unit_target_window(
            planned_target=3,
            remaining_total=8,
            remaining_units=2,
            minimum_target=1,
        )

        self.assertEqual(soft_target, 3)
        self.assertGreater(max_target, soft_target)
        self.assertLessEqual(max_target, 8)

    def test_resolve_unit_target_window_shrinks_after_overproduction(self) -> None:
        soft_target, max_target = app._resolve_unit_target_window(
            planned_target=3,
            remaining_total=2,
            remaining_units=2,
            minimum_target=1,
        )

        self.assertEqual(soft_target, 2)
        self.assertEqual(max_target, 2)

    def test_estimate_unit_generation_weight_prefers_richer_content(self) -> None:
        rich_content = (
            "FIELD A bits 1-8 range 0-255 resolution 0.5 unit knots\n"
            "FIELD B bits 9-16 value = source * 2"
        )
        sparse_content = "Overview text only."

        rich_plan = app._build_chunk_generation_plan(
            content=rich_content,
            count=3,
            requested_task_types=["protocol_understanding", "protocol_conversion"],
            requested_conversion_modes=["transcoding", "mapping"],
        )
        sparse_plan = app._build_chunk_generation_plan(
            content=sparse_content,
            count=3,
            requested_task_types=["protocol_understanding", "protocol_conversion"],
            requested_conversion_modes=["transcoding", "mapping"],
        )

        rich_weight = app._estimate_unit_generation_weight(
            rich_content,
            rich_plan,
            is_requirement_chunk=False,
        )
        sparse_weight = app._estimate_unit_generation_weight(
            sparse_content,
            sparse_plan,
            is_requirement_chunk=False,
        )

        self.assertGreater(rich_weight, sparse_weight)

    def test_field_coding_context_supports_enum_style_blocks(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 1)\n"
            "DFI | DUI | DUI/DI NAME | DI BIT CODE | DUI/DI EXPLANATION\n"
            "1626 | 001 | MISSION ASSIGNMENT DISCRETE SPECIFIES WHAT MISSION IS TO BE |  | "
            "PERFORMED. NO STATEMENT 0 REFUEL 1 ORBIT 2 ENGAGE 5"
        )

        context = app.build_field_context(content)

        self.assertIn("MISSION ASSIGNMENT DISCRETE", context)
        self.assertIn("NO STATEMENT 0", context["MISSION ASSIGNMENT DISCRETE"]["details"])

    def test_short_understanding_mapping_answer_is_low_quality(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 1)\n"
            "DFI | DUI | DUI/DI NAME | DI BIT CODE | DUI/DI EXPLANATION\n"
            "1626 | 001 | MISSION ASSIGNMENT DISCRETE SPECIFIES WHAT MISSION IS TO BE |  | "
            "PERFORMED. NO STATEMENT 0 REFUEL 1 ORBIT 2 ENGAGE 5"
        )
        context = app.build_field_context(content)

        self.assertTrue(
            app._is_too_short_understanding_answer(
                "MISSION ASSIGNMENT DISCRETE字段的值如何映射？",
                "MISSION ASSIGNMENT DISCRETE位段1。",
                context,
            )
        )

    def test_sanitize_custom_prompt_text_drops_conflicting_bias(self) -> None:
        text = "你是一个协议文档专家，专注于生成协议转换类问答对。答案只能输出公式。"
        self.assertEqual(app._sanitize_custom_prompt_text(text, ["protocol_understanding"]), "")
        self.assertEqual(app._sanitize_custom_prompt_text(text, ["protocol_understanding", "protocol_conversion"]), "")

    def test_field_coding_plan_prefers_enum_questions(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 1)\n"
            "DFI | DUI | DUI/DI NAME | DI BIT CODE | DUI/DI EXPLANATION\n"
            "1626 | 001 | MISSION ASSIGNMENT DISCRETE SPECIFIES WHAT MISSION IS TO BE |  | "
            "PERFORMED. NO STATEMENT 0 REFUEL 1 ORBIT 2 ENGAGE 5"
        )

        plan = app._build_chunk_generation_plan(
            content=content,
            count=4,
            requested_task_types=["protocol_understanding", "protocol_conversion"],
            requested_conversion_modes=["transcoding", "mapping"],
        )

        self.assertIn("MISSION ASSIGNMENT DISCRETE", plan["enum_candidates"])

    def test_low_value_question_rejects_where_it_appears_pattern(self) -> None:
        self.assertTrue(
            app._is_low_value_understanding_question(
                "LASER ILLUMINATOR CODE在哪个数据元素中出现？",
                "LASER ILLUMINATOR CODE位段16。",
            )
        )

    def test_reference_layout_contexts_are_extracted(self) -> None:
        content = (
            "WORD NUMBER: J12.0I\n"
            "REFERENCE DFI/DUI | REFERENCE DFI/DUI | DATA FIELD DESCRIPTOR | BIT POSITION | # BITS | RESOLUTION, CODING, ETC\n"
            "769 | 006 TRACK NUMBER, ADDRESSEE 13-27 | 15 |\n"
            "1626 | 001 MISSION ASSIGNMENT DISCRETE 28-33 | 6 |"
        )

        context = app.build_field_context(content)

        self.assertIn("TRACK NUMBER, ADDRESSEE", context)
        self.assertIn("MISSION ASSIGNMENT DISCRETE", context)

    def test_reference_layout_keeps_full_multiline_field_names(self) -> None:
        content = (
            "WORD NUMBER: J12.0I\n"
            "REFERENCE DFI/DUI | REFERENCE DFI/DUI | DATA FIELD DESCRIPTOR | BIT POSITION | # BITS | RESOLUTION, CODING, ETC\n"
            "769 | 010 TRACK NUMBER, | OBJECTIVE 39- 57 | 19 | |\n"
            "444 | 025 RECURRENCE RATE, | RECEIPT/ 66- 69 | 4 | |\n"
        )

        context = app.build_field_context(content)

        self.assertIn("TRACK NUMBER, OBJECTIVE", context)
        self.assertIn("RECURRENCE RATE, RECEIPT/", context)

    def test_algorithmic_seed_pairs_support_message_summary(self) -> None:
        content = (
            "J12.0 MESSAGE SUMMARY\n\n"
            "PURPOSE\n\n"
            "The J12.0 Mission Assignment Message is used to assign missions to nonC2 JU platforms.\n"
            "DATA ELEMENT SUMMARY"
        )
        plan = app._build_chunk_generation_plan(
            content=content,
            count=3,
            requested_task_types=["protocol_understanding"],
            requested_conversion_modes=[],
        )

        seeds = plan["algorithmic_seed_pairs"]
        self.assertTrue(any("主要用途" in item["question"] for item in seeds))

    def test_word_map_can_produce_seed_pairs(self) -> None:
        content = (
            "WORD MAP: J12.0I MISSION ASSIGNMENT INITIAL WORD\n"
            "WORD NUMBER: J12.0I\n"
            "WORD TITLE: MISSION ASSIGNMENT INITIAL WORD\n"
            ": TRACK NUMBER, ADDRESSEE : LENGTH : LABEL, J-SERIES :FORMAT :\n"
            ": 15 : 3 : 5 : 2 :"
        )
        plan = app._build_chunk_generation_plan(
            content=content,
            count=4,
            requested_task_types=["protocol_understanding"],
            requested_conversion_modes=[],
        )

        self.assertTrue(len(plan["field_context"]) >= 1 or len(plan["algorithmic_seed_pairs"]) >= 1)

    def test_ascii_word_map_extracts_multiple_fields(self) -> None:
        content = (
            "WORD MAP: J12.0I MISSION ASSIGNMENT INITIAL WORD\n"
            "WORD NUMBER: J12.0I\n"
            "WORD TITLE: MISSION ASSIGNMENT INITIAL WORD\n"
            "24 23 22 21 20 19 18 17 16 15 14 13: 12 11 10: 09 08 07: 06 05 04 03 02: 01 00:\n"
            ": MESSAGE : SUBLABEL, : : WORD :\n"
            "TRACK NUMBER, ADDRESSEE : LENGTH : J-SERIES : LABEL, J-SERIES :FORMAT :\n"
            ": INDICATOR : : : :\n"
            "15 : 3 : 3 : 5 : 2 :\n"
        )

        context = app.build_field_context(content)

        self.assertIn("TRACK NUMBER, ADDRESSEE", context)
        self.assertIn("MESSAGE LENGTH INDICATOR", context)
        self.assertIn("LABEL, J-SERIES", context)

    def test_message_length_indicator_no_longer_depends_on_exact_hardcode(self) -> None:
        self.assertTrue(app._looks_like_field_name("MESSAGE LENGTH INDICATOR"))
        self.assertTrue(app._looks_like_field_name("WORD FORMAT INDICATOR"))
        self.assertFalse(app._looks_like_field_name("MESSAGE USE"))

    def test_estimate_unit_supported_capacity_prefers_structured_rich_chunks(self) -> None:
        content = (
            "WORD MAP: J12.0I MISSION ASSIGNMENT INITIAL WORD\n"
            "WORD NUMBER: J12.0I\n"
            "WORD TITLE: MISSION ASSIGNMENT INITIAL WORD\n"
            "24 23 22 21 20 19 18 17 16 15 14 13: 12 11 10: 09 08 07: 06 05 04 03 02: 01 00:\n"
            ": MESSAGE : SUBLABEL, : : WORD :\n"
            "TRACK NUMBER, ADDRESSEE : LENGTH : J-SERIES : LABEL, J-SERIES :FORMAT :\n"
            ": INDICATOR : : : :\n"
            "15 : 3 : 3 : 5 : 2 :\n"
        )

        plan = app._build_chunk_generation_plan(
            content=content,
            count=5,
            requested_task_types=["protocol_understanding"],
            requested_conversion_modes=[],
        )
        capacity = app._estimate_unit_supported_capacity(
            content,
            plan,
            is_requirement_chunk=False,
        )

        self.assertGreaterEqual(capacity, 4)

    def test_simple_field_coding_mapping_rows_can_seed(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 3)\n"
            "CODING | CHARACTER\n"
            "00000 | 0\n"
            "00001 | 1\n"
            "00010 | 2\n"
            "00011 | 3\n"
        )
        plan = app._build_chunk_generation_plan(
            content=content,
            count=3,
            requested_task_types=["protocol_understanding"],
            requested_conversion_modes=[],
        )

        self.assertTrue(any("编码表" in item["question"] for item in plan["algorithmic_seed_pairs"]))

    def test_field_coding_context_filters_enum_value_labels(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 1)\n"
            "DFI | DUI | DUI/DI NAME | DI BIT CODE | DUI/DI EXPLANATION\n"
            "1626 | 001 | MISSION ASSIGNMENT DISCRETE SPECIFIES WHAT MISSION IS TO BE |  | "
            "PERFORMED. NO STATEMENT 0 REFUEL 1 ORBIT 2 RECALL 3"
        )

        context = app.build_field_context(content)

        self.assertIn("MISSION ASSIGNMENT DISCRETE", context)
        self.assertNotIn("REFUEL", context)
        self.assertNotIn("ORBIT", context)
        self.assertNotIn("RECALL", context)

    def test_field_coding_seed_pairs_do_not_generate_enum_value_bit_width_questions(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 1)\n"
            "DFI | DUI | DUI/DI NAME | DI BIT CODE | DUI/DI EXPLANATION\n"
            "1626 | 001 | MISSION ASSIGNMENT DISCRETE SPECIFIES WHAT MISSION IS TO BE |  | "
            "PERFORMED. NO STATEMENT 0 REFUEL 1 ORBIT 2 RECALL 3"
        )

        plan = app._build_chunk_generation_plan(
            content=content,
            count=6,
            requested_task_types=["protocol_understanding", "protocol_conversion"],
            requested_conversion_modes=["transcoding", "mapping"],
        )

        questions = [item["question"] for item in plan["algorithmic_seed_pairs"]]
        self.assertTrue(any("MISSION ASSIGNMENT DISCRETE字段有哪些关键取值及其含义" in q for q in questions))
        self.assertFalse(any("REFUEL字段占用多少位" in q for q in questions))
        self.assertFalse(any("ORBIT字段占用多少位" in q for q in questions))

    def test_pure_field_coding_mapping_chunk_disables_conversion_mode(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 3)\n"
            "CODING | CHARACTER\n"
            "00000 | 0\n"
            "00001 | 1\n"
            "00010 | 2\n"
            "00011 | 3\n"
        )

        plan = app._build_chunk_generation_plan(
            content=content,
            count=4,
            requested_task_types=["protocol_understanding", "protocol_conversion"],
            requested_conversion_modes=["transcoding", "mapping"],
        )

        self.assertEqual(plan["effective_task_types"], ["protocol_understanding"])
        self.assertEqual(plan["effective_conversion_modes"], [])

    def test_rich_field_coding_chunk_is_not_misclassified_as_pure_mapping(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 1)\n"
            "DFI | DUI | DUI/DI NAME | DI BIT CODE | DUI/DI EXPLANATION\n"
            "769 | 006 | TRACK NUMBER, ADDRESSEE THE TN OF A UNIT |  | "
            "TO WHICH A MESSAGE IS ADDRESSED. NO STATEMENT 0 NUMERIC 00001 THROUGH 00076\n"
            "1626 | 001 | MISSION ASSIGNMENT DISCRETE SPECIFIES WHAT MISSION IS TO BE |  | "
            "PERFORMED. NO STATEMENT 0 REFUEL 1 ORBIT 2 RECALL 3"
        )

        plan = app._build_chunk_generation_plan(
            content=content,
            count=5,
            requested_task_types=["protocol_understanding", "protocol_conversion"],
            requested_conversion_modes=["transcoding", "mapping"],
        )

        self.assertTrue(any(item.get("seed_kind") == "enum_mapping" for item in plan["algorithmic_seed_pairs"]))

    def test_enum_mapping_answer_cannot_answer_range_question(self) -> None:
        content = (
            "FIELD CODING FOR J12.0I (SHEET 1)\n"
            "DFI | DUI | DUI/DI NAME | DI BIT CODE | DUI/DI EXPLANATION\n"
            "1626 | 001 | MISSION ASSIGNMENT DISCRETE SPECIFIES WHAT MISSION IS TO BE |  | "
            "PERFORMED. NO STATEMENT 0 REFUEL 1 ORBIT 2 RECALL 3"
        )
        field_context = app.build_field_context(content)
        qa = {
            "question": "MISSION ASSIGNMENT DISCRETE的数值范围是多少？",
            "answer": "映射0=PERFORMED. NO STATEMENT，1=REFUEL，2=ORBIT，3=RECALL。",
            "qa_task_type": "protocol_understanding",
            "seed_kind": "enum_mapping",
            "source_field": "MISSION ASSIGNMENT DISCRETE",
        }

        ok, _reason = app._validate_generated_qa_against_context(
            qa,
            field_context,
            topic_context=[],
            content=content,
        )

        self.assertFalse(ok)

    def test_parse_question_plan_response(self) -> None:
        response = """
        [
          {"question": "MISSION ASSIGNMENT DISCRETE字段有哪些关键取值及其含义？", "qa_task_type": "protocol_understanding", "source_field": "MISSION ASSIGNMENT DISCRETE"},
          {"question": "RECURRENCE RATE, RECEIPT/ COMPLIANCE如何计算N？", "qa_task_type": "protocol_conversion", "conversion_mode": "transcoding"}
        ]
        """

        planned = app._parse_question_plan_response(response)

        self.assertEqual(len(planned), 2)
        self.assertEqual(planned[0]["source_field"], "MISSION ASSIGNMENT DISCRETE")
        self.assertEqual(planned[1]["qa_task_type"], "protocol_conversion")

    def test_generate_answer_for_question_preserves_planned_question(self) -> None:
        llm = mock.Mock()
        llm.generate.return_value = (
            '{"question":"占位问题","answer":"0表示PERFORMED. NO STATEMENT，1表示REFUEL。",'
            '"qa_task_type":"protocol_understanding","source_field":"MISSION ASSIGNMENT DISCRETE"}'
        )

        qa = app._generate_answer_for_question(
            llm,
            "FIELD CODING FOR J12.0I",
            {
                "question": "MISSION ASSIGNMENT DISCRETE字段有哪些关键取值及其含义？",
                "qa_task_type": "protocol_understanding",
                "conversion_mode": None,
                "source_field": "MISSION ASSIGNMENT DISCRETE",
            },
        )

        self.assertIsNotNone(qa)
        self.assertEqual(qa["question"], "MISSION ASSIGNMENT DISCRETE字段有哪些关键取值及其含义？")
        self.assertEqual(qa["source_field"], "MISSION ASSIGNMENT DISCRETE")

    def test_generate_answers_for_questions_preserves_question_plan(self) -> None:
        llm = mock.Mock()
        llm.generate.return_value = (
            '['
            '{"question":"MISSION ASSIGNMENT DISCRETE字段有哪些关键取值及其含义？","answer":"0表示PERFORMED. NO STATEMENT，1表示REFUEL。","qa_task_type":"protocol_understanding"},'
            '{"question":"TRACK NUMBER, ADDRESSEE字段有哪些关键取值及其含义？","answer":"0表示NO STATEMENT，00001表示NUMERIC。","qa_task_type":"protocol_understanding"}'
            ']'
        )

        qas = app._generate_answers_for_questions(
            llm,
            "FIELD CODING FOR J12.0I",
            [
                {
                    "question": "MISSION ASSIGNMENT DISCRETE字段有哪些关键取值及其含义？",
                    "qa_task_type": "protocol_understanding",
                    "conversion_mode": None,
                    "source_field": "MISSION ASSIGNMENT DISCRETE",
                },
                {
                    "question": "TRACK NUMBER, ADDRESSEE字段有哪些关键取值及其含义？",
                    "qa_task_type": "protocol_understanding",
                    "conversion_mode": None,
                    "source_field": "TRACK NUMBER, ADDRESSEE",
                },
            ],
        )

        self.assertEqual(len(qas), 2)
        self.assertEqual(qas[0]["source_field"], "MISSION ASSIGNMENT DISCRETE")
        self.assertEqual(qas[1]["source_field"], "TRACK NUMBER, ADDRESSEE")

    def test_chunk_generation_plan_exposes_diverse_question_types(self) -> None:
        content = (
            "WORD NUMBER: J12.0I\n"
            "REFERENCE DFI/DUI | REFERENCE DFI/DUI | DATA FIELD DESCRIPTOR | BIT POSITION | # BITS | RESOLUTION, CODING, ETC\n"
            "444 | 025 RECURRENCE RATE, RECEIPT/ COMPLIANCE 66-69 | 4 | 0-15 Hz resolution 0.5 Hz\n"
            "769 | 010 TRACK NUMBER, OBJECTIVE 39-57 | 19 | |\n"
            "1626 | 001 MISSION ASSIGNMENT DISCRETE 28-33 | 6 | 0=NO STATEMENT 1=REFUEL 2=ORBIT\n"
        )

        plan = app._build_chunk_generation_plan(
            content=content,
            count=4,
            requested_task_types=["protocol_understanding"],
            requested_conversion_modes=[],
        )

        instruction = plan["instruction"]
        self.assertIn("enum_meaning:", instruction)
        self.assertIn("range:", instruction)
        self.assertIn("unit:", instruction)
        self.assertIn("resolution:", instruction)
        self.assertIn("bit_width:", instruction)
        self.assertIn("位宽类问题不要超过总问题数的一半", instruction)

    def test_field_intent_map_only_opens_supported_question_types(self) -> None:
        content = (
            "REFERENCE DFI/DUI | DATA FIELD DESCRIPTOR | BIT POSITION | # BITS | RESOLUTION, CODING, ETC\n"
            "444 | RECURRENCE RATE, RECEIPT/ COMPLIANCE 66-69 | 4 | resolution 0.5 Hz\n"
            "769 | TRACK NUMBER, OBJECTIVE 39-57 | 19 |\n"
        )
        plan = app._build_chunk_generation_plan(
            content=content,
            count=4,
            requested_task_types=["protocol_understanding"],
            requested_conversion_modes=[],
        )

        field_intent_map = plan["field_intent_map"]
        self.assertIn("RECURRENCE RATE, RECEIPT/ COMPLIANCE", field_intent_map)
        self.assertIn("resolution", field_intent_map["RECURRENCE RATE, RECEIPT/ COMPLIANCE"])
        self.assertNotIn("unit", field_intent_map["TRACK NUMBER, OBJECTIVE"])

    def test_question_plan_filter_rejects_unsupported_unit_question(self) -> None:
        field_context = {
            "TRACK NUMBER, OBJECTIVE": {"bit_segment": "39-57", "details": "19 bits | bit range 39-57"},
        }
        field_intent_map = {
            "TRACK NUMBER, OBJECTIVE": ["bit_width", "layout"],
        }
        item = {
            "question": "TRACK NUMBER, OBJECTIVE字段的单位是什么？",
            "qa_task_type": "protocol_understanding",
            "source_field": "TRACK NUMBER, OBJECTIVE",
        }

        self.assertFalse(
            app._question_plan_matches_allowed_evidence(
                item,
                field_context,
                field_intent_map,
                topic_context=[],
            )
        )

    def test_placeholder_answer_detection_covers_more_patterns(self) -> None:
        self.assertTrue(app._looks_like_placeholder_answer("文档中未明确说明该字段的单位"))
        self.assertTrue(app._looks_like_placeholder_answer("未指定，占用6位"))
        self.assertTrue(app._looks_like_placeholder_answer("未知"))
        self.assertFalse(app._looks_like_placeholder_answer("单位为Hz，占用4位"))

    def test_fast_probe_detects_numeric_enum_and_layout_signals(self) -> None:
        probe = app._fast_probe_unit(
            "WORD NUMBER: J12.0I\nBIT POSITION 28-33\n# BITS 6\n0=NO STATEMENT 1=REFUEL\nresolution 0.5 Hz"
        )

        self.assertTrue(probe["has_numeric"])
        self.assertTrue(probe["has_enum"])
        self.assertTrue(probe["has_layout_signal"])
        self.assertTrue(probe["has_formula_signal"])
        self.assertGreater(probe["word_count"], 5)

    def test_compute_fast_unit_target_counts_preserves_total_budget(self) -> None:
        prepared_units = [
            {"unit_id": "u1", "minimum_target": 1, "supported_capacity": 8, "probe": {"word_count": 300}, "priority_score": 3.2},
            {"unit_id": "u2", "minimum_target": 1, "supported_capacity": 4, "probe": {"word_count": 120}, "priority_score": 2.1},
            {"unit_id": "u3", "minimum_target": 0, "supported_capacity": 3, "probe": {"word_count": 60}, "priority_score": 0.6},
        ]
        allocations = app._compute_fast_unit_target_counts(prepared_units, average_count=4)

        self.assertEqual(sum(allocations.values()), 8)
        self.assertGreaterEqual(allocations["u1"], allocations["u2"])
        self.assertGreaterEqual(allocations["u2"], allocations["u3"])
        self.assertEqual(allocations["u3"], 0)

    def test_build_generation_batches_merges_multiple_units(self) -> None:
        prepared_units = [
            {"unit_id": "u1", "target_count": 3, "content": "A" * 800},
            {"unit_id": "u2", "target_count": 2, "content": "B" * 700},
            {"unit_id": "u3", "target_count": 2, "content": "C" * 600},
        ]

        batches = app._build_generation_batches(prepared_units)

        self.assertGreaterEqual(len(batches), 1)
        self.assertIn("[SEGMENT_ID: u1]", batches[0]["prompt_context_text"])
        self.assertIn("[SEGMENT_ID: u2]", batches[0]["prompt_context_text"])
        self.assertEqual(batches[0]["batch_target_total"], sum(item["target_count"] for item in prepared_units))

    def test_filter_fast_batch_candidates_keeps_best_and_drops_placeholder(self) -> None:
        content = (
            "REFERENCE DFI/DUI | DATA FIELD DESCRIPTOR | BIT POSITION | # BITS | RESOLUTION, CODING, ETC\n"
            "444 | RECURRENCE RATE, RECEIPT/ COMPLIANCE 66-69 | 4 | 0-15 Hz resolution 0.5 Hz\n"
        )
        plan = app._build_chunk_generation_plan(
            content=content,
            count=2,
            requested_task_types=["protocol_understanding"],
            requested_conversion_modes=[],
        )
        batch = {
            "units": [
                {
                    "unit_id": "u1",
                    "target_count": 2,
                    "field_context": plan["field_context"],
                    "topic_context": plan["topic_context"],
                    "content": content,
                    "probe": app._fast_probe_unit(content),
                }
            ],
            "batch_target_total": 2,
        }
        candidates = [
            {
                "segment_id": "u1",
                "question": "RECURRENCE RATE, RECEIPT/ COMPLIANCE字段的分辨率是多少？",
                "answer": "该字段分辨率为0.5 Hz，占用4位，位段为66-69。",
                "qa_task_type": "protocol_understanding",
                "conversion_mode": None,
                "conversion_formula": None,
                "source_field": "RECURRENCE RATE, RECEIPT/ COMPLIANCE",
            },
            {
                "segment_id": "u1",
                "question": "RECURRENCE RATE, RECEIPT/ COMPLIANCE字段的单位是什么？",
                "answer": "未明确说明",
                "qa_task_type": "protocol_understanding",
                "conversion_mode": None,
                "conversion_formula": None,
                "source_field": "RECURRENCE RATE, RECEIPT/ COMPLIANCE",
            },
            {
                "segment_id": "u1",
                "question": "RECURRENCE RATE, RECEIPT/ COMPLIANCE字段的位宽是多少？",
                "answer": "该字段占用4位，位段为66-69。",
                "qa_task_type": "protocol_understanding",
                "conversion_mode": None,
                "conversion_formula": None,
                "source_field": "RECURRENCE RATE, RECEIPT/ COMPLIANCE",
            },
        ]

        selected, shortfall = app._filter_fast_batch_candidates(candidates, batch)

        self.assertEqual(len(selected), 2)
        self.assertEqual(shortfall, {})
        self.assertTrue(all("未明确说明" not in item["answer"] for item in selected))

    def test_fast_candidate_accepts_numeric_layout_answer_without_exact_field_match(self) -> None:
        unit_info = {
            "field_context": {},
            "topic_context": ["J12.0 MESSAGE SUMMARY"],
            "probe": {
                "has_numeric": True,
                "has_enum": False,
                "has_layout_signal": True,
                "has_formula_signal": False,
            },
        }
        qa = {
            "question": "该字段的位宽是多少？",
            "answer": "该字段占用4位，位段为66-69。",
            "qa_task_type": "protocol_understanding",
        }

        self.assertTrue(app._is_fast_candidate_acceptable(qa, unit_info))


if __name__ == "__main__":
    unittest.main()
