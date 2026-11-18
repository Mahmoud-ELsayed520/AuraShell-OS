"""
AuraShell OS - System Info & Diagnostics

Provides CPU, RAM, disk, uptime, and current-user process monitoring.

Units:
- Memory and disk values: bytes
- Usage values: percentage (%)
- Uptime: seconds
"""

import getpass
import logging
import math
import time

try:
    import psutil
except ImportError:
    psutil = None


logger = logging.getLogger(__name__)


class SystemInfoError(Exception):
    """Raised when system information cannot be retrieved."""


class SystemInfo:
    """Collect system diagnostics using psutil."""

    @staticmethod
    def is_available() -> bool:
        """
        Check whether psutil is installed.

        This checks library availability, not whether every
        system operation is accessible.
        """
        return psutil is not None

    @staticmethod
    def _require_psutil():
        """Ensure psutil is available before using it."""
        if psutil is None:
            raise SystemInfoError(
                "psutil is not installed. Install it using: pip install psutil"
            )

    def snapshot(self) -> dict:
        """
        Return CPU, RAM, and system uptime information.

        RAM percentage is taken directly from
        psutil.virtual_memory().percent.

        Memory values are returned in bytes.
        Uptime is returned in seconds.
        """
        self._require_psutil()

        try:
            cpu_percent = psutil.cpu_percent(interval=0.2)
            memory = psutil.virtual_memory()
            uptime_seconds = time.time() - psutil.boot_time()

            return {
                "cpu_percent": round(cpu_percent, 1),
                "ram_percent": memory.percent,
                "ram_used_bytes": memory.used,
                "ram_total_bytes": memory.total,
                "ram_available_bytes": memory.available,
                "uptime_seconds": round(uptime_seconds, 1),
            }

        except Exception as exc:
            logger.exception("Failed to retrieve system snapshot")
            raise SystemInfoError(
                "Unable to retrieve system information."
            ) from exc

    def disk(self, disk_path: str = "/") -> dict:
        """
        Return disk usage information for the selected path.

        Disk size values are returned in bytes.
        Usage is returned as a percentage.
        """
        self._require_psutil()

        try:
            disk_info = psutil.disk_usage(disk_path)

            return {
                "mountpoint": disk_path,
                "disk_percent": disk_info.percent,
                "disk_used_bytes": disk_info.used,
                "disk_total_bytes": disk_info.total,
                "disk_free_bytes": disk_info.free,
            }

        except Exception as exc:
            logger.exception("Failed to retrieve disk information")
            raise SystemInfoError(
                f"Unable to retrieve disk information for {disk_path}."
            ) from exc

    def processes(
        self,
        limit: int = 10,
        sort_by: str = "cpu",
        sample_interval: float = 0.2,
    ) -> dict:
        """
        Return current-user processes sorted by CPU or memory usage.

        CPU is measured using two readings separated by sample_interval,
        because the first cpu_percent(None) reading is not meaningful.

        Each process contains:
        pid, name, cpu, memory, username.

        CPU and memory values are percentages.
        'total' represents the number of successfully sampled processes
        before applying the limit.
        """
        self._require_psutil()

        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")

        if sort_by not in ("cpu", "memory"):
            raise ValueError("sort_by must be 'cpu' or 'memory'")

        if (
            isinstance(sample_interval, bool)
            or not isinstance(sample_interval, (int, float))
            or not math.isfinite(sample_interval)
            or sample_interval < 0
        ):
            raise ValueError("sample_interval must be a non-negative number")

        if limit == 0:
            return {"total": 0, "items": []}

        current_user = getpass.getuser()
        process_list = []

        try:
            candidates = list(
                psutil.process_iter(
                    ["pid", "name", "username", "memory_percent"]
                )
            )

            # First CPU reading: establish the baseline.
            valid_processes = []

            for proc in candidates:
                try:
                    info = proc.info

                    # Only include processes belonging to the current user.
                    if info.get("username") != current_user:
                        continue

                    proc.cpu_percent(interval=None)
                    valid_processes.append(proc)

                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    continue

            if sample_interval > 0:
                time.sleep(sample_interval)

            # Second CPU reading: obtain measured usage.
            for proc in valid_processes:
                try:
                    info = proc.info

                    cpu_percent = proc.cpu_percent(interval=None)
                    memory_percent = info.get("memory_percent")

                    if memory_percent is None:
                        continue

                    process_list.append({
                        "pid": info["pid"],
                        "name": info.get("name") or "Unknown",
                        "cpu": round(cpu_percent, 1),
                        "memory": round(memory_percent, 1),
                        "username": info["username"],
                    })

                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    continue

            # Sort processes by the requested metric.
            process_list.sort(
                key=lambda process: process[sort_by],
                reverse=True,
            )

            total = len(process_list)

            return {
                "total": total,
                "items": process_list[:limit],
            }

        except SystemInfoError:
            raise

        except Exception as exc:
            logger.exception("Failed to retrieve process information")
            raise SystemInfoError(
                "Unable to retrieve process information."
            ) from exc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    system = SystemInfo()

    print("=== AuraShell System Diagnostics ===")
    print(f"psutil available: {system.is_available()}")

    if system.is_available():
        try:
            print("\n=== System Snapshot ===")
            print(system.snapshot())

            print("\n=== Disk Information ===")
            print(system.disk())

            print("\n=== Top Processes by CPU ===")
            print(system.processes(limit=5, sort_by="cpu"))

            print("\n=== Top Processes by Memory ===")
            print(system.processes(limit=5, sort_by="memory"))

        except SystemInfoError as exc:
            logger.error("Diagnostics error: %s", exc)
