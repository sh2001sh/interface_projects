"""Unit tests for multi-endpoint port_config_json support."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.code_generation_adapter import normalize_port_config  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
