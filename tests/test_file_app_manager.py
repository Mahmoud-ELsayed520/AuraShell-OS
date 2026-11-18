"""Tests for FileAppManager. No real app is launched or killed."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import file_app_manager as fam
from core.file_app_manager import (
    AccessDeniedError, ActionNotAllowedError, AppCloseError, AppLaunchError,
    AppNotInstalledError, AppNotRunningError, FileAppManager, FileOperationError,
    NotAFileError, PathAlreadyExistsError, PathNotAllowedError, PathNotFoundError,
    UnknownAppError,
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


ME, OTHER = 1000, 0  # fake real UIDs


class FakeProc:
    def __init__(self, pid: int, name: str, uid: int) -> None:
        self.pid = pid
        self.info = {"name": name, "uids": SimpleNamespace(real=uid)}
        self.terminated = False
        self.terminate_error: Exception | None = None

    def terminate(self) -> None:
        if self.terminate_error is not None:
            raise self.terminate_error
        self.terminated = True

    def children(self, recursive: bool = False) -> list:
        return []


class CloseAppTests(Base):
    def patched_psutil(self, procs, alive=()):
        ps = mock.MagicMock()
        ps.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        ps.AccessDenied = type("AccessDenied", (Exception,), {})
        ps.process_iter.return_value = procs
        ps.wait_procs.return_value = ([], list(alive))
        self.ps = ps
        return mock.patch.object(fam, "psutil", ps)

    def test_terminal_close_disabled(self) -> None:
        with self.assertRaises(ActionNotAllowedError):
            self.manager().close_app("terminal")

    def test_closes_only_own_uid_exact_name_matches(self) -> None:
        mine = FakeProc(10, "chrome", ME)
        other_user = FakeProc(11, "chrome", OTHER)
        lookalike = FakeProc(12, "chrome-helper-x", ME)
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
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
        p = FakeProc(10, "code", ME)
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
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


class OpenAppExtraTests(Base):
    def test_exit_code_zero_is_handoff_not_error(self) -> None:
        self.fake_exe("code")
        proc = mock.Mock()
        proc.wait.return_value = 0
        m = self.manager()
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc):
            m.open_app("vscode")
        self.assertEqual(m._launched, {})

    def test_id_is_trimmed_and_case_insensitive(self) -> None:
        self.fake_exe("code")
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc):
            self.manager().open_app("  VSCODE  ")

    def test_finished_processes_are_reaped(self) -> None:
        self.fake_exe("code")
        proc = mock.Mock()
        proc.wait.side_effect = subprocess.TimeoutExpired("code", 0.01)
        proc.poll.return_value = None
        m = self.manager()
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc):
            m.open_app("vscode")
        self.assertEqual(len(m._launched["vscode"]), 1)
        proc.poll.return_value = 0  # the app exited on its own
        m._reap_launched()
        self.assertEqual(m._launched, {})

    def test_two_launches_are_both_tracked(self) -> None:
        self.fake_exe("code")
        procs = []
        for _ in range(2):
            p = mock.Mock()
            p.wait.side_effect = subprocess.TimeoutExpired("code", 0.01)
            p.poll.return_value = None
            procs.append(p)
        m = self.manager()
        with mock.patch.object(fam.subprocess, "Popen", side_effect=procs):
            m.open_app("vscode")
            m.open_app("vscode")
        self.assertEqual(len(m._launched["vscode"]), 2)

    def test_launch_is_logged(self) -> None:
        self.fake_exe("code")
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc):
            with self.assertLogs(fam.logger, level="INFO") as logs:
                self.manager().open_app("vscode")
        self.assertTrue(any("vscode" in line for line in logs.output))

    def test_unknown_app_is_logged_without_the_input(self) -> None:
        with self.assertLogs(fam.logger, level="WARNING") as logs:
            with self.assertRaises(UnknownAppError):
                self.manager().open_app("rm -rf /")
        self.assertFalse(any("rm -rf" in line for line in logs.output))


class CloseAppExtraTests(CloseAppTests):
    def test_bad_ids(self) -> None:
        m = self.manager()
        with self.assertRaises(TypeError):
            m.close_app(None)  # type: ignore[arg-type]
        with self.assertRaises(UnknownAppError):
            m.close_app("chrome; ls")

    def test_id_with_spaces_and_caps_is_normalised(self) -> None:
        p = FakeProc(10, "code", ME)
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
             self.patched_psutil([p]):
            self.manager().close_app("  VSCode ")
        self.assertTrue(p.terminated)

    def test_psutil_missing_is_reported(self) -> None:
        with mock.patch.object(fam, "psutil", None):
            with self.assertRaises(fam.FileAppManagerError):
                self.manager().close_app("chrome")

    def test_access_denied_during_terminate(self) -> None:
        p = FakeProc(10, "code", ME)
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
             self.patched_psutil([p]):
            p.terminate_error = self.ps.AccessDenied()
            with self.assertRaises(AccessDeniedError):
                self.manager().close_app("vscode")

    def test_process_that_vanishes_is_not_an_error(self) -> None:
        p = FakeProc(10, "code", ME)
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
             self.patched_psutil([p]):
            p.terminate_error = self.ps.NoSuchProcess()
            self.manager().close_app("vscode")

    def test_closes_tracked_launched_process_and_forgets_it(self) -> None:
        self.fake_exe("code")
        launched = mock.Mock()
        launched.wait.side_effect = subprocess.TimeoutExpired("code", 0.01)
        launched.poll.return_value = None
        launched.pid = 77
        m = self.manager()
        with mock.patch.object(fam.subprocess, "Popen", return_value=launched):
            m.open_app("vscode")
        root = FakeProc(77, "wrapper", ME)
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
             self.patched_psutil([]):
            self.ps.Process.return_value = root
            m.close_app("vscode")
        self.assertTrue(root.terminated)
        self.assertNotIn("vscode", m._launched)

    def test_list_closable_apps_excludes_terminal(self) -> None:
        closable = self.manager().list_closable_apps()
        self.assertEqual(closable, ["browser", "calculator", "chrome", "editor", "files", "firefox", "system_monitor", "vscode"])
        self.assertNotIn("terminal", closable)


class OpenPathExtraTests(Base):
    def test_folder_inside_root_opens(self) -> None:
        sub = self.root / "sub"
        sub.mkdir()
        self.fake_exe("xdg-open")
        with mock.patch.object(fam.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.manager().open_path(str(sub))
        self.assertEqual(popen.call_args[0][0], [str(self.bin / "xdg-open"), str(sub)])

    def test_uppercase_blocked_suffix(self) -> None:
        f = self.root / "RUN.SH"
        f.write_text("echo hi")
        self.fake_exe("xdg-open")
        with self.assertRaises(PathNotAllowedError):
            self.manager().open_path(str(f))

    def test_extensionless_executable_blocked(self) -> None:
        f = self.root / "tool"
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
        self.fake_exe("xdg-open")
        with self.assertRaises(PathNotAllowedError):
            self.manager().open_path(str(f))

    def test_renamed_script_with_safe_extension_blocked(self) -> None:
        f = self.root / "notes.txt"
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
        self.fake_exe("xdg-open")
        with self.assertRaises(PathNotAllowedError):
            self.manager().open_path(str(f))

    def test_missing_xdg_open(self) -> None:
        f = self.root / "a.txt"
        f.write_text("hi")
        with self.assertRaises(AppNotInstalledError):
            self.manager().open_path(str(f))

    def test_rejection_is_logged_without_the_path(self) -> None:
        f = self.outside / "secret.txt"
        f.write_text("x")
        with self.assertLogs(fam.logger, level="WARNING") as logs:
            with self.assertRaises(PathNotAllowedError):
                self.manager().open_path(str(f))
        self.assertFalse(any("secret" in line for line in logs.output))


class NewBehaviourTests(CloseAppTests):
    """Tests for the review fixes."""

    def test_user_is_matched_by_uid_not_environment(self) -> None:
        mine = FakeProc(10, "code", ME)
        with mock.patch.dict(os.environ, {"USER": "root", "LOGNAME": "root"}), \
             mock.patch.object(fam, "_current_uid", return_value=ME), \
             self.patched_psutil([mine]):
            self.manager().close_app("vscode")
        self.assertTrue(mine.terminated)

    def test_process_with_unreadable_uid_is_skipped(self) -> None:
        p = FakeProc(10, "code", ME)
        p.info["uids"] = None
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
             self.patched_psutil([p]):
            with self.assertRaises(AppNotRunningError):
                self.manager().close_app("vscode")
        self.assertFalse(p.terminated)

    def test_access_denied_still_tries_other_processes(self) -> None:
        denied, ok = FakeProc(10, "code", ME), FakeProc(11, "code", ME)
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
             self.patched_psutil([denied, ok]):
            denied.terminate_error = self.ps.AccessDenied()
            with self.assertRaises(AccessDeniedError):
                self.manager().close_app("vscode")
        self.assertTrue(ok.terminated)

    def test_close_all_nothing_running_is_not_a_failure(self) -> None:
        with self.patched_psutil([]):
            result = self.manager().close_all_closable()
        self.assertEqual(result["closed"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["not_running"],
                         ["browser", "calculator", "chrome", "editor", "files", "firefox", "system_monitor", "vscode"])

    def test_close_all_reports_closed_and_failed(self) -> None:
        a, b = FakeProc(10, "code", ME), FakeProc(11, "chrome", ME)
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
             self.patched_psutil([a, b]):
            b.terminate_error = self.ps.AccessDenied()
            result = self.manager().close_all_closable()
        self.assertEqual(result["closed"], ["vscode"])
        self.assertEqual(result["failed"], ["chrome"])
        self.assertNotIn("terminal", sum(result.values(), []))

    def test_close_all_without_psutil_reports_failures(self) -> None:
        with mock.patch.object(fam, "psutil", None):
            result = self.manager().close_all_closable()
        self.assertEqual(result["closed"], [])
        self.assertEqual(len(result["failed"]), 8)

    def test_browser_does_not_use_alternatives_symlink(self) -> None:
        self.fake_exe("x-www-browser")
        self.assertEqual(self.manager().list_available_apps(), [])

    def test_launch_oserror_message_has_no_raw_details(self) -> None:
        self.fake_exe("code")
        err = OSError(8, "secret-detail /home/someone/x")
        with mock.patch.object(fam.subprocess, "Popen", side_effect=err):
            with self.assertRaises(AppLaunchError) as ctx:
                self.manager().open_app("vscode")
        self.assertNotIn("secret-detail", str(ctx.exception))

    def test_open_path_oserror_message_has_no_raw_details(self) -> None:
        f = self.root / "a.txt"
        f.write_text("hi")
        self.fake_exe("xdg-open")
        err = OSError(8, "secret-detail")
        with mock.patch.object(fam.subprocess, "Popen", side_effect=err):
            with self.assertRaises(AppLaunchError) as ctx:
                self.manager().open_path(str(f))
        self.assertNotIn("secret-detail", str(ctx.exception))

    def test_manager_has_launch_lock(self) -> None:
        self.assertTrue(hasattr(self.manager()._lock, "acquire"))

    def test_extra_blocked_suffixes(self) -> None:
        self.fake_exe("xdg-open")
        for name in ("a.deb", "a.RPM", "a.js", "a.ps1", "a.vbs", "a.html",
                     "a.htm", "a.php", "a.url", "a.pyc", "a.snap"):
            f = self.root / name
            f.write_text("x")
            with self.subTest(name=name):
                with self.assertRaises(PathNotAllowedError):
                    self.manager().open_path(str(f))

    def test_safe_document_types_still_open(self) -> None:
        for name in ("a.pdf", "a.txt", "a.png", "a.odt"):
            f = self.root / name
            f.write_text("x")
            with self.subTest(name=name):
                popen = OpenPathTests.open_ok(self, str(f))
                self.assertEqual(popen.call_count, 1)

    def test_tilde_is_expanded_inside_approved_root(self) -> None:
        home = self.tmp / "home"
        (home / "Documents").mkdir(parents=True)
        (home / "Documents" / "a.txt").write_text("x")
        self.fake_exe("xdg-open")
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
             mock.patch.object(fam.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            m = FileAppManager(search_dirs=[str(self.bin)])  # default roots
            m.open_path("~/Documents/a.txt")
        self.assertEqual(popen.call_count, 1)

    def test_default_roots_skip_missing_folders(self) -> None:
        home = self.tmp / "home2"
        (home / "Downloads").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            m = FileAppManager(search_dirs=[str(self.bin)])
        self.assertEqual(m._allowed_roots, ((home / "Downloads").resolve(),))


class ExecutorIntegrationTests(Base):
    """CommandExecutor + the real FileAppManager (OS calls mocked)."""

    def test_executor_open_and_unknown_app(self) -> None:
        from core.executor import CommandExecutor
        self.fake_exe("code")
        proc = mock.Mock()
        proc.wait.return_value = 0
        ex = CommandExecutor(app_manager=self.manager(), system_info=mock.Mock())
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc):
            self.assertTrue(ex.execute("OPEN_APP", "vscode").ok)
        self.assertFalse(ex.execute("OPEN_APP", "rm -rf /").ok)

    def test_executor_missing_app_is_failure(self) -> None:
        from core.executor import CommandExecutor
        ex = CommandExecutor(app_manager=self.manager(), system_info=mock.Mock())
        result = ex.execute("OPEN_APP", "chrome")
        self.assertFalse(result.ok)

    def test_executor_close_terminal_is_refused(self) -> None:
        from core.executor import CommandExecutor
        ex = CommandExecutor(app_manager=self.manager(), system_info=mock.Mock())
        self.assertFalse(ex.execute("CLOSE_APP", "terminal").ok)


class NewAppRegistryTests(Base):
    def test_new_apps_resolve_their_executables(self) -> None:
        for exe in ("firefox", "nautilus", "gedit", "gnome-system-monitor"):
            self.fake_exe(exe)
        self.assertEqual(
            self.manager().list_available_apps(),
            ["browser", "editor", "files", "firefox", "system_monitor"],
        )

    def test_firefox_launch_uses_fixed_executable(self) -> None:
        self.fake_exe("firefox")
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc) as popen:
            self.manager().open_app("Firefox")
        self.assertEqual(popen.call_args[0][0], [str(self.bin / "firefox")])

    def test_editor_falls_back_to_gedit(self) -> None:
        self.fake_exe("gedit")
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch.object(fam.subprocess, "Popen", return_value=proc) as popen:
            self.manager().open_app("editor")
        self.assertEqual(popen.call_args[0][0], [str(self.bin / "gedit")])

    def test_firefox_process_variants_are_closable(self) -> None:
        procs = [FakeProc(1, "firefox-bin", ME), FakeProc(2, "firefox-esr", ME),
                 FakeProc(3, "firefox-evil", ME)]
        ps = mock.MagicMock()
        ps.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        ps.AccessDenied = type("AccessDenied", (Exception,), {})
        ps.process_iter.return_value = procs
        ps.wait_procs.return_value = ([], [])
        with mock.patch.object(fam, "_current_uid", return_value=ME), \
             mock.patch.object(fam, "psutil", ps):
            self.manager().close_app("firefox")
        self.assertTrue(procs[0].terminated and procs[1].terminated)
        self.assertFalse(procs[2].terminated)


class WorkspaceBase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.ws = self.tmp / "ws"

    def wm(self, **kw) -> FileAppManager:
        return FileAppManager([self.root], search_dirs=[str(self.bin)],
                              workspace_root=self.ws, **kw)

    def make(self, name: str, text: str = "") -> Path:
        self.ws.mkdir(mode=0o700, exist_ok=True)
        p = self.ws / name
        p.write_text(text)
        return p


class WorkspaceTests(WorkspaceBase):
    def test_workspace_is_created_lazily_and_private(self) -> None:
        m = self.wm()
        self.assertFalse(self.ws.exists())  # nothing happens in the constructor
        m.create_file("a.txt")
        self.assertTrue(self.ws.is_dir())
        self.assertEqual(stat.S_IMODE(self.ws.stat().st_mode) & 0o077, 0)

    def test_default_workspace_is_in_home_and_not_created_by_init(self) -> None:
        home = self.tmp / "h"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            FileAppManager(search_dirs=[str(self.bin)])
        self.assertFalse((home / "AuraShell_Workspace").exists())

    def test_default_workspace_used_on_first_file_operation(self) -> None:
        home = self.tmp / "h2"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            FileAppManager(search_dirs=[str(self.bin)]).create_file("a.txt")
        self.assertTrue((home / "AuraShell_Workspace" / "a.txt").is_file())

    def test_workspace_owned_by_someone_else_is_refused(self) -> None:
        self.ws.mkdir(mode=0o700)
        with mock.patch.object(fam, "_current_uid", return_value=os.getuid() + 1):
            with self.assertRaises(PathNotAllowedError):
                self.wm().create_file("a.txt")
        self.assertFalse((self.ws / "a.txt").exists())

    def test_workspace_that_is_a_file_is_an_error(self) -> None:
        self.ws.write_text("not a folder")
        with self.assertRaises(FileOperationError):
            self.wm().create_file("a.txt")

    def test_dangerous_names_rejected_everywhere(self) -> None:
        m = self.wm()
        bad = ("../x", "a/b", "..", ".", "-rf", "a\x00b", "a" * 129, "", "   ",
               "a\nb", "a\\b", "/etc/passwd", "~/x/../../y")
        for name in bad:
            for op in (m.create_file, m.create_folder, m.delete):
                with self.subTest(name=name, op=op.__name__):
                    with self.assertRaises(PathNotAllowedError):
                        op(name)
            with self.subTest(name=name, op="rename-old"):
                with self.assertRaises(PathNotAllowedError):
                    m.rename(name, "fine")
            with self.subTest(name=name, op="rename-new"):
                with self.assertRaises(PathNotAllowedError):
                    m.rename("fine", name)
        self.assertEqual(list(self.tmp.glob("x")), [])
        self.assertEqual([p.name for p in self.ws.iterdir()], [])

    def test_non_string_names_raise_type_error(self) -> None:
        m = self.wm()
        for op in (m.create_file, m.create_folder, m.delete):
            with self.assertRaises(TypeError):
                op(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            m.rename("a", None)  # type: ignore[arg-type]

    def test_name_length_limit_is_inclusive(self) -> None:
        self.wm().create_file("a" * 128)
        self.assertTrue((self.ws / ("a" * 128)).is_file())

    def test_names_are_trimmed_and_arabic_names_work(self) -> None:
        m = self.wm()
        m.create_file("  report  ")
        m.create_file("تقرير")
        self.assertTrue((self.ws / "report").is_file())
        self.assertTrue((self.ws / "تقرير").is_file())

    def test_rejections_are_logged_without_the_name(self) -> None:
        with self.assertLogs(fam.logger, level="WARNING") as logs:
            with self.assertRaises(PathNotAllowedError):
                self.wm().create_file("../secret-name")
        self.assertFalse(any("secret-name" in line for line in logs.output))

    def test_success_is_logged_without_the_name(self) -> None:
        with self.assertLogs(fam.logger, level="INFO") as logs:
            self.wm().create_file("secret-name")
        self.assertFalse(any("secret-name" in line for line in logs.output))


class CreateTests(WorkspaceBase):
    def test_create_file_is_empty_and_private(self) -> None:
        self.wm().create_file("a.txt")
        p = self.ws / "a.txt"
        self.assertEqual(p.read_text(), "")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode) & 0o077, 0)

    def test_create_existing_file_is_an_error_and_keeps_content(self) -> None:
        self.make("a.txt", "keep me")
        with self.assertRaises(PathAlreadyExistsError):
            self.wm().create_file("a.txt")
        self.assertEqual((self.ws / "a.txt").read_text(), "keep me")

    def test_create_file_over_existing_folder_is_an_error(self) -> None:
        (self.ws).mkdir(mode=0o700)
        (self.ws / "d").mkdir()
        with self.assertRaises(PathAlreadyExistsError):
            self.wm().create_file("d")

    def test_create_folder(self) -> None:
        self.wm().create_folder("projects")
        p = self.ws / "projects"
        self.assertTrue(p.is_dir())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode) & 0o077, 0)

    def test_create_existing_folder_is_an_error(self) -> None:
        m = self.wm()
        m.create_folder("projects")
        with self.assertRaises(PathAlreadyExistsError):
            m.create_folder("projects")
        self.make("f")
        with self.assertRaises(PathAlreadyExistsError):
            m.create_folder("f")


class RenameTests(WorkspaceBase):
    def test_rename_file_and_folder(self) -> None:
        m = self.wm()
        self.make("report", "data")
        m.rename("report", "notes")
        self.assertFalse((self.ws / "report").exists())
        self.assertEqual((self.ws / "notes").read_text(), "data")
        m.create_folder("d1")
        m.rename("d1", "d2")
        self.assertTrue((self.ws / "d2").is_dir())

    def test_rename_missing_source(self) -> None:
        self.ws.mkdir(mode=0o700)
        with self.assertRaises(PathNotFoundError):
            self.wm().rename("nope", "new")

    def test_rename_never_overwrites(self) -> None:
        self.make("a", "A")
        self.make("b", "B")
        with self.assertRaises(PathAlreadyExistsError):
            self.wm().rename("a", "b")
        self.assertEqual((self.ws / "a").read_text(), "A")
        self.assertEqual((self.ws / "b").read_text(), "B")

    def test_rename_to_same_name_is_an_error(self) -> None:
        self.make("a")
        with self.assertRaises(PathAlreadyExistsError):
            self.wm().rename("a", "a")

    def test_rename_os_error_is_mapped_without_details(self) -> None:
        self.make("a")
        with mock.patch.object(fam.os, "rename",
                               side_effect=OSError(5, "secret-detail")):
            with self.assertRaises(FileOperationError) as ctx:
                self.wm().rename("a", "b")
        self.assertNotIn("secret-detail", str(ctx.exception))


class SymlinkTests(WorkspaceBase):
    def setUp(self) -> None:
        super().setUp()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("outside")
        self.ws.mkdir(mode=0o700)
        (self.ws / "link").symlink_to(self.secret)
        (self.ws / "dirlink").symlink_to(self.outside)
        (self.ws / "dangling").symlink_to(self.outside / "missing")

    def test_links_are_refused_by_every_operation(self) -> None:
        m = self.wm()
        self.make("plain")
        for name in ("link", "dirlink", "dangling"):
            with self.subTest(name=name):
                with mock.patch.object(fam.subprocess, "run") as run:
                    with self.assertRaises(PathNotAllowedError):
                        m.delete(name)
                    run.assert_not_called()
                with self.assertRaises(PathNotAllowedError):
                    m.rename(name, "x")
                with self.assertRaises(PathNotAllowedError):
                    m.rename("plain", name)
                with self.assertRaises(PathNotAllowedError):
                    m.create_file(name)
                with self.assertRaises(PathNotAllowedError):
                    m.create_folder(name)
        self.assertEqual(self.secret.read_text(), "outside")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()),
                         ["secret.txt"])

    def test_workspace_itself_may_be_a_symlink_to_a_real_folder(self) -> None:
        real = self.tmp / "real_ws"
        real.mkdir(mode=0o700)
        alias = self.tmp / "alias_ws"
        alias.symlink_to(real)
        FileAppManager([self.root], search_dirs=[str(self.bin)],
                       workspace_root=alias).create_file("a.txt")
        self.assertTrue((real / "a.txt").is_file())


class DeleteTests(WorkspaceBase):
    def run_delete(self, name: str, returncode: int = 0):
        self.fake_exe("gio")
        completed = mock.Mock(returncode=returncode)
        with mock.patch.object(fam.subprocess, "run", return_value=completed) as run:
            self.wm().delete(name)
        return run

    def test_delete_moves_to_trash_with_fixed_arguments(self) -> None:
        f = self.make("notes", "x")
        run = self.run_delete("notes")
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(self.bin / "gio"), "trash", "--", str(f)])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], fam.TRASH_TIMEOUT_SECONDS)

    def test_delete_file_named_like_an_option_is_refused(self) -> None:
        self.fake_exe("gio")
        with mock.patch.object(fam.subprocess, "run") as run:
            with self.assertRaises(PathNotAllowedError):
                self.wm().delete("-rf")
            run.assert_not_called()

    def test_missing_gio_means_nothing_is_deleted(self) -> None:
        f = self.make("notes", "x")
        with mock.patch.object(fam.subprocess, "run") as run:
            with self.assertRaises(AppNotInstalledError):
                self.wm().delete("notes")
            run.assert_not_called()
        self.assertTrue(f.exists())

    def test_folders_are_refused(self) -> None:
        self.fake_exe("gio")
        self.ws.mkdir(mode=0o700)
        (self.ws / "d").mkdir()
        with mock.patch.object(fam.subprocess, "run") as run:
            with self.assertRaises(NotAFileError):
                self.wm().delete("d")
            run.assert_not_called()
        self.assertTrue((self.ws / "d").is_dir())

    def test_special_files_are_refused(self) -> None:
        self.fake_exe("gio")
        self.ws.mkdir(mode=0o700)
        os.mkfifo(self.ws / "pipe")
        with mock.patch.object(fam.subprocess, "run") as run:
            with self.assertRaises(NotAFileError):
                self.wm().delete("pipe")
            run.assert_not_called()

    def test_missing_file(self) -> None:
        self.fake_exe("gio")
        self.ws.mkdir(mode=0o700)
        with self.assertRaises(PathNotFoundError):
            self.wm().delete("nope")

    def test_gio_failure_is_reported_and_file_kept(self) -> None:
        f = self.make("notes", "x")
        with self.assertRaises(FileOperationError):
            self.run_delete("notes", returncode=1)
        self.assertTrue(f.exists())

    def test_gio_timeout_is_reported(self) -> None:
        self.make("notes")
        self.fake_exe("gio")
        with mock.patch.object(fam.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("gio", 10)):
            with self.assertRaises(FileOperationError):
                self.wm().delete("notes")

    def test_gio_permission_error_maps_to_access_denied(self) -> None:
        self.make("notes")
        self.fake_exe("gio")
        with mock.patch.object(fam.subprocess, "run", side_effect=PermissionError):
            with self.assertRaises(AccessDeniedError):
                self.wm().delete("notes")

    def test_gio_is_not_taken_from_caller_path(self) -> None:
        self.make("notes")
        other = self.tmp / "evil"
        other.mkdir()
        (other / "gio").write_text("x")
        (other / "gio").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(other)}):
            with self.assertRaises(AppNotInstalledError):
                self.wm().delete("notes")


class ListDirTests(WorkspaceBase):
    def test_empty_workspace(self) -> None:
        self.assertEqual(self.wm().list_dir(),
                         {"total": 0, "items": [], "truncated": False})

    def test_sorted_with_types_and_sizes(self) -> None:
        self.make("b.txt", "12345")
        self.make("A.txt", "")
        (self.ws / "zdir").mkdir()
        (self.ws / "adir").mkdir()
        (self.ws / "lnk").symlink_to(self.outside)
        result = self.wm().list_dir()
        self.assertEqual(
            result["items"],
            [
                {"name": "adir", "type": "folder", "size": None},
                {"name": "zdir", "type": "folder", "size": None},
                {"name": "A.txt", "type": "file", "size": 0},
                {"name": "b.txt", "type": "file", "size": 5},
                {"name": "lnk", "type": "link", "size": None},
            ],
        )
        self.assertEqual(result["total"], 5)
        self.assertFalse(result["truncated"])

    def test_limit_truncates_but_total_is_real(self) -> None:
        for i in range(7):
            self.make(f"f{i}")
        result = self.wm().list_dir(limit=3)
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["total"], 7)
        self.assertTrue(result["truncated"])

    def test_default_limit_is_fifty(self) -> None:
        for i in range(55):
            self.make(f"f{i:02}")
        result = self.wm().list_dir()
        self.assertEqual((len(result["items"]), result["total"]), (50, 55))

    def test_hidden_files_are_opt_in(self) -> None:
        self.make(".hidden")
        self.make("shown")
        m = self.wm()
        self.assertEqual([i["name"] for i in m.list_dir()["items"]], ["shown"])
        names = [i["name"] for i in m.list_dir(include_hidden=True)["items"]]
        self.assertEqual(sorted(names), [".hidden", "shown"])

    def test_invalid_limits(self) -> None:
        m = self.wm()
        for bad in (0, -1, fam.LIST_MAX_LIMIT + 1):
            with self.assertRaises(ValueError):
                m.list_dir(bad)
        for bad in ("5", None, 2.5, True):
            with self.assertRaises(TypeError):
                m.list_dir(bad)  # type: ignore[arg-type]

    def test_listing_never_leaves_the_workspace(self) -> None:
        self.make("only")
        (self.ws / "dirlink").symlink_to(self.outside)
        (self.outside / "secret.txt").write_text("x")
        names = [i["name"] for i in self.wm().list_dir()["items"]]
        self.assertNotIn("secret.txt", names)

    def test_scandir_permission_error_is_mapped(self) -> None:
        self.ws.mkdir(mode=0o700)
        with mock.patch.object(fam.os, "scandir", side_effect=PermissionError):
            with self.assertRaises(AccessDeniedError):
                self.wm().list_dir()


class FileFlowTests(WorkspaceBase):
    """The whole voice-demo story on a temp folder."""

    def test_create_rename_list_delete(self) -> None:
        self.fake_exe("gio")
        m = self.wm()
        m.create_file("report")
        m.create_folder("projects")
        m.rename("report", "notes")
        listing = m.list_dir()
        self.assertEqual([i["name"] for i in listing["items"]],
                         ["projects", "notes"])
        with mock.patch.object(fam.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            m.delete("notes")
        self.assertEqual(run.call_args[0][0][-1], str(self.ws / "notes"))


if __name__ == "__main__":
    unittest.main()
