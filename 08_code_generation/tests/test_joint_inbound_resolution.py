"""Unit tests for joint inbound protocol resolution in generated runtime code."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "code_generate"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from project_generator.models import (  # noqa: E402
    ConversionSpec,
    FieldSpec,
    ProtocolSpec,
    SourceAlias,
)
from project_generator.templates import render_messageconvert_cpp, render_messageconvert_header  # noqa: E402


def _protocol(type_name: str, total_bits: int) -> ProtocolSpec:
    """Builds a minimal protocol spec with one fixed-length field."""

    return ProtocolSpec(
        type_name=type_name,
        file_stem=type_name.lower(),
        source_path=Path(f"/tmp/{type_name}.xml"),
        namespace="",
        total_bits=total_bits,
        fields=[
            FieldSpec(
                label="field",
                cpp_name="field",
                path="field",
                path_parts=("field",),
                bit_length=total_bits,
                bit_offset=0,
                default_value="0",
                source_tag="Item",
            )
        ],
    )


class JointInboundResolutionTest(unittest.TestCase):
    """Covers direct child frames arriving on one shared joint receive endpoint."""

    def test_joint_runtime_classifies_direct_child_frames_by_unique_size(self) -> None:
        protocols = {
            "K1_6": _protocol("K1_6", 320),
            "K1_7": _protocol("K1_7", 720),
            "X0_5": _protocol("X0_5", 872),
        }
        conversion = ConversionSpec(
            name="k_to_x",
            mode="direct",
            sources=[
                SourceAlias(alias="k1_6", protocol="K1_6"),
                SourceAlias(alias="k1_7", protocol="K1_7"),
            ],
            target_protocol="X0_5",
            rules=[],
        )

        header = render_messageconvert_header(process_methods=["k_to_xdataPro"], joint=True)
        cpp = render_messageconvert_cpp(
            conversions=[conversion],
            protocol_lookup=protocols,
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={},
            joint=True,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
        )

        self.assertIn(
            "QString resolveInboundProtocolName(const QString& messageName, const QByteArray& data) const;",
            header,
        )
        self.assertIn('if (data.size() == 40) return QStringLiteral("K1_6");', cpp)
        self.assertIn('if (data.size() == 90) return QStringLiteral("K1_7");', cpp)
        self.assertIn("d->name = resolveInboundProtocolName(name, data);", cpp)


if __name__ == "__main__":
    unittest.main()
