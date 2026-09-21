"""The no-UAC ESP32 USB recovery: what it accepts, and what it refuses (mpftp#31).

The privileged half of this recovery runs as SYSTEM, so the only thing an
ordinary account hands it is the request file -- one instance id, data, never
code. These tests are about that boundary.

Two layers. The pure-Python ones check the copy of the rule that stops a bad id
being written down at all. The PowerShell ones drive the real script through
its ``-DryRun`` switch, which does everything up to the restart -- validation,
and the presence check against attached hardware -- and stops before touching
the device, so they are safe to run as an ordinary user with boards in use on
the bench. They skip where there is no powershell.exe.

To show these can fail, point MPFTP_ESP_USB_SCRIPT at a copy of the script with
a rule loosened. Measured 2026-09-21, against the four that matter:

    regex loosened to ``^USB\\VID_``     5 red, incl. accepting another vendor's UART
    one-line check ``-ne 1`` -> ``-lt 1``  1 red, two devices in one request
    link checks made to return $null    4 red, every link case
    contents echoed on a link refusal   2 red, incl. the leak assertion
"""

from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest import mock

from mpftp import espusb

REPO_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "windows" / "restart-esp-usb.ps1"

TEMBED = r"USB\VID_303A&PID_4003\3485186BFCAC0000"
ABSENT = r"USB\VID_303A&PID_4003\DEADBEEFDEADBEEF"
P4_UART = r"USB\VID_1A86&PID_55D3\5ABA052144"
MI_CHILD = r"USB\VID_303A&PID_4003&MI_00\6&BFF214A&0&0000"


class ValidateInstanceIdTests(unittest.TestCase):
    """mpftp refuses a bad id before it reaches the file the SYSTEM task reads."""

    def test_a_composite_parent_is_accepted(self):
        self.assertEqual(espusb.validate_instance_id(TEMBED), TEMBED)

    def test_surrounding_whitespace_is_trimmed_not_refused(self):
        self.assertEqual(espusb.validate_instance_id(f"  {TEMBED}\n"), TEMBED)

    def test_another_vendor_is_refused(self):
        # The bench that found mpftp#31 had a second board whose UART must not
        # be bounced; a VID check is what keeps it out.
        with self.assertRaises(ValueError):
            espusb.validate_instance_id(P4_UART)

    def test_an_mi_child_is_refused(self):
        with self.assertRaises(ValueError):
            espusb.validate_instance_id(MI_CHILD)

    def test_a_trailing_command_is_refused(self):
        with self.assertRaises(ValueError):
            espusb.validate_instance_id(TEMBED + "; calc")

    def test_a_quote_break_is_refused(self):
        with self.assertRaises(ValueError):
            espusb.validate_instance_id(TEMBED + '" ; calc ; "')

    def test_a_wildcard_is_refused(self):
        # Get-PnpDevice -InstanceId takes wildcards; one here would widen the
        # request from a device to a set of them.
        with self.assertRaises(ValueError):
            espusb.validate_instance_id(r"USB\VID_303A&PID_4003\*")

    def test_a_path_is_refused(self):
        with self.assertRaises(ValueError):
            espusb.validate_instance_id(r"C:\Windows\System32\calc.exe")

    def test_a_second_line_is_refused(self):
        with self.assertRaises(ValueError):
            espusb.validate_instance_id(TEMBED + "\n" + TEMBED)

    def test_empty_is_refused(self):
        for bad in ("", "   ", "\n"):
            with self.assertRaises(ValueError):
                espusb.validate_instance_id(bad)

    def test_an_over_long_id_is_refused(self):
        with self.assertRaises(ValueError):
            espusb.validate_instance_id(r"USB\VID_303A&PID_4003\\" + "A" * 400)


