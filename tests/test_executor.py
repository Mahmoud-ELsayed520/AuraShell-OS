
import unittest
from unittest.mock import Mock, patch

from core.executor import CommandExecutor


class ExecutorTests(unittest.TestCase):

    def setUp(self):
        self.manager = Mock()
        self.info = Mock()
        self.executor = CommandExecutor(
            app_manager=self.manager,
            system_info=self.info,
        )

    def test_open_app(self):
        result = self.executor.execute("OPEN_APP", "chrome")
        self.manager.open_app.assert_called_once_with("chrome")
        self.assertTrue(result.ok)

    def test_close_app(self):
        result = self.executor.execute("CLOSE_APP", "vscode")
        self.manager.close_app.assert_called_once_with("vscode")
        self.assertTrue(result.ok)

    def test_cpu_diagnostics(self):
        self.info.cpu.return_value = {"percent": 20}
        result = self.executor.execute("SYSTEM_DIAGNOSTICS", "cpu")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["percent"], 20)

    def test_ram_diagnostics(self):
        self.info.memory.return_value = {"percent": 40}
        result = self.executor.execute("SYSTEM_DIAGNOSTICS", "ram")
        self.assertTrue(result.ok)

    def test_invalid_intent(self):
        result = self.executor.execute("UNKNOWN", None)
        self.assertFalse(result.ok)



    def test_invalid_app(self):
        from core.file_app_manager import UnknownAppError

        self.manager.open_app.side_effect = UnknownAppError("Unknown app")

        result = self.executor.execute("OPEN_APP", "unknown")

        self.assertFalse(result.ok)

    def test_power_requires_confirmation(self):
        with patch("core.executor.subprocess.run") as run:
            result = self.executor.execute("POWER_COMMAND", "shutdown")
            self.assertTrue(result.requires_confirmation)
            run.assert_not_called()

    def test_power_invalid_target(self):
        result = self.executor.execute("POWER_COMMAND", "unknown")
        self.assertFalse(result.ok)

    def test_panic_requires_confirmation(self):
        result = self.executor.execute("PANIC_MODE", "all")
        self.assertTrue(result.requires_confirmation)
        self.manager.list_closable_apps.assert_not_called()

    def test_general_query_not_executed(self):
        result = self.executor.execute("GENERAL_AI_QUERY", None)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
