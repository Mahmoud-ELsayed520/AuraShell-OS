"""
core/executor.py
Command Execution & Security: whitelist + safe subprocess execution.
"""

import subprocess
import shlex
from dataclasses import dataclass


@dataclass
class ExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = -1
    message: str = ""


# القائمة البيضاء: اسم الأمر (intent) -> الأمر الفعلي كـ list
# مفيش أي حاجة بتتنفذ خارج القائمة دي
ALLOWED_COMMANDS = {
    "open_browser":    ["xdg-open", "https://www.google.com"],
    "open_terminal":   ["x-terminal-emulator"],
    "open_files":      ["xdg-open", "."],
    "list_files":      ["ls", "-la"],
    "current_dir":     ["pwd"],
    "show_date":       ["date"],
    "show_disk":       ["df", "-h"],
    "show_memory":     ["free", "-h"],
    "show_uptime":     ["uptime"],
    "whoami":          ["whoami"],
    "volume_up":       ["amixer", "set", "Master", "5%+"],
    "volume_down":     ["amixer", "set", "Master", "5%-"],
    "mute":            ["amixer", "set", "Master", "toggle"],
}

TIMEOUT_SECONDS = 10

# أوامر الـ GUI بتفضل شغالة، فبنشغلها في الخلفية بدون انتظار
GUI_COMMANDS = {"open_browser", "open_terminal", "open_files"}


def is_allowed(command_name: str) -> bool:
    """يتأكد إن الأمر موجود في القائمة البيضاء."""
    return command_name in ALLOWED_COMMANDS


def execute_command(command_name: str) -> ExecutionResult:
    """
    ينفذ الأمر لو هو في القائمة البيضاء فقط.
    بيرجع ExecutionResult فيه نتيجة التنفيذ.
    """
    if not isinstance(command_name, str):
        return ExecutionResult(False, message="Invalid command type")

    command_name = command_name.strip().lower()

    if not is_allowed(command_name):
        return ExecutionResult(
            False, message=f"Command '{command_name}' is not allowed"
        )

    cmd = ALLOWED_COMMANDS[command_name]

    try:
        if command_name in GUI_COMMANDS:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return ExecutionResult(
                True, return_code=0, message=f"Launched: {shlex.join(cmd)}"
            )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            shell=False,  # مهم جداً: بدون shell عشان نمنع command injection
        )
        return ExecutionResult(
            success=(result.returncode == 0),
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            return_code=result.returncode,
            message="Executed successfully" if result.returncode == 0
                    else "Command failed",
        )

    except subprocess.TimeoutExpired:
        return ExecutionResult(False, message="Command timed out")
    except FileNotFoundError:
        return ExecutionResult(False, message="Command not found on system")
    except Exception as e:
        return ExecutionResult(False, message=f"Unexpected error: {e}")


if __name__ == "__main__":
    # اختبار سريع
    for name in ["show_date", "whoami", "rm_rf_everything"]:
        r = execute_command(name)
        print(name, "->", r.success, "|", r.stdout or r.message)
