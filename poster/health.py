"""status/health.json and the daily markdown summary an external assistant can read."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from poster.config import Config
from poster.slots import choose_slot, slot_id
from poster.store import State, iter_json, parse_dt, read_json, write_json
from poster.errors import ItemError


def build_health(root: Path, config: Config, state: State, now: datetime) -> dict:
    zone = ZoneInfo(config.timezone)
    current = now.astimezone(zone)
    ready = iter_json(root / "ready")
    queue = iter_json(root / "queue")
    ready_count = len(ready)
    queue_count = len(queue)
    low = ready_count < config.low_queue_threshold
    decision = choose_slot(
        current,
        slots=config.slots,
        timezone=config.timezone,
        filled=set(state.filled_slots),
        last_post_at=state.last_post_at,
        min_minutes_between_posts=config.min_minutes_between_posts,
        catchup_hours=config.catchup_hours,
        active_from=state.active_from,
        initial_grace_minutes=config.initial_grace_minutes,
    )
    message = None
    if low:
        message = (
            f"Ready backlog is {ready_count}, below {config.low_queue_threshold}. "
            f"Unprepared queue items: {queue_count}. "
            "Refill queue/ with watermark-checked clips."
        )
    return {
        "last_checked_at": current.isoformat(timespec="seconds"),
        "timezone": config.timezone,
        "queue_count": queue_count,
        "ready_count": ready_count,
        "low_queue": low,
        "low_queue_threshold": config.low_queue_threshold,
        "message": message,
        "last_post": _last_post(root),
        "failures_last_24h": _failures_since(root, current - timedelta(hours=24)),
        "next_open_slot": slot_id(decision.slot) if decision.slot else None,
        "next_open_slot_reason": decision.reason,
        "slots": list(config.slots),
        "min_short_side": config.min_short_side,
        "active_from": state.active_from.isoformat(timespec="seconds") if state.active_from else None,
        "last_post_at": state.last_post_at.isoformat(timespec="seconds") if state.last_post_at else None,
    }


def write_health(root: Path, config: Config, state: State, now: datetime) -> bool:
    """Write health and today's summary. Return True when a file changed."""
    health = build_health(root, config, state, now)
    daily = render_daily(root, config, health, now)
    changed = _write_if_material(root / "status" / "health.json", health, config.health_heartbeat_hours, now)
    day = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
    daily_path = root / "status" / f"daily-{day}.md"
    changed = _write_text_if_changed(daily_path, daily) or changed
    return changed


def render_daily(root: Path, config: Config, health: dict, now: datetime) -> str:
    zone = ZoneInfo(config.timezone)
    current = now.astimezone(zone)
    day = current.date()
    posts = []
    for path in iter_json(root / "posted"):
        try:
            data = read_json(path)
        except ItemError:
            continue
        posted_at = data.get("posted_at")
        if not isinstance(posted_at, str):
            continue
        try:
            moment = parse_dt(posted_at).astimezone(zone)
        except ValueError:
            continue
        if moment.date() == day:
            posts.append((moment, data, path.name))
    posts.sort(key=lambda item: item[0])
    failures = []
    for entry in health["failures_last_24h"]:
        try:
            moment = parse_dt(entry["failed_at"]).astimezone(zone)
        except (KeyError, ValueError):
            continue
        if moment.date() == day:
            failures.append(entry)
    lines = [
        f"# Daily summary {day.isoformat()} ({config.timezone})",
        "",
        f"- Ready clips: {health['ready_count']}",
        f"- Queue (not yet prepared): {health['queue_count']}",
        f"- Low queue (ready < {config.low_queue_threshold}): {'yes' if health['low_queue'] else 'no'}",
        "",
    ]
    if health["message"]:
        lines.extend([f"**{health['message']}**", ""])
    lines.append("## Posts")
    if not posts:
        lines.append("- None yet today.")
    for moment, data, name in posts:
        caption = _snippet(str(data.get("caption") or ""))
        lines.append(
            f"- {moment.strftime('%H:%M')} slot {data.get('slot') or 'n/a'} — {caption} "
            f"— buffer `{data.get('buffer_post_id') or 'n/a'}` — {name}"
        )
    lines.extend(["", "## Failures"])
    if not failures:
        lines.append("- None today.")
    for entry in failures:
        lines.append(f"- {entry['failed_at']} `{entry['stage']}` — {entry['error']} ({entry['file']})")
    lines.extend(["", "## Slots", ""])
    filled = set()
    state_path = root / "status" / "state.json"
    if state_path.is_file():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            filled = set(raw.get("filled_slots") or [])
        except json.JSONDecodeError:
            filled = set()
    for label in config.slots:
        hour, minute = (int(part) for part in label.split(":"))
        moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        ident = slot_id(moment)
        if ident in filled:
            state = "filled"
        elif moment <= current:
            state = "open (missed, will catch up)"
        else:
            state = "upcoming"
        lines.append(f"- {label} {state}")
    lines.append("")
    return "\n".join(lines)


def _last_post(root: Path) -> dict | None:
    newest: tuple[datetime, dict, str] | None = None
    for path in iter_json(root / "posted"):
        try:
            data = read_json(path)
        except ItemError:
            continue
        raw = data.get("posted_at")
        if not isinstance(raw, str):
            continue
        try:
            moment = parse_dt(raw)
        except ValueError:
            continue
        if newest is None or moment > newest[0]:
            newest = (moment, data, path.name)
    if newest is None:
        return None
    moment, data, name = newest
    return {
        "posted_at": moment.isoformat(timespec="seconds"),
        "slot": data.get("slot"),
        "buffer_post_id": data.get("buffer_post_id"),
        "release_asset_url": data.get("release_asset_url"),
        "source_url": data.get("source_url"),
        "file": f"posted/{name}",
    }


def _failures_since(root: Path, start: datetime) -> list[dict]:
    found: list[dict] = []
    for path in iter_json(root / "failed"):
        try:
            data = read_json(path)
        except ItemError:
            continue
        raw = data.get("failed_at")
        if not isinstance(raw, str):
            continue
        try:
            moment = parse_dt(raw)
        except ValueError:
            continue
        if moment < start:
            continue
        found.append(
            {
                "failed_at": moment.isoformat(timespec="seconds"),
                "file": f"failed/{path.name}",
                "stage": data.get("error_stage") or "unknown",
                "error": _snippet(str(data.get("error") or ""), 400),
                "source_url": data.get("source_url"),
            }
        )
    found.sort(key=lambda item: item["failed_at"], reverse=True)
    return found[:50]


def _write_if_material(path: Path, health: dict, heartbeat_hours: int, now: datetime) -> bool:
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = None
        if isinstance(previous, dict) and _material(previous) == _material(health):
            checked = previous.get("last_checked_at")
            if isinstance(checked, str):
                try:
                    age = now - parse_dt(checked)
                except ValueError:
                    age = timedelta.max
                if age < timedelta(hours=heartbeat_hours):
                    return False
    write_json(path, health)
    return True


def _material(health: dict) -> dict:
    return {key: value for key, value in health.items() if key != "last_checked_at"}


def _write_text_if_changed(path: Path, text: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _snippet(text: str, limit: int = 140) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
