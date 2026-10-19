"""Bounded fetching of public web pages.

Every request is checked by ``safety`` and connects to the address that was checked (the host
name is sent as Host header and TLS server name, so certificates are still verified for the
domain and a second DNS answer cannot redirect the connection - no DNS rebinding). Redirects
are followed by hand, each hop checked again. Responses are streamed and cut at a size limit,
and every request is bounded by the run's remaining time. Access restrictions (401/403/429,
robots.txt) are respected and reported, never worked around.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from services.research.website.safety import ALLOWED_PORTS, Resolver, WebsiteRejected, normalize_url, resolve_public

USER_AGENT = "BorekPitchFactory-WebsiteResearch/1.0 (+https://boreksolutions.de; sales preparation research)"
ROBOTS_AGENT = "BorekPitchFactory-WebsiteResearch"
HTML_TYPES = ("text/html", "application/xhtml+xml")


@dataclass(frozen=True)
class Limits:
    max_pages: int = 7  # the homepage and at most six internal pages
    max_redirects: int = 4
    connect_seconds: float = 5.0
    read_seconds: float = 8.0
    total_seconds: float = 25.0
    max_bytes: int = 1_500_000  # per response; the rest is not downloaded
    max_robots_bytes: int = 200_000
    max_chars_per_page: int = 6_000
    max_total_chars: int = 18_000  # content kept for the analysis across all pages


@dataclass
class FetchResult:
    requested_url: str
    url: str
    status: int | None = None
    html: str | None = None
    truncated: bool = False
    error: str | None = None
    message: str = ""
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.html is not None


_STATUS_ERRORS = {
    401: ("WEBSITE_ACCESS_RESTRICTED", "The page requires a login; it was not analysed."),
    403: ("WEBSITE_ACCESS_RESTRICTED", "The website refused automated access (HTTP 403); this is respected."),
    404: ("WEBSITE_PAGE_NOT_FOUND", "The page does not exist (HTTP 404)."),
    410: ("WEBSITE_PAGE_NOT_FOUND", "The page no longer exists (HTTP 410)."),
    429: ("WEBSITE_RATE_LIMITED", "The website asked to slow down (HTTP 429); it was not retried."),
}


class Fetcher:
    """One research run: a shared deadline, one HTTP client, robots.txt rules per site."""

    def __init__(
        self,
        *,
        limits: Limits | None = None,
        resolver: Resolver = socket.getaddrinfo,
        transport: httpx.BaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.limits = limits or Limits()
        self._resolver = resolver
        self._clock = clock
        self._deadline = clock() + self.limits.total_seconds
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            trust_env=False,  # no proxies or .netrc from the environment: connections stay pinned
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en, de;q=0.8, *;q=0.5"},
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_errors: dict[str, FetchResult] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def remaining(self) -> float:
        return self._deadline - self._clock()

    # ------------------------------------------------------------------ robots.txt

    def allowed(self, url: str) -> bool:
        """robots.txt of the URL's site permits this agent to fetch it (RFC 9309 semantics)."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        rules = self._robots[origin]
        return True if rules is None else rules.can_fetch(ROBOTS_AGENT, url)

    def robots_failure(self, url: str) -> FetchResult | None:
        """Why the site's robots.txt could not be read, when that is why reading is not allowed."""
        parts = urlsplit(url)
        return self._robots_errors.get(f"{parts.scheme}://{parts.netloc}")

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        result = self.get(origin + "/robots.txt", accept=("text/plain",), max_bytes=self.limits.max_robots_bytes)
        parser = RobotFileParser()
        if result.ok:
            parser.parse((result.html or "").splitlines())
            return parser
        if (result.status is not None and 400 <= result.status < 500) or result.error == "WEBSITE_UNSUPPORTED_CONTENT":
            return None  # no robots.txt: everything is allowed
        # robots.txt unreachable (server error, timeout): assume the site disallows crawling.
        self._robots_errors[origin] = result
        parser.parse(["User-agent: *", "Disallow: /"])
        return parser

    # ------------------------------------------------------------------ pages

    def get(self, url: str, *, accept: tuple[str, ...] = HTML_TYPES, max_bytes: int | None = None) -> FetchResult:
        limit = max_bytes or self.limits.max_bytes
        try:
            current = normalize_url(url)
        except WebsiteRejected as exc:
            return FetchResult(url, url, error=exc.code, message=str(exc))
        result = FetchResult(url, current)
        for _hop in range(self.limits.max_redirects + 1):
            remaining = self.remaining()
            if remaining <= 0.5:
                result.error, result.message = "WEBSITE_TIME_BUDGET_EXCEEDED", "The time budget for the website analysis was used up."
                return result
            parts = urlsplit(current)
            host = parts.hostname or ""
            port = parts.port or ALLOWED_PORTS[parts.scheme]
            try:
                address = resolve_public(host, port, self._resolver)[0]
            except WebsiteRejected as exc:
                result.error, result.message = exc.code, str(exc)
                return result
            pinned_host = f"[{address}]" if ":" in address else address
            target = urlunsplit((parts.scheme, pinned_host, parts.path or "/", parts.query, ""))
            timeout = httpx.Timeout(
                connect=min(self.limits.connect_seconds, remaining),
                read=min(self.limits.read_seconds, remaining),
                write=min(5.0, remaining),
                pool=min(5.0, remaining),
            )
            extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
            try:
                with self._client.stream(
                    "GET",
                    target,
                    headers={"Host": host, "Accept": ", ".join(accept) + ";q=1.0, */*;q=0.1"},
                    extensions=extensions,
                    timeout=timeout,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            result.error, result.message = "WEBSITE_FETCH_FAILED", "The website sent a redirect without a target."
                            return result
                        try:
                            current = normalize_url(urljoin(current, location))
                        except WebsiteRejected as exc:
                            result.error, result.message = "WEBSITE_REDIRECT_BLOCKED", f"The website redirected to an address that is not analysed: {exc}"
                            return result
                        result.redirects.append(current)
                        result.url = current
                        continue
                    return self._read(response, result, accept, limit)
            except httpx.TimeoutException:
                result.error, result.message = "WEBSITE_TIMEOUT", "The website did not answer in time."
                return result
            except httpx.ConnectError as exc:
                text = str(exc).lower()
                if "certificate" in text or "ssl" in text or "tls" in text:
                    result.error, result.message = "WEBSITE_TLS_ERROR", "The website's TLS certificate could not be verified."
                else:
                    result.error, result.message = "WEBSITE_UNREACHABLE", "The website could not be reached."
                return result
            except httpx.HTTPError:
                result.error, result.message = "WEBSITE_FETCH_FAILED", "The website could not be read."
                return result
        result.error, result.message = "WEBSITE_TOO_MANY_REDIRECTS", "The website redirected too many times."
        return result

    def _read(self, response: httpx.Response, result: FetchResult, accept: tuple[str, ...], limit: int) -> FetchResult:
        result.status = response.status_code
        if response.status_code in _STATUS_ERRORS:
            result.error, result.message = _STATUS_ERRORS[response.status_code]
            return result
        if response.status_code >= 400:
            result.error, result.message = "WEBSITE_HTTP_ERROR", f"The website answered with HTTP {response.status_code}."
            return result
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in accept:
            result.error, result.message = "WEBSITE_UNSUPPORTED_CONTENT", f"The page is not a web page ({content_type})."
            return result
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit * 4:
            result.error, result.message = "WEBSITE_TOO_LARGE", "The page is too large to analyse."
            return result
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) >= limit:
                del body[limit:]
                result.truncated = True
                break
            if self.remaining() <= 0:
                result.truncated = True
                break
        encoding = response.charset_encoding or _meta_charset(bytes(body[:4096])) or "utf-8"
        try:
            result.html = bytes(body).decode(encoding, errors="replace")
        except LookupError:
            result.html = bytes(body).decode("utf-8", errors="replace")
        return result


def _meta_charset(head: bytes) -> str | None:
    text = head.decode("ascii", errors="ignore").lower()
    marker = text.find("charset=")
    if marker < 0:
        return None
    value = text[marker + 8 :].lstrip("\"' ")
    name = "".join(char for char in value[:40] if char.isalnum() or char in "-_")
    return name or None
