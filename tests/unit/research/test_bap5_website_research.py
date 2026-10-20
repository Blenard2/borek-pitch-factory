"""BAP-5: website research - address safety, bounded fetching, extraction, ranking, provider.

No test touches the network: sites are served by ``tests.fixtures.website_fixtures``.
"""

from __future__ import annotations

import itertools

import pytest

from services.framework.stage1_research import CompanyEvidence, _company_facts
from services.research.website import safety
from services.research.website.extract import extract_page
from services.research.website.fetch import Limits
from services.research.website.provider import WebsiteResearchProvider
from services.research.website.research import research_website
from tests.fixtures.website_fixtures import HANSA_HOME, HANSA_PAGES, PUBLIC_IP, FixtureWeb, Response, company_web

HANSA = "https://www.hansa-praezision.test"
NORDLICHT = "https://nordlicht-logistik.test"


def run(web: FixtureWeb, url: str, limits: Limits | None = None, clock=None):
    with web.fetcher(limits, clock) as fetcher:
        return research_website(url, client_name="Client", fetcher=fetcher)


# --- which addresses may be fetched -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com/"),
        ("https://Example.COM/de/?a=1#team", "https://example.com/de/?a=1"),
        ("http://example.com:80/about", "http://example.com/about"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
    ],
)
def test_addresses_are_normalized(raw: str, expected: str) -> None:
    assert safety.normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "WEBSITE_NOT_PROVIDED"),
        ("ftp://example.com", "WEBSITE_URL_INVALID"),
        ("javascript:alert(1)", "WEBSITE_URL_INVALID"),
        ("https://user:secret@example.com", "WEBSITE_URL_INVALID"),
        ("https://example.com:8443", "WEBSITE_URL_BLOCKED"),
        ("http://127.0.0.1", "WEBSITE_URL_BLOCKED"),
        ("http://169.254.169.254/latest/meta-data", "WEBSITE_URL_BLOCKED"),
        ("http://[::1]/", "WEBSITE_URL_BLOCKED"),
        ("http://localhost", "WEBSITE_URL_BLOCKED"),
        ("http://metadata.google.internal", "WEBSITE_URL_BLOCKED"),
        ("http://intranet", "WEBSITE_URL_BLOCKED"),
        ("http://printer.local", "WEBSITE_URL_BLOCKED"),
    ],
)
def test_unsafe_or_invalid_addresses_are_refused(raw: str, code: str) -> None:
    with pytest.raises(safety.WebsiteRejected) as refused:
        safety.normalize_url(raw)
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("10.0.0.5", False),
        ("172.16.3.4", False),
        ("192.168.1.10", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_only_public_internet_addresses_count_as_public(address: str, public: bool) -> None:
    assert safety.is_public_address(address) is public


def test_a_host_with_any_private_address_is_refused() -> None:
    web = FixtureWeb(sites={}, addresses={"mixed.test": [PUBLIC_IP, "10.1.2.3"], "inside.test": ["192.168.0.2"]})
    for host in ("mixed.test", "inside.test"):
        with pytest.raises(safety.WebsiteRejected) as refused:
            safety.resolve_public(host, 443, web.resolver)
        assert refused.value.code == "WEBSITE_URL_BLOCKED"
    with pytest.raises(safety.WebsiteRejected, match="could not be resolved"):
        safety.resolve_public("missing.test", 443, web.resolver)


# --- fetching: pinned, bounded, respectful ------------------------------------------------


def test_requests_connect_to_the_checked_address_with_the_real_host_name() -> None:
    web = company_web()
    result = run(web, HANSA)
    assert result.status == "complete"
    first = web.requests[0]
    assert first.url.host == PUBLIC_IP, "the connection goes to the address that was checked (no DNS rebinding)"
    assert first.headers["host"] == "www.hansa-praezision.test"
    assert first.extensions.get("sni_hostname") == "www.hansa-praezision.test", "TLS is verified for the domain"
    assert "BorekPitchFactory-WebsiteResearch" in first.headers["user-agent"]


def test_redirects_to_private_networks_or_ip_addresses_are_not_followed() -> None:
    web = FixtureWeb(
        sites={
            "evil.test": {
                "/": Response(302, "", {"location": "http://169.254.169.254/latest/meta-data/"}),
                "/robots.txt": None,
            },
            "sneaky.test": {"/": Response(301, "", {"location": "https://inside.test/admin"}), "/robots.txt": None},
            "inside.test": {"/admin": "<html><body><p>secret admin console text that must never be read by anyone</p></body></html>"},
        },
        addresses={"inside.test": ["10.0.0.7"]},
    )
    blocked = run(web, "https://evil.test")
    assert blocked.status == "failed" and blocked.errors[0]["code"] == "WEBSITE_REDIRECT_BLOCKED"
    rebound = run(web, "https://sneaky.test")
    assert rebound.status == "failed" and rebound.errors[0]["code"] == "WEBSITE_URL_BLOCKED"
    assert not web.paths("inside.test") and not web.paths("169.254.169.254"), "nothing was sent to the private target"


def test_redirect_loops_and_redirects_within_the_site_are_handled() -> None:
    web = FixtureWeb(
        sites={
            "loop.test": {"/": Response(302, "", {"location": "/a"}), "/a": Response(302, "", {"location": "/"}), "/robots.txt": None},
            "hansa.test": {"/": Response(301, "", {"location": "https://www.hansa-praezision.test/"}), "/robots.txt": None},
            "www.hansa-praezision.test": HANSA_PAGES,
        }
    )
    assert run(web, "https://loop.test").errors[0]["code"] == "WEBSITE_TOO_MANY_REDIRECTS"
    moved = run(web, "https://hansa.test")
    assert moved.status == "complete" and moved.final_url == "https://www.hansa-praezision.test/"


def test_responses_are_bounded_in_size_and_type() -> None:
    huge = "<html><body>" + "<p>Precision components for medical technology customers in Europe.</p>" * 50_000 + "</body></html>"
    web = FixtureWeb(
        sites={
            "big.test": {"/": huge, "/robots.txt": None},
            "declared.test": {"/": Response(200, "<html></html>", {"content-type": "text/html", "content-length": "900000000"}), "/robots.txt": None},
            "pdf.test": {"/": Response(200, b"%PDF-1.7", {"content-type": "application/pdf"}), "/robots.txt": None},
        }
    )
    limits = Limits(max_bytes=1_000_000)
    with web.fetcher(limits) as fetcher:
        page = fetcher.get("https://big.test/")
    assert page.ok and page.truncated and len(page.html.encode("utf-8")) <= 1_000_000
    big = run(web, "https://big.test", limits)
    assert sum(len(item["text"]) for item in big.content) <= limits.max_total_chars
    assert any(error["code"] == "WEBSITE_PAGE_TRUNCATED" for error in big.errors)
    assert run(web, "https://declared.test").errors[0]["code"] == "WEBSITE_TOO_LARGE"
    assert run(web, "https://pdf.test").errors[0]["code"] == "WEBSITE_UNSUPPORTED_CONTENT"


def test_the_page_count_and_the_time_budget_are_enforced() -> None:
    web = company_web()
    run(web, HANSA, Limits(max_pages=3))
    html_requests = [path for path in web.paths("www.hansa-praezision.test") if path != "/robots.txt"]
    assert len(html_requests) == 3, "the homepage and two internal pages, nothing more"

    ticks = itertools.count(step=4.0)
    slow = company_web()
    result = run(slow, HANSA, Limits(total_seconds=10.0), clock=lambda: next(ticks))
    assert result.status in ("partial", "failed")
    assert any(error["code"] == "WEBSITE_TIME_BUDGET_EXCEEDED" for error in result.errors)


def test_access_restrictions_are_respected_and_not_retried() -> None:
    web = company_web()
    restricted = run(web, "https://restricted.test")
    assert restricted.status == "failed" and restricted.errors[0]["code"] == "WEBSITE_ACCESS_RESTRICTED"
    assert web.paths("restricted.test").count("/") == 1, "a refusal is not retried or worked around"
    robots = run(web, "https://no-robots.test")
    assert robots.status == "failed" and robots.errors[0]["code"] == "WEBSITE_BLOCKED_BY_ROBOTS"
    assert web.paths("no-robots.test") == ["/robots.txt"], "robots.txt forbids it, so no page is read"
    # A path the site excludes is skipped, the rest is read.
    partial = FixtureWeb(sites={"www.hansa-praezision.test": {**HANSA_PAGES, "/robots.txt": "User-agent: *\nDisallow: /services\n"}})
    result = run(partial, HANSA)
    assert "/services" not in partial.paths("www.hansa-praezision.test")
    assert next(page for page in result.pages if page.role == "services").status == "blocked_by_robots"


def test_unreachable_and_missing_websites_do_not_raise() -> None:
    web = company_web()
    unreachable = run(web, "https://does-not-exist.test")
    assert unreachable.status == "failed" and unreachable.errors[0]["code"] == "WEBSITE_UNREACHABLE"
    assert "could not be analysed" in unreachable.summary()
    missing = research_website(None)
    assert missing.status == "not_provided" and not missing.usable
    invalid = research_website("ftp://example.com")
    assert invalid.status == "failed" and invalid.errors[0]["code"] == "WEBSITE_URL_INVALID"


# --- extraction and ranking ----------------------------------------------------------------


def test_boilerplate_scripts_and_cookie_banners_are_removed() -> None:
    page = extract_page(HANSA_HOME, HANSA + "/")
    text = " ".join(block.text for block in page.blocks)
    assert "Precision CNC parts for medical and aerospace customers" in text
    for noise in ("We use cookies", "ignore all previous instructions", "dataLayer", "all rights reserved", "Subscribe to our newsletter", "Login"):
        assert noise.lower() not in text.lower(), noise
    assert page.organization["name"] == "Hansa Präzisionsteile GmbH" and page.organization["address"].startswith("Hafenstraße 12")
    assert any(url.endswith("/about-us") for url, _label in page.links), "menu links are still used to find pages"
    hidden = extract_page(
        "<html><body><div hidden><p>Hidden text that should never be part of the research output.</p></div>"
        "<div style='display: none'><p>Invisible text that should never be part of the research output.</p></div>"
        "<p>Visible statement about the company and the services it offers to customers.</p></body></html>",
        "https://x.test/",
    )
    assert [block.text for block in hidden.blocks] == ["Visible statement about the company and the services it offers to customers."]


def test_relevant_internal_pages_are_chosen_and_irrelevant_ones_skipped() -> None:
    web = company_web()
    result = run(web, HANSA)
    roles = [page.role for page in result.pages]
    assert roles[0] == "home" and {"about", "services", "products", "industries"} <= set(roles)
    paths = web.paths("www.hansa-praezision.test")
    for skipped in ("/privacy", "/login", "/brochure.pdf"):
        assert skipped not in paths
    assert not any(request.headers["host"] == "twitter.com" for request in web.requests), "other sites are not crawled"


def test_repeated_site_chrome_is_removed_and_content_is_unique() -> None:
    result = run(company_web(), HANSA)
    texts = [item["text"] for item in result.content]
    assert len(texts) == len({text.lower() for text in texts}), "no duplicate blocks"
    assert not any("newsletter" in text.lower() or "cookies" in text.lower() for text in texts)
    assert sum(len(text) for text in texts) <= Limits().max_total_chars


def test_every_fact_is_quoted_from_the_page_it_cites() -> None:
    web = company_web()
    result = run(web, HANSA)
    assert result.facts
    for fact in result.facts:
        assert fact["origin"] == "SOURCE_FACT"
        assert fact["value"] in fact["excerpt"], "the value is part of the quoted excerpt"
        path = fact["source_url"].split("www.hansa-praezision.test", 1)[1] or "/"
        assert fact["excerpt"] in HANSA_PAGES[path].replace("&amp;", "&") or fact["locator"].startswith("structured data"), fact
    kinds = {fact["kind"] for fact in result.facts}
    assert {"description", "about", "headquarters", "founded", "offering", "industry"} <= kinds
    assert all(signal["origin"] == "AI_INFERENCE" and signal["status"] == "to_validate" for signal in result.signals)
    assert not {fact["value"] for fact in result.facts} & {signal["statement"] for signal in result.signals}


def test_two_companies_produce_distinct_research() -> None:
    web = company_web()
    hansa, nordlicht = run(web, HANSA), run(web, NORDLICHT)
    hansa_facts = {fact["value"] for fact in hansa.facts}
    nordlicht_facts = {fact["value"] for fact in nordlicht.facts}
    assert not hansa_facts & nordlicht_facts
    assert any("CNC" in value for value in hansa_facts) and any("fulfilment" in value for value in nordlicht_facts)
    assert hansa.signals[0]["key"] == "production" and nordlicht.signals[0]["key"] == "logistics"


def test_javascript_rendered_sites_are_reported_as_incomplete() -> None:
    result = run(company_web(), "https://vortex-analytics.test")
    assert result.status == "partial"
    assert result.pages[0].status == "requires_javascript"
    assert result.errors[0]["code"] == "WEBSITE_REQUIRES_JAVASCRIPT"
    assert [fact["kind"] for fact in result.facts] == ["description"], "only the metadata the page carries"
    assert "incomplete" in result.summary() and "JavaScript" in result.summary()


# --- provider -------------------------------------------------------------------------------


def test_the_provider_caches_successful_research_and_serves_stage1_evidence() -> None:
    web = company_web()
    provider = WebsiteResearchProvider(fetcher_factory=web.fetcher)
    first = provider.research_website(HANSA)
    count = len(web.requests)
    assert provider.research_website(HANSA + "/") is first and len(web.requests) == count, "read once per address"
    evidence = provider.research(client_name="Hansa", client_web_page=HANSA)
    assert {item.field for item in evidence} == {"description", "headquarters"}
    assert all(isinstance(item, CompanyEvidence) for item in evidence)
    facts = _company_facts(evidence)
    assert facts["description"]["status"] == "verified" and facts["headquarters"]["value"].startswith("Hafenstraße 12")
    assert provider.research(client_name="Hansa", client_web_page=None) == []
    # A failure is not cached: the next attempt reads the site again.
    provider.research_website("https://restricted.test")
    before = len(web.paths("restricted.test"))
    provider.research_website("https://restricted.test")
    assert len(web.paths("restricted.test")) > before


def test_line_breaks_separate_words_and_team_cards_are_not_offerings() -> None:
    """Found on a real website: headings split by <br>, and team cards between the services."""
    services = """<html><body><main><h1>What we do</h1>
<h3>End-to-End<br>Development</h3><p>From the first concept to operations, built and run by one team for our clients.</p>
<h3>Business Process Outsourcing</h3><p>Back-office processes run by our teams in Kosovo and India for European customers.</p>
<h3>Katharina Beispiel</h3><p>Managing Partner &amp; CCO</p>
<h3>Jonas Muster</h3><p>Head of Delivery</p>
<h3>Leading in speed and innovation. This is us!</h3></main></body></html>"""
    web = FixtureWeb(
        sites={
            "agency.test": {
                "/": "<html><head><meta name='description' content='An agency that develops software and runs back-office processes for clients.'></head>"
                "<body><a href='/what-we-do'>What we do</a><p>We develop software and run back-office processes for our clients in Europe.</p></body></html>",
                "/what-we-do": services,
                "/robots.txt": None,
            }
        }
    )
    result = run(web, "https://agency.test")
    offerings = [fact["value"] for fact in result.facts if fact["kind"] == "offering"]
    assert "End-to-End Development" in offerings and "Business Process Outsourcing" in offerings
    assert not any(name in " ".join(offerings) for name in ("Katharina", "Jonas", "Managing Partner", "Head of", "This is us"))


# --- robots.txt on redirects and robots.txt failures (PR #8 review) -----------------------------

ALLOW_ALL = "User-agent: *\nDisallow:\n"


def test_a_redirect_to_a_path_robots_txt_disallows_is_not_fetched() -> None:
    web = FixtureWeb(
        sites={
            "redirecting.test": {
                "/robots.txt": "User-agent: *\nDisallow: /private/\n",
                "/": Response(302, "", {"location": "/private/home"}),
                "/private/home": HANSA_HOME,
            }
        }
    )
    result = run(web, "https://redirecting.test")
    assert result.status == "failed"
    assert result.errors[0]["code"] == "WEBSITE_BLOCKED_BY_ROBOTS" and "/private/home" in result.errors[0]["message"]
    assert result.pages[0].status == "blocked_by_robots"
    assert "/private/home" not in web.paths("redirecting.test"), "the disallowed target is never requested"


def test_a_redirect_to_another_host_follows_that_hosts_robots_txt() -> None:
    web = FixtureWeb(
        sites={
            "start.test": {"/robots.txt": ALLOW_ALL, "/": Response(301, "", {"location": "https://elsewhere.test/landing"})},
            "elsewhere.test": {"/robots.txt": "User-agent: *\nDisallow: /\n", "/landing": HANSA_HOME},
        }
    )
    result = run(web, "https://start.test")
    assert result.status == "failed" and result.errors[0]["code"] == "WEBSITE_BLOCKED_BY_ROBOTS"
    assert web.paths("elsewhere.test") == ["/robots.txt"], "only the other host's robots.txt is read"


def test_an_internal_page_that_redirects_into_a_disallowed_path_is_skipped() -> None:
    pages = {**HANSA_PAGES, "/services": Response(302, "", {"location": "/intern/services"}), "/intern/services": HANSA_PAGES["/services"]}
    web = FixtureWeb(sites={"www.hansa-praezision.test": pages})  # robots.txt disallows /intern/
    result = run(web, HANSA)
    services = next(page for page in result.pages if page.role == "services")
    assert services.status == "blocked_by_robots" and services.error == "WEBSITE_BLOCKED_BY_ROBOTS"
    assert "/intern/services" not in web.paths("www.hansa-praezision.test")
    assert result.status == "partial" and result.facts, "the other pages are still read"


def test_permitted_redirects_are_still_followed_with_pinning() -> None:
    web = FixtureWeb(
        sites={
            "hansa.test": {"/robots.txt": ALLOW_ALL, "/": Response(301, "", {"location": "https://www.hansa-praezision.test/"})},
            "www.hansa-praezision.test": {**HANSA_PAGES, "/about-us": Response(301, "", {"location": "/company"}), "/company": HANSA_PAGES["/about-us"]},
        }
    )
    result = run(web, "https://hansa.test")
    assert result.status == "complete" and result.final_url == "https://www.hansa-praezision.test/"
    about = next(page for page in result.pages if page.role == "about")
    assert about.status == "ok" and about.url.endswith("/company")
    assert all(request.url.host == PUBLIC_IP for request in web.requests), "every request, robots.txt included, is pinned"


def test_robots_txt_is_fetched_without_a_robots_lookup_of_its_own() -> None:
    web = FixtureWeb(
        sites={
            "moved-robots.test": {
                "/robots.txt": Response(301, "", {"location": "/config/robots.txt"}),
                "/config/robots.txt": "User-agent: *\nDisallow: /secret\n",
                "/": HANSA_HOME,
            }
        }
    )
    result = run(web, "https://moved-robots.test")
    assert result.status in ("complete", "partial")
    paths = web.paths("moved-robots.test")
    assert paths.count("/robots.txt") == 1 and paths.count("/config/robots.txt") == 1, "read once, no recursion"


def _timeout(request):
    import httpx

    raise httpx.ReadTimeout("robots.txt timed out", request=request)


@pytest.mark.parametrize(
    ("robots", "allowed", "code"),
    [
        (None, True, None),  # 404: no robots.txt, crawling allowed
        (Response(410, "gone", {"content-type": "text/plain"}), True, None),
        (Response(401, "login", {"content-type": "text/plain"}), False, "WEBSITE_ACCESS_RESTRICTED"),
        (Response(403, "forbidden", {"content-type": "text/plain"}), False, "WEBSITE_ACCESS_RESTRICTED"),
        (Response(429, "slow down", {"content-type": "text/plain"}), False, "WEBSITE_RATE_LIMITED"),
        (Response(451, "legal", {"content-type": "text/plain"}), False, "WEBSITE_HTTP_ERROR"),
        (Response(500, "error", {"content-type": "text/plain"}), False, "WEBSITE_HTTP_ERROR"),
        (Response(503, "maintenance", {"content-type": "text/plain"}), False, "WEBSITE_HTTP_ERROR"),
        (_timeout, False, "WEBSITE_TIMEOUT"),
    ],
    ids=["404", "410", "401", "403", "429", "451", "500", "503", "timeout"],
)
def test_only_a_missing_robots_txt_allows_crawling(robots, allowed: bool, code: str | None) -> None:
    web = FixtureWeb(sites={"site.test": {"/robots.txt": robots, "/": HANSA_HOME}})
    result = run(web, "https://site.test")
    fetched = "/" in web.paths("site.test")
    assert fetched is allowed
    if allowed:
        assert result.status in ("complete", "partial") and result.facts
        return
    assert result.status == "failed" and result.errors[0]["code"] == code
    assert web.paths("site.test") == ["/robots.txt"], "robots.txt is asked once, never retried, and nothing else is read"
    if code == "WEBSITE_TIMEOUT":
        # The site did not answer at all: reported as that, and still nothing else is requested.
        assert result.errors[0]["message"] == "The website did not answer in time."
    else:
        assert "robots.txt" in result.errors[0]["message"]
        assert result.pages[0].status == "blocked_by_robots"


def test_a_failed_robots_txt_blocks_a_redirect_target_on_another_host() -> None:
    web = FixtureWeb(
        sites={
            "start.test": {"/robots.txt": ALLOW_ALL, "/": Response(302, "", {"location": "https://limited.test/"})},
            "limited.test": {"/robots.txt": Response(429, "slow down", {"content-type": "text/plain"}), "/": HANSA_HOME},
        }
    )
    result = run(web, "https://start.test")
    assert result.errors[0]["code"] == "WEBSITE_BLOCKED_BY_ROBOTS" and "429" in result.errors[0]["message"]
    assert web.paths("limited.test") == ["/robots.txt"]
