"""Fixed IST posting slots, with catch-up for misses and a gap between posts.

A run publishes at most one slot. Slots that were skipped or delayed stay open
until a later run fills them, as long as they are still inside the catch-up
window and at least ``min_minutes_between_posts`` have passed since the last post.
A slot also opens ``early_minutes`` (30) before its time, because GitHub often runs
the scheduled workflow early-ish and then skips the run right after the slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SlotDecision:
    slot: datetime | None
    reason: str

    @property
    def should_post(self) -> bool:
        return self.reason == "due"


def slot_id(moment: datetime) -> str:
    return moment.isoformat(timespec="minutes")


def choose_slot(
    now: datetime,
    *,
    slots: tuple[str, ...],
    timezone: str,
    filled: set[str],
    last_post_at: datetime | None,
    min_minutes_between_posts: int,
    catchup_hours: int,
    active_from: datetime | None,
    initial_grace_minutes: int,
    early_minutes: int = 30,
) -> SlotDecision:
    zone = ZoneInfo(timezone)
    current = now.astimezone(zone)
    if active_from is None:
        return SlotDecision(None, "not_armed")
    armed = active_from.astimezone(zone)
    window_start = current - timedelta(hours=catchup_hours)
    grace_start = armed - timedelta(minutes=initial_grace_minutes)
    eligible_start = max(window_start, grace_start)
    due = [
        moment
        for moment in _occurrences(
            current + timedelta(minutes=early_minutes), slots, zone, eligible_start
        )
        if slot_id(moment) not in filled
    ]
    if not due:
        return SlotDecision(None, "none")
    oldest = due[0]
    if last_post_at is not None:
        elapsed = current - last_post_at.astimezone(zone)
        if elapsed < timedelta(minutes=min_minutes_between_posts):
            return SlotDecision(oldest, "gap")
    return SlotDecision(oldest, "due")


def _occurrences(
    now: datetime,
    slots: tuple[str, ...],
    zone: ZoneInfo,
    eligible_start: datetime,
) -> list[datetime]:
    start_date = eligible_start.astimezone(zone).date()
    end_date = now.astimezone(zone).date()
    found: list[datetime] = []
    day = start_date
    while day <= end_date:
        for label in slots:
            hour, minute = (int(part) for part in label.split(":"))
            moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            if eligible_start <= moment <= now:
                found.append(moment)
        day += timedelta(days=1)
    found.sort()
    return found
