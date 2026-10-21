"""Research one company website: the homepage and a bounded set of relevant internal pages.

Output (``WebsiteResearch.to_dict``), all JSON:

* ``status``    complete | partial (some pages or content missing) | failed (nothing usable) |
                not_provided (no website), with ``errors`` that say why
* ``pages``     every page considered, with its role and outcome
* ``facts``     statements quoted verbatim from the site, each with source URL and excerpt -
                origin SOURCE_FACT; nothing is paraphrased or inferred
* ``signals``   activity hints read from the site's wording - origin AI_INFERENCE, always to be
                validated; never a verified challenge
* ``content``   the ranked, de-duplicated text passed to the analysis (bounded)

No model is called here: extraction and ranking are deterministic.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from services.research.website.extract import Block, PageContent, clean_text, extract_page
from services.research.website.fetch import USER_AGENT, Fetcher, Limits, robots_reason
from services.research.website.safety import WebsiteRejected, normalize_url

# 1.1 (BAP-10): the Discovery document prints the website facts with their sources.
# 1.2 (BAP-9): the same page, introduced in customer-facing words that name the company.
SCHEMA_VERSION = "1.2"

# Page roles, best first. Keywords match URL path segments and link texts (English and German).
_ROLES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("about", 10, ("about", "about-us", "aboutus", "company", "unternehmen", "ueber-uns", "uber-uns", "über-uns", "who-we-are", "our-story", "profile", "profil", "firma", "wir")),
    ("services", 9, ("services", "service", "leistungen", "dienstleistungen", "leistung", "what-we-do", "capabilities", "expertise", "kompetenzen", "angebot")),
    ("products", 9, ("products", "product", "produkte", "produkt", "portfolio", "sortiment", "shop-overview")),
    ("solutions", 9, ("solutions", "solution", "loesungen", "lösungen", "losungen", "anwendungen", "applications")),
    ("industries", 8, ("industries", "industry", "branchen", "branche", "sectors", "markets", "maerkte", "märkte", "markte")),
    ("references", 6, ("references", "referenzen", "case-studies", "casestudies", "customers", "kunden", "projects", "projekte", "success-stories")),
    ("imprint", 4, ("impressum", "imprint", "legal-notice", "company-information")),
    ("careers", 2, ("careers", "career", "karriere", "jobs")),
)
# Pages that never describe the business: legal texts, accounts, shop mechanics, listings.
_SKIP_WORDS = frozenset({
    "privacy", "datenschutz", "datenschutzerklaerung", "datenschutzerklärung", "cookie", "cookies", "terms",
    "agb", "disclaimer", "login", "signin", "register", "registrieren", "cart", "checkout", "account",
    "konto", "warenkorb", "search", "suche", "wp-admin", "wp-login", "feed", "sitemap", "tag", "tags",
    "author", "newsletter", "cdn-cgi",
})
_SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3", ".xml", ".json", ".css", ".js", ".ico")

_NOISE = re.compile(
    r"\b(cookies?|consent|privacy policy|datenschutz|all rights reserved|alle rechte vorbehalten|newsletter|subscribe|"
    r"enable javascript|javascript is disabled|your browser|log ?in|sign ?up|read more|mehr erfahren|weiterlesen|"
    r"click here|follow us|copyright)\b|©",
    re.IGNORECASE,
)
_BUSINESS = re.compile(
    r"\b(we (?:offer|provide|develop|design|build|manufacture|produce|deliver|support|help|specialize|specialise)|"
    r"our (?:services|products|solutions|customers|clients|team|mission|company|expertise|portfolio)|"
    r"founded|since \d{4}|headquarter|employees|locations?|sites?|customers?|clients?|industr(?:y|ies)|"
    r"services?|products?|solutions?|manufactur\w*|logistics|software|consulting|engineering|production|"
    r"wir (?:bieten|entwickeln|liefern|produzieren|unterstützen|beraten)|unsere (?:kunden|leistungen|produkte|lösungen)|"
    r"gegründet|mitarbeiter|standort\w*|kunden|leistungen|produkte|lösungen|branchen?|fertigung|beratung)\b",
    re.IGNORECASE,
)
_GENERIC_HEADINGS = re.compile(
    r"^(home|start|menu|menü|contact|kontakt|news|aktuelles|blog|more|mehr|read more|our services|our products|"
    r"services|products|solutions|leistungen|produkte|lösungen|unsere leistungen|unsere produkte|about us|über uns|"
    r"welcome|willkommen|faq|downloads?|search|suche|jobs|karriere|careers|imprint|impressum|login)\W*$",
    re.IGNORECASE,
)
# Team cards and slogans sit between the offerings on many service pages: a job title, the name
# above it (the block before a job title) or an exclamation is not an offering.
_JOB_TITLE = re.compile(
    r"\b(?:CEO|CFO|CTO|COO|CCO|CMO|CIO|Managing|Partner|Director|Founder|Co-Founder|Head of|Manager|"
    r"Geschäftsführer\w*|Vorstand|Prokurist\w*|Leiter\w*|Inhaber\w*)\b"
)


# Marketing lines address the reader ("for your success", "Ihre Herausforderung"); a name of a
# service or product does not. Sentences (ending in a full stop or a colon) are statements or
# lead-ins, and some headings only label a section.
_ADDRESSES_READER = re.compile(r"\b(?:you|your|yours|Sie|Ihr|Ihre|Ihren|Ihrem|Ihrer|Ihres|dich|dein|deine|uns|we|our|unsere?)\b", re.IGNORECASE)
_SECTION_LABEL = re.compile(
    r"^(?:key (?:competencies|facts|figures|benefits)|other (?:products|services|solutions)|more (?:products|services)|"
    r"overview|übersicht|highlights?|kategorien|categories|vorteile|benefits|advantages|features|references?|contact us|"
    r"(?:alle |all |unsere |our )?(?:standorte?|locations?|niederlassungen|branches|offices|filialen)|"
    r"(?:(?:the|our|die|unsere) )?(?:(?:main|key|core|wichtigsten|zentralen) )?(?:capabilities|competencies|strengths|kompetenzen|stärken)"
    r"(?: (?:of|der|des|von) .*)?)\W*$",
    re.IGNORECASE,
)
# Job listings and vacancies ("Auszubildende, Seelze", "Elektroniker (m/w/d)") are not offerings.
_JOB_LISTING = re.compile(
    r"(?:\b[mwdf]\s?/\s?[mwdf]\s?/\s?[mwdf]\b|\b(?:Auszubildende\w*|Azubi\w*|Ausbildung\w*|Praktik\w*|Werkstudent\w*|"
    r"Stellenangebot\w*|Stellenanzeige\w*|Jobs?|Karriere\w*|Bewerb\w*|vacanc\w*|apprentice\w*|trainee\w*|internships?|"
    r"hiring|careers?|open positions?)\b)",
    re.IGNORECASE,
)
# Company names with their legal form ("Wahl GmbH & Co. KG") name an entity, not an offering.
_LEGAL_ENTITY = re.compile(
    r"(?<![\w&])(?:GmbH|mbH|AG|KGaA|KG|SE|OHG|GbR|UG|e\.\s?K\.|e\.\s?V\.|Ltd\.?|LLC|Inc\.?|Corp\.?|PLC|S\.A\.|B\.V\.|N\.V\.)(?![\w])"
)


def _offering_like(text: str, following: str, tag: str = "") -> bool:
    return not (
        tag == "figcaption"  # a caption describes a picture
        or _JOB_TITLE.search(text)
        or _JOB_TITLE.search(following)
        or _JOB_LISTING.search(text)
        or _LEGAL_ENTITY.search(text)
        or _ADDRESSES_READER.search(text)
        or _SECTION_LABEL.match(text)
        or "!" in text
        or text.rstrip().endswith(("?", ".", ":"))
    )


# Wording on the site that hints at work an AI could support. A hint is an interpretation and
# stays one: the analysis presents it as a question to validate, never as a known challenge.
_SIGNALS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("customer_service", "Customer service and enquiries", ("customer service", "customer support", "support team", "hotline", "service desk", "help desk", "kundenservice", "kundendienst", "enquiries", "inquiries", "anfragen")),
    ("quoting_sales", "Quotes, offers and sales", ("quote", "quotation", "request an offer", "rfq", "angebot", "configurator", "konfigurator", "sales team", "vertrieb")),
    ("order_processing", "Order processing", ("orders", "ordering", "order processing", "place an order", "bestell", "auftragsabwicklung", "e-commerce", "online shop", "webshop")),
    ("logistics", "Logistics and delivery", ("logistics", "logistik", "delivery", "lieferung", "shipping", "versand", "warehouse", "lager", "transport", "spedition")),
    ("production", "Production and planning", ("production", "produktion", "manufactur", "fertigung", "plant", "werk", "assembly", "montage", "planning", "planung")),
    ("field_service", "Field service and maintenance", ("maintenance", "wartung", "repair", "reparatur", "field service", "installation", "inspection", "inspektion", "service technician", "techniker")),
    ("documents_compliance", "Documentation, quality and compliance", ("certified", "zertifiziert", "iso 9001", "iso 27001", "quality", "qualität", "compliance", "documentation", "dokumentation", "audit")),
    ("recruiting", "Recruiting and onboarding", ("we are hiring", "join our team", "open positions", "stellenangebote", "karriere", "careers", "apprenticeship", "ausbildung")),
    ("finance", "Invoicing and finance", ("invoice", "rechnung", "billing", "payment", "zahlung", "accounting", "buchhaltung")),
)


_MINOR_ERRORS = frozenset({"WEBSITE_PAGE_NOT_FOUND"})


@dataclass
class Page:
    url: str
    role: str
    status: str = "pending"  # ok | failed | requires_javascript | blocked_by_robots | skipped
    title: str | None = None
    chars: int = 0
    error: str | None = None
    message: str | None = None
    content: PageContent | None = None


@dataclass
class WebsiteResearch:
    requested_url: str | None
    status: str = "not_provided"
    site: str | None = None
    final_url: str | None = None
    pages: list[Page] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    content: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    researched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    @property
    def usable(self) -> bool:
        return self.status in ("complete", "partial") and bool(self.facts or self.content)

    def significant_errors(self) -> list[dict[str, str]]:
        """Problems that leave the research incomplete; a broken link to a missing page does not."""
        return [error for error in self.errors if error["code"] not in _MINOR_ERRORS]

    def summary(self) -> str:
        """One honest sentence for the analysis: what was read, and what is missing."""
        if self.status == "not_provided":
            return "No website was provided, so no website research was performed."
        ok = [page for page in self.pages if page.status in ("ok", "requires_javascript")]
        if not self.usable:
            reason = self.errors[0]["message"] if self.errors else "No usable business content was found."
            return f"The website {self.requested_url} could not be analysed: {reason} Company-specific content is limited to what the user provided."
        text = (
            f"Website research read {len(ok)} page{'s' if len(ok) != 1 else ''} of {self.site} "
            f"and quotes {len(self.facts)} statement{'s' if len(self.facts) != 1 else ''} with their source. "
            "Challenges and opportunities derived from it remain hypotheses to validate."
        )
        if self.status == "partial":
            missing = "; ".join(sorted({error["message"] for error in self.significant_errors()}))[:300]
            text += f" Research is incomplete: {missing}"
        return text

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "site": self.site,
            "researched_at": self.researched_at,
            "user_agent": USER_AGENT,
            "summary": self.summary(),
            "pages": [
                {key: value for key, value in {
                    "url": page.url, "role": page.role, "status": page.status, "title": page.title,
                    "chars": page.chars, "error": page.error,
                }.items() if value is not None}
                for page in self.pages
            ],
            "facts": [dict(fact) for fact in self.facts],
            "signals": [dict(signal) for signal in self.signals],
            "errors": [dict(error) for error in self.errors],
        }
        if include_content:
            result["content"] = [dict(item) for item in self.content]
        return result


def research_website(url: str | None, *, client_name: str = "", fetcher: Fetcher | None = None, limits: Limits | None = None) -> WebsiteResearch:
    """Research the website. Never raises for website problems: they are reported in the result."""
    if not str(url or "").strip():
        return WebsiteResearch(requested_url=None)
    result = WebsiteResearch(requested_url=str(url).strip())
    try:
        start = normalize_url(str(url))
    except WebsiteRejected as exc:
        result.status = "failed"
        result.errors.append({"code": exc.code, "url": result.requested_url or "", "message": str(exc)})
        return result
    own = fetcher is None
    fetcher = fetcher or Fetcher(limits=limits)
    try:
        _run(result, start, fetcher, client_name)
    finally:
        if own:
            fetcher.close()
    return result


def _run(result: WebsiteResearch, start: str, fetcher: Fetcher, client_name: str) -> None:
    limits = fetcher.limits
    home = Page(url=start, role="home")
    result.pages.append(home)
    if not fetcher.allowed(start):
        failure = fetcher.robots_failure(start)
        if failure is not None and failure.status is None:
            # The site could not be reached at all: report that, not a robots.txt rule.
            _fail(result, home, failure.error or "WEBSITE_FETCH_FAILED", failure.message)
        elif failure is not None:
            # robots.txt was refused (401/403), rate limited (429) or failed (5xx): fail closed.
            _fail(
                result,
                home,
                failure.error or "WEBSITE_BLOCKED_BY_ROBOTS",
                robots_reason(failure),
            )
            home.status = "blocked_by_robots"
        else:
            _fail(result, home, "WEBSITE_BLOCKED_BY_ROBOTS", "The website's robots.txt does not allow automated reading; this is respected.")
            home.status = "blocked_by_robots"
        result.status = "failed"
        return
    fetched = fetcher.get(start)
    if not fetched.ok:
        _fail(result, home, fetched.error or "WEBSITE_FETCH_FAILED", fetched.message)
        result.status = "failed"
        return
    home.url = fetched.url
    result.final_url = fetched.url
    result.site = _site(fetched.url)
    _read(home, fetched.html or "", fetched.url, limits)
    if fetched.truncated:
        result.errors.append({"code": "WEBSITE_PAGE_TRUNCATED", "url": home.url, "message": "A page was larger than the size limit and was read only in part."})
    if home.content and home.content.requires_javascript:
        home.status = "requires_javascript"
        result.errors.append({
            "code": "WEBSITE_REQUIRES_JAVASCRIPT",
            "url": home.url,
            "message": "The homepage is rendered with JavaScript; only its metadata could be read.",
        })

    for url, role in _select_pages(home.content, result.site, limits.max_pages - 1):
        page = Page(url=url, role=role)
        result.pages.append(page)
        if fetcher.remaining() <= 1:
            page.status, page.error = "skipped", "WEBSITE_TIME_BUDGET_EXCEEDED"
            result.errors.append({"code": "WEBSITE_TIME_BUDGET_EXCEEDED", "url": url, "message": "The time budget ran out before all relevant pages were read."})
            continue
        if not fetcher.allowed(url):
            page.status, page.error = "blocked_by_robots", "WEBSITE_BLOCKED_BY_ROBOTS"
            continue
        response = fetcher.get(url)
        if not response.ok:
            _fail(result, page, response.error or "WEBSITE_FETCH_FAILED", response.message)
            continue
        if _site(response.url) != result.site:
            page.status, page.error = "skipped", "WEBSITE_LEFT_SITE"
            continue
        page.url = response.url
        _read(page, response.html or "", response.url, limits)
        if page.content and page.content.requires_javascript:
            page.status = "requires_javascript"

    _dedupe(result.pages)
    result.content = _rank(result.pages, limits.max_total_chars)
    result.facts = _facts(result.pages, client_name)
    result.signals = _signals(result.content)
    ok = [page for page in result.pages if page.status == "ok"]
    if not result.facts and not result.content:
        result.status = "failed"
        if not result.errors:
            result.errors.append({"code": "WEBSITE_NO_CONTENT", "url": home.url, "message": "No business content could be read from the website."})
    elif result.significant_errors() or len(ok) < 2 or sum(len(item["text"]) for item in result.content) < 600:
        result.status = "partial"
        if not result.significant_errors():
            result.errors.append({"code": "WEBSITE_LIMITED_CONTENT", "url": home.url, "message": "The website offers little readable business content."})
    else:
        result.status = "complete"


def _fail(result: WebsiteResearch, page: Page, code: str, message: str) -> None:
    page.status = "blocked_by_robots" if code == "WEBSITE_BLOCKED_BY_ROBOTS" else "failed"
    page.error, page.message = code, message
    result.errors.append({"code": code, "url": page.url, "message": message})


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _read(page: Page, markup: str, url: str, limits: Limits) -> None:
    content = extract_page(markup, url)
    kept: list[Block] = []
    total = 0
    for block in content.blocks:
        if total + len(block.text) > limits.max_chars_per_page:
            break
        kept.append(block)
        total += len(block.text)
    content.blocks = kept
    page.content = content
    page.title = content.title
    page.chars = total
    page.status = "ok"


def _role(url: str, label: str) -> tuple[str, int] | None:
    path = urlsplit(url).path.lower()
    segments = [segment for segment in re.split(r"[/_.]", path) if segment]
    if path.endswith(_SKIP_EXTENSIONS) or any(
        segment in _SKIP_WORDS or any(part in _SKIP_WORDS for part in segment.split("-")) for segment in segments
    ):
        return None
    parts = set(segments) | {part for segment in segments for part in segment.split("-")}
    # The address decides first: /karriere/stellenangebote is a careers page, whatever its link
    # says. Link texts match whole words only ("Stellenangebote" is not "Angebot").
    for role, weight, keywords in _ROLES:
        if any(keyword in parts for keyword in keywords):
            return role, weight
    text = label.lower()
    for role, weight, keywords in _ROLES:
        if any(
            len(keyword) > 3 and re.search(rf"(?<![\wäöüß]){re.escape(keyword.replace('-', ' '))}(?![\wäöüß])", text)
            for keyword in keywords
        ):
            return role, weight
    return None


def _select_pages(home: PageContent | None, site: str | None, count: int) -> list[tuple[str, str]]:
    """The most relevant internal pages linked from the homepage, best role first, one level deep."""
    if home is None or count <= 0:
        return []
    candidates: dict[str, tuple[float, str]] = {}
    for url, label in home.links:
        if _site(url) != site or urlsplit(url).path in ("", "/"):
            continue
        found = _role(url, label)
        if found is None:
            continue
        role, weight = found
        depth = len([part for part in urlsplit(url).path.split("/") if part])
        score = weight - 0.5 * max(depth - 1, 0)
        if url not in candidates or candidates[url][0] < score:
            candidates[url] = (score, role)
    ranked = sorted(candidates.items(), key=lambda item: (-item[1][0], len(item[0]), item[0]))
    chosen: list[tuple[str, str]] = []
    per_role: Counter[str] = Counter()
    # First the best page of each role, then further pages of the strongest roles.
    for limit in (1, 2):
        for url, (_score, role) in ranked:
            if len(chosen) >= count:
                break
            if per_role[role] >= limit or any(url == picked for picked, _ in chosen):
                continue
            chosen.append((url, role))
            per_role[role] += 1
    return chosen


def _key(text: str) -> str:
    return hashlib.sha1(re.sub(r"[\W_]+", " ", text.lower()).strip().encode("utf-8")).hexdigest()


def _dedupe(pages: list[Page]) -> None:
    """Text repeated on most pages is site chrome and is removed; any other repeat is kept once."""
    readable = [page for page in pages if page.content is not None]
    seen_on: Counter[str] = Counter()
    for page in readable:
        for key in {_key(block.text) for block in page.content.blocks}:  # type: ignore[union-attr]
            seen_on[key] += 1
    chrome = {key for key, count in seen_on.items() if len(readable) >= 3 and count >= max(3, len(readable) // 2 + 1)}
    used: set[str] = set()
    for page in readable:
        kept = []
        for block in page.content.blocks:  # type: ignore[union-attr]
            key = _key(block.text)
            if key in chrome or key in used or _NOISE.search(block.text):
                continue
            used.add(key)
            kept.append(block)
        page.content.blocks = kept  # type: ignore[union-attr]
        page.chars = sum(len(block.text) for block in kept)


_ROLE_WEIGHT = {role: weight for role, weight, _ in _ROLES} | {"home": 9}


def _rank(pages: list[Page], budget: int) -> list[dict[str, Any]]:
    """The most business-relevant blocks within the character budget, kept in page order."""
    scored: list[tuple[float, int, dict[str, Any]]] = []
    position = 0
    for page in pages:
        if page.content is None:
            continue
        weight = _ROLE_WEIGHT.get(page.role, 1)
        for block in page.content.blocks:
            position += 1
            hits = len(_BUSINESS.findall(block.text))
            if block.kind == "heading":
                score = weight + 2 + min(hits, 2)
            else:
                score = weight + min(hits, 4) * 1.5 + (1 if 80 <= len(block.text) <= 700 else 0)
            scored.append((score, position, {"source_url": page.url, "role": page.role, "kind": block.kind, "text": block.text}))
    chosen: list[tuple[int, dict[str, Any]]] = []
    used = 0
    for score, index, item in sorted(scored, key=lambda entry: (-entry[0], entry[1])):
        if used + len(item["text"]) > budget:
            continue
        chosen.append((index, item))
        used += len(item["text"])
    return [item for _index, item in sorted(chosen, key=lambda entry: entry[0])]


def _fact(kind: str, label: str, value: str, source_url: str, locator: str, excerpt: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "value": value,
        "origin": "SOURCE_FACT",
        "source_url": source_url,
        "locator": locator,
        "excerpt": excerpt,
    }


def _clip(text: str, limit: int) -> str:
    """A leading part of a quote that still reads as a quote: whole sentences, else whole words.

    Nothing is added (no ellipsis), so the value stays a verbatim part of the excerpt.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    sentence_end = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if sentence_end >= limit // 3:
        return head[: sentence_end + 1]
    word_end = head.rfind(" ")
    return head[:word_end].rstrip(" ,;:-–") if word_end > 0 else head


