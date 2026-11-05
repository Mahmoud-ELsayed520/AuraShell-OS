"""Allowlisted application and file management for AuraShell OS.

Design rules (see PROJECT_PLAN.md, section 5):

* Only canonical app IDs from ``APP_REGISTRY`` can be launched or closed.
  Recognized text is never used as a command, path, or argument.
* Executables are resolved from a fixed list of names inside trusted system
  directories only (not the caller's ``PATH``), and are run with an argument
  list, ``shell=False`` and no stdin.
* File opening is restricted to user-approved roots. Paths are resolved
  (symlinks and ``..`` included) *before* the root check.
* Failures raise domain exceptions; nothing is converted into fake success.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

try:  # psutil is only needed for close_app
    import psutil
except ImportError:  # pragma: no cover - depends on environment
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MAX_PATH_LENGTH = 4096
TRUSTED_EXECUTABLE_DIRS: tuple[str, ...] = (
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/snap/bin",
)
# Files that a desktop "open" action may execute rather than just display.
BLOCKED_SUFFIXES = frozenset(
    {
        ".desktop", ".sh", ".bash", ".run", ".appimage", ".bin", ".exe",
        ".bat", ".cmd", ".com", ".jar", ".py", ".pl", ".rb", ".service",
    }
)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class FileAppManagerError(Exception):
    """Base class for all errors raised by this module."""


class UnknownAppError(FileAppManagerError):
    """The app ID is not in the reviewed registry."""


class AppNotInstalledError(FileAppManagerError):
    """The app is known but no approved executable was found."""


class AppLaunchError(FileAppManagerError):
    """The executable was found but failed to start."""


class AppNotRunningError(FileAppManagerError):
    """There is no matching process to close."""


class AppCloseError(FileAppManagerError):
    """Matching processes were found but could not all be closed."""


class ActionNotAllowedError(FileAppManagerError):
    """The action is intentionally disabled for this app."""


class PathNotAllowedError(FileAppManagerError):
    """The path is malformed, outside approved roots, or of a blocked type."""


class PathNotFoundError(FileAppManagerError):
    """The path does not exist."""


class AccessDeniedError(FileAppManagerError):
    """The operating system denied permission."""


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AppSpec:
    """Reviewed description of one application."""

    app_id: str
    executables: tuple[str, ...]          # candidate file names, in priority order
    args: tuple[str, ...] = ()            # fixed arguments only
    process_names: tuple[str, ...] = ()   # exact process names allowed to be closed
    closable: bool = True


APP_REGISTRY: dict[str, AppSpec] = {
    spec.app_id: spec
    for spec in (
        AppSpec("chrome", ("google-chrome-stable", "google-chrome"),
                process_names=("chrome", "google-chrome")),
        AppSpec("vscode", ("code",), process_names=("code",)),
        # Closing the terminal could kill the terminal AuraShell is running in.
        AppSpec("terminal", ("gnome-terminal", "x-terminal-emulator"),
                closable=False),
        AppSpec("calculator", ("gnome-calculator", "kcalc", "galculator"),
                process_names=("gnome-calculator", "kcalc", "galculator")),
        AppSpec("browser", ("firefox", "x-www-browser"),
                process_names=("firefox",)),
    )
}


class FileAppManager:
    """Open/close allowlisted apps and open files inside approved roots."""

    def __init__(
        self,
        allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
        *,
        search_dirs: Sequence[str] = TRUSTED_EXECUTABLE_DIRS,
        registry: dict[str, AppSpec] | None = None,
        launch_check_seconds: float = 0.5,
        close_timeout: float = 3.0,
        open_wait: float = 3.0,
    ) -> None:
        self._registry = dict(registry if registry is not None else APP_REGISTRY)
        self._search_path = os.pathsep.join(search_dirs)
        self._launch_check = launch_check_seconds
        self._close_timeout = close_timeout
        self._open_wait = open_wait
        self._launched: dict[str, subprocess.Popen] = {}

        if allowed_roots is None:
            home = Path.home()
            allowed_roots = [
                home / name
                for name in ("Documents", "Downloads", "Desktop",
                             "Pictures", "Music", "Videos")
            ]
        roots: list[Path] = []
        for root in allowed_roots:
            try:
                resolved = Path(root).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                continue  # silently skipping non-existent roots is safe (deny)
            if resolved.is_dir():
                roots.append(resolved)
        self._allowed_roots = tuple(roots)

    # ------------------------------------------------------------------ apps
    def _spec(self, app_id: object) -> AppSpec:
        if not isinstance(app_id, str):
            raise TypeError("app_id must be a string")
        spec = self._registry.get(app_id.strip().casefold())
        if spec is None:
            raise UnknownAppError("Unknown application ID.")
        return spec

    def _resolve_executable(self, spec: AppSpec) -> str | None:
        for name in spec.executables:
            found = shutil.which(name, path=self._search_path)
            if found:
                return found
        return None

    def _exists_but_not_executable(self, spec: AppSpec) -> bool:
        for directory in self._search_path.split(os.pathsep):
            for name in spec.executables:
                candidate = Path(directory) / name
                if candidate.is_file() and not os.access(candidate, os.X_OK):
                    return True
        return False

    def list_available_apps(self) -> list[str]:
        """Return sorted IDs of registry apps that are installed."""
        return sorted(
            app_id for app_id, spec in self._registry.items()
            if self._resolve_executable(spec)
        )

    def open_app(self, app_id: str) -> None:
        spec = self._spec(app_id)
        executable = self._resolve_executable(spec)
        if executable is None:
            if self._exists_but_not_executable(spec):
                raise AccessDeniedError(
                    f"'{spec.app_id}' is installed but you lack permission to run it.")
            raise AppNotInstalledError(f"'{spec.app_id}' is not installed.")

        try:
            proc = subprocess.Popen(
                [executable, *spec.args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                shell=False,
            )
        except PermissionError as exc:
            raise AccessDeniedError(
                f"Permission denied launching '{spec.app_id}'.") from exc
        except OSError as exc:
            raise AppLaunchError(f"Could not launch '{spec.app_id}': {exc}") from exc

        # Detect immediate crashes without blocking on long-running apps.
        try:
            code = proc.wait(timeout=self._launch_check)
        except subprocess.TimeoutExpired:
            self._launched[spec.app_id] = proc
            return
        if code != 0:
            raise AppLaunchError(
                f"'{spec.app_id}' exited immediately with code {code}.")
        # Exit code 0: launcher handed off to an existing instance; that's fine.

    def close_app(self, app_id: str) -> None:
        spec = self._spec(app_id)
        if not spec.closable:
            raise ActionNotAllowedError(
                f"Closing '{spec.app_id}' is disabled for safety.")
        if psutil is None:
            raise FileAppManagerError("psutil is required to close applications.")

        victims = self._find_processes(spec)
        if not victims:
            raise AppNotRunningError(f"'{spec.app_id}' is not running.")

        for proc in victims:
            try:
                proc.terminate()  # SIGTERM only; never SIGKILL
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied as exc:
                raise AccessDeniedError(
                    f"Permission denied closing '{spec.app_id}'.") from exc

        _, alive = psutil.wait_procs(victims, timeout=self._close_timeout)
        if alive:
            raise AppCloseError(
                f"'{spec.app_id}' did not exit after the close request.")
        self._launched.pop(spec.app_id, None)

    def _find_processes(self, spec: AppSpec) -> list:
        """Own-user processes matching the spec, plus ones we launched."""
        me, own_pid = getpass.getuser(), os.getpid()
        names = {n.casefold() for n in spec.process_names}
        found: dict[int, object] = {}

        for proc in psutil.process_iter(["name", "username"]):
            info = proc.info
            if (
                (info.get("name") or "").casefold() in names
                and info.get("username") == me
                and proc.pid != own_pid
            ):
                found[proc.pid] = proc

        tracked = self._launched.get(spec.app_id)
        if tracked is not None and tracked.poll() is None:
            try:
                root = psutil.Process(tracked.pid)
                for proc in [root, *root.children(recursive=True)]:
                    if proc.pid != own_pid:
                        found[proc.pid] = proc
            except psutil.NoSuchProcess:
                pass
        return list(found.values())

    # ----------------------------------------------------------------- files
    def _validate_path(self, path: object) -> Path:
        if not isinstance(path, str):
            raise TypeError("path must be a string")
        if not path.strip() or len(path) > MAX_PATH_LENGTH or "\x00" in path:
            raise PathNotAllowedError("Invalid path.")
        if not self._allowed_roots:
            raise PathNotAllowedError("No approved directories are configured.")

        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise PathNotAllowedError("Only absolute paths (or ~) are accepted.")

        try:
            resolved = candidate.resolve(strict=True)  # follows symlinks and ..
        except FileNotFoundError as exc:
            raise PathNotFoundError("Path does not exist.") from exc
        except PermissionError as exc:
            raise AccessDeniedError("Permission denied.") from exc
        except (OSError, RuntimeError) as exc:  # e.g. symlink loop
            raise PathNotAllowedError("Path could not be resolved.") from exc

        if not any(resolved.is_relative_to(root) for root in self._allowed_roots):
            raise PathNotAllowedError("Path is outside the approved directories.")
        if resolved.is_file() and resolved.suffix.casefold() in BLOCKED_SUFFIXES:
            raise PathNotAllowedError("Executable file types cannot be opened.")
        if not resolved.is_dir() and not resolved.is_file():
            raise PathNotAllowedError("Only regular files and folders can be opened.")
        if not os.access(resolved, os.R_OK):
            raise AccessDeniedError("Permission denied.")
        return resolved

    def open_path(self, path: str) -> None:
        """Open a file or folder in the default app, if inside approved roots."""
        resolved = self._validate_path(path)
        opener = shutil.which("xdg-open", path=self._search_path)
        if opener is None:
            raise AppNotInstalledError("xdg-open is not installed.")
        try:
            proc = subprocess.Popen(
                [opener, str(resolved)],  # absolute path: cannot look like an option
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                shell=False,
            )
        except PermissionError as exc:
            raise AccessDeniedError("Permission denied launching opener.") from exc
        except OSError as exc:
            raise AppLaunchError(f"Could not open path: {exc}") from exc

        try:
            code = proc.wait(timeout=self._open_wait)
        except subprocess.TimeoutExpired:
            return  # opener is still running (blocking desktop handler); not a failure
        if code == 5:
            raise AccessDeniedError("Permission denied opening the path.")
        if code != 0:
            raise AppLaunchError(f"xdg-open failed with exit code {code}.")
