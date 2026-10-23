"""Create the AuraShell OS starter layout without overwriting existing files."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DIRECTORIES = ("core", "gui", "tests")
FILES = (
    "core/__init__.py",
    "core/stt.py",
    "core/tts.py",
    "core/intent_parser.py",
    "core/executor.py",
    "core/file_app_manager.py",
    "core/system_info.py",
    "gui/__init__.py",
    "gui/main_window.py",
    "tests/test_scenarios.py",
    "main.py",
    "requirements.txt",
    "PROJECT_PLAN.md",
)


def create_project_layout() -> tuple[int, int]:
    """Create missing project directories and files, preserving existing content."""
    created_directories = 0
    created_files = 0

    for relative_directory in DIRECTORIES:
        directory = PROJECT_ROOT / relative_directory
        if not directory.exists():
            directory.mkdir(parents=True)
            created_directories += 1
        elif not directory.is_dir():
            raise NotADirectoryError(os.fspath(directory))

    for relative_file in FILES:
        file_path = PROJECT_ROOT / relative_file
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.exists():
            if not file_path.is_file():
                raise IsADirectoryError(os.fspath(file_path))
            continue
        file_path.touch()
        created_files += 1

    return created_directories, created_files


def main() -> None:
    directories, files = create_project_layout()
    print(
        f"AuraShell OS layout ready at {os.fspath(PROJECT_ROOT)} "
        f"({directories} directories, {files} files created)."
    )


if __name__ == "__main__":
    main()
