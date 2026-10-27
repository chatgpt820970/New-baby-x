import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from poster.errors import ItemError, RunAbort
from poster.runner import Prepared, run_prefetch, run_slot
from poster.store import read_json

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 27, 7, 40, tzinfo=IST)
HOSTED = "https://github.com/example/repo/releases/download/clips-2026-10-27/clip.mp4"


def make_repo(root: Path, *, last_post_at=None) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "config" / "poster.json").write_text("{}\n", encoding="utf-8")
    for name in ("queue", "ready", "posted", "failed", "status", "logs"):
        (root / name).mkdir()
    (root / "status" / "state.json").write_text(
        json.dumps(
            {
                "active_from": "2026-10-27T07:00:00+05:30",
                "filled_slots": [],
                "last_post_at": last_post_at,
            }
        ),
        encoding="utf-8",
    )
    return root


def write_item(directory: Path, name: str, **overrides) -> Path:
    payload = {
        "source_url": f"https://example.com/watch/{name}",
        "caption": "hello",
        "watermark_checked": True,
        "enqueued_at": "2026-10-27T01:00:00+05:30",
    }
    payload.update(overrides)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class RunnerTests(unittest.TestCase):
    def test_failed_clip_is_skipped_and_the_next_one_posts(self):
        root = make_repo(Path(self._tmp()) )
        write_item(
            root / "ready",
            "a.json",
            source_url="https://example.com/bad",
            enqueued_at="2026-10-27T01:00:00+05:30",
        )
        write_item(
            root / "ready",
            "b.json",
            source_url="https://example.com/good",
            enqueued_at="2026-10-27T02:00:00+05:30",
        )
        prepared = []

        def prepare(data, **_kwargs):
            prepared.append(data["source_url"])
            if "bad" in data["source_url"]:
                raise ItemError("short side 720px is below 1080px", "quality")
            return Prepared(HOSTED, {"width": 1920, "height": 1080, "duration": 8, "bytes": 1000}, False)

        published = []

        def publish(**kwargs):
            published.append(kwargs["data"]["source_url"])
            return "post-1"

        code = run_slot(
            root,
            now=NOW,
            env={"BUFFER_API_KEY": "test-key-value"},
            prepare=prepare,
            publish=publish,
            resolve_channel=lambda _env, _config: "channel-1",
        )
        self.assertEqual(code, 0)
        self.assertEqual(prepared, ["https://example.com/bad", "https://example.com/good"])
        self.assertEqual(published, ["https://example.com/good"])
        posted = list((root / "posted").glob("*.json"))
        failed = list((root / "failed").glob("*.json"))
        self.assertEqual(len(posted), 1)
        self.assertEqual(len(failed), 1)
        body = read_json(posted[0])
        self.assertEqual(body["buffer_post_id"], "post-1")
        self.assertEqual(body["release_asset_url"], HOSTED)
        self.assertEqual(body["slot"], "2026-10-27T07:30+05:30")
        self.assertIn("720", read_json(failed[0])["error"])
        state = json.loads((root / "status" / "state.json").read_text(encoding="utf-8"))
        self.assertIn("2026-10-27T07:30+05:30", state["filled_slots"])

    def test_watermark_must_be_true_and_blocks_download(self):
        root = make_repo(self._tmp())
        write_item(root / "ready", "plain.json", watermark_checked=False)
        def prepare(data, **_kwargs):
            raise AssertionError("watermark failures must not download")

        code = run_slot(
            root,
            now=NOW,
            env={"BUFFER_API_KEY": "test-key-value"},
            prepare=prepare,
            publish=lambda **_kwargs: "nope",
            resolve_channel=lambda *_args: "channel-1",
        )
        self.assertEqual(code, 1)
        failed = read_json(next((root / "failed").glob("*.json")))
        self.assertEqual(failed["error_stage"], "watermark")
        self.assertIn("watermark_checked", failed["error"])
        self.assertFalse(list((root / "posted").glob("*.json")))

    def test_duplicate_source_is_not_posted_again(self):
        root = make_repo(self._tmp())
        write_item(root / "posted", "old.json", source_url="https://example.com/same", buffer_post_id="earlier")
        write_item(root / "ready", "again.json", source_url="https://example.com/same")
        code = run_slot(
            root,
            now=NOW,
            env={"BUFFER_API_KEY": "test-key-value"},
            prepare=lambda data, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate")),
            publish=lambda **_kwargs: "nope",
            resolve_channel=lambda *_args: "channel-1",
        )
        self.assertEqual(code, 1)
        self.assertTrue(any(read_json(path)["error_stage"] == "duplicate" for path in (root / "failed").glob("*.json")))

    def test_gap_does_not_touch_the_queue(self):
        root = make_repo(self._tmp(), last_post_at="2026-10-27T07:20:00+05:30")
        item = write_item(root / "ready", "clip.json")
        code = run_slot(
            root,
            now=NOW,
            env={"BUFFER_API_KEY": "test-key-value"},
            prepare=lambda data, **_kwargs: (_ for _ in ()).throw(AssertionError("gap")),
            publish=lambda **_kwargs: "nope",
            resolve_channel=lambda *_args: "channel-1",
        )
        self.assertEqual(code, 0)
        self.assertTrue(item.exists())

    def test_missing_api_key_leaves_the_queue_in_place(self):
        root = make_repo(self._tmp())
        item = write_item(root / "ready", "clip.json")
        code = run_slot(root, now=NOW, env={}, prepare=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("key")))
        self.assertEqual(code, 2)
        self.assertTrue(item.exists())
        log = (root / "logs" / "posts.log").read_text(encoding="utf-8")
        self.assertIn("BUFFER_API_KEY", log)
        self.assertIn("Nothing was published", log)

    def test_stops_after_five_failures(self):
        root = make_repo(self._tmp())
        for index in range(6):
            write_item(
                root / "queue",
                f"{index}.json",
                source_url=f"https://example.com/{index}",
                enqueued_at=f"2026-10-27T01:0{index}:00+05:30",
            )

        def prepare(data, **_kwargs):
            raise ItemError("yt-dlp failed", "download")

        code = run_slot(
            root,
            now=NOW,
            env={"BUFFER_API_KEY": "test-key-value"},
            prepare=prepare,
            publish=lambda **_kwargs: "nope",
            resolve_channel=lambda *_args: "channel-1",
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(list((root / "failed").glob("*.json"))), 5)
        self.assertEqual(len(list((root / "queue").glob("*.json"))), 1)

    def test_dry_run_does_not_publish_or_move(self):
        root = make_repo(self._tmp())
        item = write_item(root / "queue", "clip.json")
        calls = {"publish": 0, "prepare": 0}

        def prepare(data, **kwargs):
            calls["prepare"] += 1
            self.assertTrue(kwargs["dry_run"])
            return Prepared(HOSTED, {"width": 1920, "height": 1080, "duration": 4, "bytes": 10}, False)

        def publish(**_kwargs):
            calls["publish"] += 1
            return "should-not-run"

        code = run_slot(root, dry_run=True, now=NOW, env={}, prepare=prepare, publish=publish)
        self.assertEqual(code, 0)
        self.assertEqual(calls, {"publish": 0, "prepare": 1})
        self.assertTrue(item.exists())
        report = json.loads((root / "status" / "dry-run-last.json").read_text(encoding="utf-8"))
        self.assertTrue(report["ok"])
        self.assertFalse(report["buffer_called"])

    def test_prefetch_moves_a_good_clip_to_ready(self):
        root = make_repo(self._tmp())
        write_item(root / "queue", "clip.json")

        def prepare(data, **_kwargs):
            return Prepared(HOSTED, {"width": 1080, "height": 1920, "duration": 6, "bytes": 20}, False)

        code = run_prefetch(root, now=NOW, env={"GITHUB_TOKEN": "ghs_test"}, prepare=prepare)
        self.assertEqual(code, 0)
        ready = list((root / "ready").glob("*.json"))
        self.assertEqual(len(ready), 1)
        body = read_json(ready[0])
        self.assertTrue(body["ready"])
        self.assertEqual(body["release_asset_url"], HOSTED)
        self.assertEqual(body["video_meta"]["height"], 1920)
        self.assertFalse(list((root / "queue").glob("*.json")))
        self.assertFalse(list((root / "posted").glob("*.json")))

    def test_buffer_rejection_records_the_release_url(self):
        root = make_repo(self._tmp())
        write_item(root / "ready", "clip.json")

        def publish(**_kwargs):
            raise ItemError("Buffer refused the video URL", "buffer")

        code = run_slot(
            root,
            now=NOW,
            env={"BUFFER_API_KEY": "test-key-value"},
            prepare=lambda data, **_kwargs: Prepared(
                HOSTED, {"width": 1920, "height": 1080, "duration": 8, "bytes": 1000}, False
            ),
            publish=publish,
            resolve_channel=lambda *_args: "channel-1",
        )
        self.assertEqual(code, 1)
        body = read_json(next((root / "failed").glob("*.json")))
        self.assertEqual(body["error_stage"], "buffer")
        self.assertEqual(body["release_asset_url"], HOSTED)

    def test_run_abort_does_not_fail_the_item(self):
        root = make_repo(self._tmp())
        item = write_item(root / "ready", "clip.json")

        def prepare(data, **_kwargs):
            raise RunAbort("Buffer rate limit persisted")

        code = run_slot(
            root,
            now=NOW,
            env={"BUFFER_API_KEY": "test-key-value"},
            prepare=prepare,
            publish=lambda **_kwargs: "nope",
            resolve_channel=lambda *_args: "channel-1",
        )
        self.assertEqual(code, 2)
        self.assertTrue(item.exists())
        self.assertFalse(list((root / "failed").glob("*.json")))

    def _tmp(self) -> Path:
        import tempfile

        path = Path(tempfile.mkdtemp(prefix="poster-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path


if __name__ == "__main__":
    unittest.main()
