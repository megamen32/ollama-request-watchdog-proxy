import importlib.util
import json
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "ollama_request_watchdog_proxy.py"
spec = importlib.util.spec_from_file_location("watchdog", MODULE_PATH)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class CtxGuardTests(unittest.TestCase):
    def clamp(self, payload):
        body = json.dumps(payload).encode("utf-8")
        new_body, headers, error = watchdog.clamp_body_if_needed(
            "POST", "/api/generate", body, {"Content-Type": "application/json"}
        )
        self.assertIsNone(error)
        return json.loads(new_body.decode("utf-8"))

    def test_clamps_dangerous_context_for_known_model(self):
        out = self.clamp({
            "model": "qwen3:4b-instruct",
            "prompt": "ok",
            "options": {"num_ctx": 65536},
        })
        self.assertEqual(out["options"]["num_ctx"], 4096)

    def test_injects_default_context_when_missing(self):
        out = self.clamp({"model": "unknown-model", "prompt": "ok", "options": {}})
        self.assertEqual(out["options"]["num_ctx"], 8192)

    def test_raises_tiny_context_to_safe_minimum(self):
        out = self.clamp({"model": "unknown-model", "prompt": "ok", "options": {"num_ctx": 32}})
        self.assertEqual(out["options"]["num_ctx"], 256)


if __name__ == "__main__":
    unittest.main()
