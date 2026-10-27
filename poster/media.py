"""Download a clip and make it an X-compatible MP4 without altering the picture.

No filter in this module scales, crops, blurs, or draws text. The short side is
measured and, when it is below the configured minimum, the clip is rejected.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from poster.config import Config
from poster.errors import ItemError
from poster.redact import redact

# A browser-like User-Agent is intentionally not used. Datacenter blocks should
# surface as a failed item so the assistant can supply video_file instead.
_DOWNLOAD_UA = "clips-poster"
_HTML_TYPES = ("text/html", "application/xhtml", "text/plain")


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    duration: float
    size: int
    video_codec: str
    audio_codec: str | None
    pix_fmt: str | None
    format_name: str

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    def as_meta(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "short_side": self.short_side,
            "duration": round(self.duration, 3),
            "bytes": self.size,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "pix_fmt": self.pix_fmt,
            "format_name": self.format_name,
        }


def yt_dlp_command(url: str, output_template: str, max_bytes: int) -> list[str]:
    """Highest available format. ``res`` is sorted descending, not pinned to 1080."""
    mebibytes = max(1, max_bytes // (1024 * 1024))
    return [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--restrict-filenames",
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "-f",
        "bv*+ba/b",
        "-S",
        "res,fps,br,size",
        "--merge-output-format",
        "mp4",
        "--max-filesize",
        f"{mebibytes}M",
        "-o",
        output_template,
        url,
    ]


def probe(path: Path) -> VideoInfo:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    except FileNotFoundError as exc:
        raise ItemError("ffprobe is not installed on this runner", "quality") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "ffprobe failed").strip()
        raise ItemError(f"ffprobe could not read the file: {detail[-800:]}", "quality")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ItemError("ffprobe returned unreadable output", "quality") from exc
    streams = payload.get("streams") or []
    videos = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video" and not (stream.get("disposition") or {}).get("attached_pic")
    ]
    if not videos:
        raise ItemError("File has no video stream", "quality")
    video = videos[0]
    width, height = _display_size(video)
    if width < 1 or height < 1:
        raise ItemError("ffprobe did not report video dimensions", "quality")
    duration = _duration(payload.get("format") or {}, video)
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    audio_codec = str(audios[0].get("codec_name")) if audios else None
    fmt = payload.get("format") or {}
    return VideoInfo(
        path=path,
        width=width,
        height=height,
        duration=duration,
        size=path.stat().st_size,
        video_codec=str(video.get("codec_name") or ""),
        audio_codec=audio_codec,
        pix_fmt=str(video.get("pix_fmt")) if video.get("pix_fmt") else None,
        format_name=str(fmt.get("format_name") or ""),
    )


def check_quality(info: VideoInfo, config: Config) -> None:
    if info.short_side < config.min_short_side:
        raise ItemError(
            f"Rejected {info.width}x{info.height}: short side {info.short_side}px "
            f"is below the minimum {config.min_short_side}px. "
            "1080x1920 and 1920x1080 pass. The file is not upscaled.",
            "quality",
        )
    if info.duration < config.min_duration_seconds:
        raise ItemError(
            f"Rejected duration {info.duration:.2f}s; minimum is {config.min_duration_seconds}s",
            "quality",
        )
    if info.duration > config.max_duration_seconds and not config.trim_to_max_duration:
        raise ItemError(
            f"Rejected duration {info.duration:.2f}s; maximum is {config.max_duration_seconds}s",
            "quality",
        )
    if info.size > config.max_bytes:
        raise ItemError(
            f"Rejected size {info.size} bytes; maximum is {config.max_bytes} bytes (512 MB)",
            "quality",
        )


def prepare_file(source: Path, work: Path, config: Config) -> tuple[Path, VideoInfo, bool]:
    """Return an MP4 that is H.264/AAC/yuv420p, trimming only when longer than the max."""
    info = probe(source)
    check_quality(info, config)
    needs_trim = info.duration > config.max_duration_seconds
    if _already_compatible(info) and not needs_trim:
        return source, info, False
    dest = work / "prepared.mp4"
    command = ffmpeg_command_for(
        info,
        dest,
        trim_to=config.max_duration_seconds if needs_trim else None,
    )
    _run_ffmpeg(command)
    prepared = probe(dest)
    if prepared.short_side < config.min_short_side:
        raise ItemError(
            f"Prepared file is {prepared.width}x{prepared.height}, below {config.min_short_side}px. "
            "Refusing to upscale.",
            "quality",
        )
    if prepared.duration < config.min_duration_seconds:
        raise ItemError(
            f"Prepared duration {prepared.duration:.2f}s is below {config.min_duration_seconds}s",
            "quality",
        )
    if prepared.duration > config.max_duration_seconds + 0.75:
        raise ItemError(
            f"Prepared duration {prepared.duration:.2f}s still exceeds {config.max_duration_seconds}s",
            "quality",
        )
    if prepared.size > config.max_bytes:
        raise ItemError(
            f"Prepared file is {prepared.size} bytes, over the {config.max_bytes} byte limit",
            "quality",
        )
    return dest, prepared, needs_trim


def ffmpeg_copy_command(source: Path, dest: Path, *, trim_to: float | None) -> list[str]:
    """Remux or trim with stream copy. Does not decode the picture."""
    command = ["ffmpeg", "-y", "-i", str(source)]
    if trim_to is not None:
        command.extend(["-t", f"{trim_to:.3f}"])
    command.extend(["-c", "copy", "-movflags", "+faststart", str(dest)])
    return command


def ffmpeg_command_for(info: VideoInfo, dest: Path, *, trim_to: float | None) -> list[str]:
    """Picture-preserving ffmpeg command. Never draws, crops, scales, or overlays.

    A silent AAC track is added only when the source has no audio at all, because
    X rejects some video-only files. Existing audio is re-encoded, not replaced.
    """
    copyable = info.video_codec == "h264" and info.audio_codec == "aac" and info.pix_fmt == "yuv420p"
    if copyable:
        return ffmpeg_copy_command(info.path, dest, trim_to=trim_to)
    return _reencode_command(info.path, dest, trim_to=trim_to, add_silence=info.audio_codec is None)


def download_input(data: dict, root: Path, work: Path, config: Config) -> Path:
    video_file = str(data.get("video_file") or "").strip()
    source_url = str(data.get("source_url") or "").strip()
    if video_file:
        if video_file.startswith("https://") or video_file.startswith("http://"):
            return _download_http(video_file, work / "download.bin", config.max_bytes, config.download_timeout_seconds)
        return _copy_repo_file(root, video_file, work)
    if not source_url:
        raise ItemError("Item needs source_url or video_file", "parse")
    return _download_ytdlp(source_url, work, config)


def _download_ytdlp(url: str, work: Path, config: Config) -> Path:
    template = str(work / "media.%(ext)s")
    command = yt_dlp_command(url, template, config.max_bytes)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=config.download_timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise ItemError(
            "yt-dlp is not installed. Install it on the runner, or set video_file to a direct MP4 URL "
            "or a path already committed in the repo.",
            "download",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ItemError(
            f"yt-dlp timed out after {config.download_timeout_seconds}s. "
            + _download_hint(),
            "download",
        ) from exc
    if completed.returncode != 0:
        detail = redact((completed.stderr or completed.stdout or "yt-dlp failed").strip())
        if "max-filesize" in detail.lower() or "larger than max" in detail.lower():
            raise ItemError(
                f"Best available format exceeds {config.max_bytes} bytes and was not downloaded. "
                "The poster does not fall back to a smaller rendition.",
                "quality",
            )
        raise ItemError(f"yt-dlp failed: {detail[-1500:]}. {_download_hint()}", "download")
    produced = [
        path
        for path in work.iterdir()
        if path.is_file() and path.name.startswith("media.") and not path.name.endswith(".part")
    ]
    if not produced:
        raise ItemError(f"yt-dlp exited 0 but left no file. {_download_hint()}", "download")
    produced.sort(key=lambda path: path.stat().st_size, reverse=True)
    return produced[0]


def _download_hint() -> str:
    return (
        "If the source blocks GitHub-hosted runners, re-queue this clip with video_file set to an MP4 "
        "already committed in the repo or a direct https URL, and watermark_checked true."
    )


def _download_http(url: str, dest: Path, max_bytes: int, timeout: int) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ItemError("video_file URL must be http or https", "download")
    request = urllib.request.Request(url, headers={"User-Agent": _DOWNLOAD_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if any(content_type.startswith(kind) for kind in _HTML_TYPES):
                raise ItemError(
                    "video_file URL returned a web page, not a media file. Pass a direct file URL.",
                    "download",
                )
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise ItemError(
                    f"video_file is {length} bytes, over the {max_bytes} byte limit",
                    "quality",
                )
            written = 0
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise ItemError(
                            f"Download exceeded {max_bytes} bytes and was aborted",
                            "quality",
                        )
                    handle.write(chunk)
    except ItemError:
        dest.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise ItemError(f"Download failed with HTTP {exc.code} for the video URL", "download") from exc
    except urllib.error.URLError as exc:
        dest.unlink(missing_ok=True)
        raise ItemError(f"Download failed: {redact(str(exc.reason))}", "download") from exc
    if dest.stat().st_size < 1:
        dest.unlink(missing_ok=True)
        raise ItemError("Download was empty", "download")
    return dest


def _copy_repo_file(root: Path, relative: str, work: Path) -> Path:
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if root_resolved not in candidate.parents and candidate != root_resolved:
        raise ItemError("video_file must stay inside the repository", "download")
    if not candidate.is_file():
        raise ItemError(f"video_file not found in the repo: {relative}", "download")
    dest = work / candidate.name
    shutil.copyfile(candidate, dest)
    return dest


def _display_size(stream: dict) -> tuple[int, int]:
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    rotation = 0
    tags = stream.get("tags") or {}
    if tags.get("rotate") not in (None, ""):
        try:
            rotation = abs(int(str(tags["rotate"]))) % 360
        except ValueError:
            rotation = 0
    for side in stream.get("side_data_list") or []:
        if side.get("rotation") not in (None, ""):
            try:
                rotation = abs(int(side["rotation"])) % 360
            except (TypeError, ValueError):
                continue
    if rotation in {90, 270}:
        return height, width
    return width, height


def _duration(fmt: dict, stream: dict) -> float:
    for source in (fmt.get("duration"), stream.get("duration")):
        if source not in (None, ""):
            try:
                value = float(source)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    raise ItemError("ffprobe did not report a duration", "quality")


def _codecs_ok(info: VideoInfo) -> bool:
    return (
        info.video_codec == "h264"
        and info.audio_codec == "aac"
        and info.pix_fmt == "yuv420p"
        and "mp4" in info.format_name.split(",")
    )


def _already_compatible(info: VideoInfo) -> bool:
    return _codecs_ok(info)


def _assert_picture_preserved(command: list[str]) -> None:
    blocked_flags = {"-vf", "-af", "-filter:v", "-filter:a", "-filter_complex"}
    blocked_prefixes = ("drawtext", "overlay", "subtitles", "crop=", "scale=", "boxblur", "geq=")
    for arg in command:
        if arg in blocked_flags or arg.startswith(blocked_prefixes):
            raise ItemError(f"Refusing to run ffmpeg with {arg}", "quality")


def _reencode_command(source: Path, dest: Path, *, trim_to: float | None, add_silence: bool) -> list[str]:
    if add_silence:
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
        ]
    else:
        command = ["ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0"]
    if trim_to is not None:
        command.extend(["-t", f"{trim_to:.3f}"])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    return command


def _run_ffmpeg(command: list[str]) -> None:
    _assert_picture_preserved(command)
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=900)
    except FileNotFoundError as exc:
        raise ItemError("ffmpeg is not installed on this runner", "quality") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "ffmpeg failed").strip()
        raise ItemError(f"ffmpeg failed: {detail[-800:]}", "quality")
