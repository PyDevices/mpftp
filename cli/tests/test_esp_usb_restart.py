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

To show these can fail, point MPFTP_ESP_USB_SCRIPT at a copy with the rule
loosened; the refusal cases go red. Loosening the regex to ``^USB\\VID_`` on
2026-09-21 turned five of them red, including accepting another vendor's UART.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
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


@unittest.skipIf(_powershell() is None, "needs Windows PowerShell (interop from WSL)")
class DryRunRefusalTests(unittest.TestCase):
    """The script's own rule, exercised at medium integrity, device untouched."""

    @classmethod
    def setUpClass(cls):
        override = os.environ.get("MPFTP_ESP_USB_SCRIPT")
        if override:
            cls.script = override
            cls.work = os.path.dirname(override) or "."
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

    def _run(self, request_text: str | None) -> int:
        local_dir = espusb._win_to_local(self.work)
        request = local_dir / "test.target"
        if request_text is None:
            request.unlink(missing_ok=True)
        else:
            request.write_text(request_text, encoding="utf-8")
        proc = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", self.script,
             "-RequestPath", self.work + r"\test.target",
             "-LogPath", self.work + r"\test.log",
             "-DryRun"],
            capture_output=True, text=True,
        )
        return proc.returncode

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


if __name__ == "__main__":
    unittest.main()
