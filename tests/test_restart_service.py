"""Restart the configured Ollama unit, never an unrelated instance."""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, call

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "ollama_request_watchdog_proxy.py"
spec = importlib.util.spec_from_file_location("watchdog_restart_test", MODULE_PATH)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class RestartServiceTests(unittest.TestCase):
    def test_restarts_configured_gpu_service(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(watchdog, "OLLAMA_SERVICE", "embedding-ollama.service"), \
             patch.object(watchdog, "LAST_RESTART", 0), \
             patch.object(watchdog.time, "monotonic", return_value=1000), \
             patch.object(watchdog, "service_exists", return_value=True), \
             patch.object(watchdog, "run", return_value=result) as run:
            self.assertTrue(watchdog.restart_ollama("test"))
            run.assert_called_once_with("systemctl restart embedding-ollama.service", timeout=90)

    def test_failed_restart_starts_same_configured_service(self):
        failure = subprocess.CompletedProcess(
            [], 1, "", "Unit embedding-ollama.service not found")
        success = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(watchdog, "OLLAMA_SERVICE", "embedding-ollama.service"), \
             patch.object(watchdog, "LAST_RESTART", 0), \
             patch.object(watchdog.time, "monotonic", return_value=1000), \
             patch.object(watchdog, "service_exists", return_value=True), \
             patch.object(watchdog, "install_missing_ollama_unit") as install, \
             patch.object(watchdog, "run", side_effect=[failure, success]) as run:
            self.assertTrue(watchdog.restart_ollama("test"))
            self.assertEqual(run.call_args_list, [
                call("systemctl restart embedding-ollama.service", timeout=90),
                call("systemctl start embedding-ollama.service", timeout=90),
            ])
            install.assert_called_once()

    def test_does_not_create_default_service_for_custom_instance(self):
        with patch.object(watchdog, "OLLAMA_SERVICE", "embedding-ollama.service"), \
             patch.object(watchdog, "service_exists") as exists, \
             patch.object(watchdog, "run") as run:
            self.assertFalse(watchdog.install_missing_ollama_unit())
            exists.assert_not_called()
            run.assert_not_called()

    def test_default_service_behavior_unchanged(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(watchdog, "OLLAMA_SERVICE", "ollama.service"), \
             patch.object(watchdog, "LAST_RESTART", 0), \
             patch.object(watchdog.time, "monotonic", return_value=1000), \
             patch.object(watchdog, "service_exists", return_value=True), \
             patch.object(watchdog, "run", return_value=result) as run:
            self.assertTrue(watchdog.restart_ollama("test"))
            run.assert_called_once_with("systemctl restart ollama.service", timeout=90)


if __name__ == "__main__":
    unittest.main()
