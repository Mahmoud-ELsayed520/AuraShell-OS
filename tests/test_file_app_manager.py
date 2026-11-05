"""Tests for FileAppManager. No real app is launched or killed."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import file_app_manager as fam
from core.file_app_manager import (
    AccessDeniedError, ActionNotAllowedError, AppCloseError, AppLaunchError,
    AppNotInstalledError, AppNotRunningError, FileAppManager, PathNotAllowedError,
    PathNotFoundError, UnknownAppError,
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.root = self.tmp / "docs"
        self.root.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def fake_exe(self, name: str) -> None:
        p = self.bin / name
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)

    def manager(self, **kw) -> FileAppManager:
        return FileAppManager([self.root], search_dirs=[str(self.bin)],
                              launch_check_seconds=0.01, **kw)


class OpenAppTests(Base):
    def test_unknown_and_malformed_ids_rejected(self) -> None:
        m = self.manager()
        for bad in ("rm -rf /", "../chrome", "", "chrome; ls"):
            with self.assertRaises(UnknownAppError):
                m.open_app(bad)
        with self.assertRaises(TypeError):
            m.open_app(None)  # type: ignore[arg-type]

    def test_missing_app_reported(self) -> None:
        with self.assertRaises(AppNotInstalledError):
            self.manager().open_app("chrome")

    def test_launch_uses_arg_list_no_shell(self) -> None:
        self.fake_exe("code")
        proc = mock.Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("code", 0.01)
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc) as popen:
            self.manager().open_app("VSCode")
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(self.bin / "code")])
        self.assertIs(kwargs["shell"], False)

    def test_immediate_failure_is_an_error(self) -> None:
        self.fake_exe("code")
        proc = mock.Mock()
        proc.wait.return_value = 1
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc):
            with self.assertRaises(AppLaunchError):
                self.manager().open_app("vscode")

    def test_permission_error_maps_to_access_denied(self) -> None:
        self.fake_exe("code")
        with mock.patch.object(fam.subprocess, "Popen", side_effect=PermissionError):
            with self.assertRaises(AccessDeniedError):
                self.manager().open_app("vscode")

    def test_installed_but_not_executable_is_permission_error(self) -> None:
        p = self.bin / "code"
        p.write_text("x")
        p.chmod(0o644)
        if os.access(p, os.X_OK):
            self.skipTest("running with privileges that ignore mode bits")
        with self.assertRaises(AccessDeniedError):
            self.manager().open_app("vscode")

    def test_list_available_apps(self) -> None:
        self.fake_exe("kcalc")
        self.fake_exe("code")
        self.assertEqual(self.manager().list_available_apps(), ["calculator", "vscode"])

    def test_executable_not_taken_from_caller_path(self) -> None:
        # An exe on PATH but outside trusted dirs must be ignored.
        other = self.tmp / "evil"
        other.mkdir()
        (other / "code").write_text("x")
        (other / "code").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(other)}):
            with self.assertRaises(AppNotInstalledError):
                self.manager().open_app("vscode")


class FakeProc:
    def __init__(self, pid: int, name: str, user: str) -> None:
        self.pid, self.info = pid, {"name": name, "username": user}
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class CloseAppTests(Base):
    def patched_psutil(self, procs, alive=()):
        ps = mock.MagicMock()
        ps.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        ps.AccessDenied = type("AccessDenied", (Exception,), {})
        ps.process_iter.return_value = procs
        ps.wait_procs.return_value = ([], list(alive))
        return mock.patch.object(fam, "psutil", ps)

    def test_terminal_close_disabled(self) -> None:
        with self.assertRaises(ActionNotAllowedError):
            self.manager().close_app("terminal")

    def test_closes_only_own_user_exact_name_matches(self) -> None:
        me = "tester"
        mine = FakeProc(10, "chrome", me)
        other_user = FakeProc(11, "chrome", "root")
        lookalike = FakeProc(12, "chrome-helper-x", me)
        with mock.patch.object(fam.getpass, "getuser", return_value=me), \
             self.patched_psutil([mine, other_user, lookalike]):
            self.manager().close_app("chrome")
        self.assertTrue(mine.terminated)
        self.assertFalse(other_user.terminated)
        self.assertFalse(lookalike.terminated)

    def test_not_running(self) -> None:
        with self.patched_psutil([]):
            with self.assertRaises(AppNotRunningError):
                self.manager().close_app("vscode")

    def test_survivors_reported(self) -> None:
        me = "tester"
        p = FakeProc(10, "code", me)
        with mock.patch.object(fam.getpass, "getuser", return_value=me), \
             self.patched_psutil([p], alive=[p]):
            with self.assertRaises(AppCloseError):
                self.manager().close_app("vscode")


class OpenPathTests(Base):
    def open_ok(self, path: str) -> mock.Mock:
        self.fake_exe("xdg-open")
        with mock.patch.object(fam.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.manager().open_path(path)
        return popen

    def test_file_inside_root_opens(self) -> None:
        f = self.root / "a.txt"
        f.write_text("hi")
        popen = self.open_ok(str(f))
        self.assertEqual(popen.call_args[0][0], [str(self.bin / "xdg-open"), str(f)])
        self.assertIs(popen.call_args[1]["shell"], False)

    def test_traversal_rejected(self) -> None:
        (self.outside / "secret.txt").write_text("x")
        self.fake_exe("xdg-open")
        with self.assertRaises(PathNotAllowedError):
            self.manager().open_path(f"{self.root}/../outside/secret.txt")

    def test_symlink_escape_rejected(self) -> None:
        (self.outside / "secret.txt").write_text("x")
        (self.root / "link").symlink_to(self.outside)
        self.fake_exe("xdg-open")
        with self.assertRaises(PathNotAllowedError):
            self.manager().open_path(str(self.root / "link" / "secret.txt"))

    def test_sibling_prefix_not_confused_with_root(self) -> None:
        sibling = self.tmp / "docs-evil"
        sibling.mkdir()
        (sibling / "a.txt").write_text("x")
        self.fake_exe("xdg-open")
        with self.assertRaises(PathNotAllowedError):
            self.manager().open_path(str(sibling / "a.txt"))

    def test_bad_inputs(self) -> None:
        m = self.manager()
        for bad in ("", "   ", "relative/file.txt", "a\x00b", "/" + "a" * 5000):
            with self.assertRaises(PathNotAllowedError):
                m.open_path(bad)
        with self.assertRaises(TypeError):
            m.open_path(None)  # type: ignore[arg-type]

    def test_missing_file(self) -> None:
        with self.assertRaises(PathNotFoundError):
            self.manager().open_path(str(self.root / "nope.txt"))

    def test_executable_types_blocked(self) -> None:
        f = self.root / "run.sh"
        f.write_text("echo hi")
        self.fake_exe("xdg-open")
        with self.assertRaises(PathNotAllowedError):
            self.manager().open_path(str(f))

    def test_opener_failure_reported(self) -> None:
        f = self.root / "a.txt"
        f.write_text("hi")
        self.fake_exe("xdg-open")
        for code, exc in ((5, AccessDeniedError), (4, AppLaunchError)):
            with mock.patch.object(fam.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = code
                with self.assertRaises(exc):
                    self.manager().open_path(str(f))

    def test_no_roots_means_deny(self) -> None:
        m = FileAppManager([self.tmp / "missing"], search_dirs=[str(self.bin)])
        with self.assertRaises(PathNotAllowedError):
            m.open_path(str(self.root))


if __name__ == "__main__":
    unittest.main()
