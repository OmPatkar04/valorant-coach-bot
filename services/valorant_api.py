"""Async client for HenrikDev's Unofficial Valorant API (v3 matches endpoint).

Features
--------
* One shared ``aiohttp.ClientSession`` (created by ``start()``, closed by ``close()``).
* A minimum gap between requests plus honouring of ``X-RateLimit-Remaining`` /
  ``X-RateLimit-Reset`` so the bot never hammers your key's rate limit.
* Retries with exponential backoff + jitter for network errors, timeouts, 5xx
  and 429 (respecting ``Retry-After`` / ``X-RateLimit-Reset``).
* Typed exceptions so callers can react differently to a bad key, an unknown
  Riot ID and a transient outage.

Endpoint used::

    GET https://api.henrikdev.xyz/valorant/v3/matches/{region}/{name}/{tag}
        ?mode=<mode>&size=<n>
    Header: Authorization: <your key>
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

BASE_URL = "https://api.henrikdev.xyz"
MAX_RATE_LIMIT_WAIT = 120.0  # never sleep longer than this inside one request


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class HenrikAPIError(Exception):
    """Base class for all API failures."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(HenrikAPIError):
    """The API key was rejected (HTTP 401/403). Retrying will not help."""


class NotFoundError(HenrikAPIError):
    """Riot ID / region not found (HTTP 404)."""


class RateLimitError(HenrikAPIError):
    """Rate limit still exceeded after retries (HTTP 429)."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class ValorantAPI:
    """Thin async wrapper around the HenrikDev API."""

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = 3,
        min_request_interval: float = 2.0,
        timeout_seconds: float = 20.0,
        base_url: str = BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._min_interval = min_request_interval
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

        self._throttle_lock = asyncio.Lock()
        self._next_allowed_at = 0.0  # loop-time before which no request may start

    # -- lifecycle ----------------------------------------------------------- #
    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Authorization": self._api_key,
                    "Accept": "application/json",
                    "User-Agent": "valorant-coach-bot/1.0",
                },
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "ValorantAPI":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # -- public API ---------------------------------------------------------- #
    async def get_recent_matches(
        self,
        region: str,
        name: str,
        tag: str,
        *,
        mode: str = "competitive",
        size: int = 5,
    ) -> list[dict[str, Any]]:
        """Return recent matches, **newest first**, containing only usable entries.

        Entries are dropped when they are not dicts, are flagged
        ``is_available: false``, lack metadata, or (as a safety net in case the
        API ignores the ``mode`` filter) belong to a different game mode.
        """
        path = f"/valorant/v3/matches/{quote(region, safe='')}/{quote(name, safe='')}/{quote(tag, safe='')}"
        payload = await self._request(path, params={"mode": mode, "size": str(size)})

        data = payload.get("data")
        if not isinstance(data, list):
            raise HenrikAPIError("Unexpected API response: 'data' is not a list.")

        wanted_mode = mode.strip().lower()
        usable: list[dict[str, Any]] = []
        for match in data:
            if not isinstance(match, dict) or match.get("is_available") is False:
                continue
            metadata = match.get("metadata")
            if not isinstance(metadata, dict) or not metadata.get("matchid"):
                continue
            match_mode = str(metadata.get("mode_id") or metadata.get("mode") or "").strip().lower()
            if match_mode and match_mode != wanted_mode:
                continue
            usable.append(match)

        usable.sort(key=lambda m: m["metadata"].get("game_start") or 0, reverse=True)
        return usable

    # -- internals ----------------------------------------------------------- #
    async def _throttle(self) -> None:
        """Space requests out; also waits out any server-requested cool-down."""
        loop = asyncio.get_running_loop()
        async with self._throttle_lock:
            delay = self._next_allowed_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed_at = loop.time() + self._min_interval

    def _note_rate_limit_headers(self, headers: Any) -> None:
        """If the window is exhausted, block further requests until it resets."""
        remaining = _to_float(headers.get("X-RateLimit-Remaining"))
        reset = _to_float(headers.get("X-RateLimit-Reset"))
        if remaining is not None and remaining <= 0 and reset is not None:
            wait = min(reset, MAX_RATE_LIMIT_WAIT)
            loop = asyncio.get_running_loop()
            self._next_allowed_at = max(self._next_allowed_at, loop.time() + wait)
            log.info("Rate-limit window exhausted; pausing requests for %.0fs.", wait)

    async def _request(self, path: str, params: Optional[dict[str, str]] = None) -> dict[str, Any]:
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None

        url = f"{self._base_url}{path}"
        last_error: Optional[HenrikAPIError] = None

        for attempt in range(self._max_retries + 1):
            await self._throttle()
            try:
                async with self._session.get(url, params=params) as resp:
                    self._note_rate_limit_headers(resp.headers)
                    body = await _read_json(resp)

                    if resp.status == 200:
                        if not isinstance(body, dict):
                            raise HenrikAPIError("Unexpected API response: body is not a JSON object.", 200)
                        return body

                    message = _extract_error_message(body) or resp.reason or "Unknown error"

                    if resp.status in (401, 403):
                        raise AuthError(f"API key rejected (HTTP {resp.status}): {message}", resp.status)
                    if resp.status == 404:
                        raise NotFoundError(f"Not found (HTTP 404): {message}", 404)
                    if resp.status == 429:
                        wait = _retry_delay(resp.headers, attempt)
                        last_error = RateLimitError(f"Rate limited (HTTP 429): {message}", retry_after=wait)
                        log.warning("Rate limited by API; waiting %.0fs (attempt %d).", wait, attempt + 1)
                        if attempt < self._max_retries:
                            await asyncio.sleep(wait)
                            continue
                        raise last_error
                    if resp.status in (408, 500, 502, 503, 504):
                        last_error = HenrikAPIError(f"Server error (HTTP {resp.status}): {message}", resp.status)
                        # fall through to backoff below
                    else:
                        # 400, 422, other 4xx: our request is wrong; retrying is pointless.
                        raise HenrikAPIError(f"API error (HTTP {resp.status}): {message}", resp.status)

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = HenrikAPIError(f"Network error talking to HenrikDev: {type(exc).__name__}: {exc}")

            if attempt < self._max_retries:
                backoff = min(2 ** attempt * 2 + random.uniform(0, 1), 30.0)
                log.warning("%s - retrying in %.1fs (attempt %d/%d).", last_error, backoff, attempt + 1, self._max_retries)
                await asyncio.sleep(backoff)

        assert last_error is not None
        raise last_error


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _read_json(resp: aiohttp.ClientResponse) -> Any:
    """Parse the JSON body, returning None if the body is empty or not JSON."""
    try:
        return await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return None


def _extract_error_message(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        return None
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        message = errors[0].get("message")
        if message:
            return str(message)
    message = body.get("message") or body.get("error")
    return str(message) if message else None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _retry_delay(headers: Any, attempt: int) -> float:
    """How long to wait after a 429: server hint first, else exponential backoff."""
    hinted = _to_float(headers.get("Retry-After"))
    if hinted is None:
        hinted = _to_float(headers.get("X-RateLimit-Reset"))
    if hinted is None:
        hinted = 5.0 * (2 ** attempt)
    return max(1.0, min(hinted, MAX_RATE_LIMIT_WAIT))
