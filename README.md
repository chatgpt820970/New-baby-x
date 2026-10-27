# Clips poster

This repo publishes short videos to the X account **@Clipsjohns** through a Buffer Free account. It does not call the X API.

An assistant chooses clips and writes captions, then commits one JSON file per clip into `queue/` with the GitHub contents API. Two GitHub Actions workflows do the rest:

- **prefetch** (about every 3 hours) downloads each clip, checks it, and hosts the MP4 as a public GitHub Release asset. Good clips move to `ready/`. Bad clips move to `failed/`.
- **post-slot** (every 30 minutes) publishes **one** clip when a slot is due, via Buffer's GraphQL API. If that clip fails, it is moved to `failed/` and the next one is tried, up to 5 attempts, so a bad file does not burn the slot.

Both workflows commit `ready/`, `posted/`, `failed/`, `status/`, and `logs/` back to the repo. They use the built-in `GITHUB_TOKEN` only. There is no paid host.

## Setup

1. Merge this workflow onto the default branch. Scheduled workflows do not run from a pull request.
2. In the Buffer account that has the **Clipsjohns** X channel connected, open **Settings → API**, create a personal API key with permission to post, and add it as the repository secret `BUFFER_API_KEY`.
3. Optional: set the repository variable `BUFFER_CHANNEL_ID` if you do not want the workflow to discover the channel. Discovery lists organizations and channels and picks the one whose `service` is `twitter` (or `x`) and whose `name` or `displayName` is `Clipsjohns`.
4. Confirm the repository stays **public**. Buffer has no upload endpoint; it later downloads the Release asset URL, which is only public on a public repo.
5. Confirm Actions are enabled. The slot workflow needs `permissions: contents: write` (already set) so it can upload release assets and push the queue update.

If `BUFFER_API_KEY` is missing on a real run, the job exits with a clear message and **leaves the queue untouched**. Nothing is published.

Before the key exists, run the **post-slot** workflow manually with `dry_run` set to true. That downloads, checks quality, and (when `GITHUB_TOKEN` is present) uploads the release asset, then stops before Buffer. The queue file stays where it is.

## Slots

Times are `Asia/Kolkata` (IST), editable in `config/poster.json`:

| Slot | IST |
| --- | --- |
| 1 | 03:30 |
| 2 | 07:30 |
| 3 | 18:30 |
| 4 | 21:30 |
| 5 | 23:30 |

The cron expression is UTC and only wakes the job. The script decides whether a slot is due.

- A run publishes at most one clip, for the oldest open slot.
- A slot that was missed (runner delay, failure, or a skipped cron) stays open and is caught up on a later run, as long as it is inside `catchup_hours` (default 24).
- At least `min_minutes_between_posts` (default 60) must pass between successful posts. A catch-up never squeezes two posts into the same run.
- The first slot-workflow run sets `status/state.json` `active_from` to that moment. It will still fill a slot that fell due in the previous `initial_grace_minutes` (default 75). Older slots are not backfilled, so turning the workflow on in the evening does not immediately dump the morning slots. After that, missed slots inside the catch-up window are posted.
- To skip a slot on purpose, add its id (for example `2026-10-27T18:30+05:30`) to `filled_slots` in `status/state.json`.

`post_at` does not schedule Buffer. It only holds a clip back until that timestamp. Once a slot fires and the clip is eligible, Buffer is called with `mode: shareNow`. Leave `post_at` out to let the slot clock decide.

## Queue file

One JSON object per file in `queue/`. See `examples/queue-item.json`.

| Field | Required | Meaning |
| --- | --- | --- |
| `source_url` | yes, unless `video_file` is set | Page yt-dlp can download (an X status, YouTube, and so on). Also the dedupe key. |
| `video_file` | yes, unless `source_url` is set | A path inside this repo, or a direct `https://` media URL. Used instead of yt-dlp. |
| `caption` | yes | Post text. String, may be empty. Default max 280 characters (`max_caption_chars`). |
| `watermark_checked` | yes | Must be JSON boolean `true`. Anything else is refused. |
| `post_at` | no | ISO-8601 timestamp **with a numeric offset**. The clip is ignored until this time. |
| `enqueued_at` | no | ISO-8601 with offset. Oldest first. If omitted, file mtime is used. |

`watermark_checked: true` means a person has already looked at the picture and confirmed it has **no watermark, logo, handle, or burned-in text**. This poster will not crop, blur, cover, or overlay anything to hide one, and it never adds an intro, outro, or branding. A file without the flag is moved to `failed/` with that reason.

The same `source_url` (or the same `video_file` when there is no `source_url`) is never posted twice. `posted/` is the record.

