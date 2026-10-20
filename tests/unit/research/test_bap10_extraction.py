"""BAP-10: cleaner website text and offerings - found on real company websites.

* Words hyphenated with soft hyphens ("Informa&shy;tions&shy;technik") stay whole.
* Job listings, captions, company names with their legal form, team cards, location lists and
  section labels are not offerings; real services and products are kept.
* A careers page whose link reads "Stellenangebote" is not a services page.
"""

from __future__ import annotations

import pytest

from services.research.website.extract import clean_text, extract_page
from services.research.website.research import _role, research_website
from tests.fixtures.website_fixtures import FixtureWeb

SHY = "­"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (f"Informa{SHY}tions{SHY}{SHY}tech{SHY}nik", "Informationstechnik"),
        ("Elektro​technik", "Elektrotechnik"),  # zero-width space
        ("Prozess⁠optimierung", "Prozessoptimierung"),  # word joiner
        ("Sicherheits‌technik", "Sicherheitstechnik"),  # zero-width non-joiner
        ("﻿Startseite", "Startseite"),  # byte order mark
        # Real separators stay separators.
        ("IT Services", "IT Services"),
        ("Line\nbreak", "Line break"),
        ("Tab\tseparated", "Tab separated"),
        ("Paragraph separator", "Paragraph separator"),
        ("Control\x07character", "Control character"),
        ("Elektro- und Informationstechnik", "Elektro- und Informationstechnik"),
    ],
)
def test_invisible_format_characters_keep_words_whole(raw: str, clean: str) -> None:
    assert clean_text(raw) == clean


def test_soft_hyphenated_headings_are_extracted_whole() -> None:
    page = extract_page(f"<html><body><main><h2>Informa{SHY}tions{SHY}tech{SHY}nik</h2><h2>Elektro{SHY}technik</h2></main></body></html>", "https://x.test/")
    assert [block.text for block in page.blocks] == ["Informationstechnik", "Elektrotechnik"]


@pytest.mark.parametrize(
    ("url", "label", "role"),
    [
        ("https://x.test/karriere/stellenangebote/", "Stellenangebote", "careers"),
        ("https://x.test/jobs/", "Unsere Angebote", "careers"),  # the address decides first
        ("https://x.test/leistungen/", "Leistungen", "services"),
        ("https://x.test/de/seite-3/", "Unser Angebot", "services"),  # a whole word still matches
        ("https://x.test/p/123", "Produkte", "products"),
    ],
)
def test_page_roles_follow_the_address_and_whole_words(url: str, label: str, role: str) -> None:
    found = _role(url, label)
    assert found is not None and found[0] == role


SERVICES = f"""<html><body><main>
<h1>Leistungen</h1>
<h2>Elektro{SHY}technik</h2><p>Planung und Installation elektrischer Anlagen für Gewerbe und Industrie.</p>
<h2>Informa{SHY}tions{SHY}technik</h2><p>Netzwerke, Server und Kommunikationstechnik aus einer Hand.</p>
<h2>Sicherheitstechnik</h2><p>Brandmelde-, Einbruchmelde- und Zutrittskontrollanlagen.</p>
<ul><li>Gebäudeautomation für Bürogebäude</li><li>Auszubildende, Seelze, Muster GmbH &amp; Co. KG</li><li>Elektroniker für Energie- und Gebäudetechnik (m/w/d)</li></ul>
<figure><img src="/team.jpg" alt=""><figcaption>Unser Team bei der Montage in Hannover</figcaption></figure>
<h3>Alle Standorte</h3>
<h3>Muster Sicherheitstechnik GmbH</h3>
<h3>Anna Beispiel</h3><p>Leiterin Vertrieb</p>
<h3>Die wichtigsten Kompetenzen der Gruppe</h3>
<h3>Jetzt bewerben</h3>
</main></body></html>"""


def test_offerings_keep_services_and_drop_jobs_captions_names_and_labels() -> None:
    home = (
        "<html><head><meta name='description' content='Muster Gruppe: Elektro-, Informations- und Sicherheitstechnik für Gewerbe und Industrie.'></head>"
        "<body><a href='/leistungen/'>Leistungen</a> <a href='/karriere/stellenangebote/'>Stellenangebote</a>"
        "<p>Die Muster Gruppe plant, installiert und wartet technische Anlagen für Gewerbe und Industrie.</p></body></html>"
    )
    jobs = "<html><body><main><h1>Stellenangebote</h1><h2>Projektleiter Elektrotechnik</h2><ul><li>Auszubildende Elektroniker (m/w/d), Hannover</li></ul></main></body></html>"
    web = FixtureWeb(sites={"muster-gruppe.test": {"/": home, "/leistungen/": SERVICES, "/karriere/stellenangebote/": jobs, "/robots.txt": None}})
    with web.fetcher() as fetcher:
        result = research_website("https://muster-gruppe.test", fetcher=fetcher)
    roles = {page.url.rsplit(".test", 1)[1]: page.role for page in result.pages}
    assert roles["/karriere/stellenangebote/"] == "careers"
    offerings = [fact["value"] for fact in result.facts if fact["kind"] == "offering"]
    assert offerings[:3] == ["Elektrotechnik", "Informationstechnik", "Sicherheitstechnik"]
    assert "Gebäudeautomation für Bürogebäude" in offerings, "a real service in a list is kept"
    for noise in ("Auszubildende", "m/w/d", "Unser Team", "Alle Standorte", "GmbH", "Anna Beispiel", "Leiterin", "Kompetenzen", "bewerben", "Projektleiter"):
        assert not any(noise in value for value in offerings), noise


def test_long_statements_are_clipped_at_sentence_or_word_boundaries() -> None:
    from services.research.website.research import _clip

    about = (
        "Wir sind bundesweit erfolgreich und stark vor Ort. Die Gruppe ist ein in vierter Generation geführtes "
        "Familienunternehmen, das seit 1948 konsequent und doch behutsam gewachsen ist. Wir sind mit dem Erfolg unserer "
        "Kunden gewachsen. Viele von ihnen halten uns schon seit Jahrzehnten die Treue. Und wir beweisen ihnen, wie man "
        "in der Elektro-, Informations- und Sicherheitstechnik immer Schritt hält."
    )
    assert len(about) > 300
    clipped = _clip(about, 300)
    assert clipped.endswith("Jahrzehnten die Treue.") and clipped in about, "whole sentences, verbatim"
    words = _clip("Netzwerke Server Kommunikationstechnik " * 20, 50)
    assert words == "Netzwerke Server Kommunikationstechnik Netzwerke", "no word is cut"
    assert _clip("Short statement.", 400) == "Short statement."


def test_a_refused_robots_txt_is_explained_in_one_readable_sentence() -> None:
    from tests.fixtures.website_fixtures import HANSA_HOME, Response

    web = FixtureWeb(sites={"site.test": {"/robots.txt": Response(403, "forbidden", {"content-type": "text/plain"}), "/": HANSA_HOME}})
    with web.fetcher() as fetcher:
        result = research_website("https://site.test", fetcher=fetcher)
    assert result.errors[0]["message"] == "The website refused access to its robots.txt (HTTP 403), so automated reading is treated as not allowed."
