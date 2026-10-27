"""Prefetch clips ahead of time, then publish one due slot per run."""

from __future__ import annotations

import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable

from poster.buffer_api import publish_video, require_api_key, resolve_channel_id
from poster.config import Config, load_config
from poster.errors import ItemError, RunAbort
from poster.health import write_health
from poster.media import download_input, prepare_file
from poster.redact import redact
from poster.releases import MISSING_TOKEN, release_tag, upload_asset
from poster.slots import choose_slot, slot_id
from poster.store import (
    State,
    append_log,
    ensure_dirs,
    identity_of,
    item_sort_key,
    iter_json,
    load_state,
    move_record,
    parse_dt,
    posted_identities,
    read_json,
    save_state,
    write_json,
)

PrepareFn = Callable[..., "Prepared"]
PublishFn = Callable[..., str]
ResolveFn = Callable[..., str]


@dataclass(frozen=True)
class Prepared:
    release_asset_url: str | None
    meta: dict
    trimmed: bool
    upload_skipped: str | None = None


def run_slot(
    root: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
    prepare: PrepareFn | None = None,
    publish: PublishFn | None = None,
    resolve_channel: ResolveFn | None = None,
) -> int:
    """Publish the oldest due slot, trying later clips when one fails.

    Returns 0 when a post was published, when no slot is due, or when the gap
    rule says to wait. Returns 1 when a slot was due and every attempt failed.
    Returns 2 when the run stopped without consuming the current item (missing
    key, rate limit, or similar).
    """
    config = load_config(root / "config" / "poster.json")
    current = _aware(now, config)
    environment = dict(os.environ if env is None else env)
    ensure_dirs(root)
    state = load_state(root)
    if state.active_from is None:
        state.active_from = current
        save_state(root, state)
        print(f"Armed the scheduler at {current.isoformat(timespec='seconds')}.")
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
    if dry_run:
        print(
            "Dry run: download, quality check, and release upload run; "
            "Buffer is not called and queue files stay put. "
            f"Slot decision: {decision.reason} {slot_id(decision.slot) if decision.slot else ''}."
        )
        return _dry_run(root, config, state, current, environment, prepare or _prepare)
    if decision.reason == "gap" and decision.slot is not None:
        print(
            f"Slot {slot_id(decision.slot)} is due, but the last post was under "
            f"{config.min_minutes_between_posts} minutes ago. Waiting so posts stay spaced out."
        )
        write_health(root, config, state, current)
        return 0
    if not decision.should_post or decision.slot is None:
        print("No slot is due.")
        write_health(root, config, state, current)
        return 0
    print(f"Filling slot {slot_id(decision.slot)}.")
    try:
        require_api_key(environment)
        channel_id = (resolve_channel or _resolve)(environment, config)
    except RunAbort as exc:
        return _abort(root, config, state, current, exc)
    seen = posted_identities(root)
    attempts = 0
    held = 0
    considered = 0
    for path in _candidates(root, ready_and_queue=True):
        if attempts >= config.max_attempts_per_run:
            print(f"Stopped after {config.max_attempts_per_run} failed attempts. The slot stays open.")
            break
        considered += 1
        try:
            data = read_json(path)
        except ItemError as exc:
            attempts += 1
            _fail(root, path, {"caption": ""}, exc, current, None)
            continue
        if _held_for_later(data, current):
            held += 1
            print(f"Skipping {path.name}; post_at is still in the future.")
            continue
        try:
            _validate(data, config)
            ident = identity_of(data)
            if ident and ident in seen:
                raise ItemError(
                    "This source_url or video_file is already in posted/. Refusing to post it twice.",
                    "duplicate",
                )
        except ItemError as exc:
            attempts += 1
            _fail(root, path, data, exc, current, None)
            continue
        attempts += 1
        work = root / "work" / path.stem
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        current_path = path
        try:
            prepared = (prepare or _prepare)(
                data,
                root=root,
                config=config,
                env=environment,
                work=work,
                now=current,
                dry_run=False,
                source_name=path.stem,
            )
            if not prepared.release_asset_url:
                raise RunAbort(prepared.upload_skipped or "No public video URL was produced.")
            current_path = _save_ready(root, current_path, data, prepared, current)
            data = read_json(current_path)
            post_id = (publish or _publish)(
                data=data,
                prepared=prepared,
                channel_id=channel_id,
                env=environment,
                config=config,
            )
            _mark_posted(
                root,
                current_path,
                data,
                prepared,
                current,
                slot=slot_id(decision.slot),
                channel_id=channel_id,
                post_id=post_id,
            )
        except ItemError as exc:
            _fail(root, current_path, data, exc, current, decision.slot)
            if exc.stop_run:
                write_health(root, config, state, current)
                print(redact(str(exc)))
                return 2
            continue
        except RunAbort as exc:
            return _abort(root, config, state, current, exc)
        else:
            state.filled_slots.add(slot_id(decision.slot))
            state.last_post_at = current
            save_state(root, state)
            append_log(
                root,
                {
                    "action": "posted",
                    "buffer_post_id": post_id,
                    "file": current_path.name,
                    "release_asset_url": prepared.release_asset_url,
                    "slot": slot_id(decision.slot),
                    "source_url": data.get("source_url"),
                    "ts": current.isoformat(timespec="seconds"),
                },
            )
            write_health(root, config, state, current)
            print(f"Posted {current_path.name} via Buffer post {post_id}.")
            return 0
        finally:
            shutil.rmtree(work, ignore_errors=True)
    write_health(root, config, state, current)
    if considered and held == considered:
        print("Every queued clip has a future post_at. Nothing was published.")
        return 0
    print("Slot stayed open: no clip was published.")
    return 1


