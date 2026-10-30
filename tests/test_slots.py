import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from poster.slots import choose_slot, slot_id

IST = ZoneInfo("Asia/Kolkata")
SLOTS = ("03:30", "07:30", "18:30", "21:30", "23:30")


def decide(now, **kwargs):
    params = dict(
        slots=SLOTS,
        timezone="Asia/Kolkata",
        filled=set(),
        last_post_at=None,
        min_minutes_between_posts=60,
        catchup_hours=24,
        active_from=datetime(2026, 10, 20, 0, 0, tzinfo=IST),
        initial_grace_minutes=75,
    )
    params.update(kwargs)
    return choose_slot(now, **params)


class SlotTests(unittest.TestCase):
    def test_fresh_install_only_picks_the_recent_slot(self):
        now = datetime(2026, 10, 27, 19, 0, tzinfo=IST)
        decision = decide(now, active_from=now)
        self.assertEqual(decision.reason, "due")
        self.assertEqual(slot_id(decision.slot), "2026-10-27T18:30+05:30")

    def test_before_first_slot_posts_nothing(self):
        now = datetime(2026, 10, 27, 2, 55, tzinfo=IST)
        decision = decide(now, active_from=now)
        self.assertEqual(decision.reason, "none")
        self.assertIsNone(decision.slot)

    def test_slot_opens_thirty_minutes_early(self):
        now = datetime(2026, 10, 27, 3, 4, tzinfo=IST)
        decision = decide(now, active_from=now)
        self.assertEqual(decision.reason, "due")
        self.assertEqual(slot_id(decision.slot), "2026-10-27T03:30+05:30")

    def test_catchup_is_oldest_open_slot_only(self):
        now = datetime(2026, 10, 27, 19, 0, tzinfo=IST)
        decision = decide(now)
        self.assertEqual(decision.reason, "due")
        self.assertEqual(slot_id(decision.slot), "2026-10-26T21:30+05:30")

    def test_filled_slots_are_skipped(self):
        now = datetime(2026, 10, 27, 19, 0, tzinfo=IST)
        filled = {
            "2026-10-26T21:30+05:30",
            "2026-10-26T23:30+05:30",
            "2026-10-27T03:30+05:30",
        }
        decision = decide(now, filled=filled)
        self.assertEqual(slot_id(decision.slot), "2026-10-27T07:30+05:30")

    def test_gap_blocks_a_second_post(self):
        now = datetime(2026, 10, 27, 19, 0, tzinfo=IST)
        decision = decide(now, last_post_at=now - timedelta(minutes=30))
        self.assertEqual(decision.reason, "gap")
        self.assertFalse(decision.should_post)

    def test_sixty_minutes_is_enough(self):
        now = datetime(2026, 10, 27, 19, 0, tzinfo=IST)
        decision = decide(now, last_post_at=now - timedelta(minutes=60))
        self.assertEqual(decision.reason, "due")

    def test_slots_older_than_the_catchup_window_are_dropped(self):
        now = datetime(2026, 10, 27, 19, 0, tzinfo=IST)
        decision = decide(now, catchup_hours=2, active_from=datetime(2026, 10, 1, tzinfo=IST))
        self.assertEqual(slot_id(decision.slot), "2026-10-27T18:30+05:30")

    def test_unarmed_scheduler_does_not_invent_a_slot(self):
        now = datetime(2026, 10, 27, 19, 0, tzinfo=IST)
        decision = decide(now, active_from=None)
        self.assertEqual(decision.reason, "not_armed")


if __name__ == "__main__":
    unittest.main()
