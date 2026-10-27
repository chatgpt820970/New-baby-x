"""Host an MP4 as a public GitHub Release asset.

The URL handed to Buffer is the stable downloads URL:

    https://github.com/<owner>/<repo>/releases/download/<tag>/<file>

GitHub redirects that URL to a CDN. Buffer's docs ask for a direct, permanent
HTTPS file URL, so a redirect is a known risk and is called out in the README.
The redirect target itself expires; the downloads URL does not.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from poster.errors import ItemError, RunAbort
from poster.redact import redact

API = "https://api.github.com"
UPLOAD = "uploads.github.com"
MISSING_TOKEN = (
    "GITHUB_TOKEN is not set, so the MP4 cannot be published as a release asset. "
    "GitHub Actions provides this token when the workflow has permissions: contents: write."
)


def release_tag(prefix: str, day: str) -> str:
    return f"{prefix}-{day}"


def download_url(repo: str, tag: str, name: str) -> str:
    owner, name_repo = repo.split("/", 1)
    quoted = urllib.parse.quote(name)
    return f"https://github.com/{owner}/{name_repo}/releases/download/{urllib.parse.quote(tag)}/{quoted}"


def upload_asset(
    path: Path,
    *,
    repo: str,
    token: str,
    tag: str,
    asset_name: str,
    release_name: str,
) -> str:
    if not token.strip():
        raise RunAbort(MISSING_TOKEN)
    if "/" not in repo:
        raise RunAbort(f"GITHUB_REPOSITORY must look like owner/name, got {repo!r}")
    release = _get_or_create_release(repo, token, tag, release_name)
    existing = _asset_names(release)
    final_name = asset_name
    if final_name in existing:
        final_name = _dedupe_name(final_name, existing)
    _upload(repo, token, int(release["id"]), path, final_name)
    return download_url(repo, tag, final_name)


def _get_or_create_release(repo: str, token: str, tag: str, title: str) -> dict:
    status, payload = _json(
        "GET",
        f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}",
        token,
    )
    if status == 200 and isinstance(payload, dict):
        return payload
    if status not in {404, 200}:
        _raise_github("looking up the release", status, payload)
    status, payload = _json(
        "POST",
        f"{API}/repos/{repo}/releases",
        token,
        {
            "tag_name": tag,
            "name": title,
            "body": (
                "MP4 files hosted for Buffer. Buffer fetches these URLs when a post publishes, "
                "so do not delete the release or its assets."
            ),
            "draft": False,
            "prerelease": False,
        },
    )
    if status == 201 and isinstance(payload, dict):
        return payload
    if status == 422:
        status, payload = _json(
            "GET",
            f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}",
            token,
        )
        if status == 200 and isinstance(payload, dict):
            return payload
    _raise_github("creating the release", status, payload)
    raise RunAbort("GitHub release creation failed")


def _upload(repo: str, token: str, release_id: int, path: Path, name: str) -> None:
    query = urllib.parse.urlencode({"name": name})
    url_path = f"/repos/{repo}/releases/{release_id}/assets?{query}"
    size = path.stat().st_size
    content_type = mimetypes.guess_type(name)[0] or "video/mp4"
    connection = http.client.HTTPSConnection(UPLOAD, timeout=180)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "Content-Length": str(size),
        "Accept": "application/vnd.github+json",
        "User-Agent": "clips-poster",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with path.open("rb") as handle:
            connection.request("POST", url_path, body=handle, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
    except OSError as exc:
        raise RunAbort(f"GitHub asset upload failed: {redact(str(exc))}") from exc
    finally:
        connection.close()
    if status not in {200, 201}:
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", errors="replace")[:500]
        if status in {401, 403}:
            raise RunAbort(
                "GitHub refused the release upload (HTTP "
                f"{status}). The workflow needs permissions: contents: write. {payload}"
            )
        if status >= 500:
            raise RunAbort(f"GitHub upload returned HTTP {status}. The queue item was left in place.")
        raise ItemError(f"GitHub rejected the asset upload (HTTP {status}): {payload}", "upload")


def _json(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "clips-poster",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"message": raw[:500]}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise RunAbort(f"GitHub API unreachable: {redact(str(exc.reason))}") from exc


def _asset_names(release: dict) -> set[str]:
    names: set[str] = set()
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name"):
            names.add(str(asset["name"]))
    return names


def _dedupe_name(name: str, existing: set[str]) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix
    index = 2
    while True:
        candidate = f"{stem}-{index}{suffix}"
        if candidate not in existing:
            return candidate
        index += 1


def _raise_github(action: str, status: int, payload: object) -> None:
    if status in {401, 403}:
        raise RunAbort(
            f"GitHub refused {action} (HTTP {status}). "
            "Confirm the workflow has permissions: contents: write."
        )
    message = payload.get("message") if isinstance(payload, dict) else str(payload)
    raise RunAbort(f"GitHub failed while {action} (HTTP {status}): {redact(str(message))}")
