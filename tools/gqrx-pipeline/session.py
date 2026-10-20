#!/usr/bin/env python3
"""Session directory, JSONL index, quota and retention helpers.

The session layout is intentionally plain so that gqrx, rtl_433, URH, Python
and spreadsheet tools can all read it:

    <root>/<timestamp>/
        session.json
        index.jsonl
        scan.jsonl
        events.jsonl
        bundles/<timestamp>-<freq>/
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


class QuotaExceeded(RuntimeError):
    """Raised when a capture would exceed the configured storage budget."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp(moment: Optional[datetime] = None) -> str:
    moment = moment or utc_now()
    return moment.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")


def human_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{value} B"


def sha256_file(path: Path, limit: Optional[int] = None) -> str:
    digest = hashlib.sha256()
    remaining = limit
    with path.open("rb") as handle:
        while True:
            want = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if want <= 0:
                break
            block = handle.read(want)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def copy_limited(src: Path, dst: Path, max_bytes: int) -> int:
    """Copy at most max_bytes and return the number of bytes written."""
    written = 0
    with src.open("rb") as source, dst.open("wb") as target:
        while written < max_bytes:
            block = source.read(min(1024 * 1024, max_bytes - written))
            if not block:
                break
            target.write(block)
            written += len(block)
    return written


class Session:
    def __init__(
        self,
        path: Path,
        *,
        quota_bytes: int = 1024 * 1024 * 1024,
        retention_days: float = 7.0,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.path = Path(path)
        self.quota_bytes = int(quota_bytes)
        self.retention_days = float(retention_days)
        self.metadata = dict(metadata or {})
        self.bundles_dir = self.path / "bundles"
        self.index_path = self.path / "index.jsonl"
        self.scan_path = self.path / "scan.jsonl"
        self.events_path = self.path / "events.jsonl"
        self.session_path = self.path / "session.json"

    @classmethod
    def create(
        cls,
        root: Path,
        *,
        quota_bytes: int = 1024 * 1024 * 1024,
        retention_days: float = 7.0,
        metadata: Optional[Dict[str, Any]] = None,
        moment: Optional[datetime] = None,
    ) -> "Session":
        root = Path(root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        stamp = utc_stamp(moment)
        path = root / stamp
        suffix = 1
        while path.exists():
            suffix += 1
            path = root / f"{stamp}-{suffix:02d}"
        path.mkdir(parents=True)
        (path / "bundles").mkdir()
        session = cls(
            path,
            quota_bytes=quota_bytes,
            retention_days=retention_days,
            metadata=metadata,
        )
        session.write_json(
            session.session_path,
            {
                "created_at": (moment or utc_now()).astimezone(timezone.utc).isoformat(),
                "quota_bytes": session.quota_bytes,
                "retention_days": session.retention_days,
                "metadata": session.metadata,
            },
        )
        for name in ("index.jsonl", "scan.jsonl", "events.jsonl"):
            (path / name).touch()
        return session

    def write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)

    def append_jsonl(self, path: Path, payload: Dict[str, Any]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")

    def bundle_dir(self, frequency_hz: int, moment: Optional[datetime] = None) -> Path:
        self.bundles_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_stamp(moment)
        base = f"{stamp}-{int(frequency_hz)}"
        candidate = self.bundles_dir / base
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = self.bundles_dir / f"{base}-{suffix:02d}"
        candidate.mkdir(parents=True)
        return candidate

    def total_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except FileNotFoundError:
                    pass
        return total

    def ensure_capacity(self, extra_bytes: int = 0) -> None:
        if self.total_bytes() + int(extra_bytes) > self.quota_bytes:
            raise QuotaExceeded(
                f"session quota {human_bytes(self.quota_bytes)} would be exceeded "
                f"(current {human_bytes(self.total_bytes())}, extra {human_bytes(int(extra_bytes))})"
            )

    def plan_iq_capture(
        self,
        rate_hz: float,
        *,
        bytes_per_sample: int = 2,
        max_bytes: int = 8 * 1024 * 1024,
        desired_seconds: float = 1.0,
    ) -> tuple[float, int]:
        """Return (seconds, estimated_bytes) bounded by max_bytes and quota."""
        if rate_hz <= 0:
            raise ValueError("rate_hz must be positive")
        seconds = max(0.0, float(desired_seconds))
        bytes_wanted = int(seconds * rate_hz * bytes_per_sample)
        allowed = min(int(max_bytes), bytes_wanted)
        free = max(0, self.quota_bytes - self.total_bytes())
        allowed = min(allowed, free)
        if allowed <= 0:
            return 0.0, 0
        return allowed / (rate_hz * bytes_per_sample), allowed

    def record_bundle(self, bundle: Path, record: Dict[str, Any]) -> None:
        self.append_jsonl(self.index_path, record)

    def record_scan(self, record: Dict[str, Any]) -> None:
        self.append_jsonl(self.scan_path, record)

    def record_event(self, record: Dict[str, Any]) -> None:
        self.append_jsonl(self.events_path, record)

    def cleanup_expired(self, now: Optional[datetime] = None) -> list[Path]:
        """Delete bundle directories older than retention. ``KEEP`` skips a bundle."""
        now = now or utc_now()
        cutoff = now - timedelta(days=self.retention_days)
        removed: list[Path] = []
        if not self.bundles_dir.exists():
            return removed
        for bundle in sorted(self.bundles_dir.iterdir()):
            if not bundle.is_dir():
                continue
            if (bundle / "KEEP").exists():
                continue
            mtime = datetime.fromtimestamp(bundle.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                shutil.rmtree(bundle)
                removed.append(bundle)
        return removed


def bundle_artifacts(bundle: Path) -> list[Dict[str, Any]]:
    artifacts = []
    for path in sorted(bundle.iterdir()):
        if path.is_file():
            artifacts.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path) if path.stat().st_size <= 16 * 1024 * 1024 else None,
                }
            )
    return artifacts
