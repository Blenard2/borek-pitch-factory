"""BAP-13: English and German content - the language contract and every generator below the API.

English is the default and must stay exactly as it was; German is generated end to end from the
same structure. Input material may be in either language; facts are never rewritten.
"""

from __future__ import annotations

import copy
import io
import json
import re
import zipfile
from typing import Any

import pytest

from services.framework.discovery_analysis import live
from services.framework.discovery_analysis.fixture import load_library
from services.framework.discovery_analysis.model import validate_discovery_analysis
from services.framework.discovery_analysis.pipeline import generate_discovery_analysis
from services.language import UnsupportedLanguage, normalize_language, stored_language
from services.meeting.discovery_docx import render_discovery1_docx
from tests.unit.framework.test_discovery_analysis import FakeModel

FIXED = {"generated_at": "2026-10-26T09:00:00Z", "document_id": "22222222-2222-4222-8222-222222222222"}
GERMAN_LETTERS = "äöüÄÖÜß"
# Words that would show an English label left in a German document (whole words only).
ENGLISH_LABELS = re.compile(
    r"\b(?:Contents|As of|Technically|Quick win|Strategic|industry benchmark|Working hypothesis|Opportunity map|Discussion basis|"
    r"In short|Key message|Business case footnote)\b"
)


def opportunity(name: str = "Müller Präzisionstechnik", **intake: Any) -> dict[str, Any]:
    return {"id": "11111111-1111-4111-8111-111111111111", "client_name": name, "opportunity_name": "KI", "stage1_intake": intake or None}


