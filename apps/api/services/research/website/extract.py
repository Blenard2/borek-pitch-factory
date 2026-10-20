"""The business content of one HTML page: metadata, organisation data, headings and text.

Scripts, styles, navigation, headers, footers, forms, cookie and consent banners, hidden
elements and link lists are removed. What is left is kept as short text blocks in page order,
each with its kind (heading or text), so facts can be quoted verbatim with their source.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml import etree, html

_DROP_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
    "form", "button", "input", "select", "textarea", "nav", "header", "footer", "aside", "dialog",
    "video", "audio", "picture", "map",
)
_DROP_ROLES = {"navigation", "banner", "contentinfo", "dialog", "alertdialog", "search", "menu", "menubar"}
_BOILERPLATE_HINT = re.compile(
    r"(?:^|[-_\s])(?:cookie|cookies|consent|gdpr|cmp|onetrust|usercentrics|borlabs|newsletter|popup|modal|"
    r"breadcrumbs?|social-(?:links|icons|media)|share-buttons|sharing|skip-link|offcanvas|navbar|nav|menu|megamenu|sidebar|footer|"
    r"site-header|topbar|login|signin|search)(?:$|[-_\s])",
    re.IGNORECASE,
)
_HEADINGS = {"h1", "h2", "h3", "h4"}
_BLOCKS = _HEADINGS | {"p", "li", "dd", "dt", "blockquote", "figcaption", "td", "summary"}
_SPACE = re.compile(r"\s+")
_ORG_TYPES = {
    "organization", "corporation", "localbusiness", "store", "professionalservice", "manufacturer",
    "ngo", "educationalorganization", "medicalorganization", "onlinebusiness", "company",
}


@dataclass
class Block:
    kind: str  # "heading" | "text"
    text: str
    level: int = 0  # heading level, 0 for text
    following: str = ""  # headings: the text right after the heading, however short (e.g. a job title)
    tag: str = ""  # the HTML element the text comes from (a figcaption is a caption, not an offering)


@dataclass
class PageContent:
    url: str
    language: str | None = None
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    organization: dict[str, str] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, anchor text)
    requires_javascript: bool = False

    @property
    def text_chars(self) -> int:
        return sum(len(block.text) for block in self.blocks)


def clean_text(value: Any) -> str:
    """Visible text: whitespace collapsed, control characters removed.

    Invisible format characters inside a word - soft hyphens ("Informa&shy;tions&shy;technik"),
    zero-width spaces and joiners, word joiners, byte order marks - are dropped, so the word stays
    whole. Every other unprintable character (control characters, line and paragraph
    separators, non-breaking and other spaces) still separates words.
    """
    text = "".join(
        "" if unicodedata.category(char) == "Cf" else char if char.isprintable() or char in "\t\n " else " "
        for char in str(value or "")
    )
    return _SPACE.sub(" ", text).strip()


def extract_page(markup: str, url: str) -> PageContent:
    page = PageContent(url=url)
    try:
        root = html.document_fromstring(markup)
    except (etree.ParserError, ValueError):
        return page
    page.language = (root.get("lang") or "").split("-", 1)[0].lower() or None
    page.title = _first_text(root.xpath("//head/title"))
    page.description = _meta(root, "description") or _meta(root, "og:description")
    page.site_name = _meta(root, "og:site_name")
    page.organization = _organization(root)
    # Links before boilerplate is removed: the menu is where About, Services and Products live.
    page.links = _links(root, url)
    noscript = " ".join(clean_text(node.text_content()) for node in root.iter("noscript"))
    script_count = sum(1 for _ in root.iter("script"))
    app_shell = bool(root.xpath("//*[@id='root' or @id='app' or @id='__next' or @id='__nuxt' or @ng-version or @data-reactroot]"))
    _strip_boilerplate(root)
    _separate_words(root)
    body = root.find("body")
    page.blocks = _blocks(body if body is not None else root)
    visible = page.text_chars
    page.requires_javascript = visible < 250 and (
        "javascript" in noscript.lower() or app_shell or script_count >= 5
    )
    return page


def _meta(root: Any, name: str) -> str | None:
    for node in root.xpath("//meta[@name=$n or @property=$n]", n=name):
        value = clean_text(node.get("content"))
        if value:
            return value
    return None


def _first_text(nodes: list[Any]) -> str | None:
    for node in nodes:
        value = clean_text(node.text_content())
        if value:
            return value
    return None


def _links(root: Any, base: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for anchor in root.iter("a"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urljoin(base, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        canonical = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", "", ""))
        if canonical in seen:
            continue
        seen.add(canonical)
        label = clean_text(anchor.text_content()) or clean_text(anchor.get("title") or anchor.get("aria-label"))
        links.append((canonical, label[:120]))
        if len(links) >= 400:
            break
    return links


def _strip_boilerplate(root: Any) -> None:
    doomed = []
    for node in root.iter():
        if not isinstance(node.tag, str):  # comments and processing instructions
            doomed.append(node)
            continue
        tag = node.tag.lower()
        if tag in _DROP_TAGS:
            doomed.append(node)
            continue
        if tag in ("html", "body", "main", "article"):
            continue
        role = (node.get("role") or "").lower()
        style = (node.get("style") or "").replace(" ", "").lower()
        hint = f"{node.get('id') or ''} {node.get('class') or ''}"
        if (
            role in _DROP_ROLES
            or node.get("hidden") is not None
            or node.get("aria-hidden") == "true"
            or "display:none" in style
            or "visibility:hidden" in style
            or _BOILERPLATE_HINT.search(hint)
        ):
            doomed.append(node)
    for node in doomed:
        if node.getparent() is not None:
            node.drop_tree()  # keeps the text that follows the element


_SEPARATED = {"br", "div", "p", "li", "td", "th", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "figcaption", "tr"}


def _separate_words(root: Any) -> None:
    """A line break or block boundary separates words ("End-to-End<br>Development")."""
    for node in root.iter(*_SEPARATED):
        node.tail = " " + (node.tail or "")
        if node.tag == "br":
            continue
        node.text = " " + (node.text or "")


def _blocks(container: Any) -> list[Block]:
    blocks: list[Block] = []
    seen: set[str] = set()
    for node in container.iter(*_BLOCKS):
        tag = node.tag.lower()
        # The innermost block holds the text; an outer list item or cell would repeat it.
        if tag not in _HEADINGS and any(child.tag in _BLOCKS for child in node.iterdescendants() if isinstance(child.tag, str)):
            continue
        text = clean_text(node.text_content())
        if not text or text.lower() in seen:
            continue
        link_chars = sum(len(clean_text(anchor.text_content())) for anchor in node.iter("a"))
        if tag in _HEADINGS:
            if len(text) < 3 or len(text) > 200:
                continue
            sibling = node.getnext()
            following = clean_text(sibling.text_content())[:120] if sibling is not None and isinstance(sibling.tag, str) else ""
            blocks.append(Block("heading", text, int(tag[1]), following, tag))
        else:
            minimum = 25 if tag in ("li", "dd", "dt", "td") else 40
            if len(text) < minimum or link_chars > 0.6 * len(text):
                continue
            blocks.append(Block("text", text[:1200], tag=tag))
        seen.add(text.lower())
    return blocks


def _organization(root: Any) -> dict[str, str]:
    """Organisation data the site publishes as JSON-LD (schema.org): name, description, address..."""
    found: dict[str, str] = {}
    for script in root.xpath("//script[@type='application/ld+json']"):
        try:
            data = json.loads(script.text or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk(data):
            types = item.get("@type")
            names = {str(value).lower() for value in (types if isinstance(types, list) else [types]) if value}
            if not names & _ORG_TYPES:
                continue
            for key in ("name", "legalName", "description", "slogan", "foundingDate"):
                value = clean_text(item.get(key)) if isinstance(item.get(key), str) else ""
                if value and key not in found:
                    found[key] = value[:600]
            address = item.get("address")
            if isinstance(address, list) and address:
                address = address[0]
            if isinstance(address, dict) and "address" not in found:
                parts = [
                    clean_text(address.get(key))
                    for key in ("streetAddress", "postalCode", "addressLocality", "addressRegion", "addressCountry")
                    if isinstance(address.get(key), str) and clean_text(address.get(key))
                ]
                if parts:
                    found["address"] = ", ".join(parts)[:300]
            elif isinstance(address, str) and clean_text(address) and "address" not in found:
                found["address"] = clean_text(address)[:300]
    return found


def _walk(data: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(data, list):
        return [item for entry in data[:50] for item in _walk(entry, depth + 1)]
    if isinstance(data, dict):
        found = [data]
        for key in ("@graph", "publisher", "provider", "author", "brand", "parentOrganization"):
            if key in data:
                found.extend(_walk(data[key], depth + 1))
        return found
    return []
