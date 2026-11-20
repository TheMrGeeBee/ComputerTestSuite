# ComputerTestSuite (cts)

A bootable USB stick with a live Arch Linux system that stress-tests a computer's major
subsystems for **endurance, reliability and stability**, all from a root shell.

The Python orchestrator (stdlib only) drives proven tools: `stress-ng`, `memtester`, `fio`,
`smartctl`, `glmark2`, `vkmark` and `iperf3`. Alongside them it monitors the hardware the whole time:

- temperatures from every hwmon sensor, measured against the sensor's critical threshold
- CPU frequency and thermal-throttle counters
- EDAC memory error counters
- the kernel log, for MCE, PCIe AER, disk I/O, NVMe, GPU hang/reset and lockup messages

A test fails when the tool reports a verification error, **or** when the hardware logs an error
while the test runs.

## Tests

| Test             | Subsystem | What it does                                                                      |
|------------------|-----------|-----------------------------------------------------------------------------------|
| `battery`        | power     | Laptops: health (capacity left vs. new, WARN below 60%), charge cycles, charging; unplugged: idle and full-load drain with estimated runtime, sudden charge drops (runs first) |
| `storage-smart`  | storage   | SMART/NVMe health and error counters; age (power-on hours, power cycles) and, for SSDs, wear and data written (WARN from 90% of the rated endurance used); unexpected power losses and SATA link resets/CRC errors (information); a short drive self-test (standard/burnin) |
| `cpu`            | cpu       | Every stress-ng CPU method on all threads, then matrix/FPU math, results verified |
| `memory-pattern` | memory    | memtester bit-pattern tests over almost all free RAM                              |
| `memory-stress`  | memory    | stress-ng VM stressors (all methods) with data verification                       |
| `storage-read`   | storage   | **Read-only** sequential surface scan and random-read load on every disk          |
| `storage-write`  | storage   | **DESTRUCTIVE** full-device write and verify. Opt-in only, see below              |
| `gpu`            | gpu       | Continuous OpenGL ES (glmark2, KMS) and Vulkan (vkmark) rendering                 |
| `network`        | network   | Link and NIC error counters; iperf3 throughput both ways with `--iperf-server`    |
| `wifi`           | network   | Adapter/driver/firmware and radio check, scan for access points; signal, bitrate and iperf3 throughput when connected |
| `usb`            | usb       | USB inventory and link speeds; watches for enumeration and over-current errors    |
| `system`         | system    | Everything at once: CPU, RAM, disks and GPU (PSU, VRM and case-thermal stability) |

The boot menu also keeps archiso's **Memtest86+** entries, which test RAM before Linux boots.

### Profiles

| Profile    | Total time  | Per test | Combined `system` load | Disks                                       |
|------------|-------------|----------|------------------------|---------------------------------------------|
| `quick`    | 15–20 min   | 2.5 min  | 3 min                  | 2.5 min read sample, shared across all disks |
| `standard` | ~2–3 h      | 15 min   | 30 min                 | full surface read                           |
| `burnin`   | many hours  | 1 h      | 4 h                    | full surface read                           |

`quick` is the default for `cts run`. `--duration` overrides the per-test time for any profile.
Each profile has its own "CTS auto-run" entry in the boot menu.

### Verdicts
- **FAIL**: a verification or tool failure; MCE, uncorrectable EDAC or PCIe errors; disk I/O errors; a SMART health failure or growing bad-sector counters; a GPU hang; a sensor at or above its critical temperature.
- **WARN**: a temperature within 5 °C of critical (plus any thermal throttling that came with it); corrected memory or PCIe errors; NIC CRC errors; slow network throughput.
- **Information only**: thermal throttling while the CPU stayed clear of its critical temperature. Laptops often do this by design; the report shows the number of events.
- **SKIP**: the tool or hardware isn't present (for example no GPU driver or no iperf server).

`cts run` exits with code 1 if any test failed.

## Usage (on the live system)

### Terminal interface
After the keyboard-layout question, the live system opens a full-screen menu on tty1. Typing
`cts` with no arguments opens it too.

```
 ComputerTestSuite 0.1.0  Intel(R) Core(TM) i7-8565U                CPU 81°C  2.4 GHz
 ──────────────────────────────────────────────────────────────────────────────────────
 (I)nfo       │ Profile: quick      Elapsed 6m12s     Left ~11m                Errors: 0
 (T)ests      │ Overall [####################..........................................]  34%
 (S)tatus     │ PASS  storage-smart   [####################################] 100% 12s
 (R)eports    │ WARN  cpu             [####################################] 100% 2m31s
 (K)eyboard   │ RUN   memory-pattern  [##############......................]  38% 57s/2m30s
 (Q)uit       │       memory-stress   [....................................]   0%
```

