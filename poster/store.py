"""Queue, ready, posted, and failed files, plus the slot state file."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from poster.errors import ItemError

DIRS = ("queue", "ready", "posted", "failed")


def parse_dt(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp {value!r} must include a numeric UTC offset")
    return parsed


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ItemError(f"{path.name} is not valid JSON: {exc}", "parse") from exc
    if not isinstance(data, dict):
        raise ItemError(f"{path.name} must be a JSON object", "parse")
    return data


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def iter_json(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def ensure_dirs(root: Path) -> None:
    for name in (*DIRS, "logs", "status", "work"):
        (root / name).mkdir(parents=True, exist_ok=True)


def identity_of(data: dict) -> str | None:
    source = str(data.get("source_url") or "").strip()
    if source:
        return "url:" + source
    video = str(data.get("video_file") or "").strip()
    if video:
        return "file:" + video
    return None


def item_sort_key(path: Path, data: dict) -> tuple[str, str]:
    raw = data.get("enqueued_at")
    if isinstance(raw, str) and raw.strip():
        try:
            stamp = parse_dt(raw).astimezone(timezone.utc).isoformat()
        except ValueError:
            stamp = raw
    else:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return (stamp, path.name)


def posted_identities(root: Path) -> set[str]:
    found: set[str] = set()
    for path in iter_json(root / "posted"):
        try:
            data = read_json(path)
        except ItemError:
            continue
        ident = identity_of(data)
        if ident:
            found.add(ident)
    return found


@dataclass
class State:
    active_from: datetime | None = None
    filled_slots: set[str] = field(default_factory=set)
    last_post_at: datetime | None = None


def load_state(root: Path) -> State:
    path = root / "status" / "state.json"
    raw: dict = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    filled = {str(item) for item in raw.get("filled_slots") or []}
    last = _optional_dt(raw.get("last_post_at"))
    active = _optional_dt(raw.get("active_from"))
    for posted in iter_json(root / "posted"):
        try:
            data = read_json(posted)
        except ItemError:
            continue
        slot = data.get("slot")
        if isinstance(slot, str) and slot:
            filled.add(slot)
        posted_at = _optional_dt(data.get("posted_at"))
        if posted_at is not None and (last is None or posted_at > last):
            last = posted_at
    return State(active_from=active, filled_slots=filled, last_post_at=last)


def save_state(root: Path, state: State) -> None:
    write_json(
        root / "status" / "state.json",
        {
            "active_from": state.active_from.isoformat(timespec="seconds") if state.active_from else None,
            "filled_slots": sorted(state.filled_slots),
            "last_post_at": state.last_post_at.isoformat(timespec="seconds") if state.last_post_at else None,
        },
    )


def append_log(root: Path, record: dict) -> None:
    path = root / "logs" / "posts.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def unique_dest(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / name
    if not dest.exists():
        return dest
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    return directory / f"{dest.stem}-{stamp}{dest.suffix}"


def move_record(src: Path, directory: Path, data: dict) -> Path:
    dest = unique_dest(directory, src.name)
    write_json(dest, data)
    if src.exists() and src.resolve() != dest.resolve():
        src.unlink()
    return dest


def _optional_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return parse_dt(value)
