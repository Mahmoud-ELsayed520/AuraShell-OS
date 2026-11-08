"""Behavioral tests for the bilingual intent parser."""

from __future__ import annotations

import unittest

from core.intent_parser import IntentParser


class IntentParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = IntentParser()

    def assert_intent(self, text: str, intent: str, target: str | None) -> None:
        result = self.parser.parse(text)
        self.assertEqual(result["intent"], intent)
        self.assertEqual(result["target"], target)
        self.assertEqual(result["raw_text"], text)

    def test_open_app_in_arabic_and_english(self) -> None:
        self.assert_intent("افتح كروم", "OPEN_APP", "chrome")
        self.assert_intent("افتح فيس كود", "OPEN_APP", "vscode")
        self.assert_intent("open terminal", "OPEN_APP", "terminal")
        self.assert_intent("open calculator", "OPEN_APP", "calculator")

    def test_close_app_in_arabic_and_english(self) -> None:
        self.assert_intent("اقفل كروم", "CLOSE_APP", "chrome")
        self.assert_intent("اغلق المتصفح", "CLOSE_APP", "browser")
        self.assert_intent("close vscode", "CLOSE_APP", "vscode")

    def test_system_diagnostics(self) -> None:
        self.assert_intent("حالة النظام", "SYSTEM_DIAGNOSTICS", "system")
        self.assert_intent("استهلاك الرام", "SYSTEM_DIAGNOSTICS", "ram")
        self.assert_intent("المعالج", "SYSTEM_DIAGNOSTICS", "cpu")
        self.assert_intent("system status", "SYSTEM_DIAGNOSTICS", "system")
        self.assert_intent("ram usage", "SYSTEM_DIAGNOSTICS", "ram")

    def test_power_commands(self) -> None:
        self.assert_intent("طفي الجهاز", "POWER_COMMAND", "shutdown")
        self.assert_intent("ريستارت", "POWER_COMMAND", "restart")
        self.assert_intent("shutdown system", "POWER_COMMAND", "shutdown")
        self.assert_intent("restart", "POWER_COMMAND", "restart")

    def test_panic_precedes_generic_close_matching(self) -> None:
        self.assert_intent("طوارئ", "PANIC_MODE", "all")
        self.assert_intent("اقفل كل حاجة", "PANIC_MODE", "all")
        self.assert_intent("panic mode", "PANIC_MODE", "all")

    def test_unknown_text_is_a_general_query(self) -> None:
        self.assert_intent("Tell me a story", "GENERAL_AI_QUERY", None)

    def test_non_string_input_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            self.parser.parse(None)  # type: ignore[arg-type]


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
    """Every demo command must be classified as expected."""

    def setUp(self) -> None:
        self.parser = IntentParser()

    def test_every_demo_command_is_understood(self) -> None:
        for text, intent, target in DEMO_COMMANDS:
            with self.subTest(command=text):
                result = self.parser.parse(text)
                self.assertEqual(result["intent"], intent)
                self.assertEqual(result["target"], target)
                self.assertEqual(result["raw_text"], text)


if __name__ == "__main__":
    unittest.main()
