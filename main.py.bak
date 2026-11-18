"""Interactive terminal entry point for the AuraShell OS parser prototype."""

from __future__ import annotations

import json
import sys

from core.intent_parser import IntentParser


BANNER = r"""
    ___                    _____ __         ____
   /   | __  ______ _____ / ___// /_  ___  / / /
  / /| |/ / / / __ `/ __ \\__ \/ __ \/ _ \/ / /
 / ___ / /_/ / /_/ / /_/ /__/ / / / /  __/ / /
/_/  |_\__,_/\__,_/\____/____/_/ /_/\___/_/_/
                  A U R A S H E L L   O S
"""


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print(BANNER)
    print("Parser-only mode. Type a command, or exit / quit / خروج to stop.")
    parser = IntentParser()

    try:
        while True:
            try:
                recognized_text = input("AuraShell> ")
            except EOFError:
                print("\nInput stream closed. AuraShell OS stopped.")
                break

            if recognized_text.strip().casefold() in {"exit", "quit", "خروج"}:
                print("AuraShell OS stopped.")
                break

            parsed = parser.parse(recognized_text)
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        print("\nInterrupted. AuraShell OS stopped.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
