"""Load config/poster.json. Missing keys fall back to the defaults below."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS: dict[str, object] = {
    "min_short_side": 1080,
    "min_duration_seconds": 0.5,
    "max_duration_seconds": 140,
    "max_bytes": 512 * 1024 * 1024,
    "trim_to_max_duration": True,
    "timezone": "Asia/Kolkata",
    "slots": ["03:30", "07:30", "18:30", "21:30", "23:30"],
    "min_minutes_between_posts": 60,
    "catchup_hours": 24,
    "initial_grace_minutes": 75,
    "max_attempts_per_run": 5,
    "prefetch_batch": 5,
    "buffer_retries": 3,
    "buffer_retry_base_seconds": 2,
    "buffer_retry_cap_seconds": 60,
    "download_timeout_seconds": 900,
    "low_queue_threshold": 5,
    "max_caption_chars": 280,
    "channel_services": ["twitter", "x"],
    "channel_names": ["Clipsjohns"],
    "release_tag_prefix": "clips",
    "health_heartbeat_hours": 3,
}


@dataclass(frozen=True)
class Config:
    min_short_side: int
    min_duration_seconds: float
    max_duration_seconds: float
    max_bytes: int
    trim_to_max_duration: bool
    timezone: str
    slots: tuple[str, ...]
    min_minutes_between_posts: int
    catchup_hours: int
    initial_grace_minutes: int
    max_attempts_per_run: int
    prefetch_batch: int
    buffer_retries: int
    buffer_retry_base_seconds: float
    buffer_retry_cap_seconds: float
    download_timeout_seconds: int
    low_queue_threshold: int
    max_caption_chars: int
    channel_services: tuple[str, ...]
    channel_names: tuple[str, ...]
    release_tag_prefix: str
    health_heartbeat_hours: int

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise FileNotFoundError(f"Missing config file: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"Unknown config keys in {path}: {', '.join(unknown)}")
    merged = {**DEFAULTS, **raw}
    slots = _slots(merged["slots"])
    timezone = str(merged["timezone"])
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone {timezone}") from exc
    min_short = int(merged["min_short_side"])
    if min_short < 1:
        raise ValueError("min_short_side must be positive")
    min_dur = float(merged["min_duration_seconds"])
    max_dur = float(merged["max_duration_seconds"])
    if not (0 < min_dur < max_dur):
        raise ValueError("duration bounds must satisfy 0 < min < max")
    return Config(
        min_short_side=min_short,
        min_duration_seconds=min_dur,
        max_duration_seconds=max_dur,
        max_bytes=int(merged["max_bytes"]),
        trim_to_max_duration=bool(merged["trim_to_max_duration"]),
        timezone=timezone,
        slots=slots,
        min_minutes_between_posts=int(merged["min_minutes_between_posts"]),
        catchup_hours=int(merged["catchup_hours"]),
        initial_grace_minutes=int(merged["initial_grace_minutes"]),
        max_attempts_per_run=int(merged["max_attempts_per_run"]),
        prefetch_batch=int(merged["prefetch_batch"]),
        buffer_retries=int(merged["buffer_retries"]),
        buffer_retry_base_seconds=float(merged["buffer_retry_base_seconds"]),
        buffer_retry_cap_seconds=float(merged["buffer_retry_cap_seconds"]),
        download_timeout_seconds=int(merged["download_timeout_seconds"]),
        low_queue_threshold=int(merged["low_queue_threshold"]),
        max_caption_chars=int(merged["max_caption_chars"]),
        channel_services=tuple(str(s) for s in merged["channel_services"]),
        channel_names=tuple(str(s) for s in merged["channel_names"]),
        release_tag_prefix=str(merged["release_tag_prefix"]),
        health_heartbeat_hours=int(merged["health_heartbeat_hours"]),
    )


def _slots(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("slots must be a non-empty list of HH:MM strings")
    parsed: list[tuple[int, int, str]] = []
    for item in value:
        text = str(item).strip()
        parts = text.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError(f"Invalid slot time {item!r}; use HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if hour > 23 or minute > 59:
            raise ValueError(f"Invalid slot time {text}")
        parsed.append((hour, minute, f"{hour:02d}:{minute:02d}"))
    parsed.sort()
    labels = tuple(label for _, _, label in parsed)
    if len(set(labels)) != len(labels):
        raise ValueError("slots contains a duplicate time")
    return labels