def _facts(pages: list[Page], client_name: str) -> list[dict[str, Any]]:
    """Verified statements: quoted verbatim from the site (value is always part of the excerpt)."""
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(fact: dict[str, Any]) -> None:
        key = (fact["kind"], fact["value"].lower())
        if fact["value"] and fact["value"] in fact["excerpt"] and str(key) not in seen:
            seen.add(str(key))
            facts.append(fact)

    readable = [page for page in pages if page.content is not None]
    home = next((page for page in readable if page.role == "home"), None)
    org: dict[str, str] = {}
    org_url = ""
    for page in readable:
        if page.content.organization and not org:  # type: ignore[union-attr]
            org, org_url = page.content.organization, page.url  # type: ignore[union-attr]
    # Company description: the site's own summary, best source first.
    description = org.get("description")
    if description:
        add(_fact("description", "Company description (website)", _clip(description, 400), org_url, "structured data (schema.org Organization)", description[:600]))
    elif home and home.content and home.content.description and len(home.content.description) >= 40:
        add(_fact("description", "Company description (website)", _clip(home.content.description, 400), home.url, "meta description", home.content.description[:600]))
    about = next((page for page in readable if page.role == "about"), None)
    if about and about.content:
        first = next((block.text for block in about.content.blocks if block.kind == "text" and len(block.text) >= 80 and _BUSINESS.search(block.text)), None)
        if first:
            add(_fact("about", "About the company (website)", _clip(first, 400), about.url, "about page", first[:600]))
    if org.get("address"):
        add(_fact("headquarters", "Address (website)", org["address"], org_url, "structured data (schema.org address)", org["address"]))
    if org.get("foundingDate"):
        add(_fact("founded", "Founded (website)", org["foundingDate"], org_url, "structured data (schema.org foundingDate)", org["foundingDate"]))
    # Offerings and industries: specific headings and list items on the matching pages.
    labels = {
        "services": ("offering", "Service (website)"),
        "products": ("offering", "Product (website)"),
        "solutions": ("offering", "Solution (website)"),
        "industries": ("industry", "Industry served (website)"),
    }
    per_kind: Counter[str] = Counter()
    name = client_name.strip().lower()
    for page in readable:
        if page.role not in labels or page.content is None:
            continue
        kind, label = labels[page.role]
        blocks = page.content.blocks
        for index, block in enumerate(blocks):
            text = block.text
            short = (block.kind == "heading" and block.level >= 2) or (block.kind == "text" and len(text) <= 90)
            if not short or _GENERIC_HEADINGS.match(text) or text.lower() == name or len(text) < 4 or per_kind[kind] >= 8:
                continue
            following = block.following or (blocks[index + 1].text if index + 1 < len(blocks) else "")
            if not _offering_like(text, following[:120], block.tag):
                continue
            add(_fact(kind, label, text[:160], page.url, f"{page.role} page {'heading' if block.kind == 'heading' else 'list'}", text[:600]))
            per_kind[kind] += 1
    return facts


