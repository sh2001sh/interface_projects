"""Unit tests for multi-endpoint port_config_json support."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from code_generation_adapter import normalize_port_config  # noqa: E402


class MultiEndpointPortConfigTest(unittest.TestCase):
    """Covers multi-port config support for multiple source protocols."""

    def test_endpoints_mode_supports_multiple_recv_ports(self) -> None:
        payload = {
            "messageType": "temp_sensor_bundle",
            "messageRuleDetailList": [
                {"messageName": "temp_report", "delayRequirement": 0, "filterConfig": {}}
            ],
            "endpoints": [
                {"name": "temp_sensor_a", "ip": "127.0.0.1", "port": 4100, "type": "udp", "recv": 1},
                {"name": "temp_sensor_b", "ip": "127.0.0.1", "port": 4101, "type": "udp", "recv": 1},
                {"name": "temp_report", "ip": "127.0.0.1", "port": 5100, "type": "udp", "recv": 0},
            ],
        }

        normalized = normalize_port_config(payload)

        self.assertEqual(normalized["transport"]["recvPort"], 4100)
        self.assertEqual(normalized["transport"]["sendPort"], 5100)
        self.assertEqual(len(normalized["endpoints"]), 3)
        self.assertEqual([item["port"] for item in normalized["endpoints"] if item["recv"] == 1], [4100, 4101])

    def test_endpoints_mode_deduplicates_identical_endpoint_items(self) -> None:
        payload = {
            "messageType": "x0_5",
            "messageRuleDetailList": [
                {"messageName": "X0.5", "delayRequirement": 0, "filterConfig": {}}
            ],
            "endpoints": [
                {"name": "x0.5_recv", "ip": "192.168.2.130", "port": 7788, "type": "udp", "recv": 1},
                {"name": "x0.5_recv", "ip": "192.168.2.130", "port": 7788, "type": "udp", "recv": 1},
                {
                    "name": "k5.1_send",
                    "ip": "192.168.23.17",
                    "port": 23,
                    "type": "udp",
                    "recv": 0,
                    "feedBackPort": 7788,
                },
            ],
        }

        normalized = normalize_port_config(payload)

        self.assertEqual(len(normalized["endpoints"]), 2)
        self.assertEqual(normalized["endpoints"][0]["name"], "x0.5_recv")
        self.assertEqual(normalized["endpoints"][1]["name"], "k5.1_send")

    def test_endpoints_mode_rejects_duplicate_endpoint_conflicts(self) -> None:
        payload = {
            "messageType": "x0_5",
            "messageRuleDetailList": [
                {"messageName": "X0.5", "delayRequirement": 0, "filterConfig": {}}
            ],
            "endpoints": [
                {
                    "name": "x0.5_recv",
                    "ip": "192.168.2.130",
                    "port": 7788,
                    "type": "udp",
                    "recv": 1,
                    "feedBackPort": 7788,
                },
                {
                    "name": "x0.5_recv",
                    "ip": "192.168.2.130",
                    "port": 7788,
                    "type": "udp",
                    "recv": 1,
                    "feedBackPort": 7799,
                },
                {
                    "name": "k5.1_send",
                    "ip": "192.168.23.17",
                    "port": 23,
                    "type": "udp",
                    "recv": 0,
                    "feedBackPort": 7788,
                },
            ],
        }

        with self.assertRaisesRegex(ValueError, "重复端点但配置不一致"):
            normalize_port_config(payload)

    def test_endpoints_mode_requires_recv_and_send_endpoint(self) -> None:
        payload = {
            "messageType": "temp_sensor_bundle",
            "messageRuleDetailList": [
                {"messageName": "temp_report", "delayRequirement": 0, "filterConfig": {}}
            ],
            "endpoints": [
                {"name": "temp_sensor_a", "ip": "127.0.0.1", "port": 4100, "type": "udp", "recv": 1},
            ],
        }

        with self.assertRaisesRegex(ValueError, "至少需要一个 recv=0 的发送端口"):
            normalize_port_config(payload)

    def test_endpoints_mode_falls_back_to_top_level_ports_when_recv_side_missing(self) -> None:
        payload = {
            "recvIp": "127.0.0.1",
            "recvPort": 4100,
            "sendIp": "127.0.0.1",
            "sendPort": 5100,
            "messageRuleDetailList": [
                {"messageName": "temp_report", "delayRequirement": 0, "filterConfig": {}}
            ],
            "endpoints": [
                {"name": "temp_report_send", "ip": "127.0.0.1", "port": 5100, "type": "udp", "recv": 0},
            ],
        }

        normalized = normalize_port_config(payload)

        recv_endpoints = [item for item in normalized["endpoints"] if item["recv"] == 1]
        send_endpoints = [item for item in normalized["endpoints"] if item["recv"] == 0]
        self.assertEqual(len(recv_endpoints), 1)
        self.assertEqual(recv_endpoints[0]["port"], 4100)
        self.assertEqual(len(send_endpoints), 1)
        self.assertEqual(normalized["transport"]["recvPort"], 4100)
        self.assertEqual(normalized["transport"]["sendPort"], 5100)

    def test_endpoints_mode_infers_recv_direction_from_endpoint_name(self) -> None:
        payload = {
            "messageType": "temp_sensor_bundle",
            "messageRuleDetailList": [
                {"messageName": "temp_report", "delayRequirement": 0, "filterConfig": {}}
            ],
            "endpoints": [
                {"name": "temp_sensor_recv", "ip": "127.0.0.1", "port": 4100, "type": "udp", "recv": 0},
                {"name": "temp_report_send", "ip": "127.0.0.1", "port": 5100, "type": "udp", "recv": 0},
            ],
        }

        normalized = normalize_port_config(payload)

        recv_endpoints = [item for item in normalized["endpoints"] if item["recv"] == 1]
        send_endpoints = [item for item in normalized["endpoints"] if item["recv"] == 0]
        self.assertEqual(len(recv_endpoints), 1)
        self.assertEqual(recv_endpoints[0]["name"], "temp_sensor_recv")
        self.assertEqual(len(send_endpoints), 1)
        self.assertEqual(send_endpoints[0]["name"], "temp_report_send")

    def test_message_type_is_optional_for_joint_multi_source_payload(self) -> None:
        payload = {
            "messageRuleDetailList": [
                {"messageName": "X0.5", "delayRequirement": 0, "filterConfig": {}}
            ],
            "endpoints": [
                {"name": "K1_6_RECV", "ip": "127.0.0.1", "port": 4100, "type": "udp", "recv": 1},
                {"name": "K1_7_RECV", "ip": "127.0.0.1", "port": 4101, "type": "udp", "recv": 1},
                {"name": "X0_5_SEND", "ip": "127.0.0.1", "port": 5100, "type": "udp", "recv": 0},
            ],
        }
        conversions = [
            {
                "sources": [
                    {"alias": "k1_6", "protocol": "K1.6"},
                    {"alias": "k1_7", "protocol": "K1.7"},
                ],
                "target": {"protocol": "X0.5"},
            }
        ]

        normalized = normalize_port_config(payload, conversions=conversions)

        self.assertEqual(normalized["transport"]["messageType"], "joint_bundle")
        self.assertEqual(normalized["transport"]["recvPort"], 4100)
        self.assertEqual(normalized["transport"]["sendPort"], 5100)

    def test_filter_config_string_preserves_condition_fields_from_existing_json_shape(self) -> None:
        payload = {
            "messageType": "joint_bundle",
            "messageRuleDetailList": [
                {
                    "messageName": "k1.7",
                    "delayRequirement": 2,
                    "filterConfig": '{"crcCheck": {"enabled": true, "bindElement": "偏航2"}, "aggregation": {"mode": "BY_TIME", "count": null, "timeMs": "100", "operator": "gt", "value": 400}, "aggregationType": {"type": "ORDER", "bindElement": "纬度1"}}',
                }
            ],
            "endpoints": [
                {"name": "x0.5_recv", "ip": "192.168.2.130", "port": 7788, "type": "udp", "recv": 1},
                {"name": "k1.7_send", "ip": "192.168.23.17", "port": 23, "type": "udp", "recv": 0, "feedBackPort": 7788},
            ],
        }

        normalized = normalize_port_config(payload)

        rule = normalized["transport"]["messageRuleDetailList"][0]
        self.assertEqual(rule["filterConfig"]["aggregation"]["operator"], "GT")
        self.assertEqual(rule["filterConfig"]["aggregation"]["value"], "400")
        self.assertEqual(rule["filterConfig"]["aggregationType"]["bindElement"], "纬度1")

    def test_filter_config_rejects_unknown_condition_operator(self) -> None:
        payload = {
            "messageType": "joint_bundle",
            "messageRuleDetailList": [
                {
                    "messageName": "k1.7",
                    "delayRequirement": 2,
                    "filterConfig": {
                        "aggregation": {"mode": "BY_TIME", "timeMs": 100, "operator": "BETWEEN", "value": 400},
                        "aggregationType": {"type": "ORDER", "bindElement": "纬度1"},
                    },
                }
            ],
            "endpoints": [
                {"name": "x0.5_recv", "ip": "192.168.2.130", "port": 7788, "type": "udp", "recv": 1},
                {"name": "k1.7_send", "ip": "192.168.23.17", "port": 23, "type": "udp", "recv": 0, "feedBackPort": 7788},
            ],
        }

        with self.assertRaisesRegex(ValueError, "aggregation.operator"):
            normalize_port_config(payload)


if __name__ == "__main__":
    unittest.main()
