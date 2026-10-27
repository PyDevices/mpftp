# Recover a board

For a board that won't answer, won't take firmware, or sits in a bootloader.

## Enter the bootloader

`mpftp bootloader` picks the right mechanism per interpreter:
`machine.bootloader()` on MicroPython, and
`microcontroller.on_next_reset(RunMode.BOOTLOADER)` plus `microcontroller.reset()`
on CircuitPython. It reports `ok: false` when it couldn't reach the REPL,
rather than claiming success.

### When Ctrl-C can't reach the board

If the board is so wedged that Ctrl-C never reaches the interpreter, the REPL
path can't work. On UF2 boards, opening the port at 1200 baud with DTR low
enters the bootloader from the USB stack, which keeps running when the VM does
not:

```python
import serial, time
s = serial.Serial("/dev/ttyACM0", 1200, timeout=0.3); s.dtr = False
time.sleep(0.3); s.close()          # the board re-enumerates as its UF2 drive
```

Copy a `.uf2` onto that drive to get back. On RP2040 the `CIRCUITPY` filesystem
survives the reflash, because firmware and filesystem are separate regions.

## ESP32-S3 over native USB

On an S3 whose only serial line is its own USB, `mpftp bootloader` can leave the
port unopenable on Windows. `mpftp ports` and Device Manager keep reporting the
device as fine, but every open fails with:

```text
PermissionError(13, 'A device attached to the system is not functioning.', None, 31)
```

Error 31 is the device state, not a process holding the port (a sharing conflict
reports error 5), so don't hunt for something to kill. Replugging clears the
error, but the board comes back running MicroPython, and esptool then fails
with `Invalid head of packet (0x08)` because it found a REPL.

Two ways through:

- **By hand.** Unplug, hold BOOT, plug in, release. The board enumerates as a
  different device, `303A:1001` on a new port number rather than the
  `303A:4001` of the running firmware, and `firmware flash -d <that port>` works.
  Check `mpftp ports` for the `1001` before flashing: it is the only reliable
  sign the board is in download mode. After flashing it stays in ROM mode until
  reset, and no software reset reaches it there, so someone has to press the
  button.
- **By restarting the stale device node.** On Windows, restart the node with
  elevation and Windows enumerates what the chip is presenting:

  ```bash
  powershell.exe -NoProfile -Command "Start-Process pnputil -Verb RunAs -ArgumentList '/restart-device','USB\VID_303A&PID_4001\<serial>' -Wait"
  ```

  That pops one UAC prompt and grants nothing that outlives it. The board goes
  from the stale `303A:4001` node to `303A:1001`, and esptool can flash it with
  no button press. [`tools/windows/restart-esp-usb.ps1`](../../tools/windows/restart-esp-usb.ps1) in the repository does
  this for whatever Espressif board is attached. The order to try is
  `machine.bootloader()` over `exec`, then the device-node restart, then flash
  the `1001` port, and the physical BOOT-and-plug only when no one can click.

The network tells you where you are while you guess: a board that answers HTTP is
running your firmware, and one that answers nothing while its port refuses to
open is in ROM mode behind a stale USB node. Restart the node only after the
board has stopped answering ping, because that is how you know
`machine.bootloader()` ran.

## ESP32-S2 over native USB

An S2 has no USB-Serial-JTAG, so in ROM download mode it shows up as
`303A:0002`, the ROM's own USB port. The ROM keeps its USB connection through a
chip reset, so anything that resets the chip there leaves the S2 running its
firmware while Windows still holds the dead `0002` node. An RTS hard reset does
it, and so does leaving esptool's flasher stub.

`firmware detect` and `firmware flash` recognise the `0002` port. Detect probes
without the stub and resets nothing, so the board is still in download mode
afterwards, and flash ends with a watchdog reset, which resets the USB too. The
board comes back as `303A:4001` running the new firmware, with no button press.
If you drive esptool yourself on that port, use `--before no-reset` and either
`--no-stub` with `--after no-reset`, or `--after watchdog-reset`. Don't restart
the node of an S2 that is still running MicroPython: it ends as "Device
Descriptor Request Failed" and needs the RESET button.

