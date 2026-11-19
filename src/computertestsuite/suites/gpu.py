# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 TheMrGeeBee

"""GPU stability: continuous OpenGL ES and Vulkan rendering on the bare console (KMS/DRM)."""

from __future__ import annotations

import re

from ..core import proc
from ..core.base import StressTest
from ..core.context import RunContext
from ..core.model import Status, TestResult
from ..core.sysinfo import drm_cards

# Framebuffer-only drivers: no 3D acceleration to test.
NON_3D_DRIVERS = {"", "simpledrm", "efifb", "vesafb", "bochs-drm", "bochs", "cirrus", "cirrus-qemu", "hyperv_drm",
                  "vboxvideo", "ast", "mgag200", "simple-framebuffer"}

GLMARK2 = ["glmark2-es2-drm", "glmark2-drm"]

_GL_ERR = re.compile(r"^(Error|Fatal)[: ].*|.*\b(segmentation fault|GPU hang|context lost)\b.*", re.I | re.M)


def accelerated_cards() -> list[dict[str, str]]:
    return [c for c in drm_cards() if c["driver"] not in NON_3D_DRIVERS]


class GpuRender(StressTest):
    name = "gpu"
    subsystem = "gpu"
    description = "Continuous GL ES (glmark2) then Vulkan (vkmark) rendering on every accelerated GPU"
    needs_root = True
    order = 50

    def hardware_available(self, ctx: RunContext) -> str | None:
        if not any(proc.have(t) for t in GLMARK2) and not proc.have("vkmark"):
            return "missing tools: glmark2 / vkmark"
        cards = accelerated_cards()
        if not cards:
            drivers = ", ".join(sorted({c["driver"] or "none" for c in drm_cards()})) or "no DRM devices"
            return f"no GPU with a 3D driver ({drivers})"
        return None

    def run(self, ctx: RunContext, result: TestResult) -> None:
        log = ctx.log_path(self.name)
        cards = accelerated_cards()
        result.metrics["gpus"] = ", ".join(f"{c['card']}({c['driver']})" for c in cards)
        gl = next((t for t in GLMARK2 if proc.have(t)), None)
        vk = proc.have("vkmark") and proc.capture(["vulkaninfo", "--summary"]).find("deviceName") >= 0
        phases = []
        if gl:
            phases.append(("opengl", [gl, "--off-screen", "--run-forever", "--annotate"]))
        if vk:
            phases.append(("vulkan", ["vkmark", "--winsys", "headless", "--run-forever"]))
        if not vk:
            result.metrics["vulkan"] = "no Vulkan device, skipped"
        if not phases:
            result.add(Status.SKIP, "no usable OpenGL or Vulkan benchmark for this GPU")
            return
        per = max(30, ctx.seconds // len(phases))
        for label, cmd in phases:
            if proc.ABORT.is_set():
                break
            # The tools run until killed; reaching the time limit is the normal outcome.
            r = proc.run(cmd, timeout=per, log=log)
            if r.aborted:
                break
            lines = [m.group(0).strip() for m in _GL_ERR.finditer(r.output)]
            if not r.timed_out:
                # Exited on its own before the time limit = crash or start-up failure.
                tail = " / ".join(r.output.strip().splitlines()[-3:])[:300]
                result.add(Status.FAIL, f"{label}: {cmd[0]} stopped early (exit {r.returncode}): {tail}")
            for ln in lines[:5]:
                result.add(Status.FAIL, f"{label}: {ln[:200]}")
            scores = [int(s) for s in re.findall(r"FPS:\s*(\d+)", r.output)]
            if scores:
                result.metrics[f"{label}.frames_reported"] = len(scores)
                result.metrics[f"{label}.fps_min_max"] = f"{min(scores)}/{max(scores)}"
