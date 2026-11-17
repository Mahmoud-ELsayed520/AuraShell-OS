
"""Safe command execution and authorization for AuraShell OS."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

from core.file_app_manager import FileAppManager, FileAppManagerError
from core.system_info import SystemInfo, SystemInfoError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None
    requires_confirmation: bool = False


class CommandExecutor:
    """Authorize and execute supported local intents."""

    POWER_COMMANDS = {
        "shutdown": ["systemctl", "poweroff"],
        "restart": ["systemctl", "reboot"],
    }

    def __init__(
        self,
        app_manager: FileAppManager | None = None,
        system_info: SystemInfo | None = None,
    ) -> None:
        self.app_manager = app_manager or FileAppManager()
        self.system_info = system_info or SystemInfo()

    def execute(
        self,
        intent: str,
        target: str | None,
        *,
        confirmed: bool = False,
    ) -> ActionResult:

        if not isinstance(intent, str):
            return ActionResult(False, "Invalid intent.")

        if target is not None and not isinstance(target, str):
            return ActionResult(False, "Invalid target.")

        intent = intent.strip().upper()
        target = target.strip().casefold() if target else None

        if intent == "OPEN_APP":
            return self._open_app(target)

        if intent == "CLOSE_APP":
            return self._close_app(target)

        if intent == "SYSTEM_DIAGNOSTICS":
            return self._diagnostics(target)

        if intent == "POWER_COMMAND":
            return self._power(target, confirmed)

        if intent == "PANIC_MODE":
            return self._panic(confirmed)

        if intent == "GENERAL_AI_QUERY":
            return ActionResult(
                False,
                "General AI integration is not configured yet."
            )

        return ActionResult(False, "Unsupported intent.")

    def _open_app(self, target: str | None) -> ActionResult:
        if not target:
            return ActionResult(False, "Missing application ID.")

        try:
            self.app_manager.open_app(target)
            return ActionResult(
                True,
                f"Successfully launched {target}."
            )
        except FileAppManagerError as exc:
            logger.warning("Application launch rejected: %s", type(exc).__name__)
            return ActionResult(False, str(exc))

    def _close_app(self, target: str | None) -> ActionResult:
        if not target:
            return ActionResult(False, "Missing application ID.")

        try:
            self.app_manager.close_app(target)
            return ActionResult(
                True,
                f"Successfully closed {target}."
            )
        except FileAppManagerError as exc:
            logger.warning("Application close rejected: %s", type(exc).__name__)
            return ActionResult(False, str(exc))

    def _diagnostics(self, target: str | None) -> ActionResult:
        if target not in {"cpu", "ram", "system"}:
            return ActionResult(False, "Unsupported diagnostics target.")

        try:
            if target == "cpu":
                data = self.system_info.cpu()

            elif target == "ram":
                data = self.system_info.memory()

            else:
                data = self.system_info.snapshot()

            return ActionResult(
                True,
                f"{target.upper()} diagnostics collected successfully.",
                data=data,
            )

        except SystemInfoError as exc:
            return ActionResult(False, str(exc))

    def _power(
        self,
        target: str | None,
        confirmed: bool,
    ) -> ActionResult:

        command = self.POWER_COMMANDS.get(target)

        if command is None:
            return ActionResult(False, "Unsupported power command.")

        if not confirmed:
            return ActionResult(
                False,
                f"Confirmation required before {target}.",
                requires_confirmation=True,
            )

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )

            if result.returncode != 0:
                logger.error("Power command failed: %s", target)
                return ActionResult(
                    False,
                    "The operating system rejected the power command.",
                )

            return ActionResult(
                True,
                f"{target.capitalize()} command accepted."
            )

        except (OSError, subprocess.TimeoutExpired):
            logger.exception("Power command execution failed")
            return ActionResult(
                False,
                "Could not execute the power command."
            )

    def _panic(self, confirmed: bool) -> ActionResult:
        """Close only reviewed, closable applications; never all processes."""

        if not confirmed:
            return ActionResult(
                False,
                "Confirmation required before closing approved applications.",
                requires_confirmation=True,
            )

        closed = []
        failed = []

        for app_id in self.app_manager.list_closable_apps():
            try:
                self.app_manager.close_app(app_id)
                closed.append(app_id)
            except FileAppManagerError:
                failed.append(app_id)

        return ActionResult(
            not failed,
            "Approved application cleanup completed."
            if not failed else "Some applications could not be closed.",
            data={
                "closed": closed,
                "failed": failed,
            },
        )

