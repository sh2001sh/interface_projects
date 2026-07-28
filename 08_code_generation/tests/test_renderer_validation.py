"""Unit tests for generated project static validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "code_generate"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from project_generator.models import (  # noqa: E402
    AggregationSpec,
    AggregationTypeSpec,
    BranchNode,
    CrcCheckSpec,
    ConversionRuntime,
    ConversionSpec,
    FieldSpec,
    FetchAttempt,
    GroupNode,
    MappingRule,
    RouteSpec,
    SourceAlias,
    SourceRuntime,
    MappingSpec,
    MessageRuleDetailSpec,
    ProtocolSpec,
    ProtocolVerifySpec,
    ResponseActionSpec,
    RuntimeSpec,
    ScalarNode,
    SectionSpec,
    SequenceSpec,
    TransportSpec,
    VerifyRuleSpec,
)
from project_generator.renderer import (  # noqa: E402
    _build_manifest,
    _build_alias_field_lookup,
    _is_redundant_zero_rule,
    _merge_protocol_verifies,
    _mapping_body,
    _mapping_signature,
    _mapping_target_var_name,
    _validate_codec_content,
    _validate_protocol_header_content,
)
from project_generator.templates import (  # noqa: E402
    render_codec_cpp,
    render_codec_header,
    render_config_xml,
    render_mapping_cpp,
    render_messageconvert_cpp,
    render_messageconvert_header,
    render_protocol_header,
)


def _build_protocol(type_name: str, file_stem: str, fields: list[str]) -> ProtocolSpec:
    """Builds one minimal protocol spec for validation tests."""

    return ProtocolSpec(
        type_name=type_name,
        file_stem=file_stem,
        source_path=Path(f"/tmp/{file_stem}.xml"),
        namespace="",
        fields=[
            FieldSpec(
                label=name,
                cpp_name=name,
                path=name,
                path_parts=(name,),
                bit_length=1,
                bit_offset=index,
                default_value="0",
                source_tag="Item",
                cpp_type="long",
            )
            for index, name in enumerate(fields)
        ],
    )


def _build_repeated_protocol(type_name: str, file_stem: str) -> ProtocolSpec:
    """Builds one minimal protocol spec containing a repeated group."""

    return ProtocolSpec(
        type_name=type_name,
        file_stem=file_stem,
        source_path=Path(f"/tmp/{file_stem}.xml"),
        namespace="",
        fields=[
            FieldSpec(
                label="count",
                cpp_name="count",
                path="count",
                path_parts=("count",),
                bit_length=1,
                bit_offset=0,
                default_value="0",
                source_tag="Item",
                cpp_type="long",
            ),
            FieldSpec(
                label="value",
                cpp_name="group_1_value",
                path="group_1/value",
                path_parts=("group_1", "value"),
                bit_length=1,
                bit_offset=1,
                default_value="0",
                source_tag="Item",
                cpp_type="long",
            ),
            FieldSpec(
                label="value",
                cpp_name="group_2_value",
                path="group_2/value",
                path_parts=("group_2", "value"),
                bit_length=1,
                bit_offset=2,
                default_value="0",
                source_tag="Item",
                cpp_type="long",
            ),
        ],
        nodes=[
            ScalarNode(
                label="count",
                cpp_name="count",
                path="count",
                path_parts=("count",),
                bit_length=1,
                default_value="0",
                source_tag="Item",
                cpp_type="long",
            ),
            GroupNode(
                label="group",
                path="group",
                path_parts=("group",),
                corr="count",
                condition=None,
                max_repeat=2,
                repeat_count=2,
                control_fields=("count",),
                children=[
                    ScalarNode(
                        label="value",
                        cpp_name="group_value",
                        path="group/value",
                        path_parts=("group", "value"),
                        bit_length=1,
                        default_value="0",
                        source_tag="Item",
                        cpp_type="long",
                    )
                ],
            ),
        ],
        label_to_cpp={"count": "count"},
    )


def _build_parent_route_protocol() -> ProtocolSpec:
    """Builds one minimal parent protocol carrying X.xml-style routes."""

    return ProtocolSpec(
        type_name="X",
        file_stem="x",
        source_path=Path("/tmp/x.xml"),
        namespace="",
        fields=[
            FieldSpec(
                label="子标识",
                cpp_name="head_origin_子标识",
                path="子标识",
                path_parts=("子标识",),
                bit_length=3,
                bit_offset=0,
                default_value="0",
                source_tag="StructMess",
                cpp_type="long",
            ),
            FieldSpec(
                label="消息标识",
                cpp_name="head_origin_消息标识",
                path="消息标识",
                path_parts=("消息标识",),
                bit_length=2,
                bit_offset=3,
                default_value="0",
                source_tag="StructMess",
                cpp_type="long",
            ),
        ],
        routes=[
            RouteSpec(
                corr="Head_Origin:StructMess.子标识,Head_Origin:StructMess.消息标识",
                value="5,0",
                target_protocol="X0.5",
                control_fields=("子标识", "消息标识"),
            )
        ],
        label_to_cpp={
            "子标识": "head_origin_子标识",
            "消息标识": "head_origin_消息标识",
        },
    )


class RendererValidationTest(unittest.TestCase):
    """Covers post-render static validation."""

    def test_protocol_header_validation_accepts_normal_render(self) -> None:
        protocol = _build_protocol("K1_6", "k1_6", ["gpi4", "field_a"])
        content = render_protocol_header(protocol)
        _validate_protocol_header_content(protocol, "k1_6_def.h", content)

    def test_protocol_header_validation_rejects_joined_fields(self) -> None:
        protocol = _build_protocol("K1_6", "k1_6", ["gpi4", "field_a"])
        content = """#ifndef K1_6_DEF_H
