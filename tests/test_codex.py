import os
from pathlib import Path
import sys
import tempfile
import unittest

from im_agent.codex import Session
from im_agent.contracts import Rejected


class CodexTests(unittest.TestCase):
    def session(self, mode="normal", allow=False):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        peer = Path(__file__).parent / "fixtures/fake_codex.py"
        session = Session([sys.executable, "-B", str(peer), mode], os.environ.copy(), temp.name, allow_inference=allow, timeout=8)
        self.addCleanup(session.close)
        return session

    def schema(self):
        return {"type": "object", "properties": {"answer": {"type": "string", "maxLength": 100}},
                "required": ["answer"], "additionalProperties": False}

    def test_initialize_never_enables_inference(self):
        session = self.session()
        self.assertEqual(session.initialize()["model_turns"], 0)
        with self.assertRaisesRegex(Rejected, "inference_not_enabled"): session.start("fixture", self.schema())
        with self.assertRaisesRegex(Rejected, "already_initialized"): session.initialize()

    def test_synthetic_result_and_interactive_approval_rejection(self):
        session = self.session(allow=True); session.initialize(); session.start("fixture", self.schema())
        self.assertEqual(session.wait(), {"answer": "synthetic"})

    def test_resume_preserves_thread_and_steer(self):
        session = self.session("controls", allow=True); session.initialize()
        session.start("fixture", self.schema(), thread="synthetic-thread")
        session.steer("synthetic supplement")
        self.assertEqual(session.wait(), {"answer": "synthetic"})

    def test_interrupt_is_not_reported_as_success(self):
        session = self.session("controls", allow=True); session.initialize(); session.start("fixture", self.schema())
        session.interrupt()
        with self.assertRaisesRegex(Rejected, "interrupted"): session.wait()

    def test_different_thread_on_resume_is_rejected(self):
        session = self.session("wrong-thread", allow=True); session.initialize()
        with self.assertRaisesRegex(Rejected, "binding_changed"):
            session.start("fixture", self.schema(), thread="synthetic-thread")