class TaskStateTests(unittest.TestCase):
    """Tell 'the recovery is not installed' from 'the board did not come back'."""

    @staticmethod
    def _state_for(ps_output: str) -> dict:
        with mock.patch.object(espusb, "_ps", return_value=ps_output):
            return espusb.task_state()

    def test_a_missing_task_is_not_usable(self):
        state = self._state_for("installed|no\n")
        self.assertFalse(state["installed"])
        self.assertFalse(state["usable"])
        self.assertIn("install-restart-esp-usb-task.ps1", state["reason"])

    def test_the_broken_cmdlet_is_named_not_just_reported_as_failing(self):
        # The exact action registered on the bench until 2026-09-21.
        state = self._state_for(
            "installed|yes\nexecute|powershell.exe\n"
            "arguments|-NoProfile -Command \"Get-PnpDevice -PresentOnly | Where-Object "
            "{ $_.InstanceId -like 'USB\\VID_303A*' } | Restart-PnpDevice -Confirm:$false\"\n"
            "user|SYSTEM\nrunlevel|Highest\nlastResult|1\nlastRun|9/17/2026 5:01:14 PM\nstate|Ready\n"
        )
        self.assertTrue(state["installed"])
        self.assertFalse(state["usable"])
        self.assertIn("Restart-PnpDevice", state["reason"])
        self.assertEqual(state["lastResult"], 1)

    def test_a_task_running_the_script_as_system_is_usable(self):
        state = self._state_for(
            "installed|yes\nexecute|powershell.exe\n"
            'arguments|-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden '
            '-File "C:\\Program Files\\mpftp\\restart-esp-usb.ps1"\n'
            "user|SYSTEM\nrunlevel|Highest\nlastResult|0\nlastRun|9/21/2026 7:39:00 AM\nstate|Ready\n"
        )
        self.assertTrue(state["usable"])
        self.assertEqual(state["reason"], "ready")
        self.assertEqual(state["lastResult"], 0)

    def test_the_script_run_as_an_ordinary_user_is_not_usable(self):
        # pnputil /restart-device inside it would fail with Access is denied.
        state = self._state_for(
            "installed|yes\nexecute|powershell.exe\n"
            'arguments|-File "C:\\Program Files\\mpftp\\restart-esp-usb.ps1"\n'
            "user|bradb\nrunlevel|Limited\nlastResult|0\nlastRun|9/21/2026 7:39:00 AM\nstate|Ready\n"
        )
        self.assertFalse(state["usable"])
        self.assertIn("Access is denied", state["reason"])

    def test_restart_refuses_to_run_when_the_task_is_not_usable(self):
        with (
            mock.patch.object(espusb, "recovery_available", return_value=(False, "no such task")),
            self.assertRaises(espusb.RecoveryError),
        ):
            espusb.restart_device(TEMBED)


def _powershell() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("powershell")


class _StagedScript:
    """Stages the real script somewhere powershell.exe can run it, and drives it."""

    @classmethod
    def setUpClass(cls):
        override = os.environ.get("MPFTP_ESP_USB_SCRIPT")
        if override:
            cls.script = override
            # ntpath, not os.path: this is a Windows path and these tests run
            # under Linux Python, where os.path.dirname sees no separator in it
            # and returns "" -- which silently pointed every request path at the
            # current directory instead.
            cls.work = ntpath.dirname(override)
            if not cls.work:
                raise unittest.SkipTest(f"MPFTP_ESP_USB_SCRIPT needs an absolute path, got {override!r}")
            return
        # A .ps1 under \\wsl.localhost\ is awkward for powershell.exe to run;
        # stage it on the Windows side instead.
        temp = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-Command", "[Console]::Out.Write($env:TEMP)"],
            capture_output=True, text=True,
        ).stdout.strip()
        if not temp:
            raise unittest.SkipTest("Windows did not report %TEMP%")
        cls.work = temp + r"\mpftp-esp-usb-tests"
        local = espusb._win_to_local(cls.work)
        local.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_SCRIPT, local / "restart-esp-usb.ps1")
        cls.script = cls.work + r"\restart-esp-usb.ps1"

    def _run_path(self, win_request: str) -> int:
        proc = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", self.script,
             "-RequestPath", win_request,
             "-LogPath", self.work + r"\test.log",
             "-DryRun"],
            capture_output=True, text=True,
        )
        return proc.returncode

    def _run(self, request_text: str | None) -> int:
        local_dir = espusb._win_to_local(self.work)
        request = local_dir / "test.target"
        if request_text is None:
            request.unlink(missing_ok=True)
        else:
            request.write_text(request_text, encoding="utf-8")
        return self._run_path(self.work + r"\test.target")

    def _run_as_the_task_does(self, *extra: str) -> int:
        """`-File`, and NO `-LogPath` -- the scheduled task's own invocation.

        Every other leg here passes `-LogPath`, and that is what hid the first
        install's failure (2026-09-21): under `powershell.exe -File`,
        `$PSScriptRoot` is still empty while the param block's defaults are
        evaluated, so a default of `Join-Path $PSScriptRoot ...` threw before
        the first log line and the task exited 1 having done nothing.
        """
        proc = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", self.script, *extra],
            capture_output=True, text=True,
        )
        return proc.returncode

    def _log_text(self) -> str:
        log = espusb._win_to_local(self.work) / "test.log"
        return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


