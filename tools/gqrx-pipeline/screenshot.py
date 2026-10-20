#!/usr/bin/env python3
"""Screenshot capture for the evidence bundle.

Wayland/XWayland windows are captured through the user's
``wayland-self-screenshot-any`` wrapper. The wrapper must launch the target
application; it cannot attach to an already-running gqrx. For an existing
window use ``grim`` (Wayland) or ``import`` (X11), or restart gqrx under the
wrapper.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional


class ScreenshotError(RuntimeError):
    pass


def signal_number(name: str) -> int:
    name = name.strip().upper()
    if name.startswith("SIG"):
        name = name[3:]
    if name.startswith("RTMIN"):
        suffix = name[5:]
        if suffix.startswith("+"):
            return int(signal.SIGRTMIN) + int(suffix[1:])
        return int(signal.SIGRTMIN)
    if name.isdigit():
        return int(name)
    value = getattr(signal, "SIG" + name, None)
    if value is None:
        raise ScreenshotError(f"unknown signal: {name}")
    return int(value)


def _newest_file(directory: Path, before: set[Path], extensions: tuple[str, ...]) -> Optional[Path]:
    candidates = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in extensions and path not in before
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def capture_via_wrapper(
    dest: Path,
    *,
    pidfile: Path,
    wrapper_dir: Path,
    signal_name: str = "RTMIN",
    timeout: float = 5.0,
) -> Path:
    pidfile = Path(pidfile)
    wrapper_dir = Path(wrapper_dir)
    if not pidfile.exists():
        raise ScreenshotError(f"pidfile not found: {pidfile}")
    if not wrapper_dir.exists():
        raise ScreenshotError(f"wrapper output directory not found: {wrapper_dir}")
    pid = int(pidfile.read_text(encoding="utf-8").strip())
    before = set(wrapper_dir.iterdir())
    try:
        os.kill(pid, signal_number(signal_name))
    except OSError as exc:
        raise ScreenshotError(f"cannot signal wrapper pid {pid}: {exc}") from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        newest = _newest_file(wrapper_dir, before, (".jpg", ".jpeg", ".png", ".ppm"))
        if newest is not None:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(newest, dest)
            return dest
        time.sleep(0.05)
    raise ScreenshotError(f"no screenshot appeared in {wrapper_dir} within {timeout}s")


def capture_grim(dest: Path) -> Path:
    if not shutil.which("grim"):
        raise ScreenshotError("grim not found")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["grim", str(dest)], check=True)
    return dest


def capture_x11(dest: Path, *, window: Optional[str] = None) -> Path:
    if not shutil.which("import"):
        raise ScreenshotError("ImageMagick 'import' not found")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = window or "root"
    subprocess.run(["import", "-window", target, str(dest)], check=True)
    return dest


def capture_command(dest: Path, template: str) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = [part.replace("{dest}", str(dest)) for part in shlex.split(template)]
    if not argv:
        raise ScreenshotError("empty screenshot command")
    subprocess.run(argv, check=True)
    return dest


def capture(
    dest: Path,
    *,
    method: str = "auto",
    pidfile: Optional[Path] = None,
    wrapper_dir: Optional[Path] = None,
    signal_name: str = "RTMIN",
    timeout: float = 5.0,
    window: Optional[str] = None,
    command: Optional[str] = None,
) -> Path:
    method = method.lower()
    if method == "auto":
        if pidfile and wrapper_dir:
            method = "wrapper"
        elif os.environ.get("WAYLAND_DISPLAY"):
            method = "grim"
        elif os.environ.get("DISPLAY"):
            method = "x11"
        else:
            raise ScreenshotError("no screenshot method available (no pidfile, Wayland or X11 display)")

    if method == "wrapper":
        if not pidfile or not wrapper_dir:
            raise ScreenshotError("wrapper screenshot requires --screenshot-pidfile and --screenshot-dir")
        return capture_via_wrapper(
            dest,
            pidfile=pidfile,
            wrapper_dir=wrapper_dir,
            signal_name=signal_name,
            timeout=timeout,
        )
    if method == "grim":
        return capture_grim(dest)
    if method == "x11":
        return capture_x11(dest, window=window)
    if method == "command":
        if not command:
            raise ScreenshotError("--screenshot-command is required for method=command")
        return capture_command(dest, command)
    raise ScreenshotError(f"unknown screenshot method: {method}")