### Add a clip with the contents API

```bash
FILE=queue/2026-10-28-clip-1.json
CONTENT=$(base64 -w0 examples/queue-item.json)   # or your own JSON file

curl -sS -X PUT \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/chatgpt820970/New-baby-x/contents/${FILE}" \
  -d "{\"message\":\"Queue clip\",\"content\":\"${CONTENT}\"}"
```

The token needs `contents: write` on this repo. It is not `BUFFER_API_KEY`. Updating an existing file also requires its current `sha`.

Direct-file fallback when yt-dlp cannot reach the source (X often blocks GitHub-hosted IPs):

```json
{
  "source_url": "https://x.com/someone/status/123",
  "video_file": "https://example.com/clip.mp4",
  "caption": "Caption",
  "watermark_checked": true
}
```

`video_file` may also be a path already committed in the repo, for example `incoming/clip.mp4`. GitHub rejects files over 100 MB in git. Larger files (up to the 512 MB X limit) should be a direct HTTPS URL; the workflow uploads the result to a Release, which allows it.

## What the workflows do

Prefetch, for up to `prefetch_batch` items (default 5) each run:

1. Refuse a missing `watermark_checked: true`, a caption that is too long, or a source already in `posted/`.
2. Download. `video_file` wins over yt-dlp. yt-dlp asks for the highest format (`-S res,fps,br,size`, `bv*+ba/b`) and merges to MP4. It does not pick "closest to 1080".
3. Quality gate (see below). Re-encode to H.264/AAC/yuv420p only when the file is not already that. A silent AAC track is added only if the file has no audio at all.
4. Upload the MP4 to a rolling daily Release tagged `clips-YYYY-MM-DD` (IST date).
5. Move the JSON to `ready/` with `release_asset_url`, `video_meta`, and `prepared_at`, or to `failed/` with `error` and `error_stage`.

Slot run:

1. If no slot is due, or the last post was under 60 minutes ago, exit without publishing.
2. If the API key is missing, or Buffer/GitHub fails in a way that would hit every clip (auth, rate limit after retries, channel locked), exit and leave the current clip in place.
3. Otherwise try up to 5 clips, `ready/` first (oldest `enqueued_at`), then `queue/`. A content failure (download, quality, watermark, Buffer invalid input) moves that clip to `failed/` and the loop continues.
4. On success, move the JSON to `posted/` with `buffer_post_id`, `release_asset_url`, `posted_at`, and `slot`, append one JSON line to `logs/posts.log`, and mark the slot filled.

Transient Buffer and network errors (HTTP 429/5xx, timeouts) are retried with backoff. `Retry-After` is honored but capped at `buffer_retry_cap_seconds` (default 60) so the job cannot sleep for days. After the retries are exhausted the run stops and the clip stays queued, because the next clip would fail the same way.

Logs and `failed/` files are passed through a redactor. `BUFFER_API_KEY` and bearer tokens are not written.

## Quality gate

Configured in `config/poster.json`. Defaults:

- Short side `min(width, height)` must be **at least 1080**. Both 1920×1080 and 1080×1920 pass. 1280×720 fails. Display rotation is applied before the check. The file is never upscaled.
- Duration must be at least 0.5 s. Longer than 140 s is **trimmed to 140 s** (no other cut). Set `trim_to_max_duration` to `false` to reject long clips instead.
- Size must be at most 512 MB (536870912 bytes). If the highest format is larger, the clip is rejected. A smaller rendition is not substituted.
- Container/codec target is MP4, H.264, yuv420p, AAC, with `+faststart`.

X's limits used here are duration 0.5–140 seconds and size ≤ 512 MB.

## Health files

An external assistant can read these for a nightly report and to refill the queue:

- `status/health.json` — `ready_count`, `queue_count`, `low_queue` (true when ready clips are under 5), `last_post`, `failures_last_24h` (stage and reason), `next_open_slot`.
- `status/daily-YYYY-MM-DD.md` — that IST day's posts, failures, and slot states.

`low_queue` counts only `ready/`. Clips still sitting in `queue/` are listed separately as `queue_count` so you can tell "prefetch has not run" from "the backlog is actually empty".

The files are rewritten when the counts, failures, or slot state change. A check that changes nothing does not create a commit. `last_checked_at` is still refreshed every `health_heartbeat_hours` (default 3) so a quiet day leaves a heartbeat commit.

```bash
gh api repos/chatgpt820970/New-baby-x/contents/status/health.json --jq .content | base64 -d
```

## Buffer API

Endpoint: `POST https://api.buffer.com` with `Authorization: Bearer <BUFFER_API_KEY>` and `Content-Type: application/json`.

