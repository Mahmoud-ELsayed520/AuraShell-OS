"""English response rendering for AuraShell OS."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_RESPONSE_LENGTH = 300

APP_NAMES = {
    "chrome": "Google Chrome",
    "vscode": "Visual Studio Code",
    "terminal": "the terminal",
    "calculator": "the calculator",
    "browser": "the browser",
}

CODE_ALIASES = {
    "SUCCESS": "OK",
    "APP_NOT_FOUND": "APP_NOT_INSTALLED",
    "NOT_INSTALLED": "APP_NOT_INSTALLED",
    "CONFIRMATION_REQUIRED": "NEEDS_CONFIRMATION",
}


def _get(result: Any, *names: str, default: Any = None) -> Any:
    """Get the first available field from a mapping or object."""

    for name in names:
        if isinstance(result, Mapping) and name in result:
            return result[name]

        if hasattr(result, name):
            return getattr(result, name)

    return default


def _number(value: Any) -> float | None:
    """Convert a value to a valid number."""

    if isinstance(value, bool):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if number != number or abs(number) == float("inf"):
        return None

    return number


def _format_number(value: Any, digits: int = 0) -> str | None:
    """Format a number with the requested precision."""

    number = _number(value)

    if number is None:
        return None

    rounded = round(number, digits)

    if digits == 0 or rounded.is_integer():
        return str(int(rounded))

    return f"{rounded:.{digits}f}".rstrip("0").rstrip(".")


def _format_gb(value: Any) -> str | None:
    """Convert bytes to gigabytes."""

    number = _number(value)

    if number is None or number < 0:
        return None

    return f"{number / (1024 ** 3):.1f}".rstrip("0").rstrip(".")


def _get_app_name(target: Any) -> str:
    """Return a readable application name."""

    key = str(target or "").strip().casefold()

    return APP_NAMES.get(key, "the application")


def _get_data(result: Any) -> Mapping[str, Any]:
    """Extract result data safely."""

    data = _get(result, "data", default={})

    return data if isinstance(data, Mapping) else {}


def _get_code(result: Any) -> str:
    """Determine the result code."""

    raw_code = _get(result, "code", default=None)

    if raw_code:
        code = str(raw_code).strip().upper()

    elif _get(result, "requires_confirmation", default=False):
        code = "NEEDS_CONFIRMATION"

    elif _get(result, "ok", default=None) is True:
        code = "OK"

    elif _get(result, "ok", default=None) is False:
        message = str(
            _get(result, "message", "summary", default="")
        ).casefold()

        if "not installed" in message:
            code = "APP_NOT_INSTALLED"
        elif "not running" in message:
            code = "APP_NOT_RUNNING"
        elif "unknown application" in message or "unknown app" in message:
            code = "UNKNOWN_APP"
        elif "permission" in message or "access denied" in message:
            code = "ACCESS_DENIED"
        elif "not found" in message or "does not exist" in message:
            code = "NOT_FOUND"
        elif "confirmation required" in message:
            code = "NEEDS_CONFIRMATION"
        elif "unsupported intent" in message or "invalid intent" in message:
            code = "UNKNOWN_COMMAND"
        else:
            code = "INTERNAL_ERROR"

    else:
        code = "UNKNOWN_COMMAND"

    return CODE_ALIASES.get(code, code)


def _get_file_name(target: Any) -> str:
    """Extract a readable file name from a target."""

    if isinstance(target, Mapping):
        target = target.get("name") or target.get("path") or "the file"

    name = str(target or "the file")
    name = name.replace("\\", "/").rstrip("/").split("/")[-1]

    return name[:80]


def _get_status_message(
    code: str,
    intent: str,
    target: Any
) -> str:
    """Generate a response for an action status."""

    app = _get_app_name(target)

    if code == "OK":
        if intent == "OPEN_APP":
            return f"Opened {app}."

        if intent == "CLOSE_APP":
            return f"Closed {app}."

        if intent == "DELETE_FILE":
            return "The file was deleted successfully."

        if intent == "CREATE_FILE":
            return "The file was created successfully."

        if intent == "CREATE_FOLDER":
            return "The folder was created successfully."

        if intent == "RENAME_FILE":
            return "The file was renamed successfully."

        if intent == "POWER_COMMAND":
            return "The system command was accepted."

        if intent == "PANIC_MODE":
            return "Approved applications were closed."

        return "The operation completed successfully."

    if code == "NEEDS_CONFIRMATION":
        if intent == "DELETE_FILE":
            name = _get_file_name(target)
            return (
                f"You are about to delete {name}. "
                "Say confirm or cancel."
            )

        if intent == "POWER_COMMAND":
            return "Are you sure? Say confirm or cancel."

        if intent == "PANIC_MODE":
            return (
                "Are you sure you want to close all applications? "
                "Say confirm or cancel."
            )

        return "This action needs confirmation. Say confirm or cancel."

    messages = {
        "APP_NOT_INSTALLED":
            f"{app} is not installed on this device.",

        "APP_NOT_RUNNING":
            f"{app} is not currently running.",

        "UNKNOWN_APP":
            "The requested application is not recognized.",

        "NOT_FOUND":
            "The requested file or path was not found.",

        "PATH_INVALID":
            "The file path is invalid or not allowed.",

        "ACCESS_DENIED":
            "You do not have permission to perform this action.",

        "ALREADY_EXISTS":
            "A file or folder with that name already exists.",

        "CANCELLED":
            "The operation was cancelled.",

        "CONFIRM_EXPIRED":
            "The confirmation expired. Please request the action again.",

        "UNKNOWN_COMMAND":
            "I didn't understand that command. Please try again.",

        "INTERNAL_ERROR":
            "Something went wrong while performing the operation.",
    }

    return messages.get(
        code,
        "Something went wrong while performing the operation."
    )


def _format_diagnostics(
    data: Mapping[str, Any],
    target: Any
) -> str:
    """Generate a readable system diagnostics response."""

    metric = str(target or "system").casefold()

    if metric == "system" and isinstance(data.get("memory"), Mapping):
        return _format_diagnostics(data["memory"], "ram")

    percent = _format_number(data.get("percent"))

    if metric in {"ram", "memory"}:
        if percent is None:
            return "Could not read memory usage."

        used = _format_gb(data.get("used"))
        total = _format_gb(data.get("total"))

        if used is not None and total is not None:
            return (
                f"Memory usage is {percent} percent, "
                f"{used} of {total} GB."
            )

        return f"Memory usage is {percent} percent."

    if metric == "cpu":
        if percent is None:
            return "Could not read CPU usage."

        return f"CPU usage is {percent} percent."

    if metric == "disk":
        if percent is None:
            return "Could not read disk usage."

        used = _format_gb(data.get("used"))
        total = _format_gb(data.get("total"))

        if used is not None and total is not None:
            return (
                f"Disk usage is {percent} percent, "
                f"{used} of {total} GB."
            )

        return f"Disk usage is {percent} percent."

    return "System information was collected."


def _format_file_list(data: Mapping[str, Any]) -> str:
    """Generate a short response for a file listing."""

    items = data.get("items", data.get("files", []))

    if not isinstance(items, (list, tuple)):
        items = []

    names = []

    for item in items[:5]:
        if isinstance(item, Mapping):
            name = item.get("name") or item.get("filename")
        else:
            name = item

        if name is not None:
            clean_name = (
                str(name)
                .replace("\\", "/")
                .rstrip("/")
                .split("/")[-1]
            )

            if clean_name:
                names.append(clean_name[:60])

    count_value = _number(
        data.get("total", data.get("count", len(items)))
    )

    count = (
        max(0, int(count_value))
        if count_value is not None
        else len(items)
    )

    if count == 0:
        return "There are no files in this folder."

    if names:
        joined_names = ", ".join(names)
        more = " and others" if count > len(names) else ""

        return (
            f"You have {count} files: "
            f"{joined_names}{more}."
        )

    return f"You have {count} files."


def _format_process_list(data: Mapping[str, Any]) -> str:
    """Generate a short response for a running process listing."""

    processes = data.get(
        "processes",
        data.get("items", data.get("process_list", []))
    )

    if not isinstance(processes, (list, tuple)):
        processes = []

    process_names = []

    for process in processes[:5]:
        if isinstance(process, Mapping):
            name = (
                process.get("name")
                or process.get("process_name")
                or process.get("image_name")
            )
        else:
            name = process

        if name is not None:
            clean_name = str(name).strip()

            if clean_name:
                process_names.append(clean_name[:60])

    count_value = _number(
        data.get(
            "total",
            data.get("count", data.get("process_count", len(processes)))
        )
    )

    count = (
        max(0, int(count_value))
        if count_value is not None
        else len(processes)
    )

    if count == 0:
        return "There are no running processes."

    if process_names:
        names = ", ".join(process_names)
        more = " and others" if count > len(process_names) else ""

        return (
            f"There are {count} running processes: "
            f"{names}{more}."
        )

    return f"There are {count} running processes."


def render(
    result: Any,
    intent: str,
    target: Any,
    lang: str
) -> str:
    """Convert an ActionResult into a short response.

    Args:
        result: ActionResult or a compatible mapping/object.
        intent: Canonical intent name, such as OPEN_APP.
        target: Application name, file path, or diagnostic metric.
        lang: Language parameter. English is used by this module.

    Returns:
        A human-readable string suitable for speech and display.
    """

    action = str(intent or "UNKNOWN").strip().upper()

    code = _get_code(result)
    data = _get_data(result)

    if code == "OK" and action == "SYSTEM_DIAGNOSTICS":
        message = _format_diagnostics(data, target)

    elif code == "OK" and action == "LIST_FILES":
        message = _format_file_list(data)

    elif code == "OK" and action == "SHOW_PROCESSES":
        message = _format_process_list(data)

    else:
        message = _get_status_message(
            code,
            action,
            target
        )

    message = " ".join(str(message).split()).strip()

    if len(message) > MAX_RESPONSE_LENGTH:
        message = (
            message[:MAX_RESPONSE_LENGTH - 1].rstrip()
            + "..."
        )

    return messagerr
