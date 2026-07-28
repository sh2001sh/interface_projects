"""Example XML parsing tests for interface 8 generator."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "code_generate"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from project_generator.xml_parser import load_protocols  # noqa: E402


EXAMPLE_DIR = Path(os.environ.get("CODEGEN_EXAMPLE_DIR", str(PROJECT_ROOT / "示例")))


class ExampleXmlParserTest(unittest.TestCase):
    """Validates the X/X0.5 example XML semantics."""

    def test_example_xml_supports_declared_types_and_placeholder_lengths(self) -> None:
        protocols = {protocol.type_name: protocol for protocol in load_protocols(EXAMPLE_DIR)}

        self.assertIn("X", protocols)
        self.assertIn("X0_5", protocols)

        x_protocol = protocols["X"]
        x05_protocol = protocols["X0_5"]

        self.assertEqual("big", x_protocol.endian)
        self.assertEqual("float", x_protocol.fields[0].cpp_type)
        self.assertEqual("double", x_protocol.fields[4].cpp_type)
        self.assertEqual("char", x_protocol.fields[5].cpp_type)

        x05_fields = {field.label: field for field in x05_protocol.fields}
        self.assertEqual(98, x05_fields["数据1"].bit_length)

        continue_placeholder_fields = [
            field for field in x05_protocol.fields if field.bit_length == 98 and "continue" in field.cpp_name
        ]
        self.assertGreaterEqual(len(continue_placeholder_fields), 1)

    def test_example_xml_generates_unique_cpp_field_names(self) -> None:
        protocols = {protocol.type_name: protocol for protocol in load_protocols(EXAMPLE_DIR)}
        x05_protocol = protocols["X0_5"]
        cpp_names = [field.cpp_name for field in x05_protocol.fields]
        self.assertEqual(len(cpp_names), len(set(cpp_names)))
        self.assertIn("origin_u8fd0u884cu72b6u6001", cpp_names)
        self.assertIn("continue1_u5907u7528", cpp_names)
        self.assertIn("continue4_u5907u7528", cpp_names)


if __name__ == "__main__":
    unittest.main()
