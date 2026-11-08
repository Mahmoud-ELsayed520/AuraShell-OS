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

Threading note: ``open_app``, ``close_app`` and ``open_path`` are BLOCKING
(bounded by ``launch_check_seconds``, ``close_timeout`` and ``open_wait``).
Callers on a GUI thread must run them in a worker thread.

Logging: events are logged on the ``core.file_app_manager`` logger. Only
canonical app IDs and denial reasons are logged, never full file paths,
recognized text, or process command lines.
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
# Extension-less executables are blocked separately via the executable bit.
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
    """Open/close allowlisted apps and open files inside approved roots.

    All public methods that touch processes are blocking; see module docs.
    Every failure is a subclass of :class:`FileAppManagerError`, except
    ``TypeError`` for arguments that are not strings.
    """

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
        # app_id -> processes we started that may still be running.
        self._launched: dict[str, list[subprocess.Popen]] = {}

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
        logger.debug("FileAppManager ready with %d approved root(s)", len(roots))

    # ------------------------------------------------------------------ apps
    def _spec(self, app_id: object) -> AppSpec:
        if not isinstance(app_id, str):
            raise TypeError("app_id must be a string")
        spec = self._registry.get(app_id.strip().casefold())
        if spec is None:
            logger.warning("Rejected unknown app ID")
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

    def _reap_launched(self) -> None:
        """Drop (and reap) launched processes that have already exited.

        ``Popen.poll()`` collects the exit status, so finished children do not
        stay behind as zombies.
        """
        for app_id in list(self._launched):
            alive = [p for p in self._launched[app_id] if p.poll() is None]
            if alive:
                self._launched[app_id] = alive
            else:
                del self._launched[app_id]

    def list_available_apps(self) -> list[str]:
        """Return sorted IDs of registry apps that are installed."""
        return sorted(
            app_id for app_id, spec in self._registry.items()
            if self._resolve_executable(spec)
        )

    def list_closable_apps(self) -> list[str]:
        """Return sorted IDs of apps that ``close_app`` is allowed to close.

        Does not check whether they are installed or running. Intended for the
        executor's panic mode, so it closes only this reviewed set.
        """
        return sorted(
            app_id for app_id, spec in self._registry.items()
            if spec.closable and spec.process_names
        )

    def open_app(self, app_id: str) -> None:
        """Launch an allowlisted application. BLOCKING for up to
        ``launch_check_seconds``.

        Raises:
            TypeError: ``app_id`` is not a string.
            UnknownAppError: ``app_id`` is not in the registry.
            AppNotInstalledError: no approved executable was found.
            AccessDeniedError: the executable exists but cannot be run.
            AppLaunchError: the process failed to start or exited non-zero
                immediately.
        """
        spec = self._spec(app_id)
        executable = self._resolve_executable(spec)
        if executable is None:
            if self._exists_but_not_executable(spec):
                logger.warning("Launch denied for '%s': no execute permission",
                               spec.app_id)
                raise AccessDeniedError(
                    f"'{spec.app_id}' is installed but you lack permission to run it.")
            logger.warning("Launch failed for '%s': not installed", spec.app_id)
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
            logger.warning("Launch denied for '%s' by the OS", spec.app_id)
            raise AccessDeniedError(
                f"Permission denied launching '{spec.app_id}'.") from exc
        except OSError as exc:
            logger.error("Launch failed for '%s': %s", spec.app_id, exc)
            raise AppLaunchError(f"Could not launch '{spec.app_id}': {exc}") from exc

        # Detect immediate crashes without blocking on long-running apps.
        try:
            code = proc.wait(timeout=self._launch_check)
        except subprocess.TimeoutExpired:
            self._reap_launched()
            self._launched.setdefault(spec.app_id, []).append(proc)
            logger.info("Launched '%s'", spec.app_id)
            return
        if code != 0:
            logger.error("'%s' exited immediately with code %s", spec.app_id, code)
            raise AppLaunchError(
                f"'{spec.app_id}' exited immediately with code {code}.")
        # Exit code 0: launcher handed off to an existing instance; that's fine.
        logger.info("Launched '%s' (handed off to an existing instance)",
                    spec.app_id)

    def close_app(self, app_id: str) -> None:
        """Ask an allowlisted application to exit (SIGTERM only). BLOCKING for
        up to ``close_timeout``.

        Only processes owned by the current user with an exact registered
        process name, plus processes this manager launched, are touched.

        Raises:
            TypeError: ``app_id`` is not a string.
            UnknownAppError: ``app_id`` is not in the registry.
            ActionNotAllowedError: closing this app is disabled.
            FileAppManagerError: ``psutil`` is not installed.
            AppNotRunningError: no matching process was found.
            AccessDeniedError: the OS refused to signal a process.
            AppCloseError: a process was still alive after the timeout.
        """
        spec = self._spec(app_id)
        if not spec.closable:
            logger.warning("Close blocked for '%s': disabled for safety",
                           spec.app_id)
            raise ActionNotAllowedError(
                f"Closing '{spec.app_id}' is disabled for safety.")
        if psutil is None:
            raise FileAppManagerError("psutil is required to close applications.")

        victims = self._find_processes(spec)
        if not victims:
            logger.info("Close requested for '%s' but it is not running",
                        spec.app_id)
            raise AppNotRunningError(f"'{spec.app_id}' is not running.")

        for proc in victims:
            try:
                proc.terminate()  # SIGTERM only; never SIGKILL
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied as exc:
                logger.warning("Close denied for '%s' by the OS", spec.app_id)
                raise AccessDeniedError(
                    f"Permission denied closing '{spec.app_id}'.") from exc

        _, alive = psutil.wait_procs(victims, timeout=self._close_timeout)
        if alive:
            logger.error("'%s' did not exit after the close request", spec.app_id)
            raise AppCloseError(
                f"'{spec.app_id}' did not exit after the close request.")
        self._launched.pop(spec.app_id, None)
        logger.info("Closed '%s'", spec.app_id)

    def _find_processes(self, spec: AppSpec) -> list:
        """Own-user processes matching the spec, plus ones we launched."""
        self._reap_launched()
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

        for tracked in self._launched.get(spec.app_id, []):
            if tracked.poll() is not None:
                continue
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
        """Validate and resolve ``path``; log the denial reason (not the path)."""
        try:
            return self._validate_path_unlogged(path)
        except FileAppManagerError as exc:
            logger.warning("Path rejected: %s", exc)
            raise

    def _validate_path_unlogged(self, path: object) -> Path:
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
        if resolved.is_file():
            if resolved.suffix.casefold() in BLOCKED_SUFFIXES:
                raise PathNotAllowedError("Executable file types cannot be opened.")
            if os.access(resolved, os.X_OK):
                # Catches extension-less binaries and renamed scripts.
                raise PathNotAllowedError("Executable files cannot be opened.")
        elif not resolved.is_dir():
            raise PathNotAllowedError("Only regular files and folders can be opened.")
        if not os.access(resolved, os.R_OK):
            raise AccessDeniedError("Permission denied.")
        return resolved

    def open_path(self, path: str) -> None:
        """Open a file or folder in the default app, if inside approved roots.
        BLOCKING for up to ``open_wait``.

        Raises:
            TypeError: ``path`` is not a string.
            PathNotAllowedError: invalid, relative, outside approved roots, or
                an executable file type.
            PathNotFoundError: the path does not exist.
            AccessDeniedError: the OS denied read access or opening.
            AppNotInstalledError: ``xdg-open`` is not available.
            AppLaunchError: the opener failed to start or returned an error.
        """
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
            logger.info("Opened a path (opener still running)")
            return  # opener is still running (blocking desktop handler); not a failure
        if code == 5:
            raise AccessDeniedError("Permission denied opening the path.")
        if code != 0:
            raise AppLaunchError(f"xdg-open failed with exit code {code}.")
        logger.info("Opened a path")