## Restart a USB node without the prompt

`mpftp usb-restart` runs the restart through a scheduled task that ordinary
accounts may start, so there is no UAC click. Ask whether it works before you
plan around it:

```bash
mpftp usb-restart --status     # exits 1, and says why, if it would not work
mpftp usb-restart --list       # attached VID_303A (Espressif) and VID_1A86 (CH343) devices, with instance ids
mpftp usb-restart --instance 'USB\VID_303A&PID_4003\<serial>'
mpftp usb-restart --instance 'USB\VID_1A86&PID_55D3\<serial>'   # a CH343 serial bridge
```

It covers an Espressif board on its native USB (`VID_303A`) and a WCH CH343
USB-serial bridge (`VID_1A86`), one per run and always by explicit instance id.
An Espressif board in ROM download mode has no USB serial number, so Windows
names the node after the chip's MAC address, colons included
(`USB\VID_303A&PID_0009\48:27:E2:E9:B0:10`); `--list` prints it that way and
`--instance` accepts it.

**One elevated install first.** The task needs a one-time install by someone
with administrator rights. Copy [`tools/windows/`](../../tools/windows/) from the repository to a plain
Windows path (an elevated shell can't reliably read `\\wsl.localhost\...`), then
run, in an administrator PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\<where you put it>\install-restart-esp-usb-task.ps1
```

It is idempotent, prints what it did, and checks its own registration before it
returns. Until it has run, flashing an S3 over native USB may need a person
nearby. `--status` reads the action the task is registered with, not just
whether a task by that name exists, since `schtasks /run` reports success for
starting a task, never for what it did.

**A CH343 port that answers "Incorrect function".** When a port behind a CH343
fails every open with Windows error 1, the node usually still reads `Status OK`
and `mpftp ports` still lists it. The vendor driver (`CH343SER`) binds straight
to the `USB\VID_1A86&PID_55D3\<serial>` node, so restarting it reloads the
driver, the same as Disable and Enable in Device Manager. Find the id with
`--list`, restart it, and the port should answer under the same instance id and
port number. A CH343 has no download mode to re-enumerate into, so a node that
is gone afterwards is a failure (exit 6) for it, not a success. A restart
reloads the driver, the usual cause; if the port still refuses opens, the
bridge itself needs unplugging.

`--instance` exits non-zero when the restart didn't leave a reachable board,
and `taskResult` says which wall it hit:

| Exit | Meaning |
|---|---|
| 0 | Restarted |
| 2 | No request |
| 3 | Request refused (for example a colon id on an older install) |
| 4 | No such device attached |
| 5 | The restart itself failed |
| 6 | The restart was accepted and the node came back not OK |

A node that is gone after the restart is a success for an Espressif board: it
re-enumerated into ROM download mode as `303A:1001` on a new port number. The
transcript is `C:\Program Files\mpftp\restart-esp-usb.log`, readable by
everyone and writable only by SYSTEM.

It can't bring back a board that isn't on the bus (result 4 means a hand on a
cable), and don't send it a request for a board sitting in ROM download mode
under a different PID: the id you name is absent just then. Run by hand,
`restart-esp-usb.ps1 -InstanceId ... ` from an elevated shell works too, and
`-DryRun` does everything except touch the device.

The task runs as SYSTEM, so it never executes anything an ordinary account can
write. The script lives in `C:\Program Files\mpftp`, and the only thing you write
unprivileged is one instance id into `C:\ProgramData\mpftp\restart-esp-usb.target`,
which the script matches whole against an allow-list for a `VID_303A` or
`VID_1A86` parent node, never evaluates, and deletes on read. It refuses a
request that is a symlink, a hard link, or under a reparse point. Malware
already running as an administrator's desktop account has other ways up, and
this doesn't stop it; the aim is to not add one.

## If connect fails with a filesystem-corruption banner

MicroPython may print a banner when the filesystem is corrupt, and a boot loop
blocks soft-reset. Run `firmware detect`, then erase and reflash
([Download and flash firmware](download-and-flash-firmware.md)).