| Key | Action |
|-----|--------|
| `I` | System information |
| `T` | Choose the profile (Left/Right) and tick tests on or off (Space, `A` for all) |
| `S` / Enter | Start the run, or show the running one |
| `L` | Live log of the current test |
| Esc | Stop the current test (asks first; the report is still written) |
| `R` | Saved reports: Enter opens one |
| `W` | Wi-Fi: scan, connect (password box), disconnect |
| `D` | Display test: full-screen colour patterns for dead pixels and backlight problems |
| `N` | (Tests screen) Set the iperf3 server used by the network and Wi-Fi tests |
| `K` | Change the keyboard layout |
| `Q` | Quit to the root shell (asks first if a test is running) |

Progress bars are time-based: each test's elapsed time against its estimate. A bar stays at 99%
if a test runs over, until the test finishes. Tests that can't run on the machine are shown as
skipped, with the reason. The boot menu's **CTS auto-run** entries open the interface and start
their profile after a 10-second countdown; any key cancels it. The destructive disk test is
only available from the command line.

### Battery
The `battery` test always reports how much charge the battery still holds compared with new, its
charge cycles (when the battery reports them) and whether it charges. To also measure runtime,
unplug the charger before starting: the test runs first, measures the power draw for a minute at
idle and then under full CPU load (up to 10 minutes), and estimates the runtime on a full charge
for both. It stops early at 15% charge and needs at least 30% to start. A charge level that drops
several percent within seconds is reported as a weak cell. Plug the charger back in afterwards.

### Wi-Fi
The `wifi` test always checks that the adapter's driver and firmware loaded, that the radio is
not switched off (rfkill), and that a scan finds access points. A very weak strongest signal
hints at a loose antenna cable. To also measure the connection, press `W`, pick a network,
type the password, then run the `wifi` test. With an iperf3 server set (`N` on the Tests screen),
it measures throughput over Wi-Fi in both directions. Driver or firmware crashes in the kernel log
fail the test.

A chip that Linux on the stick has no driver for is a SKIP, not a FAIL: the hardware may be fine.
Many older Broadcom chips (e.g. BCM43142, BCM4352) only work with Broadcom's proprietary `wl`
driver, the same one Ubuntu installs as "Additional Drivers". The ISO build compiles it for the
ISO kernel and keeps only the module (no compiler on the stick). It is never loaded automatically,
since it claims chips the open drivers handle better; at boot `cts-broadcom-wl` loads it only for
a Broadcom Wi-Fi chip that no open driver took.

### Display (LCD) test
Press `D`, or run `cts lcd` on the text console. It shows full-screen patterns one after another:
black, white, red, green, blue, 50% gray, gray and colour gradients, a 1-pixel checkerboard, and a
grid with 1-pixel borders. Look for dead or stuck pixels, backlight bleed, clouding, banding,
flicker and missing edge pixels. Right/Space shows the next pattern, Left the previous one, and
Esc quits. The test draws straight into the framebuffer, so it works without any graphical
desktop.

### Command line
```
cts lcd                                   # display test (text console only)
cts info                                  # hardware inventory
cts list                                  # tests, and why any would be skipped
cts run                                   # quick profile, all default tests
cts run --profile burnin                  # endurance run
cts run --only cpu,memory --duration 30m  # pick tests or subsystems
cts run --exclude gpu
cts run --iperf-server 192.168.1.10       # run `iperf3 -s` on that machine first
cts report                                # show the last report again
```

Pressing Ctrl+C stops the current test and still writes the report. Press it twice to kill everything.

To start a run automatically, pick **CTS auto-run: quick**, **standard** or **burn-in** in the
boot menu, or add `cts.autorun=<profile>` to the kernel command line.

**Keyboard layout:** at login, cts asks which layout to use (Swedish, US, UK, Nordic, German and
more, or any keymap name). Press Enter, or wait 10 seconds, to keep the default Swedish layout
(`sv-latin1`). Run `cts-keymap` to change it later. To skip the question, add
`cts.keymap=<map>` (e.g. `cts.keymap=us`) to the kernel command line. Auto-run also waits for
this choice.

### Destructive disk test
```
cts run --only storage --destructive --device /dev/sdX
```
This writes the whole device with checksummed data and reads it back. **All data on it is lost.**
You must type each device path to confirm. The suite always refuses:
- the CTS boot stick (label `CTS_*`)
- any disk with a mounted filesystem
- any disk that holds the running system

### Reports
Each run writes `report.txt`, `report.json` and per-tool logs to a timestamped directory:
- On a partition labelled **`CTS_REPORTS`** if one exists. These reports survive a reboot.
- Otherwise in `/root/cts-reports`, which is **in RAM on the live system and lost at power-off**.

To make reports persistent, add a partition to the free space on the stick after writing the image:
```
sudo fdisk /dev/sdX          # n -> p -> 3 -> accept defaults -> w
sudo mkfs.fat -F32 -n CTS_REPORTS /dev/sdX3
```
FAT32 means the reports can be read on Windows and macOS too.

