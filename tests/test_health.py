import json
import unittest
from datetime import datetime
from pathlib import Path

from poster.config import load_config
from poster.health import build_health, render_daily
from poster.store import State, write_json

ROOT = Path(__file__).resolve().parents[1]


class HealthTests(unittest.TestCase):
    def test_low_queue_flag_and_recent_failures(self):
        import tempfile

        root = Path(tempfile.mkdtemp(prefix="health-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        for name in ("queue", "ready", "posted", "failed", "status"):
            (root / name).mkdir()
        config = load_config(ROOT / "config" / "poster.json")
        now = datetime.fromisoformat("2026-10-27T20:00:00+05:30")
        for index in range(4):
            write_json(root / "ready" / f"{index}.json", {"caption": "x", "watermark_checked": True})
        write_json(
            root / "failed" / "old.json",
            {"failed_at": "2026-10-20T20:00:00+05:30", "error": "stale", "error_stage": "quality"},
        )
        write_json(
            root / "failed" / "new.json",
            {
                "failed_at": "2026-10-27T18:00:00+05:30",
                "error": "short side 720px",
                "error_stage": "quality",
                "source_url": "https://example.com/a",
            },
        )
        state = State(active_from=datetime.fromisoformat("2026-10-01T00:00:00+05:30"))
        health = build_health(root, config, state, now)
        self.assertEqual(health["ready_count"], 4)
        self.assertTrue(health["low_queue"])
        self.assertIn("below 5", health["message"])
        self.assertEqual(len(health["failures_last_24h"]), 1)
        self.assertEqual(health["failures_last_24h"][0]["stage"], "quality")
        daily = render_daily(root, config, health, now)
        self.assertIn("Low queue", daily)
        self.assertIn("yes", daily)
        self.assertIn("short side 720px", daily)
        self.assertNotIn("stale", daily)
        json.dumps(health)


if __name__ == "__main__":
    unittest.main()
