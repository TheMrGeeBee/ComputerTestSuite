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
| `storage-smart`  | storage   | SMART/NVMe health and error counters, plus a short drive self-test (standard/burnin) |
| `cpu`            | cpu       | Every stress-ng CPU method on all threads, then matrix/FPU math, results verified |
| `memory-pattern` | memory    | memtester bit-pattern tests over almost all free RAM                              |
| `memory-stress`  | memory    | stress-ng VM stressors (all methods) with data verification                       |
| `storage-read`   | storage   | **Read-only** sequential surface scan and random-read load on every disk          |
| `storage-write`  | storage   | **DESTRUCTIVE** full-device write and verify. Opt-in only, see below              |
| `gpu`            | gpu       | Continuous OpenGL ES (glmark2, KMS) and Vulkan (vkmark) rendering                 |
| `network`        | network   | Link and NIC error counters; iperf3 throughput both ways with `--iperf-server`    |
| `usb`            | usb       | USB inventory and link speeds; watches for enumeration and over-current errors    |
| `system`         | system    | Everything at once: CPU, RAM, disks and GPU (PSU, VRM and case-thermal stability) |

The boot menu also keeps archiso's **Memtest86+** entries, which test RAM before Linux boots.

### Profiles

| Profile    | Per test   | Combined `system` load | Disks                    |
|------------|------------|------------------------|--------------------------|
| `quick`    | 2 min      | 2 min                  | time-limited read sample |
| `standard` | 15 min     | 30 min                 | full surface read        |
| `burnin`   | 1 h        | 4 h                    | full surface read        |

`--duration` overrides the per-test time for any profile.

### Verdicts
- **FAIL**: a verification or tool failure; MCE, uncorrectable EDAC or PCIe errors; disk I/O errors; a SMART health failure or growing bad-sector counters; a GPU hang; a sensor at or above its critical temperature.
- **WARN**: thermal throttling; corrected memory or PCIe errors; a temperature within 5 °C of critical; NIC CRC errors; slow network throughput.
- **SKIP**: the tool or hardware isn't present (for example no GPU driver or no iperf server).

`cts run` exits with code 1 if any test failed.

## Usage (on the live system)

```
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

To start a run automatically, pick **CTS auto-run: standard** or **CTS auto-run: burn-in** in the
boot menu, or add `cts.autorun=<profile>` to the kernel command line.

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
sudo scripts/build-iso.sh                 # -> out/cts-YYYY.MM.DD-x86_64.iso
scripts/run-qemu.sh                       # boot it in QEMU (UEFI); `scripts/run-qemu.sh bios` for BIOS
sudo dd if=out/cts-*.iso of=/dev/sdX bs=4M conv=fsync oflag=direct status=progress
```

`build-iso.sh` copies `/usr/share/archiso/configs/releng` into `build/profile` and then:
1. appends `iso/packages.extra` to the package list
2. appends `iso/profiledef.overrides.sh` (ISO name, the `CTS_` label, file permissions)
3. overlays `iso/airootfs/` (the `cts` launcher, motd, hostname, Swedish console keymap in `vconsole.conf`) and appends `iso/zlogin.append` to root's `.zlogin`
4. copies `src/computertestsuite` to `/opt/cts`
5. runs `scripts/patch_boot.py` to add the auto-run boot entries
6. runs `mkarchiso` with its work dir in `/var/tmp/cts-archiso-work` (set `CTS_WORK_DIR` to change it)

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
