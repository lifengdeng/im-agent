"""Optional pinned-SDK compatibility; never establishes a real connection."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from im_agent.feishu import normalize, receive, receive_events
from im_agent.contracts import Rejected
from im_agent.twin_config import load

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("lark_oapi"), "optional Feishu SDK is not installed")
class SDKCompatibilityTests(unittest.TestCase):
    def test_official_event_object_preserves_identity_and_original_text(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        raw = json.loads((ROOT / "examples/digital-twin/message.example.json").read_text(encoding="utf-8"))
        config = load(ROOT / "examples/digital-twin/instance.example.json")
        config["profile"] = "feishu"; config["feishu"]["accept_after_ms"] = 1800000000000
        raw["event"]["message"]["create_time"] = "1800000000001"
        value = normalize(json.loads(lark.JSON.marshal(P2ImMessageReceiveV1(raw))), config)
        self.assertEqual(value["actor"]["user"], "example-colleague")
        self.assertEqual(value["input"]["text"], "检查示例项目最近一次发布")

    def test_long_connection_registration_uses_the_pinned_sdk_without_connecting(self):
        import lark_oapi as lark
        with patch.object(lark.ws, "Client", autospec=True) as client:
            receive(load(ROOT / "examples/digital-twin/instance.example.json"), self.fail, "synthetic-app-secret")
        client.return_value.start.assert_called_once_with()
        self.assertIsInstance(client.call_args.kwargs["event_handler"], lark.EventDispatcherHandler)

    def test_sdk_error_does_not_expose_signed_urls_or_credentials(self):
        import lark_oapi as lark
        with patch.object(lark.ws, "Client", autospec=True) as client:
            client.return_value.start.side_effect = RuntimeError("synthetic-signed-url-and-credential")
            with self.assertRaises(Rejected) as caught:
                receive(load(ROOT / "examples/digital-twin/instance.example.json"), self.fail, "synthetic-app-secret")
        self.assertEqual(str(caught.exception), "feishu_connection_failed")

    def test_pairing_receiver_uses_authenticated_sdk_objects_without_connecting(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        captured = []
        raw = json.loads((ROOT / "examples/digital-twin/message.example.json").read_text(encoding="utf-8"))
        with patch.object(lark.ws, "Client", autospec=True) as client:
            receive_events("example-app", "synthetic-app-secret", captured.append)
        handler = client.call_args.kwargs["event_handler"]
        # Use the dispatcher's registered callback, as the SDK does after authentication.
        processor = handler._processorMap["p2.im.message.receive_v1"]
        processor.do(P2ImMessageReceiveV1(raw))
        self.assertEqual(captured[0]["event"]["sender"]["sender_id"]["open_id"], "example-colleague")
        client.return_value.start.assert_called_once_with()