def generate(opp: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return generate_discovery_analysis(opp, persist=lambda _paper: None, **FIXED, **kwargs)


def printed(paper: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key not in {"id", "page_id", "source_url", "lang", "type", "layout"}:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for page in paper["page_manifest"]:
        walk(page["content"])
    return out


# --------------------------------------------------------------- the contract


@pytest.mark.parametrize(("value", "expected"), [(None, "en"), ("", "en"), ("  ", "en"), ("en", "en"), ("de", "de"), ("DE", "de"), (" de ", "de")])
def test_a_missing_language_is_english_and_the_two_codes_are_accepted(value: Any, expected: str) -> None:
    assert normalize_language(value) == expected


@pytest.mark.parametrize("value", ["fr", "german", "de-DE", "en-US", "deutsch", 1])
def test_any_other_language_is_refused_not_guessed(value: Any) -> None:
    with pytest.raises(UnsupportedLanguage):
        normalize_language(value)
    with pytest.raises(UnsupportedLanguage):
        generate(opportunity(), language=value)


def test_documents_stored_before_bap13_read_as_english() -> None:
    assert [stored_language(value) for value in (None, "", "en", "de", "xx", "English")] == ["en", "en", "en", "de", "en", "en"]


# --------------------------------------------------------------- Discovery 1


def test_english_stays_the_default_and_unchanged() -> None:
    default = generate(opportunity("Hansa"))
    explicit = generate(opportunity("Hansa"), language="en")
    assert default == explicit and default["language"] == "en"
    cover = default["page_manifest"][0]["content"]
    assert cover["title"] == "AI opportunities for Hansa" and cover["eyebrow"] == "AI & Automation · Discussion basis"
    assert cover["as_of"] == "As of 10/26" and default["page_manifest"][1]["content"]["title"] == "Contents"
    assert all("lang" not in page["content"] for page in default["page_manifest"]), "English pages are built exactly as before"


def test_a_german_analysis_is_german_from_cover_to_conclusion() -> None:
    paper = generate(opportunity(sales_topic_description="Schnellere Angebote aus Kundenzeichnungen"), language="de")
    validate_discovery_analysis(paper)
    assert paper["status"] == "ready" and paper["language"] == "de"
    pages = paper["page_manifest"]
    cover = pages[0]["content"]
    assert cover["title"] == "KI-Potenziale für Müller Präzisionstechnik"
    assert cover["eyebrow"] == "KI & Automatisierung · Diskussionsgrundlage" and cover["as_of"] == "Stand 10/26"
    assert pages[1]["content"]["title"] == "Inhalt"
    assert all(page["content"].get("lang") == "de" for page in pages), "the print renderer uses German labels"
    texts = printed(paper)
    leftovers = [text for text in texts if ENGLISH_LABELS.search(text)]
    assert leftovers == [], leftovers
    joined = " ".join(texts)
    for word in ("Arbeitshypothese", "Branchen-Benchmarks", "keine Zusagen", "Opportunity", "Zielbild"):
        assert word in joined, word
    assert set(GERMAN_LETTERS) & set(joined) >= {"ä", "ö", "ü", "ß"}
    closing = next(page for page in pages if page["type"] == "M9")["content"]
    assert closing["closing_line"] == "Lassen Sie uns die erste Baseline gemeinsam erheben."


def test_english_input_gives_a_german_document_without_changing_the_facts() -> None:
    about = "Contract manufacturer of turned and milled precision parts, founded 1987."
    paper = generate(opportunity("Hansa Präzisionsteile", about_company=about, sales_topic_description="Faster quotes"), language="de")
    facts = {fact["label"]: fact["value"] for fact in paper["analysis"]["research"]["known_facts"]}
    assert facts["Weitere Informationen"] == about and facts["Gesprächsanlass"] == "Faster quotes", "facts stay word for word"
    assert facts["Unternehmensname"] == "Hansa Präzisionsteile"
    assert paper["analysis"]["framing"]["document"]["title"] == "KI-Potenziale für Hansa Präzisionsteile"


def test_german_input_gives_an_english_document_without_changing_the_facts() -> None:
    about = "Familiengeführter Hersteller von Präzisionsteilen für die Medizintechnik."
    paper = generate(opportunity("Müller GmbH", about_company=about), language="en")
    facts = {fact["label"]: fact["value"] for fact in paper["analysis"]["research"]["known_facts"]}
    assert facts["Additional information"] == about
    assert paper["page_manifest"][0]["content"]["title"] == "AI opportunities for Müller GmbH"


def test_the_german_library_mirrors_the_english_one() -> None:
    english, german = load_library("en"), load_library("de")
    assert [area["id"] for area in english["areas"]] == [area["id"] for area in german["areas"]]
    assert [o["id"] for a in english["areas"] for o in a["opportunities"]] == [o["id"] for a in german["areas"] for o in a["opportunities"]]
    assert [w["id"] for w in english["target_workflows"]] == [w["id"] for w in german["target_workflows"]]
    assert german["language"] == "de" and english["language"] == "en"
    for area in german["areas"]:
        assert live.quality_problems("deep_dives", {"opportunities": area["opportunities"]}, language="de") == [], area["id"]


@pytest.mark.parametrize(
    ("question", "measurable"),
    [
        ("Wie viele Tage dauert ein Angebot heute?", True),
        ("Welcher Anteil der Rechnungen wird vollständig geprüft?", True),
        ("Was kostet eine Woche Leerlauf?", True),
        ("Wann erfahren Sie, dass sich ein Auftrag verzögert?", True),
        ("Können Sie die Kosten je Auftrag beziffern?", True),
        ("Kostet Sie das Tage?", False),
        ("Gibt es eine Fehlerquote?", False),
        ("Nutzen Sie Tabellen dafür?", False),
    ],
)
def test_german_questions_count_as_measurable_only_when_they_ask_for_a_figure(question: str, measurable: bool) -> None:
    assert live.measurable_question(question, "de") is measurable


@pytest.mark.parametrize(
    ("text", "human"),
    [
        ("Liest die Anfrage und entwirft das Angebot; der Vertrieb entscheidet über den Preis.", True),
        ("Strukturiert den Fall; die Qualitätssicherung klassifiziert, entscheidet und gibt frei.", True),
        ("Saubere Fälle laufen durch, Ausnahmen gehen mit vollem Kontext an eine Person.", True),
        ("Erstellt die Zolldokumente; die Disponenten prüfen Sonderfälle.", True),
        ("Das System prüft jede Rechnung und gibt saubere Fälle frei.", False),
        ("Die KI entscheidet die Priorität und steuert den Plan.", False),
        ("Liest die Zeichnung, prüft die Maße und sendet das Angebot automatisch.", False),
    ],
)
def test_a_german_future_process_needs_a_person_who_decides(text: str, human: bool) -> None:
    assert live.names_human_step(text, "de") is human


class GermanModel(FakeModel):
    """Answers from the German library and records the system prompts it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.library = load_library("de")
        self.systems: list[str] = []

    def __call__(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.systems.append(system)
        return super().__call__(system, user, schema)

    def answer(self, stage: str) -> dict[str, Any]:
        answer = super().answer(stage)
        if stage == "research":
            answer.update(
                core_thesis="Arbeitshypothese: Angebote und Rechnungen verlieren Tage in Übergaben.",
                overview_context="Der Hebel liegt in der Arbeit rund um die Kernarbeit.",
                overview_key_message="Jede entfallene Übergabe ist Marge.",
                in_short_thesis="Arbeitshypothese: Angebote verlieren Tage in Übergaben.",
                sector_label="Präzisionsteile",
            )
        if stage == "overview":
            for area in answer["areas"]:
                area["business_case"]["value_levers"] = ["Entwürfe in Stunden", "Einheitliche Prüfungen"]
                area["business_case"]["outcome"] = "Kapazität fließt in die Kernarbeit"
                area["business_case"]["impact_ranges"] = [{"metric": "Durchlaufzeit", "range": "−30–50 %", "basis": "industry_benchmark"}]
        if stage == "closing":
            answer = {"headline": "Die Baseline entscheidet", "paragraphs": ["Drei Schritte folgen.", "Quick Wins tragen das Programm."]}
        return answer


def test_live_generation_writes_german_and_keeps_every_rule() -> None:
    model = GermanModel()
    paper = generate(opportunity(about_company="Hersteller von Präzisionsteilen."), complete=model, language="de")
    assert paper["status"] == "ready" and paper["generation"]["mode"] == "live"
    assert model.systems and all(system == live.SYSTEM_PROMPT_DE for system in model.systems)
    assert "OUTPUT LANGUAGE: German" in live.SYSTEM_PROMPT_DE and "No web or company research" in live.SYSTEM_PROMPT_DE
    assert "OUTPUT LANGUAGE" not in live.SYSTEM_PROMPT, "English prompts are unchanged"
    framing = paper["analysis"]["framing"]
    assert framing["document"]["eyebrow"] == "Präzisionsteile · Diskussionsgrundlage", "grounded in the company's own description"
    assert framing["leads"]["target_table"].startswith("5 Workflow-Ketten")
    assert "quality_warnings" not in paper["generation"], "the German library meets the German quality bar"


def test_a_german_live_answer_that_leaks_process_talk_is_retried() -> None:
    class Leaking(GermanModel):
        def answer(self, stage: str) -> dict[str, Any]:
            answer = super().answer(stage)
            if stage == "research" and self.calls.count("research") == 1:
                answer["overview_context"] = "Auf Basis der begrenzten Angaben bleibt dieser Abschnitt allgemein."
            return answer

    model = Leaking()
    paper = generate(opportunity(), complete=model, language="de")
    assert model.calls.count("research") == 2 and paper["status"] == "ready"
    assert "begrenzten Angaben" not in " ".join(printed(paper))


def test_the_german_word_file_has_german_labels_and_letters() -> None:
    paper = generate(opportunity(sales_topic_description="Schnellere Angebote für Großkunden"), language="de")
    content = render_discovery1_docx(paper, {"version_number": 2, "status": "approved", "approved_at": "2026-10-26T10:00:00Z"})
    xml = zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml").decode("utf-8")
    styles = zipfile.ZipFile(io.BytesIO(content)).read("word/styles.xml").decode("utf-8")
    for label in ("Kernthese", "Opportunities nach Bereich", "Ziel-Workflows", "Menschliche Entscheidung", "Version 2 · freigegeben"):
        assert label in xml, label
    assert all(letter in xml for letter in "äöüß") and "Großkunden" in xml
    assert 'w:val="de-DE"' in styles, "German spelling and hyphenation"
    english = render_discovery1_docx(generate(opportunity("Hansa")), {"version_number": 1, "status": "approved", "approved_at": None})
    assert "Core thesis" in zipfile.ZipFile(io.BytesIO(english)).read("word/document.xml").decode("utf-8")
    assert "de-DE" not in zipfile.ZipFile(io.BytesIO(english)).read("word/styles.xml").decode("utf-8")


# --------------------------------------------------------------- Master Presentation V1


def approved_row(paper: dict[str, Any]) -> dict[str, Any]:
    return {"id": "33333333-3333-4333-8333-333333333333", "opportunity_id": paper["opportunity_id"], "document_id": paper["document_id"], "status": "approved", "paper_json": paper}


def test_master_presentation_v1_appendix_follows_the_discovery_language() -> None:
    from services.presentation.master_deck.plan import build_master_plan, generation_manifest

    german = generate(opportunity(sales_topic_description="Schnellere Angebote"), language="de")
    plan = build_master_plan(approved_row(german))
    manifest = generation_manifest(approved_row(german))
    assert plan["content_language"] == "de" and plan["lang"] == "EN", "the canonical master stays English"
    assert manifest["language"] == "de" and manifest["master_language"] == "en"
    appendix = [slide["borekSlide"] for slide in plan["slides"] if slide["layoutId"] != "CANONICAL"]
    kickers = [slide.get("kicker") for slide in appendix]
    assert kickers[:4] == ["Anhang · Kundensicht", "Kundenkontext", "Priorisierte Opportunities", "Priorisierte Bereiche"]
    text = json.dumps(appendix, ensure_ascii=False)
    for english in ("Client context", "Priority areas", "Working hypothesis", "Next steps", "Benchmarks, not commitments"):
        assert english not in text, english
    english_plan = build_master_plan(approved_row(generate(opportunity("Hansa"))))
    assert english_plan["content_language"] == "en" and generation_manifest(approved_row(generate(opportunity("Hansa"))))["language"] == "en"


# --------------------------------------------------------------- follow-up email (rule-based writer)


STATICS = {
    "project_name": "Angebotsbeschleunigung",
    "client_short": "Müller",
    "salutation_style": "formal",
    "standard_recipients": [{"email": "d.weber@mueller.example", "first_name": "Dana", "last_name": "Weber", "salutation": "Frau", "kind": "to", "primary": True}],
    "sender_profile": {"name": "Jörg Müller", "role": "Managing Partner", "email": "joerg@borek.example"},
}


def german_package() -> dict[str, Any]:
    finding = lambda text: {"text": text, "source": "transcript"}  # noqa: E731
    return {
        "findings": {
            "requirements": [finding("Angebote müssen innerhalb eines Tages beim Kunden sein.")],
            "challenges": [finding("Die Preisdaten liegen in drei Systemen.")],
            "priorities": [finding("Zuerst soll das Vertriebsteam entlastet werden.")],
            "discussed_solutions": [finding("Ein Assistent, der das Angebot aus der Anfrage vorbereitet.")],
            "opportunities": [],
            "decisions": [finding("Ein Pilot mit zehn Angeboten wird gestartet.")],
            "follow_ups": [finding("Dana Weber schickt den Preisexport bis Freitag.")],
        },
        "meeting_extraction": {"execution_mode": "fixture"},
    }


def test_the_german_email_is_a_german_letter_with_the_statements_in_their_own_words() -> None:
    from services.followup.finalized import content_from_snapshot, render_finalized_lengths
    from services.followup.rendering import followup_content_word_count

    content = content_from_snapshot(german_package(), "de")
    assert "action_owner_unconfirmed" not in content["review_flags"] and "action_date_unconfirmed" not in content["review_flags"]
    lengths, _flags = render_finalized_lengths(content, STATICS, attachment=True)
    for name, draft in lengths.items():
        body = draft["body"]
        assert body.startswith("Sehr geehrte Frau Weber,"), name
        assert "Mit freundlichen Grüßen\nJörg Müller" in body
        assert draft["subject"] == "Angebotsbeschleunigung – Nachbereitung unseres Gesprächs"
        assert draft["word_count"] == followup_content_word_count(body) and draft["word_count"] > 5
        for english in ("Dear", "Best regards", "Thank you", "As next steps", "date to be confirmed", "attached"):
            assert english not in body, (name, english)
    medium = lengths["medium"]["body"]
    assert "Die zentrale Anforderung, wie wir sie verstanden haben: Angebote müssen innerhalb eines Tages beim Kunden sein." in medium, "German nouns keep their capital"
    assert "Anbei finden Sie die Präsentation" in medium
    plain, _ = render_finalized_lengths(content, STATICS, attachment=False)
    assert "Anbei" not in plain["medium"]["body"] and "Anhang" not in plain["medium"]["body"]


def test_an_english_package_still_writes_the_english_email() -> None:
    from services.followup.finalized import content_from_snapshot, render_finalized_lengths

    package = copy.deepcopy(german_package())
    package["findings"] = {key: [{"text": "Quotes must go out within one day.", "source": "transcript"}] if key == "requirements" else [] for key in package["findings"]}
    lengths, _ = render_finalized_lengths(content_from_snapshot(package), {**STATICS, "salutation_style": "informal"}, attachment=True)
    assert lengths["short"]["body"].startswith("Hi Dana,") and "Best regards" in lengths["short"]["body"]


@pytest.mark.parametrize(("salutation", "expected"), [("Herr", "Sehr geehrter Herr Weber,"), ("Mr.", "Sehr geehrter Herr Weber,"), ("Ms", "Sehr geehrte Frau Weber,"), ("Dr.", "Guten Tag Dr. Weber,")])
def test_german_forms_of_address_never_guess_a_gender(salutation: str, expected: str) -> None:
    from services.followup.rendering import greeting

    statics = {**STATICS, "standard_recipients": [{**STATICS["standard_recipients"][0], "salutation": salutation}]}
    assert greeting(statics, "de") == expected
    assert greeting({**statics, "salutation_style": "informal"}, "de") == "Hallo Dana,"
