
"""AuraShell OS - Interactive voice and keyboard assistant."""

from __future__ import annotations

import json
import sys

from core.intent_parser import IntentParser
from core.executor import CommandExecutor
from core.file_app_manager import FileAppManager
from core.system_info import SystemInfo
from core.stt import listen_and_recognize


BANNER = r"""
    ___                    _____ __         ____
   /   | __  ______ _____ / ___// /_  ___  / / /
  / /| |/ / / / __ `/ __ \\__ \/ __ \/ _ \/ / /
 / ___ / /_/ / /_/ / /_/ /__/ / / / /  __/ / /
/_/  |_\__,_/\__,_/\____/____/_/ /_/\___/_/_/
                  A U R A S H E L L   O S
"""


def display_result(result) -> None:
    """Display an action result."""
    status = "SUCCESS" if result.ok else "FAILED"

    print(f"\n[{status}] {result.message}")

    if result.data is not None:
        print(json.dumps(
            result.data,
            indent=4,
            ensure_ascii=False
        ))

    print()


def confirm_action(message: str) -> bool:
    """Require an explicit keyboard confirmation."""
    print("\n" + "=" * 45)
    print("SECURITY CONFIRMATION REQUIRED")
    print(message)
    print("Type YES to continue, anything else to cancel.")
    print("=" * 45)

    try:
        answer = input("Confirmation: ").strip()
    except (EOFError, KeyboardInterrupt):
        return False

    return answer == "YES"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(BANNER)
    print("Welcome to AuraShell OS!")

    parser = IntentParser()
    manager = FileAppManager()
    diagnostics = SystemInfo()

    executor = CommandExecutor(
        app_manager=manager,
        system_info=diagnostics,
    )

    print("\nChoose input mode:")
    print("1. Keyboard")
    print("2. Voice (Arabic / English)")

    try:
        mode = input("\nYour choice: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nExiting AuraShell OS.")
        return 0

    if mode not in {"1", "2"}:
        print("Invalid choice.")
        return 1

    print("\nAuraShell OS is ready!")
    print("Type or say 'exit', 'quit' or 'خروج' to stop.")
    print("Type 'help' to see available commands.\n")

    while True:
        try:
            if mode == "1":
                recognized_text = input("AuraShell> ")

            else:
                print("Listening...")

                recognized_text = listen_and_recognize()

                if recognized_text is None:
                    print("Could not recognize speech. Try again.\n")
                    continue

                print(f"You said: {recognized_text}")

            if not recognized_text.strip():
                continue

            command = recognized_text.strip().casefold()

            if command in {"exit", "quit", "خروج"}:
                print("AuraShell OS stopped.")
                break

            if command == "help":
                print("""
Available commands:

Open applications:
    افتح كروم
    open terminal
    open calculator

Close applications:
    اقفل كروم
    close vscode

System diagnostics:
    حالة النظام
    استهلاك الرام
    المعالج

Power:
    طفي الجهاز
    ريستارت

Other:
    طوارئ
    exit
""")
                continue

            parsed = parser.parse(recognized_text)

            print("\nRecognized intent:")
            print(json.dumps(
                parsed,
                ensure_ascii=False,
                indent=4
            ))

            result = executor.execute(
                parsed["intent"],
                parsed["target"],
            )

            if result.requires_confirmation:
                approved = confirm_action(result.message)

                if not approved:
                    print("\nAction cancelled.\n")
                    continue

                result = executor.execute(
                    parsed["intent"],
                    parsed["target"],
                    confirmed=True,
                )

            display_result(result)

        except KeyboardInterrupt:
            print("\nAuraShell OS stopped.")
            break

        except EOFError:
            print("\nInput stream closed.")
            break

        except Exception as error:
            print(f"\nUnexpected error: {type(error).__name__}")
            print("The command was not completed.\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