#define K1_6_DEF_H

class K1_6 {
public:
    long gpi4 = 0; double field_a = 0.0;
};

#endif
"""
        with self.assertRaisesRegex(ValueError, "字段声明格式异常"):
            _validate_protocol_header_content(protocol, "k1_6_def.h", content)

    def test_codec_validation_rejects_missing_field_reference(self) -> None:
        protocol = _build_protocol("K1_6", "k1_6", ["field_a"])
        codec = """#include \"codec.h\"

QString decodeMsg(uchar* pData, int len, K1_6& value)
{
    Q_UNUSED(pData);
    Q_UNUSED(len);
    value.gpi4 = 1;
    return QString();
}
"""
        with self.assertRaisesRegex(ValueError, "未声明字段"):
            _validate_codec_content([protocol], "codec.cpp", codec)

    def test_codec_validation_rejects_bad_append_arity(self) -> None:
        protocol = _build_protocol("K1_6", "k1_6", ["field_a"])
        codec = """#include \"codec.h\"

void appendBits(QByteArray& data, quint64 value, int bitLength)
{
    Q_UNUSED(data);
    Q_UNUSED(value);
    Q_UNUSED(bitLength);
}

void encodeMsg(QByteArray& data, K1_6& value)
{
    int bitOffset = 0;
    appendBits(data, value.field_a, bitOffset);
}
        """
        with self.assertRaisesRegex(ValueError, "参数数量异常"):
            _validate_codec_content([protocol], "codec.cpp", codec)

    def test_codec_header_includes_qtglobal(self) -> None:
        protocol = _build_protocol("K1_6", "k1_6", ["field_a"])
        header = render_codec_header([protocol], ["k1_6_to_k1_7.h"])
        self.assertIn("#include <QtGlobal>", header)
        self.assertIn("QString extractRuntimeFieldValue", header)

    def test_codec_helpers_are_protocol_scoped(self) -> None:
        protocol_a = _build_protocol("K1_6", "k1_6", ["field_a"])
        protocol_b = _build_protocol("K1_7", "k1_7", ["field_b"])
        codec = render_codec_cpp([protocol_a, protocol_b])
        self.assertIn("static QString checkEncodeSeqNumberK1_6", codec)
        self.assertIn("static QString checkEncodeSeqNumberK1_7", codec)
        self.assertIn("static void writeK1_6Seq1", codec)
        self.assertIn("static void writeK1_7Seq1", codec)
        self.assertIn("extractRuntimeFieldValueK1_6", codec)
        self.assertIn("extractRuntimeFieldValueK1_7", codec)

    def test_codec_updates_optional_branch_control_fields_from_child_values(self) -> None:
        protocol = _build_protocol("K1_6", "k1_6", ["flag", "field_a"])
        protocol.nodes = [
            ScalarNode(
                label="flag",
                cpp_name="flag",
                path="flag",
                path_parts=("flag",),
                bit_length=1,
                default_value="0",
                source_tag="Item",
                cpp_type="long",
            ),
            BranchNode(
                label="branch",
                path="branch",
                path_parts=("branch",),
                corr="flag",
                value="1",
                control_fields=("flag",),
                children=[
                    ScalarNode(
                        label="field_a",
                        cpp_name="field_a",
                        path="branch/field_a",
                        path_parts=("branch", "field_a"),
                        bit_length=5,
                        default_value="0",
                        source_tag="Item",
                        cpp_type="long",
                    )
                ],
            ),
        ]
        protocol.sections = [
            SectionSpec(
                name="Origin",
                cpp_name="origin",
                tag_name="NameSpace",
                path="Origin",
                nodes=protocol.nodes,
            )
        ]
        protocol.label_to_cpp = {"flag": "flag"}

        codec = render_codec_cpp([protocol])
        self.assertIn("value.flag = (value.field_a != 0) ? 1 : 0;", codec)

    def test_xml_protocol_verify_is_merged_when_runtime_config_missing(self) -> None:
        protocol = _build_protocol("X0_5", "x0_5", ["field_a"])
        protocol.xml_protocol_verify = ProtocolVerifySpec(
            protocol="X0_5",
            verify_rules=[VerifyRuleSpec(name="verify1", when_seq="Seq_1", constraint=None)],
            response_actions=[ResponseActionSpec(on_verify="verify1", encode_seq="Seq_1", return_code=0)],
            default_return_code=-1,
        )
        mappings = MappingSpec(version="1.0", project_name="demo", conversions=[], runtime=RuntimeSpec())

        merged = _merge_protocol_verifies([protocol], mappings)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].protocol, "X0_5")

        manifest = _build_manifest([protocol], mappings, choreography=None)
        protocol_verifies = manifest["runtime"]["protocol_verifies"]
        self.assertEqual(len(protocol_verifies), 1)
        self.assertEqual(protocol_verifies[0]["protocol"], "X0_5")
        self.assertEqual(protocol_verifies[0]["verify_rules"][0]["when_seq"], "Seq_1")

    def test_messageconvert_consumes_parent_route_and_aliases_parent_name(self) -> None:
        parent_protocol = _build_parent_route_protocol()
        child_protocol = _build_protocol("X0_5", "x0_5", ["时间1"])
        target_protocol = _build_protocol("K1_6", "k1_6", ["飞临时间"])
        conversion = ConversionSpec(
            name="X0_5_to_K1_6",
            mode="simple",
            sources=[SourceAlias(alias="x0_5", protocol="X0_5")],
            target_protocol="K1_6",
            rules=[
                MappingRule(
                    target_field="飞临时间",
                    formula="x0_5.时间1",
                    source_fields=["x0_5.时间1"],
                    rule_type="direct",
                    when=None,
                    default_value=None,
                    description=None,
                )
            ],
            runtime=ConversionRuntime(
                sources=[
                    SourceRuntime(
                        alias="x0_5",
                        message_name="X",
                        fetches=[FetchAttempt(count=1, cycle_ms=10)],
                    )
                ]
            ),
        )

        content = render_messageconvert_cpp(
            conversions=[conversion],
            protocol_lookup={
                "X": parent_protocol,
                "X0_5": child_protocol,
                "K1_6": target_protocol,
            },
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={},
            joint=False,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
            transport=None,
        )

        self.assertIn("resolveInboundProtocolName", content)
        self.assertIn('extractRuntimeFieldValue(QStringLiteral("X"), QStringLiteral("\\u5b50\\u6807\\u8bc6"), data)', content)
        self.assertIn('extractRuntimeFieldValue(QStringLiteral("X"), QStringLiteral("\\u6d88\\u606f\\u6807\\u8bc6"), data)', content)
        self.assertIn('return QStringLiteral("X0_5");', content)

    def test_joint_bundle_runtime_can_classify_direct_child_frames_by_size(self) -> None:
        parent_protocol = ProtocolSpec(
            type_name="K",
            file_stem="k",
            source_path=Path("/tmp/k.xml"),
            namespace="",
            total_bits=12,
            routes=[
                RouteSpec(
                    corr="Head_Origin:StructMess.消息标识,Head_Origin:StructMess.消息子标识",
                    value="1,6",
                    target_protocol="K1.6",
                    control_fields=("消息标识", "消息子标识"),
                ),
                RouteSpec(
                    corr="Head_Origin:StructMess.消息标识,Head_Origin:StructMess.消息子标识",
                    value="1,7",
                    target_protocol="K1.7",
                    control_fields=("消息标识", "消息子标识"),
                ),
            ],
        )
        source_a = _build_protocol("K1_6", "k1_6", ["a"])
        source_a.total_bits = 319
        source_b = _build_protocol("K1_7", "k1_7", ["b"])
        source_b.total_bits = 717
        target_protocol = _build_protocol("X0_5", "x0_5", ["out"])
        conversion = ConversionSpec(
            name="K1_6_K1_7_to_X0_5",
            mode="joint",
            sources=[
                SourceAlias(alias="k1_6", protocol="K1_6"),
                SourceAlias(alias="k1_7", protocol="K1_7"),
            ],
            target_protocol="X0_5",
            rules=[],
            runtime=ConversionRuntime(
                sources=[
                    SourceRuntime(alias="k1_6", fetches=[FetchAttempt(count=1, cycle_ms=0)]),
                    SourceRuntime(alias="k1_7", fetches=[FetchAttempt(count=1, cycle_ms=0)]),
                ]
            ),
        )

        content = render_messageconvert_cpp(
            conversions=[conversion],
            protocol_lookup={
                "K": parent_protocol,
                "K1_6": source_a,
                "K1_7": source_b,
                "X0_5": target_protocol,
            },
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={},
            joint=True,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
            transport=TransportSpec(
                message_type="joint_bundle",
                recv_ip="127.0.0.1",
                recv_port=4620,
                send_ip="127.0.0.1",
                send_port=5620,
            ),
        )

        self.assertIn('if (data.size() == 40) return QStringLiteral("K1_6");', content)
        self.assertIn('if (data.size() == 90) return QStringLiteral("K1_7");', content)
        self.assertIn("? resolveCanonicalRuntimeMessageName(resolvedProtocol, name)", content)
        self.assertIn(": resolvedProtocol;", content)

    def test_parent_named_transport_rule_applies_crc_to_child_protocol(self) -> None:
        parent_protocol = _build_parent_route_protocol()
        child_protocol = _build_protocol("X0_5", "x0_5", ["时间1"])
        target_protocol = _build_protocol("K1_6", "k1_6", ["飞临时间"])
        conversion = ConversionSpec(
            name="X0_5_to_K1_6",
            mode="simple",
            sources=[SourceAlias(alias="x0_5", protocol="X0_5")],
            target_protocol="K1_6",
            rules=[],
            runtime=ConversionRuntime(
                sources=[
                    SourceRuntime(
                        alias="x0_5",
                        message_name="X",
                        fetches=[FetchAttempt(count=1, cycle_ms=10)],
                    )
                ]
            ),
        )

        content = render_messageconvert_cpp(
            conversions=[conversion],
            protocol_lookup={
                "X": parent_protocol,
                "X0_5": child_protocol,
                "K1_6": target_protocol,
            },
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={},
            joint=False,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
            transport=None,
        )
        self.assertIn("getData(QStringLiteral(\"X0_5\"), QStringLiteral(\"X\")", content)

        content_with_crc = render_messageconvert_cpp(
            conversions=[conversion],
            protocol_lookup={
                "X": parent_protocol,
                "X0_5": child_protocol,
                "K1_6": target_protocol,
            },
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={},
            joint=False,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
            transport=TransportSpec(
                message_type="bundle",
                recv_ip="127.0.0.1",
                recv_port=4300,
                send_ip="127.0.0.1",
                send_port=5300,
                message_rules=[
                    MessageRuleDetailSpec(
                        message_name="X",
                        crc_check=CrcCheckSpec(enabled=True, bind_element="时间1"),
                    )
                ],
            ),
        )
        self.assertIn('if (!validateRuntimeCrc(QStringLiteral("X0_5"), QStringLiteral("\\u65f6\\u95f41"), x0_5Data)) continue;', content_with_crc)

    def test_runtime_protocol_verify_does_not_override_xml_default(self) -> None:
        protocol = _build_protocol("X0_5", "x0_5", ["field_a"])
        protocol.xml_protocol_verify = ProtocolVerifySpec(
            protocol="X0_5",
            verify_rules=[VerifyRuleSpec(name="xml_verify", when_seq="Seq_XML", constraint=None)],
        )
        runtime_verify = ProtocolVerifySpec(
            protocol="X0_5",
            verify_rules=[VerifyRuleSpec(name="runtime_verify", when_seq="Seq_Runtime", constraint=None)],
        )
        mappings = MappingSpec(
            version="1.0",
            project_name="demo",
            conversions=[],
            runtime=RuntimeSpec(protocol_verifies=[runtime_verify]),
        )

        merged = _merge_protocol_verifies([protocol], mappings)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].verify_rules[0].name, "xml_verify")

    def test_verify_response_uses_protocol_scoped_sequence_writer(self) -> None:
        protocol = _build_protocol("X0_5", "x0_5", ["field_a"])
        protocol.sequences = [SequenceSpec(name="Seq_1", cycle=1, times=1)]
        protocol.xml_protocol_verify = ProtocolVerifySpec(
            protocol="X0_5",
            verify_rules=[VerifyRuleSpec(name="verify1", when_seq="Seq_1", constraint=None)],
            response_actions=[ResponseActionSpec(on_verify="verify1", encode_seq="Seq_1", return_code=0)],
            default_return_code=-1,
        )

        codec = render_codec_cpp([protocol], protocol_verifies={"X0_5": protocol.xml_protocol_verify})
        self.assertIn("writeX0_5Seq1(value, data);", codec)
        self.assertNotIn("writeSeq_1(value, data);", codec)
        self.assertIn("static void writeX0_5Seq1(X0_5& value, QByteArray& data);", codec)
        self.assertLess(
            codec.index("static void writeX0_5Seq1(X0_5& value, QByteArray& data);"),
            codec.index("static bool applyResponse_1(X0_5& value, QByteArray& data)"),
        )

    def test_manifest_excludes_parse_only_routes_and_unsupported_features(self) -> None:
        protocol = _build_protocol("X0_5", "x0_5", ["field_a"])
        protocol.unsupported_features = ["RootFieldRoute"]
        protocol.routes = [
            RouteSpec(
                corr="消息标识",
                value="1",
                target_protocol="X0_5",
                control_fields=("origin_u6d88u606fu6807u8bc6",),
            )
        ]
        mappings = MappingSpec(version="1.0", project_name="demo", conversions=[], runtime=RuntimeSpec())

        manifest = _build_manifest([protocol], mappings, choreography=None)
        protocol_summary = manifest["protocols"][0]
        self.assertNotIn("unsupported_features", protocol_summary)
        self.assertNotIn("routes", protocol_summary)
        self.assertNotIn("reference_profile", manifest["runtime"])

    def test_mapping_cpp_uses_protocol_specific_target_name_and_explicit_defaults(self) -> None:
        source_protocol = _build_protocol("K1_6", "k1_6", ["field_a"])
        target_protocol = _build_protocol("X0_5", "x0_5", ["field_b", "field_c"])
        conversion = type(
            "ConversionStub",
            (),
            {
                "name": "K1_6_to_X0_5",
                "sources": [type("SourceStub", (), {"alias": "k1_6", "protocol": "K1_6"})()],
                "rules": [
                    type(
                        "RuleStub",
                        (),
                        {
                            "target_field": "field_b",
                            "formula": "k1_6.field_a",
                            "source_fields": ["k1_6.field_a"],
                            "when": None,
                            "default_value": None,
                        },
                    )()
                ],
            },
        )()
        alias_fields = _build_alias_field_lookup(conversion, {"K1_6": source_protocol, "X0_5": target_protocol})
        target_var_name = _mapping_target_var_name(target_protocol.type_name)
        signature = _mapping_signature(
            conversion.name,
            target_protocol.type_name,
            [("k1_6", source_protocol.type_name)],
        )
        body = _mapping_body(conversion, target_protocol, alias_fields, target_var_name)
        content = render_mapping_cpp(
            header_name="k1_6_to_x0_5.h",
            function_signature=signature,
            target_protocol=target_protocol.type_name,
            target_var_name=target_var_name,
            body=body,
        )

        self.assertIn("X0_5 x0_5Target;", content)
        self.assertIn("x0_5Target.field_b = 0;", content)
        self.assertIn("x0_5Target.field_c = 0;", content)
        self.assertIn("x0_5Target.field_b = k1_6.field_a;", content)
        self.assertIn("return x0_5Target;", content)

    def test_mapping_body_zero_initializes_even_when_schema_default_is_non_zero(self) -> None:
        source_protocol = _build_protocol("K1_6", "k1_6", ["field_a"])
        target_protocol = ProtocolSpec(
            type_name="X0_5",
            file_stem="x0_5",
            source_path=Path("/tmp/x0_5.xml"),
            namespace="",
            fields=[
                FieldSpec(
                    label="field_b",
                    cpp_name="field_b",
                    path="field_b",
                    path_parts=("field_b",),
                    bit_length=1,
                    bit_offset=0,
                    default_value="9",
                    source_tag="Item",
                    cpp_type="long",
                )
            ],
        )
        conversion = type(
            "ConversionStub",
            (),
            {
                "name": "K1_6_to_X0_5",
                "sources": [type("SourceStub", (), {"alias": "k1_6", "protocol": "K1_6"})()],
                "rules": [],
            },
        )()

        alias_fields = _build_alias_field_lookup(conversion, {"K1_6": source_protocol, "X0_5": target_protocol})
        body = _mapping_body(conversion, target_protocol, alias_fields, "x0_5Target")
        self.assertIn("x0_5Target.field_b = 0;", body)
        self.assertNotIn("(9)", body)

    def test_mapping_body_uses_plain_assignment_for_integral_targets(self) -> None:
        source_protocol = _build_protocol("K1_6", "k1_6", ["field_a"])
        target_protocol = ProtocolSpec(
            type_name="X0_5",
            file_stem="x0_5",
            source_path=Path("/tmp/x0_5.xml"),
            namespace="",
            fields=[
                FieldSpec(
                    label="field_b",
                    cpp_name="field_b",
                    path="field_b",
                    path_parts=("field_b",),
                    bit_length=5,
                    bit_offset=0,
                    default_value=None,
                    source_tag="Item",
                    cpp_type="long",
                )
            ],
        )
        conversion = type(
            "ConversionStub",
            (),
            {
                "name": "K1_6_to_X0_5",
                "sources": [type("SourceStub", (), {"alias": "k1_6", "protocol": "K1_6"})()],
                "rules": [
                    type(
                        "RuleStub",
                        (),
                        {
                            "target_field": "field_b",
                            "formula": "k1_6.field_a",
                            "source_fields": ["k1_6.field_a"],
                            "when": None,
                            "default_value": None,
                        },
                    )()
                ],
            },
        )()

        alias_fields = _build_alias_field_lookup(conversion, {"K1_6": source_protocol, "X0_5": target_protocol})
        body = _mapping_body(conversion, target_protocol, alias_fields, "x0_5Target")
        self.assertIn("x0_5Target.field_b = k1_6.field_a;", body)
        self.assertNotIn("clampToUnsignedBitRange", body)

    def test_codec_encode_clamps_integral_values_to_bit_length(self) -> None:
        protocol = _build_protocol("X0_5", "x0_5", ["field_a"])
        protocol.fields[0].bit_length = 5
        protocol.nodes = [
            ScalarNode(
                label="field_a",
                cpp_name="field_a",
                path="field_a",
                path_parts=("field_a",),
                bit_length=5,
                default_value="0",
                source_tag="Item",
                cpp_type="long",
            )
        ]
        protocol.sections = [
            SectionSpec(
                name="Origin",
                cpp_name="origin",
                tag_name="NameSpace",
                path="Origin",
                nodes=protocol.nodes,
            )
        ]
        codec = render_codec_cpp([protocol])
        self.assertIn("quint64 normalizeUnsignedBits(qint64 value, int bitLength)", codec)
        self.assertIn(
            "appendBits(data, normalizeUnsignedBits(static_cast<qint64>(value.field_a), 5), bitOffset, 5);",
            codec,
        )

    def test_redundant_zero_rule_is_skipped_after_zero_initialization(self) -> None:
        rule = type(
            "RuleStub",
            (),
            {
                "when": None,
                "source_fields": [],
                "formula": "0",
                "default_value": "0",
            },
        )()
        self.assertTrue(_is_redundant_zero_rule(rule))

    def test_messageconvert_header_uses_newb_style_qstringlist_state(self) -> None:
        header = render_messageconvert_header([], joint=False)
        self.assertIn("QStringList state = {};", header)
        self.assertNotIn("int state = 0;", header)

    def test_messageconvert_cpp_aggregates_on_receive_before_conversion(self) -> None:
        content = render_messageconvert_cpp(
            conversions=[],
            protocol_lookup={},
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={"X0_5": "X协议.X0_5"},
            joint=True,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
            transport=None,
        )

        self.assertIn("dataInfo[index]->num++;", content)
        self.assertIn("dataInfo[index]->state.clear();", content)
        self.assertIn(
            "QVector<int> batchIndexes = collectReadyBatchIndexes(dataInfo, protocolName, name, time, num, false);",
            content,
        )
        self.assertIn("removeQueueIndexes(dataInfo, batchIndexes);", content)
        self.assertIn("if (item->num != requiredCount) continue;", content)
        self.assertIn("if (item->state.indexOf(normalizedStateKey) != -1) continue;", content)

        route_start = content.index("void messageConvert::routeGeneratedTarget(")
        route_end = content.index("void messageConvert::msgConvertThread()")
        route_block = content[route_start:route_end]
        self.assertIn("if (cacheOnly) cacheGeneratedTarget(cacheName, cacheNum, data);", route_block)
        self.assertIn("else onSendMessage(protocolName, targetName, data);", route_block)
        self.assertIn("void messageConvert::onSendMessage(const QString& protocolName, const QString& targetName, QByteArray msg)", content)
        self.assertIn("const QString normalizedEndpoint = normalizeRuntimeMessageName(var->name);", content)
        self.assertNotIn("targetDataInfo.push_back", route_block)
        self.assertNotIn("flushPendingTargetMessage(", route_block)

    def test_messageconvert_cpp_batches_fixed_source_into_target_loop(self) -> None:
        source_protocol = _build_protocol("SRC", "src", ["field_a"])
        target_protocol = _build_repeated_protocol("DST", "dst")
        conversion = ConversionSpec(
            name="SRC_to_DST",
            mode="simple",
            sources=[SourceAlias(alias="src", protocol="SRC")],
            target_protocol="DST",
            rules=[],
            runtime=ConversionRuntime(),
        )

        content = render_messageconvert_cpp(
            conversions=[conversion],
            protocol_lookup={"SRC": source_protocol, "DST": target_protocol},
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={"DST": "DST"},
            joint=False,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
            transport=None,
        )

        self.assertIn("void getDataBatch(", render_messageconvert_header(["SRC_to_DSTdataPro"], joint=False))
        self.assertIn("static DST convert_src_to_dst_batch(const QVector<SRC>& batch)", content)
        self.assertIn('getDataBatch(QStringLiteral("SRC"), QStringLiteral("SRC")', content)
        self.assertIn("bool messageConvert::isLoopAggregationSource(", content)
        self.assertIn("QVector<int> messageConvert::normalizeAggregatedBatchIndexes(", content)
        self.assertIn('if (aggregationType == QStringLiteral("ORDER") && !bindElement.isEmpty())', content)
        self.assertIn('if (aggregationType == QStringLiteral("DISTINCT") && !bindElement.isEmpty())', content)
        self.assertIn("repeatedGroupCapacitydst()", content)
        self.assertIn("convert_src_to_dst_batch(srcBatch);", content)

    def test_messageconvert_cpp_splits_loop_source_into_multiple_fixed_targets(self) -> None:
        source_protocol = _build_repeated_protocol("SRC_LOOP", "src_loop")
        target_protocol = _build_protocol("DST_FIXED", "dst_fixed", ["field_a"])
        conversion = ConversionSpec(
            name="SRC_LOOP_to_DST_FIXED",
            mode="simple",
            sources=[SourceAlias(alias="src_loop", protocol="SRC_LOOP")],
            target_protocol="DST_FIXED",
            rules=[],
            runtime=ConversionRuntime(),
        )

        content = render_messageconvert_cpp(
            conversions=[conversion],
            protocol_lookup={"SRC_LOOP": source_protocol, "DST_FIXED": target_protocol},
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={"DST_FIXED": "DST_FIXED"},
            joint=False,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
            transport=None,
        )

        self.assertIn("static QVector<DST_FIXED> convert_src_loop_to_dst_fixed_split(const SRC_LOOP& source)", content)
        self.assertIn("QVector<DST_FIXED> generatedTargets = convert_src_loop_to_dst_fixed_split(src_loop);", content)
        self.assertIn('getData(QStringLiteral("SRC_LOOP"), QStringLiteral("SRC_LOOP")', content)
        self.assertIn("for (int targetIndex = 0; targetIndex < generatedTargets.size(); ++targetIndex)", content)
        self.assertIn("loadRepeatedGroupIterationsrc_loop(source, index, item);", content)

    def test_runtime_rule_condition_renders_into_config_and_messageconvert(self) -> None:
        source_protocol = _build_protocol("SRC", "src", ["threshold", "field_a"])
        target_protocol = _build_protocol("DST", "dst", ["field_a"])
        transport = TransportSpec(
            message_type="bundle",
            recv_ip="127.0.0.1",
            recv_port=4300,
            send_ip="127.0.0.1",
            send_port=5300,
            message_rules=[
                MessageRuleDetailSpec(
                    message_name="SRC",
                    aggregation=AggregationSpec(mode="SINGLE", operator="GT", value="400"),
                    aggregation_type=AggregationTypeSpec(type="ORDER", bind_element="threshold"),
                )
            ],
        )

        config_xml = render_config_xml([], transport=transport)
        content = render_messageconvert_cpp(
            conversions=[
                ConversionSpec(
                    name="SRC_to_DST",
                    mode="simple",
                    sources=[SourceAlias(alias="src", protocol="SRC")],
                    target_protocol="DST",
                    rules=[],
                    runtime=ConversionRuntime(),
                )
            ],
            protocol_lookup={"SRC": source_protocol, "DST": target_protocol},
            source_cache_keys={},
            source_protocol_names={},
            target_protocol_names={"DST": "DST"},
            joint=False,
            loop_sleep_ms=2,
            check_data_interval_ms=5000,
            transport=transport,
        )

        self.assertIn('operator="GT"', config_xml)
        self.assertIn('value="400"', config_xml)
        self.assertIn("bool messageConvert::shouldApplyRuleCondition(", content)
        self.assertIn('if (!shouldApplyRuleCondition(QStringLiteral("SRC"), QStringLiteral("SRC"), srcData)) break;', content)
        self.assertIn('if (operatorName == QStringLiteral("GT")) return actualNumber > expectedNumber;', content)


if __name__ == "__main__":
    unittest.main()
