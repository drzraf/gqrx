#!/usr/bin/env python3
"""Build bounded evidence bundles from gqrx.

A bundle is the human-facing representation of an event:

    screenshot.jpg   (if enabled)
    audio.wav        (if enabled)
    spectrogram.png  (if enabled and audio exists)
    iq.*             (optional, bounded)
    metadata.json

The IQ segment is disabled by default because it is the largest artifact.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import session as session_mod
import screenshot as screenshot_mod
from gqrxctl import GqrxClient, GqrxError


@dataclass
class EvidenceConfig:
    screenshot: bool = True
    screenshot_method: str = "auto"
    screenshot_pidfile: Optional[Path] = None
    screenshot_dir: Optional[Path] = None
    screenshot_signal: str = "RTMIN"
    screenshot_timeout: float = 5.0
    screenshot_command: Optional[str] = None

    audio: bool = True
    audio_seconds: float = 2.0
    audio_dir: Optional[Path] = None

    spectrogram: bool = True

    iq: bool = False
    iq_seconds: float = 1.0
    iq_max_bytes: int = 8 * 1024 * 1024
    iq_dir: Optional[Path] = None
    iq_bytes_per_sample: int = 8  # gqrx master writes CF32; CU8 with PR #1013
    iq_rate_hz: Optional[float] = None

    extra: Dict[str, Any] = field(default_factory=dict)


def _files_in(directory: Path, suffixes: tuple[str, ...]) -> set[Path]:
    if not directory.exists():
        return set()
    return {
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in suffixes
    }


def _newest_new(directory: Path, before: set[Path], suffixes: tuple[str, ...]) -> Optional[Path]:
    candidates = [
        path
        for path in _files_in(directory, suffixes)
        if path not in before and path.stat().st_size > 0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _wait_for_new(
    directory: Path,
    before: set[Path],
    suffixes: tuple[str, ...],
    timeout: float,
) -> Optional[Path]:
    deadline = time.monotonic() + timeout
    while True:
        newest = _newest_new(directory, before, suffixes)
        if newest is not None:
            return newest
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def make_spectrogram(audio_path: Path, out_path: Path) -> bool:
    if shutil.which("sox"):
        command = ["sox", str(audio_path), "-n", "spectrogram", "-o", str(out_path)]
    elif shutil.which("ffmpeg"):
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(audio_path),
            "-lavfi",
            "showspectrumpic=s=1024x512:legend=disabled",
            str(out_path),
        ]
    else:
        return False
    try:
        subprocess.run(command, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def _copy_audio(
    client: GqrxClient,
    cfg: EvidenceConfig,
    bundle: Path,
    errors: list[str],
) -> Optional[Path]:
    if not cfg.audio:
        return None
    audio_dir = Path(cfg.audio_dir).expanduser() if cfg.audio_dir else Path.home()
    before = _files_in(audio_dir, (".wav",))
    try:
        client.audio_record(True)
        time.sleep(max(0.1, cfg.audio_seconds))
        client.audio_record(False)
    except GqrxError as exc:
        errors.append(f"audio: {exc}")
        try:
            client.audio_record(False)
        except GqrxError:
            pass
        return None
    newest = _wait_for_new(audio_dir, before, (".wav",), timeout=3.0)
    if newest is None:
        errors.append(f"audio: no new WAV in {audio_dir}")
        return None
    dest = bundle / "audio.wav"
    shutil.copy2(newest, dest)
    return dest


def _copy_iq(
    client: GqrxClient,
    session: session_mod.Session,
    cfg: EvidenceConfig,
    bundle: Path,
    errors: list[str],
) -> Optional[Path]:
    if not cfg.iq or cfg.iq_seconds <= 0:
        return None
    if not cfg.iq_rate_hz or cfg.iq_rate_hz <= 0:
        errors.append("iq: sample rate is required for a bounded IQ capture")
        return None
    iq_dir = Path(cfg.iq_dir).expanduser() if cfg.iq_dir else Path.home()
    seconds, estimated = session.plan_iq_capture(
        cfg.iq_rate_hz,
        bytes_per_sample=cfg.iq_bytes_per_sample,
        max_bytes=cfg.iq_max_bytes,
        desired_seconds=cfg.iq_seconds,
    )
    if seconds <= 0:
        errors.append("iq: no storage budget available")
        return None
    before = _files_in(iq_dir, (".raw", ".sigmf-data", ".cu8", ".cs16"))
    try:
        client.iq_record(True)
        time.sleep(seconds)
        client.iq_record(False)
    except GqrxError as exc:
        errors.append(f"iq: {exc}")
        try:
            client.iq_record(False)
        except GqrxError:
            pass
        return None
    newest = _wait_for_new(iq_dir, before, (".raw", ".sigmf-data", ".cu8", ".cs16"), timeout=5.0)
    if newest is None:
        errors.append(f"iq: no new recording in {iq_dir}")
        return None
    dest = bundle / ("iq" + "".join(newest.suffixes))
    written = session_mod.copy_limited(newest, dest, int(estimated))
    if written < newest.stat().st_size:
        errors.append(
            f"iq: truncated {newest.name} to {session_mod.human_bytes(written)} "
            f"(planned {session_mod.human_bytes(int(estimated))})"
        )
    if newest.name.endswith(".sigmf-data"):
        meta = newest.with_name(newest.name[: -len(".sigmf-data")] + ".sigmf-meta")
        if meta.exists():
            shutil.copy2(meta, bundle / "iq.sigmf-meta")
    return dest


def capture_evidence(
    client: GqrxClient,
    session: session_mod.Session,
    cfg: EvidenceConfig,
    *,
    reason: str,
    frequency_hz: int,
    mode: Optional[str] = None,
    passband_hz: Optional[int] = None,
    level_db: Optional[float] = None,
    squelch_db: Optional[float] = None,
    when: Optional[datetime] = None,
) -> Path:
    when = when or datetime.now(timezone.utc)
    session.ensure_capacity(0)
    bundle = session.bundle_dir(frequency_hz, when)
    errors: list[str] = []
    artifacts: Dict[str, str] = {}

    if cfg.screenshot:
        try:
            dest = screenshot_mod.capture(
                bundle / "screenshot.jpg",
                method=cfg.screenshot_method,
                pidfile=cfg.screenshot_pidfile,
                wrapper_dir=cfg.screenshot_dir,
                signal_name=cfg.screenshot_signal,
                timeout=cfg.screenshot_timeout,
                command=cfg.screenshot_command,
            )
            artifacts["screenshot"] = dest.name
        except (screenshot_mod.ScreenshotError, OSError, subprocess.SubprocessError) as exc:
            errors.append(f"screenshot: {exc}")

    audio_path = _copy_audio(client, cfg, bundle, errors)
    if audio_path is not None:
        artifacts["audio"] = audio_path.name
        if cfg.spectrogram:
            if make_spectrogram(audio_path, bundle / "spectrogram.png"):
                artifacts["spectrogram"] = "spectrogram.png"
            else:
                errors.append("spectrogram: no working sox/ffmpeg backend")

    iq_path = _copy_iq(client, session, cfg, bundle, errors)
    if iq_path is not None:
        artifacts["iq"] = iq_path.name

    metadata = {
        "created_at": when.astimezone(timezone.utc).isoformat(),
        "reason": reason,
        "frequency_hz": int(frequency_hz),
        "mode": mode,
        "passband_hz": passband_hz,
        "level_db": level_db,
        "squelch_db": squelch_db,
        "sample_rate_hz": cfg.iq_rate_hz,
        "artifacts": artifacts,
        "errors": errors,
        **cfg.extra,
    }
    session.write_json(bundle / "metadata.json", metadata)

    record = {
        "kind": "bundle",
        "dir": str(bundle.relative_to(session.path)),
        "start": metadata["created_at"],
        "freq_hz": int(frequency_hz),
        "mode": mode,
        "passband_hz": passband_hz,
        "trigger": reason,
        "level_db": level_db,
        "squelch_db": squelch_db,
        "artifacts": list(artifacts.values()),
        "iq_bytes": int(iq_path.stat().st_size) if iq_path is not None else 0,
        "errors": errors,
    }
    session.record_bundle(bundle, record)
    return bundle
