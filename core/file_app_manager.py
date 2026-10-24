"""Allowlisted application control and sandboxed file operations for AuraShell OS.

Design rules (see PROJECT_PLAN.md, section 5):
* Apps are addressed by canonical IDs (``chrome``, ``vscode`` ...) that resolve
  through a reviewed registry. Recognized text is never run as a command.
* No ``shell=True``: processes start from a fixed executable + fixed arguments.
* File operations are confined to user-approved root directories. Paths are
  resolved first, so ``..`` traversal and symlink escapes are rejected.
* Deleting requires an explicit ``confirmed=True`` supplied by the caller
  (after the user confirmed in the GUI/CLI). Nothing is deleted otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import psutil


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class FileAppManagerError(Exception):
    """Base class for all domain errors raised by this module."""


class UnknownAppError(FileAppManagerError):
    """The app ID is not in the allowlist."""


class AppNotInstalledError(FileAppManagerError):
    """The app is allowlisted but no matching executable was found."""


class AppLaunchError(FileAppManagerError):
    """The operating system failed to start the application."""


class AppNotRunningError(FileAppManagerError):
    """There is no running process to close for this app."""


class PathNotAllowedError(FileAppManagerError):
    """The path resolves outside the approved root directories."""


class ConfirmationRequiredError(FileAppManagerError):
    """A destructive action was requested without explicit confirmation."""


# --------------------------------------------------------------------------
# Reviewed application registry
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AppSpec:
    """Fixed launch description for one canonical app ID."""

    executables: tuple[str, ...]  # tried in order, resolved with shutil.which
    args: tuple[str, ...] = ()  # fixed arguments only
    process_names: tuple[str, ...] = ()  # names matched when closing


APP_REGISTRY: dict[str, AppSpec] = {
    "chrome": AppSpec(
        executables=(
            "google-chrome",
            "google-chrome-stable",
            "chromium-browser",
            "chromium",
        ),
        process_names=("chrome", "google-chrome", "chromium", "chromium-browser"),
    ),
    "vscode": AppSpec(executables=("code",), process_names=("code",)),
    "terminal": AppSpec(
        executables=("gnome-terminal", "x-terminal-emulator"),
        # Linux truncates process names to 15 characters.
        process_names=("gnome-terminal-server", "gnome-terminal-"),
    ),
    "calculator": AppSpec(
        executables=("gnome-calculator", "kcalc", "mate-calc"),
        process_names=("gnome-calculator", "kcalc", "mate-calc"),
    ),
    "browser": AppSpec(
        executables=("firefox", "google-chrome", "chromium-browser"),
        process_names=("firefox", "firefox-bin", "chrome", "chromium"),
    ),
}

_CLOSE_GRACE_SECONDS = 5.0
_MAX_PATH_LENGTH = 4096
_MAX_FILE_BYTES = 1_000_000  # 1 MB cap for create_file content


class FileAppManager:
    """Open/close allowlisted apps and manage files inside approved roots."""

    def __init__(self, allowed_roots: Sequence[str | os.PathLike[str]] | None = None) -> None:
        roots = allowed_roots or (Path.home() / "AuraShell",)
        resolved_roots: list[Path] = []
        for root in roots:
            resolved = Path(root).expanduser().resolve()
            if resolved.parent == resolved:
                raise ValueError("The filesystem root cannot be an approved root")
            resolved.mkdir(parents=True, exist_ok=True)
            resolved_roots.append(resolved)
        self._roots: tuple[Path, ...] = tuple(resolved_roots)
        self._launched: list[subprocess.Popen[bytes]] = []

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------
    @staticmethod
    def _spec(app_id: str) -> AppSpec:
        if not isinstance(app_id, str):
            raise TypeError("app_id must be a string")
        try:
            return APP_REGISTRY[app_id]
        except KeyError:
            raise UnknownAppError(f"Application '{app_id}' is not allowed") from None

    @staticmethod
    def _find_executable(spec: AppSpec) -> str | None:
        for name in spec.executables:
            found = shutil.which(name)
            if found:
                return found
        return None

    def _reap_finished(self) -> None:
        """Collect exited children so they do not stay as zombies."""
        self._launched = [p for p in self._launched if p.poll() is None]

    def list_available_apps(self) -> list[str]:
        """Return allowlisted app IDs whose executable is installed."""
        return sorted(
            app_id
            for app_id, spec in APP_REGISTRY.items()
            if self._find_executable(spec) is not None
        )

    def open_app(self, app_id: str) -> None:
        """Start an allowlisted application (no shell, fixed arguments)."""
        spec = self._spec(app_id)
        executable = self._find_executable(spec)
        if executable is None:
            raise AppNotInstalledError(f"'{app_id}' is not installed on this system")
        self._reap_finished()
        try:
            process = subprocess.Popen(
                [executable, *spec.args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise AppLaunchError(f"Could not start '{app_id}': {exc}") from exc
        self._launched.append(process)

    def close_app(self, app_id: str) -> None:
        """Gracefully terminate the current user's processes of an allowed app.

        Sends SIGTERM first and SIGKILL only to processes that are still alive
        after a short grace period. Never touches other users' processes or
        AuraShell itself (or its parent terminal).
        """
        spec = self._spec(app_id)
        protected = {os.getpid()}
        current = psutil.Process()
        protected.update(parent.pid for parent in current.parents())
        owner = current.username()

        targets: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name", "username"]):
            info = proc.info
            if (
                info["name"] in spec.process_names
                and info["username"] == owner
                and info["pid"] not in protected
            ):
                targets.append(proc)

        if not targets:
            raise AppNotRunningError(f"'{app_id}' is not running")

        for proc in targets:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _, alive = psutil.wait_procs(targets, timeout=_CLOSE_GRACE_SECONDS)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self._reap_finished()

    # ------------------------------------------------------------------
    # Files (sandboxed to approved roots)
    # ------------------------------------------------------------------
    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self._roots

    def _resolve(self, path: str | os.PathLike[str]) -> Path:
        """Resolve ``path`` and ensure it stays inside an approved root."""
        if not isinstance(path, (str, os.PathLike)):
            raise TypeError("path must be a string or path-like object")
        raw = os.fspath(path)
        if not raw.strip() or "\x00" in raw or len(raw) > _MAX_PATH_LENGTH:
            raise ValueError("Invalid path")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self._roots[0] / candidate
        resolved = candidate.resolve(strict=False)  # follows symlinks and '..'
        if not any(resolved.is_relative_to(root) for root in self._roots):
            raise PathNotAllowedError(f"Path is outside the approved folders: {raw}")
        if resolved in self._roots:
            raise PathNotAllowedError("Approved root folders themselves cannot be modified")
        return resolved

    def open_path(self, path: str) -> None:
        """Open an existing file/folder inside approved roots with the desktop default."""
        resolved = self._resolve(path)
        if not resolved.exists():
            raise FileNotFoundError(f"No such file or folder: {resolved}")
        opener = shutil.which("xdg-open")
        if opener is None:
            raise AppNotInstalledError("xdg-open is not installed")
        try:
            process = subprocess.Popen(
                [opener, str(resolved)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise AppLaunchError(f"Could not open '{resolved}': {exc}") from exc
        self._launched.append(process)

    def create_file(self, path: str, content: str = "", *, make_parents: bool = True) -> Path:
        """Create a new file. Never overwrites an existing one."""
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
            raise ValueError("Content is too large")
        resolved = self._resolve(path)
        if make_parents:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "x", encoding="utf-8") as handle:  # "x" -> FileExistsError
            handle.write(content)
        return resolved

    def create_directory(self, path: str) -> Path:
        """Create a new folder inside approved roots."""
        resolved = self._resolve(path)
        resolved.mkdir(parents=True, exist_ok=False)
        return resolved

    def describe_delete(self, path: str) -> str:
        """Return the text to show the user when asking for delete confirmation."""
        resolved = self._resolve(path)
        return f"Permanently delete this file? {resolved}"

    def delete_file(self, path: str, *, confirmed: bool = False) -> None:
        """Delete one file. Requires ``confirmed=True`` from an explicit user answer."""
        resolved = self._resolve(path)
        if not resolved.exists():
            raise FileNotFoundError(f"No such file: {resolved}")
        if not resolved.is_file():
            raise ValueError("Only files can be deleted (not folders)")
        if confirmed is not True:
            raise ConfirmationRequiredError(self.describe_delete(path))
        resolved.unlink()


if __name__ == "__main__":
    # Manual demo. Deletion asks for an explicit typed confirmation.
    manager = FileAppManager()
    print("Approved folder:", manager.allowed_roots[0])
    print("Available apps:", manager.list_available_apps())

    created = manager.create_file("demo/hello.txt", "hello from AuraShell")
    print("Created:", created)

    try:
        manager.delete_file("demo/hello.txt")
    except ConfirmationRequiredError as exc:
        print("Confirmation needed ->", exc)
        answer = input("Type YES to delete: ").strip()
        if answer in {"YES", "نعم"}:
            manager.delete_file("demo/hello.txt", confirmed=True)
            print("Deleted.")
        else:
            print("Cancelled. File kept.")