@unittest.skipIf(_powershell() is None, "needs Windows PowerShell (interop from WSL)")
class DryRunRefusalTests(_StagedScript, unittest.TestCase):
    """The script's own rule, exercised at medium integrity, device untouched."""

    def test_a_missing_request_exits_2(self):
        self.assertEqual(self._run(None), 2)

    def test_an_empty_request_exits_2(self):
        self.assertEqual(self._run(""), 2)
        self.assertEqual(self._run("   \n\n"), 2)

    def test_another_vendors_device_is_refused(self):
        self.assertEqual(self._run(P4_UART + "\n"), 3)

    def test_an_mi_child_is_refused(self):
        self.assertEqual(self._run(MI_CHILD + "\n"), 3)

    def test_a_trailing_command_is_refused(self):
        self.assertEqual(self._run(TEMBED + "; calc\n"), 3)

    def test_a_quote_break_is_refused(self):
        self.assertEqual(self._run(TEMBED + '" ; calc ; "\n'), 3)

    def test_a_subexpression_is_refused(self):
        self.assertEqual(self._run("$(calc)\n"), 3)

    def test_a_wildcard_is_refused(self):
        self.assertEqual(self._run(TEMBED + "*\n"), 3)

    def test_a_path_is_refused(self):
        self.assertEqual(self._run("C:\\Windows\\System32\\calc.exe\n"), 3)

    def test_two_devices_in_one_request_are_refused(self):
        self.assertEqual(self._run(TEMBED + "\n" + ABSENT + "\n"), 3)

    def test_an_over_long_request_is_refused(self):
        self.assertEqual(self._run("USB\\VID_303A&PID_4003\\" + "A" * 400 + "\n"), 3)

    def test_a_well_formed_id_for_an_absent_device_fails_loudly(self):
        # The whole point: a wrong id must not exit 0. A caller cannot tell a
        # silent success from a real one.
        self.assertEqual(self._run(ABSENT + "\n"), 4)

    def test_the_request_is_consumed_on_read(self):
        # A request left behind is replayed by the next run, on somebody else's
        # device.
        self._run(ABSENT + "\n")
        self.assertFalse((espusb._win_to_local(self.work) / "test.target").exists())


@unittest.skipIf(_powershell() is None, "needs Windows PowerShell (interop from WSL)")
class LinkFollowingTests(_StagedScript, unittest.TestCase):
    """SYSTEM reads and deletes inside a directory an ordinary account owns.

    So the request must be a real file in a real directory. All three links
    below were creatable unprivileged on this bench, and they do not present
    alike: a hard link carries no ReparsePoint attribute, and a WSL symlink
    carries the attribute with a blank LinkType. Either check alone misses one.

    This does not defend against malware already running as an administrator's
    desktop account -- that has other routes up. It is about not adding one.
    """

    SECRET = "MPFTP31-SECRET-THAT-MUST-NOT-REACH-THE-LOG"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.local = espusb._win_to_local(cls.work)

    def setUp(self):
        for name in ("real.target", "link.target", "hard.target", "secret.txt"):
            (self.local / name).unlink(missing_ok=True)
        (self.local / "realdir").mkdir(exist_ok=True)

    def _make_junction(self) -> str:
        # A fresh name per test: a junction left behind by an earlier run makes
        # mklink fail with "Access is denied", which reads like a privilege
        # problem and is not one.
        name = "jdir-" + uuid.uuid4().hex[:8]
        win = self.work + "\\" + name
        proc = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", win, self.work + r"\realdir"],
            capture_output=True, text=True,
        )
        if not (self.local / name).exists():
            raise unittest.SkipTest(f"could not create a junction here: {proc.stdout} {proc.stderr}")
        self.addCleanup(subprocess.run, ["cmd.exe", "/c", "rmdir", win],
                        capture_output=True, text=True)
        return win

    def test_a_symlinked_request_is_refused_and_not_deleted(self):
        real = self.local / "real.target"
        real.write_text(TEMBED + "\n", encoding="utf-8")
        link = self.local / "link.target"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest(f"cannot create a file symlink here: {exc}") from None
        self.assertEqual(self._run_path(self.work + r"\link.target"), 3)
        # Refusing is half of it; SYSTEM must not have deleted through the link.
        self.assertTrue(real.exists(), "the symlink's target was deleted")

    def test_a_hard_linked_request_is_refused(self):
        real = self.local / "real.target"
        real.write_text(TEMBED + "\n", encoding="utf-8")
        hard = self.local / "hard.target"
        try:
            os.link(real, hard)
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest(f"cannot create a hard link here: {exc}") from None
        # A hard link has no ReparsePoint attribute at all -- only LinkType
        # tells you, which is why both conditions are checked.
        self.assertEqual(self._run_path(self.work + r"\hard.target"), 3)
        self.assertTrue(real.exists(), "the hard link's target was deleted")

    def test_a_request_inside_a_junction_is_refused(self):
        junction = self._make_junction()
        (self.local / "realdir" / "test.target").write_text(TEMBED + "\n", encoding="utf-8")
        self.assertEqual(self._run_path(junction + r"\test.target"), 3)
        self.assertTrue((self.local / "realdir" / "test.target").exists(),
                        "the request was deleted through a junction")

    def test_a_refused_link_does_not_leak_its_contents_to_the_log(self):
        # The log is readable by everyone; with Developer Mode on, the link
        # could point at something only SYSTEM can read.
        secret = self.local / "secret.txt"
        secret.write_text(self.SECRET + "\n", encoding="utf-8")
        link = self.local / "link.target"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest(f"cannot create a file symlink here: {exc}") from None
        self.assertEqual(self._run_path(self.work + r"\link.target"), 3)
        self.assertNotIn(self.SECRET, self._log_text())


