"""Typed HTTP client for the AragonAgent sidecar (CONTRACT §10).

This module is the **only** place in the backend that talks to the sidecar. Two
consequences, both deliberate:

  * The engine's wire format is confined here. If the sidecar's shape changes,
    one file changes; nothing else in the backend knows the engine exists.
  * Transport failure is translated once. Every method raises
    ``ApiError('engine_unavailable', …)`` with an actionable sentence naming the
    URL and the likely cause, because "the agent panel is broken" is not something
    an operator can act on — "nothing is listening on 127.0.0.1:7317, start the
    sidecar" is.

SSE parsing lives here too (``stream_events``), including resume support: the
generator yields each event's ``seq`` so the caller can persist it and reconnect
with ``last_event_id`` without duplicating or losing an event.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from app.core.config import get_settings
from app.core.errors import ApiError

log = logging.getLogger("skillrouter.agent_adapter")

ADAPTER_VERSION = "1.0.0"

DEFAULT_TIMEOUT = 30.0
STREAM_TIMEOUT = 600.0


@dataclass
class SidecarEvent:
    """One normalised event from the sidecar's SSE stream."""

    seq: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """The event body as stored in ``agent_events.payload``."""
        return self.data


class AgentAdapter:
    """Thin, typed, sync-transport-free client. One instance per process is fine."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.aragon_sidecar_url).rstrip("/")
        self.token = token if token is not None else settings.aragon_sidecar_token
        self.timeout = timeout

    # ---------------------------------------------------------------- plumbing --
    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def _unavailable(self, action: str, exc: BaseException, *, hint: str | None = None) -> ApiError:
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        message = (
            f"The AragonAgent sidecar at {self.base_url} could not be reached while trying to "
            f"{action}. {detail}"
        )
        actions = [
            "Start the sidecar: cd sidecar && npm run build && npm start",
            f"Check SR_ARAGON_SIDECAR_URL points at the running sidecar (currently {self.base_url}).",
            "Check SR_ARAGON_SIDECAR_TOKEN matches the sidecar's SR_ARAGON_SIDECAR_TOKEN.",
        ]
        if hint:
            actions.insert(0, hint)
        if not self.token:
            actions.insert(0, "SR_ARAGON_SIDECAR_TOKEN is empty — the sidecar will reject every call.")
        return ApiError(
            "engine_unavailable",
            message,
            status_code=503,
            details={"base_url": self.base_url, "reason": detail, "adapter_version": ADAPTER_VERSION},
            actions=actions,
        )

    def _upstream_error(self, action: str, response: httpx.Response) -> ApiError:
        code = "engine_unavailable"
        message = f"The sidecar rejected the request to {action} (HTTP {response.status_code})."
        details: dict[str, Any] = {"http_status": response.status_code}
        actions: list[str] = []
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            body = None
        if isinstance(body, dict):
            if isinstance(body.get("message"), str):
                message = body["message"]
            if isinstance(body.get("code"), str):
                code = body["code"]
            if isinstance(body.get("details"), (dict, list)):
                details["engine"] = body["details"]
            if isinstance(body.get("actions"), list):
                actions = [str(item) for item in body["actions"]]
        else:
            text = response.text[:400]
            if text:
                details["body"] = text
        if response.status_code == 401:
            code = "engine_unauthorised"
            actions = actions or [
                "Set the same SR_ARAGON_SIDECAR_TOKEN value on the backend and the sidecar."
            ]
        if not actions:
            actions = ["Check the sidecar log for the failing request.", "Retry the action."]
        return ApiError(code, message, status_code=502, details=details, actions=actions)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                response = await client.request(
                    method, url, headers=self._headers(), json=json_body, params=params
                )
        except (httpx.HTTPError, OSError) as exc:
            raise self._unavailable(action or f"{method} {path}", exc) from exc
        if response.status_code >= 400:
            raise self._upstream_error(action or f"{method} {path}", response)
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ApiError(
                "engine_invalid_response",
                f"The sidecar answered {action or path} with something that is not JSON.",
                status_code=502,
                details={"http_status": response.status_code, "body": response.text[:400]},
                actions=["Check the sidecar log.", "Restart the sidecar after fixing the handler."],
            ) from exc
        if not isinstance(payload, dict):
            return {"value": payload}
        return payload

    # -------------------------------------------------------------- operations --
    async def check_health(self) -> dict[str, Any]:
        """`GET /health`. The one call that does not need the token.

        A health check must not raise: it is how the UI reports "sidecar down" as a
        state rather than as a failed request.
        """
        url = f"{self.base_url}/health"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
        except (httpx.HTTPError, OSError) as exc:
            return {
                "ok": False,
                "reachable": False,
                "engine": {"adapter_version": ADAPTER_VERSION},
                "model_configured": False,
                "error": {
                    "code": "engine_unavailable",
                    "message": f"No sidecar is listening at {self.base_url}.",
                    "details": f"{type(exc).__name__}: {exc}",
                    "actions": ["Start the sidecar: cd sidecar && npm run build && npm start"],
                },
            }
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            body = {"ok": False, "error": {"code": "engine_invalid_response", "message": response.text[:200]}}
        if not isinstance(body, dict):
            body = {"ok": False}
        body.setdefault("ok", response.status_code < 400)
        body["reachable"] = True
        body["http_status"] = response.status_code
        body.setdefault("engine", {})
        if isinstance(body["engine"], dict):
            body["engine"].setdefault("adapter_version", ADAPTER_VERSION)
        return body

    async def info(self) -> dict[str, Any]:
        return await self._request("GET", "/info", action="read engine info")

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/sessions", json_body=payload, action="create the agent session")

    async def send_message(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/sessions/{session_id}/messages",
            json_body=payload,
            action="send the message to the engine",
        )

    async def stop(self, session_id: str, *, reason: str | None = None) -> dict[str, Any]:
        body = {"reason": reason} if reason else {}
        return await self._request(
            "POST",
            f"/sessions/{session_id}/stop",
            json_body=body,
            timeout=max(self.timeout, 60.0),
            action="stop the run",
        )

    async def dispose(self, session_id: str) -> dict[str, Any]:
        return await self._request(
            "DELETE", f"/sessions/{session_id}", action="dispose the agent session"
        )

    async def transcript(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/sessions/{session_id}", action="read the transcript")

    async def check_releases(self, *, channel: str | None = None) -> dict[str, Any]:
        params = {"channel": channel} if channel else None
        return await self._request(
            "GET",
            "/releases/check",
            params=params,
            timeout=max(self.timeout, 30.0),
            action="check upstream releases",
        )

    # -------------------------------------------------------------------- SSE --
    async def stream_events(
        self,
        session_id: str,
        *,
        last_event_id: int | None = None,
        stop_on: tuple[str, ...] = ("run.finished", "run.stopped", "run.failed"),
        idle_timeout: float = 120.0,
        max_events: int | None = None,
    ) -> AsyncIterator[SidecarEvent]:
        """Yield parsed SSE events from ``GET /sessions/{id}/events``.

        Resume: pass ``last_event_id`` and the sidecar replays from
        ``seq > last_event_id``, so a reconnect after a dropped connection
        continues instead of starting over.

        Termination: the stream is session-scoped and stays open across runs, so
        this generator stops itself on the first terminal event for the run
        (``run.finished``/``run.stopped``/``run.failed``). ``idle_timeout`` bounds
        a silent stretch — a dead sidecar would otherwise look like a slow model.
        """
        url = f"{self.base_url}/sessions/{session_id}/events"
        headers = {**self._headers(), "accept": "text/event-stream"}
        if last_event_id is not None:
            headers["last-event-id"] = str(last_event_id)
        params = {"since": last_event_id} if last_event_id is not None else None

        seen = 0
        timeout = httpx.Timeout(connect=10.0, read=idle_timeout, write=30.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", url, headers=headers, params=params) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise self._upstream_error(
                            "stream engine events",
                            httpx.Response(
                                response.status_code,
                                content=body,
                                headers=response.headers,
                                request=response.request,
                            ),
                        )
                    buffer: list[str] = []
                    current: dict[str, str] = {}
                    async for line in response.aiter_lines():
                        if line == "":
                            # A blank line terminates the frame: merge the buffered
                            # `data:` lines into the field map before parsing.
                            event = _parse_frame({**current, "data": "\n".join(buffer)})
                            current = {}
                            buffer = []
                            if event is not None:
                                seen += 1
                                yield event
                                if event.type in stop_on:
                                    return
                                if max_events is not None and seen >= max_events:
                                    return
                            continue
                        if line.startswith(":"):
                            continue  # keepalive comment
                        field_name, _, value = line.partition(":")
                        if value.startswith(" "):
                            value = value[1:]
                        if field_name == "data":
                            buffer.append(value)
                        else:
                            current[field_name] = value
                    # Stream ended without a blank line: flush the partial frame.
                    if buffer:
                        event = _parse_frame({**current, "data": "\n".join(buffer)})
                        if event is not None:
                            yield event
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise self._unavailable("stream engine events", exc) from exc


def _parse_frame(frame: dict[str, str]) -> SidecarEvent | None:
    raw = frame.get("data")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        log.warning("sidecar sent a non-JSON SSE data line (%s bytes)", len(raw))
        return None
    if not isinstance(payload, dict):
        return None
    seq_raw = frame.get("id") or payload.get("seq")
    try:
        seq = int(seq_raw)
    except (TypeError, ValueError):
        return None
    event_type = payload.get("type") or frame.get("event") or "run.progress"
    return SidecarEvent(seq=seq, type=str(event_type), data=payload)


def adapter() -> AgentAdapter:
    """Factory used by the API layer; keeps call sites free of settings lookups."""
    return AgentAdapter()
