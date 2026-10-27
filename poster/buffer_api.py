"""Buffer GraphQL client for the public API at https://api.buffer.com.

Auth is ``Authorization: Bearer <personal API key>``. Video is passed as a
public URL. There is no upload endpoint. See developers.buffer.com.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from poster.errors import ItemError, RunAbort, TransientError
from poster.redact import redact

API_URL = "https://api.buffer.com"

ORG_QUERY = """
query GetOrganizations {
  account {
    organizations { id name }
  }
}
"""

CHANNEL_QUERY = """
query GetChannels($organizationId: OrganizationId!) {
  channels(input: { organizationId: $organizationId }) {
    id
    name
    displayName
    service
    isDisconnected
    isLocked
  }
}
"""

CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess {
      post { id status }
    }
    ... on MutationError {
      message
    }
  }
}
"""

MISSING_KEY = (
    "BUFFER_API_KEY is not set. Add a personal API key from Buffer Settings → API "
    "as the repository secret BUFFER_API_KEY (Settings → Secrets and variables → Actions). "
    "Nothing was published and the queue was left unchanged."
)

# These __typename values mean every remaining item would fail the same way.
_STOP_TYPES = {
    "LimitReachedError",
    "UnauthorizedError",
    "UnexpectedError",
    "NotFoundError",
}
_ITEM_TYPES = {"InvalidInputError", "RestProxyError"}

Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]
Sleeper = Callable[[float], None]


def require_api_key(env: dict[str, str]) -> str:
    key = (env.get("BUFFER_API_KEY") or "").strip()
    if not key:
        raise RunAbort(MISSING_KEY)
    return key


def build_post_input(*, text: str, channel_id: str, video_url: str, due_at: str | None = None) -> dict[str, Any]:
    """shareNow publishes immediately. A future due_at uses customScheduled."""
    payload: dict[str, Any] = {
        "text": text,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": "shareNow",
        "assets": [{"video": {"url": video_url}}],
    }
    if due_at:
        payload["mode"] = "customScheduled"
        payload["dueAt"] = due_at
    return payload


def resolve_channel_id(
    api_key: str,
    *,
    override: str | None,
    services: tuple[str, ...],
    names: tuple[str, ...],
    transport: Transport | None = None,
    sleeper: Sleeper | None = None,
    retries: int = 3,
    base_delay: float = 2,
    cap_delay: float = 60,
) -> str:
    chosen = (override or "").strip()
    if chosen:
        return chosen
    organizations = graphql(
        ORG_QUERY,
        {},
        api_key,
        transport=transport,
        sleeper=sleeper,
        retries=retries,
        base_delay=base_delay,
        cap_delay=cap_delay,
    )
    orgs = ((organizations.get("account") or {}).get("organizations")) or []
    if not orgs:
        raise RunAbort("Buffer returned no organizations for this API key.")
    wanted_services = {item.lower() for item in services}
    wanted_names = {item.lower().lstrip("@") for item in names}
    matches: list[dict[str, Any]] = []
    for org in orgs:
        payload = graphql(
            CHANNEL_QUERY,
            {"organizationId": org["id"]},
            api_key,
            transport=transport,
            sleeper=sleeper,
            retries=retries,
            base_delay=base_delay,
            cap_delay=cap_delay,
        )
        for channel in payload.get("channels") or []:
            service = str(channel.get("service") or "").lower()
            if service not in wanted_services:
                continue
            name = str(channel.get("name") or "").lower().lstrip("@")
            display = str(channel.get("displayName") or "").lower().lstrip("@")
            if name in wanted_names or display in wanted_names:
                matches.append(channel)
    if not matches:
        raise RunAbort(
            "No Buffer channel matched service "
            f"{', '.join(sorted(wanted_services))} and name {', '.join(sorted(wanted_names))}. "
            "Connect @Clipsjohns in Buffer, or set the repository variable BUFFER_CHANNEL_ID."
        )
    if len(matches) > 1:
        listed = ", ".join(f"{item.get('name')} ({item.get('id')})" for item in matches)
        raise RunAbort(f"More than one Buffer channel matched Clipsjohns: {listed}. Set BUFFER_CHANNEL_ID.")
    channel = matches[0]
    if channel.get("isDisconnected"):
        raise RunAbort(f"Buffer channel {channel.get('name')} is disconnected. Reconnect it in Buffer.")
    if channel.get("isLocked"):
        raise RunAbort(
            f"Buffer channel {channel.get('name')} is locked. The Free plan cannot publish to a locked channel."
        )
    return str(channel["id"])


def publish_video(
    *,
    api_key: str,
    channel_id: str,
    text: str,
    video_url: str,
    transport: Transport | None = None,
    sleeper: Sleeper | None = None,
    retries: int = 3,
    base_delay: float = 2,
    cap_delay: float = 60,
) -> str:
    variables = {"input": build_post_input(text=text, channel_id=channel_id, video_url=video_url)}
    payload = graphql(
        CREATE_POST,
        variables,
        api_key,
        transport=transport,
        sleeper=sleeper,
        retries=retries,
        base_delay=base_delay,
        cap_delay=cap_delay,
    )
    return _interpret_create(payload)


