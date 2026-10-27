import json
import unittest

from poster.buffer_api import (
    MISSING_KEY,
    build_post_input,
    graphql,
    publish_video,
    require_api_key,
    resolve_channel_id,
)
from poster.errors import RunAbort
from poster.media import yt_dlp_command
from poster.redact import redact


class BufferContractTests(unittest.TestCase):
    def test_video_post_uses_share_now_and_a_url(self):
        payload = build_post_input(
            text="caption",
            channel_id="channel-1",
            video_url="https://github.com/example/repo/releases/download/clips-2026-10-27/a.mp4",
        )
        self.assertEqual(payload["mode"], "shareNow")
        self.assertEqual(payload["schedulingType"], "automatic")
        self.assertEqual(payload["assets"], [{"video": {"url": payload["assets"][0]["video"]["url"]}}])
        self.assertNotIn("thumbnailOffset", json.dumps(payload))
        self.assertNotIn("metadata", payload)

    def test_future_due_at_is_custom_scheduled(self):
        payload = build_post_input(
            text="caption",
            channel_id="channel-1",
            video_url="https://example.com/a.mp4",
            due_at="2026-10-28T18:30:00+05:30",
        )
        self.assertEqual(payload["mode"], "customScheduled")
        self.assertEqual(payload["dueAt"], "2026-10-28T18:30:00+05:30")

    def test_missing_key_message_is_explicit(self):
        with self.assertRaises(RunAbort) as caught:
            require_api_key({})
        self.assertIn("BUFFER_API_KEY", str(caught.exception))
        self.assertIn("Nothing was published", str(caught.exception))
        self.assertEqual(str(caught.exception), MISSING_KEY)

    def test_channel_match_prefers_twitter_clipsjohns(self):
        def transport(query, extra):
            self.assertNotIn(extra["key"], query)
            if "organizations" in query:
                body = {"data": {"account": {"organizations": [{"id": "org1", "name": "Personal"}]}}}
            else:
                body = {
                    "data": {
                        "channels": [
                            {
                                "id": "ig",
                                "name": "Clipsjohns",
                                "displayName": "Clipsjohns",
                                "service": "instagram",
                                "isDisconnected": False,
                                "isLocked": False,
                            },
                            {
                                "id": "tw",
                                "name": "Clipsjohns",
                                "displayName": "Clipsjohns",
                                "service": "twitter",
                                "isDisconnected": False,
                                "isLocked": False,
                            },
                        ]
                    }
                }
            return 200, {}, json.dumps(body).encode()

        channel_id = resolve_channel_id(
            "test-key-value",
            override=None,
            services=("twitter", "x"),
            names=("Clipsjohns",),
            transport=transport,
            sleeper=lambda _seconds: None,
        )
        self.assertEqual(channel_id, "tw")

    def test_override_skips_lookup(self):
        def transport(query, extra):
            raise AssertionError("lookup should not run")

        channel_id = resolve_channel_id(
            "test-key-value",
            override="given-id",
            services=("twitter",),
            names=("Clipsjohns",),
            transport=transport,
        )
        self.assertEqual(channel_id, "given-id")

    def test_retries_rate_limit_then_succeeds(self):
        waits = []
        calls = {"n": 0}

        def transport(query, extra):
            calls["n"] += 1
            if calls["n"] == 1:
                return 429, {"retry-after": "1"}, b"{}"
            body = {
                "data": {
                    "createPost": {
                        "__typename": "PostActionSuccess",
                        "post": {"id": "post-9", "status": "buffer"},
                    }
                }
            }
            return 200, {}, json.dumps(body).encode()

        post_id = publish_video(
            api_key="test-key-value",
            channel_id="tw",
            text="caption",
            video_url="https://example.com/a.mp4",
            transport=transport,
            sleeper=waits.append,
            retries=3,
            base_delay=2,
            cap_delay=60,
        )
        self.assertEqual(post_id, "post-9")
        self.assertEqual(waits, [1])

    def test_rate_limit_gives_up_without_echoing_the_key(self):
        def transport(query, extra):
            return 429, {"retry-after": "1000"}, b"{}"

        with self.assertRaises(RunAbort) as caught:
            graphql(
                "query { account { id } }",
                {},
                "buf_secretvalue",
                transport=transport,
                sleeper=lambda _seconds: None,
                retries=2,
                cap_delay=60,
            )
        self.assertNotIn("buf_secretvalue", str(caught.exception))

    def test_redact_strips_bearer_and_env(self):
        text = redact("Authorization: Bearer buf_abc123 and buf_abc123", {"BUFFER_API_KEY": "buf_abc123"})
        self.assertNotIn("buf_abc123", text)
        self.assertIn("REDACTED", text)

    def test_ytdlp_requests_the_highest_format(self):
        command = yt_dlp_command("https://example.com/watch", "out.%(ext)s", 512 * 1024 * 1024)
        self.assertIn("bv*+ba/b", command)
        self.assertIn("res,fps,br,size", command)
        joined = " ".join(command)
        self.assertNotIn("res:1080", joined)
        self.assertNotIn("height<=", joined)


if __name__ == "__main__":
    unittest.main()