def run_prefetch(
    root: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
    prepare: PrepareFn | None = None,
) -> int:
    """Download and quality-check queue items, moving good ones to ready/."""
    config = load_config(root / "config" / "poster.json")
    current = _aware(now, config)
    environment = dict(os.environ if env is None else env)
    ensure_dirs(root)
    state = load_state(root)
    if dry_run:
        print("Prefetch dry run: validate the oldest queue item without moving it or calling Buffer.")
        return _dry_run(root, config, state, current, environment, prepare or _prepare, queue_only=True)
    if not (environment.get("GITHUB_TOKEN") or "").strip():
        return _abort(root, config, state, current, RunAbort(MISSING_TOKEN))
    seen = posted_identities(root)
    made = 0
    failed = 0
    for path in _candidates(root, ready_and_queue=False):
        if made + failed >= config.prefetch_batch:
            break
        data = {}
        try:
            data = read_json(path)
            _validate(data, config)
            ident = identity_of(data)
            if ident and ident in seen:
                raise ItemError(
                    "This source_url or video_file is already in posted/. Removed from the queue.",
                    "duplicate",
                )
        except ItemError as exc:
            _fail(root, path, data, exc, current, None)
            failed += 1
            continue
        work = root / "work" / path.stem
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            prepared = (prepare or _prepare)(
                data,
                root=root,
                config=config,
                env=environment,
                work=work,
                now=current,
                dry_run=False,
                source_name=path.stem,
            )
            if not prepared.release_asset_url:
                raise RunAbort(prepared.upload_skipped or "No public video URL was produced.")
            ready_path = _save_ready(root, path, data, prepared, current)
        except ItemError as exc:
            _fail(root, path, data, exc, current, None)
            failed += 1
            if exc.stop_run:
                write_health(root, config, state, current)
                return 2
        except RunAbort as exc:
            return _abort(root, config, state, current, exc)
        else:
            made += 1
            append_log(
                root,
                {
                    "action": "ready",
                    "file": ready_path.name,
                    "release_asset_url": prepared.release_asset_url,
                    "source_url": data.get("source_url"),
                    "ts": current.isoformat(timespec="seconds"),
                },
            )
            print(f"Ready {ready_path.name} at {prepared.release_asset_url}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    write_health(root, config, state, current)
    print(f"Prefetch prepared {made} clip(s); {failed} failed.")
    if failed and made == 0:
        return 1
    return 0


def run_health(root: Path, *, now: datetime | None = None) -> int:
    config = load_config(root / "config" / "poster.json")
    current = _aware(now, config)
    ensure_dirs(root)
    state = load_state(root)
    write_health(root, config, state, current)
    print(f"Wrote status/health.json ({config.timezone}).")
    return 0


def _dry_run(
    root: Path,
    config: Config,
    state: State,
    now: datetime,
    env: dict[str, str],
    prepare: PrepareFn,
    *,
    queue_only: bool = False,
) -> int:
    paths = _candidates(root, ready_and_queue=not queue_only)
    report: dict = {"action": "dry_run", "ts": now.isoformat(timespec="seconds"), "buffer_called": False}
    chosen: Path | None = None
    data: dict = {}
    for path in paths:
        try:
            data = read_json(path)
        except ItemError as exc:
            report.update({"file": path.name, "ok": False, "stage": exc.stage, "error": redact(str(exc))})
            _write_dry_run(root, report)
            print(redact(str(exc)))
            return 1
        if not queue_only and _held_for_later(data, now):
            continue
        chosen = path
        break
    if chosen is None:
        report.update({"ok": False, "error": "No queue item to test."})
        _write_dry_run(root, report)
        write_health(root, config, state, now)
        print("No queue item to test.")
        return 1 if paths else 0
    work = root / "work" / chosen.stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        _validate(data, config)
        ident = identity_of(data)
        if ident and ident in posted_identities(root):
            raise ItemError("This source was already posted.", "duplicate")
        prepared = prepare(
            data,
            root=root,
            config=config,
            env=env,
            work=work,
            now=now,
            dry_run=True,
            source_name=chosen.stem,
        )
    except ItemError as exc:
        report.update(
            {
                "file": chosen.name,
                "ok": False,
                "stage": exc.stage,
                "error": redact(str(exc)),
                "source_url": data.get("source_url"),
            }
        )
        _write_dry_run(root, report)
        append_log(root, report)
        print(redact(str(exc)))
        return 1
    except RunAbort as exc:
        report.update({"file": chosen.name, "ok": False, "error": redact(str(exc))})
        _write_dry_run(root, report)
        print(redact(str(exc)))
        return 2
    else:
        report.update(
            {
                "file": chosen.name,
                "ok": True,
                "source_url": data.get("source_url"),
                "release_asset_url": prepared.release_asset_url,
                "upload_skipped": prepared.upload_skipped,
                "trimmed": prepared.trimmed,
                "video_meta": prepared.meta,
            }
        )
        _write_dry_run(root, report)
        append_log(root, report)
        write_health(root, config, state, now)
        print(f"Dry run passed for {chosen.name}. Buffer was not called.")
        if prepared.upload_skipped:
            print(prepared.upload_skipped)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _prepare(
    data: dict,
    *,
    root: Path,
    config: Config,
    env: dict[str, str],
    work: Path,
    now: datetime,
    dry_run: bool,
    source_name: str,
) -> Prepared:
    cached = _cached_release(data, config)
    if cached is not None and _url_reachable(cached):
        meta = dict(data["video_meta"])
        return Prepared(cached, meta, bool(data.get("trimmed")))
    local = download_input(data, root, work, config)
    path, info, trimmed = prepare_file(local, work, config)
    token = (env.get("GITHUB_TOKEN") or "").strip()
    repo = (env.get("GITHUB_REPOSITORY") or "").strip()
    if not token or not repo:
        if dry_run:
            return Prepared(None, info.as_meta(), trimmed, upload_skipped=MISSING_TOKEN)
        raise RunAbort(MISSING_TOKEN)
    day = now.astimezone(config.zone).date().isoformat()
    tag = release_tag(config.release_tag_prefix, day)
    name = _asset_name(source_name, now)
    hosted = upload_asset(
        path,
        repo=repo,
        token=token,
        tag=tag,
        asset_name=name,
        release_name=f"Clips {day}",
    )
    return Prepared(hosted, info.as_meta(), trimmed)


def _publish(*, data: dict, prepared: Prepared, channel_id: str, env: dict[str, str], config: Config) -> str:
    if not prepared.release_asset_url:
        raise RunAbort("Refusing to call Buffer without a public video URL.")
    return publish_video(
        api_key=require_api_key(env),
        channel_id=channel_id,
        text=str(data.get("caption") or ""),
        video_url=prepared.release_asset_url,
        retries=config.buffer_retries,
        base_delay=config.buffer_retry_base_seconds,
        cap_delay=config.buffer_retry_cap_seconds,
    )


def _resolve(env: dict[str, str], config: Config) -> str:
    return resolve_channel_id(
        require_api_key(env),
        override=env.get("BUFFER_CHANNEL_ID"),
        services=config.channel_services,
        names=config.channel_names,
        retries=config.buffer_retries,
        base_delay=config.buffer_retry_base_seconds,
        cap_delay=config.buffer_retry_cap_seconds,
    )


def _validate(data: dict, config: Config) -> None:
    if data.get("watermark_checked") is not True:
        raise ItemError(
            "Refusing to post: watermark_checked is not true. The assistant must look at the video "
            "and confirm it has no watermark, logo, handle, or burned-in text, then enqueue it with "
            '\"watermark_checked\": true. This poster never crops, blurs, or covers a watermark and '
            "never adds an intro, outro, overlay, or branding.",
            "watermark",
        )
    caption = data.get("caption")
    if not isinstance(caption, str):
        raise ItemError("caption is required and must be a string (it can be empty).", "parse")
    if len(caption) > config.max_caption_chars:
        raise ItemError(
            f"caption is {len(caption)} characters; the maximum is {config.max_caption_chars}.",
            "parse",
        )
    source = str(data.get("source_url") or "").strip()
    video = str(data.get("video_file") or "").strip()
    if not source and not video:
        raise ItemError("Item needs source_url or video_file.", "parse")
    if source and not source.startswith(("https://", "http://")):
        raise ItemError("source_url must be an http(s) URL.", "parse")
    for key in ("post_at", "enqueued_at"):
        value = data.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ItemError(f"{key} must be an ISO-8601 string with a numeric offset.", "parse")
        try:
            parse_dt(value)
        except ValueError as exc:
            raise ItemError(str(exc), "parse") from exc


def _cached_release(data: dict, config: Config) -> str | None:
    meta = data.get("video_meta")
    url = data.get("release_asset_url")
    if not isinstance(meta, dict) or not isinstance(url, str) or not url.startswith("https://"):
        return None
    try:
        width = int(meta["width"])
        height = int(meta["height"])
        duration = float(meta["duration"])
        size = int(meta["bytes"])
    except (KeyError, TypeError, ValueError):
        return None
    short = min(width, height)
    if short < config.min_short_side:
        raise ItemError(
            f"Stored video is {width}x{height}, short side {short}px, below {config.min_short_side}px.",
            "quality",
        )
    if duration < config.min_duration_seconds:
        raise ItemError(f"Stored duration {duration:.2f}s is below the minimum.", "quality")
    if duration > config.max_duration_seconds + 0.75:
        return None
    if size > config.max_bytes:
        raise ItemError(f"Stored file is {size} bytes, over the {config.max_bytes} byte limit.", "quality")
    return url


def _url_reachable(url: str) -> bool:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "clips-poster", "Range": "bytes=0-0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status in {200, 206}
    except urllib.error.HTTPError as exc:
        return exc.code in {200, 206}
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _save_ready(root: Path, src: Path, data: dict, prepared: Prepared, now: datetime) -> Path:
    record = dict(data)
    record["enqueued_at"] = record.get("enqueued_at") or _mtime_iso(src)
    record["ready"] = True
    record["release_asset_url"] = prepared.release_asset_url
    record["video_meta"] = prepared.meta
    record["trimmed"] = prepared.trimmed
    record["prepared_at"] = now.isoformat(timespec="seconds")
    ready_dir = root / "ready"
    if src.parent.resolve() == ready_dir.resolve():
        write_json(src, record)
        return src
    return move_record(src, ready_dir, record)


def _mark_posted(
    root: Path,
    src: Path,
    data: dict,
    prepared: Prepared,
    now: datetime,
    *,
    slot: str,
    channel_id: str,
    post_id: str,
) -> None:
    record = dict(data)
    record.update(
        {
            "buffer_post_id": post_id,
            "channel_id": channel_id,
            "release_asset_url": prepared.release_asset_url,
            "posted_at": now.isoformat(timespec="seconds"),
            "slot": slot,
            "trimmed": prepared.trimmed,
            "video_meta": prepared.meta,
            "ready": True,
        }
    )
    move_record(src, root / "posted", record)


def _fail(root: Path, src: Path, data: dict, exc: ItemError, now: datetime, slot: datetime | None) -> None:
    record = dict(data)
    record["error"] = redact(str(exc))
    record["error_stage"] = exc.stage
    record["failed_at"] = now.isoformat(timespec="seconds")
    if exc.buffer_post_id:
        record["buffer_post_id"] = exc.buffer_post_id
    if slot is not None:
        record["attempted_slot"] = slot_id(slot)
    dest = move_record(src, root / "failed", record)
    name = dest.name
    append_log(
        root,
        {
            "action": "failed",
            "error": redact(str(exc)),
            "file": name,
            "source_url": data.get("source_url"),
            "stage": exc.stage,
            "ts": now.isoformat(timespec="seconds"),
        },
    )
    print(f"Failed {name} at {exc.stage}: {redact(str(exc))}")


def _abort(root: Path, config: Config, state: State, now: datetime, exc: RunAbort) -> int:
    message = redact(str(exc))
    append_log(root, {"action": "abort", "error": message, "ts": now.isoformat(timespec="seconds")})
    write_health(root, config, state, now)
    print(message)
    return 2


def _candidates(root: Path, *, ready_and_queue: bool) -> list[Path]:
    folders = ("ready", "queue") if ready_and_queue else ("queue",)
    rows: list[tuple[str, int, str, Path]] = []
    for rank, folder in enumerate(folders):
        for path in iter_json(root / folder):
            try:
                data = read_json(path)
                stamp, name = item_sort_key(path, data)
            except ItemError:
                stamp, name = "9999", path.name
            rows.append((stamp, rank, name, path))
    rows.sort()
    return [row[3] for row in rows]


def _held_for_later(data: dict, now: datetime) -> bool:
    raw = data.get("post_at")
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        return parse_dt(raw) > now
    except ValueError:
        return False


def _asset_name(stem: str, now: datetime) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-") or "clip"
    stamp = now.astimezone(timezone.utc).strftime("%H%M%S")
    return f"{safe[:48]}-{stamp}.mp4"


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def _aware(now: datetime | None, config: Config) -> datetime:
    if now is None:
        return datetime.now(config.zone)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone offset")
    return now.astimezone(config.zone)


def _write_dry_run(root: Path, report: dict) -> None:
    write_json(root / "status" / "dry-run-last.json", report)