def graphql(
    query: str,
    variables: dict[str, Any],
    api_key: str,
    *,
    transport: Transport | None = None,
    sleeper: Sleeper | None = None,
    retries: int = 3,
    base_delay: float = 2,
    cap_delay: float = 60,
) -> dict[str, Any]:
    send = transport or _urllib_transport
    pause = sleeper or time.sleep
    delay = base_delay
    last = "Buffer request failed"
    for attempt in range(1, retries + 1):
        try:
            status, headers, body = send(query, {"variables": json.dumps(variables), "key": api_key})
        except TransientError as exc:
            last = str(exc)
            if attempt == retries:
                raise RunAbort(
                    f"Buffer or the network stayed unreachable after {retries} tries: {redact(last)}"
                ) from exc
            pause(_wait(exc.retry_after, delay, cap_delay))
            delay = min(cap_delay, delay * 2)
            continue
        try:
            return _interpret_http(status, headers, body)
        except TransientError as exc:
            last = str(exc)
            if attempt == retries:
                raise RunAbort(
                    f"Buffer rate limit or server error persisted after {retries} tries: {redact(last)}"
                ) from exc
            pause(_wait(exc.retry_after, delay, cap_delay))
            delay = min(cap_delay, delay * 2)
    raise RunAbort(redact(last))


def _interpret_http(status: int, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    if status in {429, 500, 502, 503, 504}:
        retry_after = _retry_after(headers)
        raise TransientError(f"HTTP {status}", retry_after=retry_after)
    if status in {401, 403}:
        raise RunAbort(
            "Buffer rejected the API key (HTTP "
            f"{status}). Check BUFFER_API_KEY and that the key has posting permission. "
            "The queue was left unchanged."
        )
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        raise RunAbort(f"Buffer returned non-JSON (HTTP {status})") from exc
    if status >= 400:
        raise RunAbort(f"Buffer HTTP {status}: {_error_summary(parsed)}")
    errors = parsed.get("errors") or []
    if errors:
        code = str(((errors[0].get("extensions") or {}).get("code")) or "")
        message = "; ".join(str(item.get("message") or "") for item in errors)
        if code == "RATE_LIMIT_EXCEEDED":
            raise TransientError(message or "RATE_LIMIT_EXCEEDED")
        if "GRAPHQL_VALIDATION" in code or "Cannot query field" in message:
            raise RunAbort(f"Buffer rejected the query: {message}")
        raise RunAbort(f"Buffer error: {message or code}")
    data = parsed.get("data")
    if not isinstance(data, dict):
        raise RunAbort("Buffer returned an empty data payload")
    return data


def _interpret_create(data: dict[str, Any]) -> str:
    result = data.get("createPost")
    if not isinstance(result, dict):
        raise RunAbort("Buffer createPost returned no payload")
    typename = str(result.get("__typename") or "")
    message = str(result.get("message") or "").strip()
    post = result.get("post") if isinstance(result.get("post"), dict) else None
    if typename == "PostActionSuccess" and post and post.get("id"):
        status = str(post.get("status") or "")
        if status == "draft":
            raise ItemError(
                "Buffer stored the post as a draft instead of publishing it. The channel likely "
                "requires approval. Change that role in Buffer before trying again. "
                f"Draft id: {post['id']}.",
                "buffer",
                stop_run=True,
                buffer_post_id=str(post["id"]),
            )
        return str(post["id"])
    if typename in _STOP_TYPES:
        raise RunAbort(
            f"Buffer {typename}: {message or 'request refused'}. The queue item was left in place."
        )
    if typename in _ITEM_TYPES or message:
        raise ItemError(message or f"Buffer refused the post ({typename or 'unknown'})", "buffer")
    raise RunAbort("Buffer createPost returned neither a post id nor an error message")


def _urllib_transport(query: str, extra: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    api_key = extra["key"]
    variables = json.loads(extra["variables"])
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "clips-poster",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, headers, response.read()
    except urllib.error.HTTPError as exc:
        headers = {key.lower(): value for key, value in exc.headers.items()} if exc.headers else {}
        return exc.code, headers, exc.read()
    except urllib.error.URLError as exc:
        raise TransientError(f"network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise TransientError("network timeout") from exc


def _retry_after(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _wait(retry_after: float | None, delay: float, cap: float) -> float:
    chosen = delay if retry_after is None else retry_after
    return max(0.0, min(cap, chosen))


def _error_summary(parsed: dict[str, Any]) -> str:
    errors = parsed.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item.get("message") or "") for item in errors if isinstance(item, dict))
    return "request failed"
