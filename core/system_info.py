
"""Read-only system diagnostics for AuraShell OS."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None


class SystemInfoError(RuntimeError):
    """Raised when system information cannot be collected."""


class SystemInfo:
    """Collect CPU, memory, disk and uptime information."""

    def __init__(self) -> None:
        if psutil is None:
            raise SystemInfoError(
                "psutil is missing. Install it using: pip install psutil"
            )

    def memory(self) -> dict[str, int | float]:
        """Return memory information in bytes and percentage."""
        mem = psutil.virtual_memory()

        return {
            "total": mem.total,
            "available": mem.available,
            "used": mem.used,
            "free": mem.free,
            "percent": mem.percent,
        }

    def cpu(self, interval: float = 0.1) -> dict[str, int | float]:
        """Return CPU utilization information."""
        if not isinstance(interval, (int, float)) or isinstance(interval, bool):
            raise ValueError("interval must be a number")

        if not 0 <= interval <= 2:
            raise ValueError("interval must be between 0 and 2 seconds")

        return {
            "percent": psutil.cpu_percent(interval=interval),
            "cores": psutil.cpu_count(logical=True) or 0,
            "physical_cores": psutil.cpu_count(logical=False) or 0,
        }

    def disk(self, path: str = "/") -> dict[str, int | float | str]:
        """Return disk usage for a specified mounted path."""
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")

        target = Path(path).expanduser()

        if not target.exists():
            raise SystemInfoError("Disk path does not exist.")

        usage = psutil.disk_usage(str(target))

        return {
            "path": str(target.resolve()),
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": usage.percent,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a complete system diagnostics snapshot."""
        try:
            boot_time = psutil.boot_time()
            uptime = max(0.0, time.time() - boot_time)

            return {
                "hostname": os.uname().nodename,
                "platform": os.uname().sysname,
                "cpu": self.cpu(),
                "memory": self.memory(),
                "disk": self.disk("/"),
                "uptime_seconds": round(uptime, 2),
            }
        except (OSError, ValueError) as exc:
            raise SystemInfoError(
                "Could not collect system information."
            ) from exc


if __name__ == "__main__":
    import json

    try:
        info = SystemInfo()
        print(json.dumps(info.snapshot(), indent=4, ensure_ascii=False))
    except SystemInfoError as error:
        print(f"System diagnostics error: {error}")

