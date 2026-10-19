"""The website research as a company research provider (``stage1_research.CompanyResearchProvider``).

``research_website`` gives the full result to the Discovery analysis; ``research`` gives the
Stage 1 research the cited evidence its contract knows (company description, address). A result
is kept for a few minutes per address, so one generation flow does not read a site twice.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from services.framework.stage1_research import CompanyEvidence
from services.research.website.fetch import Fetcher, Limits
from services.research.website.research import WebsiteResearch, research_website
from services.research.website.safety import WebsiteRejected, normalize_url


class WebsiteResearchProvider:
    def __init__(
        self,
        *,
        limits: Limits | None = None,
        fetcher_factory: Callable[[], Fetcher] | None = None,
        cache_seconds: float = 600.0,
        cache_size: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits or Limits()
        self._fetcher_factory = fetcher_factory or (lambda: Fetcher(limits=self._limits))
        self._cache_seconds = cache_seconds
        self._cache_size = cache_size
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, WebsiteResearch]] = OrderedDict()
        self._lock = threading.Lock()

    def research_website(self, url: str | None, *, client_name: str = "") -> WebsiteResearch:
        try:
            key = normalize_url(url or "")
        except WebsiteRejected:
            return research_website(url, client_name=client_name)  # reports why, without fetching
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self._cache_seconds:
                self._cache.move_to_end(key)
                return cached[1]
        with self._fetcher_factory() as fetcher:
            result = research_website(key, client_name=client_name, fetcher=fetcher)
        # A failure is not cached: the site may be back on the next attempt.
        if result.status in ("complete", "partial"):
            with self._lock:
                self._cache[key] = (now, result)
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        return result

    def research(self, *, client_name: str, client_web_page: str | None) -> list[CompanyEvidence]:
        if not client_web_page:
            return []
        result = self.research_website(client_web_page, client_name=client_name)
        fields = {"description": "description", "headquarters": "headquarters"}
        evidence = []
        for fact in result.facts:
            field = fields.get(fact["kind"])
            if field and not any(item.field == field for item in evidence):
                evidence.append(
                    CompanyEvidence(
                        field=field,
                        value=fact["value"],
                        source_id=fact["source_url"],
                        locator=fact["locator"],
                        excerpt=fact["excerpt"],
                    )
                )
        return evidence