def _signals(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interpretations: what the site's wording hints at. Each needs validation in the meeting."""
    signals: list[dict[str, Any]] = []
    for key, topic, words in _SIGNALS:
        evidence = []
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + ")", re.IGNORECASE)
        mentions = 0
        for item in content:
            if pattern.search(item["text"]):
                mentions += 1
                if len(evidence) < 2:
                    evidence.append({"source_url": item["source_url"], "excerpt": item["text"][:300]})
        if evidence:
            signals.append(
                {
                    "key": key,
                    "topic": topic,
                    "origin": "AI_INFERENCE",
                    "status": "to_validate",
                    "statement": f"The website mentions activities related to {topic.lower()}; whether this work is a bottleneck is unknown and should be asked.",
                    "mentions": mentions,
                    "evidence": evidence,
                }
            )
    return sorted(signals, key=lambda signal: -signal["mentions"])


def text_for_matching(research: dict[str, Any] | WebsiteResearch) -> str:
    """The researched wording (facts and signals) as one string, for keyword matching."""
    data = research.to_dict(include_content=False) if isinstance(research, WebsiteResearch) else research
    parts = [str(fact.get("value") or "") for fact in data.get("facts") or []]
    parts += [str(signal.get("topic") or "") for signal in data.get("signals") or []]
    return clean_text(" ".join(parts))