### Booting from a Ventoy stick
Copy `out/cts-*.iso` to the Ventoy partition and boot it like any other ISO.

- **Protection:** disks with a `Ventoy` or `VTOYEFI` label are never written to, and the disk
  tests skip them, so the other files on the stick are safe.
- **Reports:** with no `CTS_REPORTS` partition, cts saves reports to a `cts-reports/` folder on the
  Ventoy partition, next to your ISOs. Ventoy keeps that partition busy while a Linux ISO runs, so
  you must allow remounting it once. Create `ventoy/ventoy.json` on the stick (or add the `control`
  entry to an existing one):
  ```json
  {
    "control": [
      { "VTOY_LINUX_REMOUNT": "1" }
    ]
  }
  ```
  Ventoy marks this option as experimental. cts only creates files in `cts-reports/` and never
  touches the ISOs. If the mount fails, cts says so and falls back to RAM.

## Building the ISO

You need an Arch-based host (CachyOS works). The ISO is built from the official Arch repos
through archiso's own `pacman.conf`.

```
sudo pacman -S archiso                    # plus qemu-desktop edk2-ovmf for testing in a VM
sudo scripts/build-iso.sh                 # -> out/cts-<version>-x86_64.iso, e.g. cts-1.2.0-x86_64.iso
scripts/run-qemu.sh                       # boot it in QEMU (UEFI); `scripts/run-qemu.sh bios` for BIOS
sudo dd if=out/cts-*.iso of=/dev/sdX bs=4M conv=fsync oflag=direct status=progress
```

`build-iso.sh` copies `/usr/share/archiso/configs/releng` into `build/profile` and then:
1. appends `iso/packages.extra` to the package list
2. appends `iso/profiledef.overrides.sh` (ISO name, the `CTS_` label, file permissions) and
   the version, taken from the latest git tag
3. overlays `iso/airootfs/` (the `cts` launcher, motd, hostname, Swedish console keymap in `vconsole.conf`) and appends `iso/zlogin.append` to root's `.zlogin`
4. copies `src/computertestsuite` to `/opt/cts` and stamps the version into it
5. runs `scripts/patch_boot.py` to add the auto-run boot entries
6. runs `mkarchiso` with its work dir in `/var/tmp/cts-archiso-work` (set `CTS_WORK_DIR` to change it)
   and its own package cache in `/var/cache/cts-archiso-pkg` (`CTS_PKG_CACHE`), so packages from
   a derivative host such as CachyOS never mix with Arch's

**Versions** come from git tags named `vX.Y.Z`. A build on the tagged commit is `1.2.0`; a build
with later commits or uncommitted changes gets that added, e.g. `1.2.0+3.gabc1234.dirty`, so a
report always shows exactly which code tested the machine. The version is shown in the menu header,
`cts --version` and every report. To release: commit, `git tag v1.3.0`, then build (and update
`version` in `pyproject.toml` to match).

The build runs at low priority so the computer stays usable while it runs. By default it gets
half of the cores, 40% of the memory (file cache included) and 300 MB/s of disk writes; xz
compression of the root filesystem is the heaviest step. Adjust this with `CTS_BUILD_CPUS`,
`CTS_BUILD_MEM` and `CTS_BUILD_IO`, or turn the limits off with `CTS_BUILD_LIMITS=0`, e.g.
`sudo CTS_BUILD_CPUS=4 scripts/build-iso.sh`.

Keep the work dir outside the project. It holds a full root filesystem with `/proc` and `/sys`
bind-mounted into it, and an IDE that indexes it can freeze.

The live system boots to a root shell (autologin on tty1) and supports both UEFI and BIOS.

## Development

```
uv sync
uv run pytest                              # parser, safety-guard and runner tests
uv run cts list                            # works without root; root-only tests show as skipped
sudo .venv/bin/cts run --only cpu,memory --duration 2m
```

To add a test, create a `StressTest` subclass in `src/computertestsuite/suites/`. It is discovered
automatically. Set `name`, `subsystem`, `required_tools` and `order`, and implement
`run(ctx, result)` so it records findings with `result.add(Status.X, "...")`. Timeouts, Ctrl+C,
sensor and kernel-log monitoring, and reporting are handled by the runner.

Layout:
```
src/computertestsuite/
  cli.py            info | list | run | report
  core/             runner, monitor (sensors + kmsg), disks (safety), sysinfo, report, profiles
  suites/           cpu, memory, storage, gpu, network, usb, system
iso/                archiso overlay (packages, profiledef overrides, airootfs)
scripts/            build-iso.sh, patch_boot.py, run-qemu.sh
tests/              pytest unit tests + fixtures
```

## License

Copyright (C) 2026 TheMrGeeBee

ComputerTestSuite is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either version 3 of
the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
[LICENSE](LICENSE) file for the full text.

The live ISO also contains Arch Linux packages, which are covered by their own licenses
(listed under `/usr/share/licenses` on the live system).