@unittest.skipIf(_powershell() is None, "needs Windows PowerShell (interop from WSL)")
class TheTaskRunsItWithFileAndNoLogPath(_StagedScript, unittest.TestCase):
    """The production invocation, which no other leg exercises."""

    def test_a_malformed_id_is_refused_not_a_param_block_crash(self):
        # 3 = request refused. 1 is what PowerShell returns when the script
        # never got as far as its first statement.
        self.assertEqual(self._run_as_the_task_does("-InstanceId", "not-an-id", "-DryRun"), 3)

    def test_it_writes_its_transcript_beside_itself_by_default(self):
        log = espusb._win_to_local(self.work) / "restart-esp-usb.log"
        log.unlink(missing_ok=True)
        self._run_as_the_task_does("-InstanceId", "not-an-id", "-DryRun")
        self.assertTrue(log.exists(), "no transcript beside the script: the default -LogPath did not resolve")
        self.assertIn("request refused", log.read_text(encoding="utf-8", errors="replace"))


@unittest.skipIf(_powershell() is None, "needs Windows PowerShell (interop from WSL)")
class ItNeverLeavesANodeDisabled(_StagedScript, unittest.TestCase):
    """The restart itself, run for real over pretend hardware.

    On 2026-09-21 the first installed version disabled a board's USB node and
    then failed to enable it; the board enumerated with no COM port until an
    administrator ran `pnputil /enable-device`. `esp_usb_doubles.ps1` defines
    Get-PnpDevice, pnputil.exe and the two PnpDevice cmdlets as functions --
    which PowerShell resolves first -- records every call, and dot-sources the
    real script under them.

    Shown failing, 2026-09-21, against copies of the script with one line
    changed each (MPFTP_ESP_USB_SCRIPT):

        the `[void](Enable-IfDisabled ...)` call removed    1 red: the disabled node
        `if (-not $hasRestartVerb)` -> `if ($true)`         1 red: the board that went away
        the `finally` block's enable removed               1 red: the old-Windows fallback
    """

    DOUBLES = Path(__file__).resolve().parent / "esp_usb_doubles.ps1"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        shutil.copy(cls.DOUBLES, espusb._win_to_local(cls.work) / "esp_usb_doubles.ps1")

    def _play(self, scenario: str) -> tuple[int, list[str]]:
        calls = espusb._win_to_local(self.work) / "doubles.calls"
        calls.unlink(missing_ok=True)
        proc = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", self.work + r"\esp_usb_doubles.ps1",
             "-Script", self.script,
             "-Scenario", scenario,
             "-Calls", self.work + r"\doubles.calls",
             "-Log", self.work + r"\doubles.log"],
            capture_output=True, text=True,
        )
        seen = calls.read_text(encoding="utf-8-sig").split("\n") if calls.exists() else []
        return proc.returncode, [line.strip() for line in seen if line.strip()]

    def test_a_healthy_node_is_restarted_and_nothing_else_is_done_to_it(self):
        code, calls = self._play("healthy")
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["pnputil /restart-device"])

    def test_a_disabled_node_is_enabled_before_it_is_restarted(self):
        code, calls = self._play("disabled")
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["pnputil /enable-device", "pnputil /restart-device"])

    def test_a_board_that_has_gone_away_is_never_disabled(self):
        # pnputil has the verb and said 1167: the board left. Disabling a node
        # that is not there is what stuck, so there must be no second attempt.
        code, calls = self._play("gone-away")
        self.assertEqual(code, 5)
        self.assertNotIn("Disable-PnpDevice", calls)
        self.assertNotIn("Enable-PnpDevice", calls)

    def test_the_old_windows_fallback_enables_again_when_its_own_enable_fails(self):
        code, calls = self._play("old-windows")
        self.assertEqual(code, 5)
        self.assertIn("Disable-PnpDevice", calls)
        self.assertEqual(calls[-1], "pnputil /enable-device",
                         "the node was disabled and the last thing done to it was not an enable")


if __name__ == "__main__":
    unittest.main()