The publish call is the `createPost` mutation (`schedulingType: automatic`, `mode: shareNow`) and a video asset URL:

```graphql
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status } }
    ... on MutationError { message }
  }
}
```

```json
{
  "input": {
    "text": "caption",
    "channelId": "<id>",
    "schedulingType": "automatic",
    "mode": "shareNow",
    "assets": [{ "video": { "url": "https://github.com/.../releases/download/clips-YYYY-MM-DD/file.mp4" } }]
  }
}
```

Channel discovery uses `account { organizations { id } }` and `channels(input: { organizationId }) { id name displayName service isDisconnected isLocked }`. The documented service value for X is `twitter`.

Limits from the Buffer docs (Free plan, checked against [developers.buffer.com](https://developers.buffer.com/guides/api-limits.html)):

| Window | Free plan |
| --- | --- |
| API keys | 1 |
| 15 minutes | 150 requests |
| 24 hours | 600 requests |
| 30 days | 1,000 per unlocked channel, up to 3,000 |

All personal API keys **and** MCP connections share one bucket. A 429 includes `Retry-After`. These are request limits, not the channel's daily posting cap (`dailyPostingLimits` in the API). This repo makes a handful of requests per post (org, channels, createPost), well under the Free quota at five posts a day.

Media rules from [Hosting media](https://developers.buffer.com/guides/hosting-media.html): there is no upload API. The video URL must be public, HTTPS, a direct file, and still reachable when Buffer publishes. Expiring signed URLs are rejected by that guidance. `thumbnailOffset` is for Instagram, TikTok, and Pinterest only, so it is not sent for X.

GitHub's stable asset URL (`https://github.com/<owner>/<repo>/releases/download/<tag>/<file>`) redirects to a CDN. Buffer asks for a URL that is not a redirect. The redirect target expires; the `releases/download` URL does not, and that is the URL we store. If Buffer refuses to follow the redirect, the clip lands in `failed/` with Buffer's message and the slot run tries the next clip.

If Buffer stores a draft (`status: draft`) the channel role probably requires approval. The clip is failed, the draft id is recorded, and the run stops so it does not create more drafts. A posting-limit error leaves the clip in `ready/` for a later run.

## Local checks

```bash
python -m unittest discover -s tests -v
```

ffmpeg is required for the media tests. yt-dlp is not. A dry run against one local file, with no API key and no release upload:

```bash
python -m poster slot --dry-run --root /path/to/a/scratch/copy
```

The scratch copy needs `config/poster.json` (copy this repo's) and one JSON file in `queue/` or `ready/` whose `video_file` points at an MP4. The command prints the quality result and writes `status/dry-run-last.json`. It does not call Buffer.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `BUFFER_API_KEY is not set` | Add the secret. The queue was not modified. Use `dry_run` until the key exists. |
| `No Buffer channel matched` | Connect @Clipsjohns in Buffer, or set `BUFFER_CHANNEL_ID`. The key must be allowed to post. |
| `yt-dlp failed` in `failed/` | The runner IP is probably blocked. Re-queue with `video_file` set to a direct MP4 URL or a file in the repo, and `watermark_checked: true`. |
| `watermark_checked is not true` | A person must look at the video first. Do not expect the poster to crop or blur a watermark. |
| `short side ... is below ... 1080` | The best format is under 1080 on its shorter side. Do not upscale it. Pick another source. |
| `Best available format exceeds` 512 MB | The highest format is too big. The poster will not silently pick a smaller one. |
| Slot did not post | Read the Actions log. Usual causes: no slot due, 60 minute gap, `active_from` not armed yet, empty `ready/` and `queue/`, or a run-level Buffer error in `logs/posts.log` (`action` `abort`). |
| `low_queue: true` | Fewer than 5 clips in `ready/`. Add more JSON files to `queue/`. |
| Release URL works in a browser but Buffer fails | Buffer may be rejecting the GitHub redirect. The error text is on the `failed/` record. |
| Workflow never starts | Merge to the default branch and enable Actions. Schedules do not run on pull-request branches. |

## Layout

```
config/poster.json          thresholds, slots, attempt caps
queue/                      incoming JSON, one clip each
ready/                      prefetched clips with a release URL
posted/                     published clips, including the Buffer id
failed/                     rejected clips and the reason
logs/posts.log              one JSON line per ready, post, failure, dry run, or abort
status/health.json          machine-readable backlog and recent failures
status/daily-YYYY-MM-DD.md  IST day summary
status/state.json           armed time, filled slots, last post time
.github/workflows/post-slot.yml
.github/workflows/prefetch.yml
.github/workflows/test.yml
```
