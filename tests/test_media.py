import shutil
import subprocess
import unittest
from pathlib import Path

from poster.config import load_config
from poster.errors import ItemError
from poster.media import VideoInfo, check_quality, ffmpeg_command_for, prepare_file, probe

ROOT = Path(__file__).resolve().parents[1]


def info(**kwargs) -> VideoInfo:
    base = dict(
        path=Path("clip.mp4"),
        width=1920,
        height=1080,
        duration=12,
        size=1000,
        video_codec="h264",
        audio_codec="aac",
        pix_fmt="yuv420p",
        format_name="mov,mp4",
    )
    base.update(kwargs)
    return VideoInfo(**base)


class QualityRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "config" / "poster.json")

    def test_real_config_is_1080_and_ist_slots(self):
        self.assertEqual(self.config.min_short_side, 1080)
        self.assertEqual(self.config.timezone, "Asia/Kolkata")
        self.assertEqual(self.config.slots, ("03:30", "07:30", "18:30", "21:30", "23:30"))
        self.assertEqual(self.config.max_duration_seconds, 140)
        self.assertTrue(self.config.trim_to_max_duration)

    def test_landscape_and_vertical_1080_pass_and_720_fails(self):
        check_quality(info(width=1920, height=1080), self.config)
        check_quality(info(width=1080, height=1920), self.config)
        with self.assertRaises(ItemError) as caught:
            check_quality(info(width=1920, height=720), self.config)
        self.assertEqual(caught.exception.stage, "quality")
        self.assertIn("720", str(caught.exception))

    def test_over_long_is_allowed_when_trim_is_on_but_oversize_is_not(self):
        check_quality(info(duration=180), self.config)
        with self.assertRaises(ItemError):
            check_quality(info(size=self.config.max_bytes + 1), self.config)
        with self.assertRaises(ItemError):
            check_quality(info(duration=0.2), self.config)

    def test_ffmpeg_plan_never_filters_the_picture(self):
        compatible = ffmpeg_command_for(info(), Path("out.mp4"), trim_to=None)
        self.assertIn("copy", compatible)
        self.assertNotIn("anullsrc=channel_layout=stereo:sample_rate=44100", compatible)
        silent = ffmpeg_command_for(info(audio_codec=None), Path("out.mp4"), trim_to=10)
        joined = " ".join(silent)
        self.assertIn("anullsrc", joined)
        self.assertIn("0:v:0", silent)
        self.assertNotIn("-vf", silent)
        for token in ("drawtext", "overlay", "subtitles", "crop=", "scale=", "boxblur"):
            self.assertNotIn(token, joined)
        reencoded = ffmpeg_command_for(info(video_codec="vp9", audio_codec="opus"), Path("out.mp4"), trim_to=None)
        self.assertIn("libx264", reencoded)
        self.assertIn("0:a:0", reencoded)
        self.assertNotIn("anullsrc", reencoded)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class FfmpegPipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config" / "poster.json")

    def test_probe_accepts_1080_and_rejects_720(self):
        high = self._render(1920, 1080, "high.mp4")
        tall = self._render(1080, 1920, "tall.mp4")
        low = self._render(1280, 720, "low.mp4")
        high_path, high_info, trimmed = prepare_file(high, self._work("high"), self.config)
        self.assertFalse(trimmed)
        self.assertEqual(high_info.short_side, 1080)
        self.assertEqual(high_path, high)
        tall_info = probe(tall)
        self.assertGreaterEqual(tall_info.short_side, 1080)
        check_quality(tall_info, self.config)
        with self.assertRaises(ItemError):
            prepare_file(low, self._work("low"), self.config)

    def test_reencode_keeps_resolution_and_adds_audio_only_when_missing(self):
        silent = self._render(1920, 1080, "silent.mp4", audio=False)
        prepared, meta, _trimmed = prepare_file(silent, self._work("silent"), self.config)
        self.assertEqual((meta.width, meta.height), (1920, 1080))
        self.assertEqual(meta.video_codec, "h264")
        self.assertEqual(meta.audio_codec, "aac")
        self.assertNotEqual(prepared, silent)

    def test_trim_cuts_to_the_configured_maximum(self):
        source = self._render(1920, 1080, "long.mp4", duration=3)
        config_path = self._work("trim") / "poster.json"
        config_path.write_text(
            '{"max_duration_seconds": 1.0, "min_duration_seconds": 0.4, "min_short_side": 1080}\n',
            encoding="utf-8",
        )
        config = load_config(config_path)
        _path, meta, trimmed = prepare_file(source, config_path.parent, config)
        self.assertTrue(trimmed)
        self.assertLessEqual(meta.duration, 1.75)
        self.assertGreaterEqual(meta.duration, 0.4)
        self.assertEqual(meta.short_side, 1080)

    def _work(self, name: str) -> Path:
        folder = Path("/tmp/clips-poster-tests") / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _render(self, width: int, height: int, name: str, duration: int = 1, audio: bool = True) -> Path:
        folder = Path("/tmp/clips-poster-tests")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{width}x{height}-{duration}-{int(audio)}-{name}"
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        command = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30:duration={duration}"]
        if audio:
            command.extend(["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-shortest", "-c:a", "aac"])
        command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)])
        subprocess.run(command, check=True, capture_output=True)
        return dest


if __name__ == "__main__":
    unittest.main()
