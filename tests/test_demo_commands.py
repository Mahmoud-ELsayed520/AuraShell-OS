"""Demo command checks for AuraShell OS."""

import unittest

from core.intent_parser import IntentParser

DEMO_COMMANDS = [
    ("افتح كروم", "OPEN_APP", "chrome"),
    ("open terminal", "OPEN_APP", "terminal"),
    ("افتح فيس كود", "OPEN_APP", "vscode"),
    ("open calculator", "OPEN_APP", "calculator"),
    ("اقفل كروم", "CLOSE_APP", "chrome"),
    ("close vscode", "CLOSE_APP", "vscode"),
    ("اغلق المتصفح", "CLOSE_APP", "browser"),
    ("استهلاك الرام", "SYSTEM_DIAGNOSTICS", "ram"),
    ("المعالج", "SYSTEM_DIAGNOSTICS", "cpu"),
    ("cpu usage", "SYSTEM_DIAGNOSTICS", "cpu"),
    ("حالة النظام", "SYSTEM_DIAGNOSTICS", "system"),
    ("system status", "SYSTEM_DIAGNOSTICS", "system"),
    ("طفي الجهاز", "POWER_COMMAND", "shutdown"),
    ("restart", "POWER_COMMAND", "restart"),
    ("ريستارت", "POWER_COMMAND", "restart"),
    ("panic mode", "PANIC_MODE", "all"),
    ("اقفل كل حاجة", "PANIC_MODE", "all"),
    ("How does voice recognition work?", "GENERAL_AI_QUERY", None),
    ("Tell me a story", "GENERAL_AI_QUERY", None),
    ("open chrome; rm -rf /", "GENERAL_AI_QUERY", None),
]


class DemoCommandTests(unittest.TestCase):
    def setUp(self):
        self.parser = IntentParser()

    def test_every_demo_command_is_understood(self):
        for text, intent, target in DEMO_COMMANDS:
            with self.subTest(command=text):
                result = self.parser.parse(text)
                self.assertEqual(result["intent"], intent)
                self.assertEqual(result["target"], target)
                self.assertEqual(result["raw_text"], text)


if __name__ == "__main__":
    unittest.main()
