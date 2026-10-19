"""BT-34 integration seams: the company research provider.

BAP-5: the research reads the client's public website - the address the user entered - with
bounded, SSRF-protected requests (``services.research.website``). It returns cited evidence
only; an LLM's general knowledge is never treated as research. No audio provider is configured.
"""

from functools import lru_cache

from app.config import settings
from services.framework.stage1_research import CompanyResearchProvider


@lru_cache(maxsize=1)
def _website_provider() -> CompanyResearchProvider:
    from services.research.website.provider import WebsiteResearchProvider

    return WebsiteResearchProvider()


def get_company_research_provider() -> CompanyResearchProvider | None:
    """The website research provider, or None when it is switched off (WEBSITE_RESEARCH_ENABLED)."""
    if not settings.WEBSITE_RESEARCH_ENABLED:
        return None
    return _website_provider()
