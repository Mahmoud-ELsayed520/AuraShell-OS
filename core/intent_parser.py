"""Regex-based Arabic and English command classification for AuraShell OS."""

from __future__ import annotations

import json
import re
import sys
from typing import Pattern


IntentResult = dict[str, str | None]


class IntentParser:
    """Classify a recognized phrase without executing the requested action."""

    _OPEN_ALIASES = {
        "google chrome": "chrome",
        "chrome": "chrome",
        "جوجل كروم": "chrome",
        "كروم": "chrome",
        "visual studio code": "vscode",
        "vs code": "vscode",
        "vscode": "vscode",
        "في اس كود": "vscode",
        "فيس كود": "vscode",
        "terminal": "terminal",
        "ترمينال": "terminal",
        "طرفية": "terminal",
        "calculator": "calculator",
        "calc": "calculator",
        "الحاسبة": "calculator",
        "الالة الحاسبة": "calculator",
        "browser": "browser",
        "المتصفح": "browser",
    }
    _CLOSE_ALIASES = {
        **_OPEN_ALIASES,
    }
    _ARABIC_TRANSLATION = str.maketrans(
        {
            "أ": "ا",
            "إ": "ا",
            "آ": "ا",
            "ٱ": "ا",
            "ى": "ي",
        }
    )
    _DIACRITICS = re.compile(r"[\u064b-\u065f\u0670\u0640]")
    _DIAGNOSTIC_PATTERNS: tuple[tuple[Pattern[str], str], ...] = (
        (
            re.compile(
                r"(?<!\w)(?:استهلاك\s+الرام|استخدام\s+الرام|ram\s+usage|"
                r"memory\s+usage|ram)(?!\w)",
                re.IGNORECASE,
            ),
            "ram",
        ),
        (
            re.compile(
                r"(?<!\w)(?:استهلاك\s+المعالج|استخدام\s+المعالج|"
                r"cpu\s+usage|processor\s+usage|المعالج|cpu)(?!\w)",
                re.IGNORECASE,
            ),
            "cpu",
        ),
        (
            re.compile(
                r"(?<!\w)(?:حالة\s+النظام|system\s+status|"
                r"system\s+diagnostics|حاله\s+النظام)(?!\w)",
                re.IGNORECASE,
            ),
            "system",
        ),
    )
    _PANIC_PATTERN = re.compile(
        r"(?<!\w)(?:طوارئ|وضع\s+الطوارئ|اقفل\s+كل\s+حاجة|"
        r"اغلق\s+كل\s+حاجة|panic\s+mode|panic|close\s+everything)(?!\w)",
        re.IGNORECASE,
    )
    _POWER_PATTERNS: tuple[tuple[Pattern[str], str], ...] = (
        (
            re.compile(
                r"(?<!\w)(?:ريستارت|اعادة\s+تشغيل|اعد\s+التشغيل|"
                r"restart|reboot)(?!\w)",
                re.IGNORECASE,
            ),
            "restart",
        ),
        (
            re.compile(
                r"(?<!\w)(?:طفي\s+الجهاز|اطفي\s+الجهاز|"
                r"ايقاف\s+التشغيل|shutdown(?:\s+system)?|power\s+off)(?!\w)",
                re.IGNORECASE,
            ),
            "shutdown",
        ),
    )

    def __init__(self) -> None:
        self._open_pattern = self._build_app_pattern(
            r"(?:افتح|شغل|open|launch|start)", self._OPEN_ALIASES
        )
        self._close_pattern = self._build_app_pattern(
            r"(?:اقفل|اغلق|سكر|close)", self._CLOSE_ALIASES
        )

    @classmethod
    def _build_app_pattern(
        cls, command_pattern: str, aliases: dict[str, str]
    ) -> Pattern[str]:
        ordered_aliases = sorted(aliases, key=len, reverse=True)
        alternatives = "|".join(
            re.escape(alias).replace(r"\ ", r"\s+") for alias in ordered_aliases
        )
        return re.compile(
            rf"^\s*(?:{command_pattern})\s+(?P<target>{alternatives})"
            r"\s*[.!؟?]*\s*$",
            re.IGNORECASE,
        )

    @classmethod
    def _normalize(cls, text: str) -> str:
        unmarked = cls._DIACRITICS.sub("", text.translate(cls._ARABIC_TRANSLATION))
        return re.sub(r"\s+", " ", unmarked.casefold()).strip()

    def parse(self, text: str) -> IntentResult:
        """Return the best local intent, or ``GENERAL_AI_QUERY`` as a fallback.

        ``raw_text`` always contains the exact original input. This method is
        deliberately classification-only; callers must separately authorize
        and execute any local action.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")

        normalized = self._normalize(text)
        result: IntentResult = {
            "intent": "GENERAL_AI_QUERY",
            "target": None,
            "raw_text": text,
        }

        if self._PANIC_PATTERN.search(normalized):
            result.update(intent="PANIC_MODE", target="all")
            return result

        for pattern, target_map, intent in (
            (self._open_pattern, self._OPEN_ALIASES, "OPEN_APP"),
            (self._close_pattern, self._CLOSE_ALIASES, "CLOSE_APP"),
        ):
            match = pattern.fullmatch(normalized)
            if match:
                alias = match.group("target")
                result.update(intent=intent, target=target_map[alias])
                return result

        for pattern, target in self._DIAGNOSTIC_PATTERNS:
            if pattern.search(normalized):
                result.update(intent="SYSTEM_DIAGNOSTICS", target=target)
                return result

        for pattern, target in self._POWER_PATTERNS:
            if pattern.search(normalized):
                result.update(intent="POWER_COMMAND", target=target)
                return result

        return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = IntentParser()
    examples = (
        "افتح كروم",
        "افتح فيس كود",
        "اقفل كروم",
        "حالة النظام",
        "استهلاك الرام",
        "طفي الجهاز",
        "open terminal",
        "open calculator",
        "close vscode",
        "system status",
        "ram usage",
        "restart",
        "panic mode",
        "How does voice recognition work?",
    )
    for example in examples:
        print(json.dumps(parser.parse(example), ensure_ascii=False))
