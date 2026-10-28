"""Post-meeting appendix of Master Presentation V2: planner and validation.

The V2 appendix prepares the SECOND client conversation. It is built from one frozen snapshot
(see ``master_deck/v2.py``) and from nothing else:

* the meeting findings of the approved Discovery 2 - never an excluded one,
* the selected, approved Borek use cases,
* the approved Discovery 2's carried-forward opportunities and open questions (BAP-7), in their
  approved wording; the approved Discovery analysis (Discovery 1) as background for the rest.

Every statement keeps its origin visible. Each slide carries exactly one kind of content, named
in its kicker and explained in its notes:

    client     said in the first meeting (supported by the transcript) and confirmed by the owner;
               it may summarise what was said, so it is never called a quote
    owner      an observation from the owner's personal notes that the owner deliberately shared
               with the client in Discovery 2, or a point the owner wrote or reworded when
               reviewing Discovery 2 - never shown as a client statement. Notes the owner did not
               share never reach the snapshot (``v2.build_snapshot``), so no slide can show them.
    reference  an approved Borek reference use case
    borek      a Borek recommendation, taken from the approved Discovery analysis
    open       a question the approved Discovery 2 lists as still open

Nothing is invented: every finding is rendered in its approved wording and never shortened, figures must occur in the snapshot,
and industry benchmarks from the Discovery analysis stay labelled as benchmarks. The planner is
deterministic and adds a slide only where the snapshot has content for it.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from services.language import stored_language
from services.presentation.borek_deck.engine import engine
from services.presentation.master_deck.appendix import (
    BENCHMARK_NOTE,
    _COUNTER_RE,
    _NUMBER_RE,
    _fits,
    _numbered,
    _opportunity_pool,
    _stage_text,
    _texts,
    AppendixPlanError,
    layout_fit_problems,
)
from services.presentation.master_deck.layouts import LAYOUT_IDS, implemented_layouts, measured_height

# v3: notes-only findings are internal unless shared with the client; no "noted for ourselves" slide.
PLANNER_VERSION = "master-presentation-v2:v3"
PLANNER_LAYOUTS: tuple[str, ...] = ("L06", "L08", "L07")
CATEGORIES = ("requirements", "challenges", "priorities", "opportunities", "discussed_solutions", "decisions", "follow_ups")
CATEGORY_LABEL = {
    "requirements": "Requirement",
    "challenges": "Challenge",
    "priorities": "Priority",
    "opportunities": "Opportunity",
    "discussed_solutions": "Discussed solution",
    "decisions": "Decision",
    "follow_ups": "Follow-up",
}
CATEGORY_CODE = {
    "requirements": "R",
    "challenges": "C",
    "priorities": "P",
    "opportunities": "O",
    "discussed_solutions": "S",
    "decisions": "D",
    "follow_ups": "F",
}
# Findings that occur in the transcript are client statements; "both" was also noted by the owner.
CLIENT_SOURCES = frozenset({"transcript", "both"})
OWNER_SOURCE = "personal_notes"
# Written or reworded by the owner while reviewing Discovery 2 (BAP-7): not a note, not a quote.
REVIEWED_SOURCE = "owner"
OWNER_SOURCES = frozenset({OWNER_SOURCE, REVIEWED_SOURCE})
MAX_OPEN_QUESTIONS = 3
# A recommendation needs this many content words in common with what the client said.
MIN_SHARED_TERMS = 2
MAX_RECOMMENDATIONS = 4
_WORD_RE = re.compile(r"[a-zäöüß]{4,}", re.IGNORECASE)
# Words that say nothing about the subject of a statement. A recommendation must not rest on them.
_STOPWORDS = frozenset(
    "with that this from they them their have will would should could must about into over under than then when what which "
    "your ours ourselves also only more most some such very just need needs want wants start first next before after "
    "every each other another same many much several team teams people company client customer today currently "
    "system systems data process processes work works time times thing things make makes take takes done does".split()
)
_MATRIX_TOP, _MATRIX_BOTTOM = 400.0, 940.0

# BAP-13: German content words that say nothing about the subject of a statement.
_STOPWORDS_DE = frozenset(
    "dass diese dieser dieses diesem diesen eine einen einer eines einem haben hatte hatten werden wird wurde wurden "
    "nicht noch auch mehr sehr nach über unter durch beim sowie oder aber wenn dann heute aktuell derzeit immer alle "
    "jede jeder jedes jeden unser unsere unseren ihre ihren ihrer sein sind kann können müssen muss sollen soll sollte "
    "wollen will möchten zuerst erste ersten nächste nächsten vorher danach andere anderen gleiche viele mehrere team "
    "teams leute unternehmen firma kunde kunden heute system systeme daten prozess prozesse arbeit zeit sache sachen "
    "machen macht nehmen nimmt erledigt bitte damit dafür dabei dort hier etwa weitere weiteren schon wieder".split()
)

# BAP-13: the appendix's own wording per language - the language of the approved Discovery. Codes
# (R, C, P, O, S, D, F, N, B, A, Q) stay as they are: they are structure, not words.
V2_TEXT: dict[str, dict[str, Any]] = {
    "en": {
        "category": CATEGORY_LABEL,
        "named": "the client",
        "divider_kicker": "Appendix · After our first meeting",
        "divider_title": "After our first meeting",
        "divider_text": "What we heard in our first conversation, what we suggest on that basis, and what is still open.",
        "said_title": "Said in the meeting",
        "said_text": "Each point is supported by the transcript of our first conversation and was confirmed for this presentation. It may summarise what was said rather than quote it.",
        "correct_title": "Please correct us",
        "correct_text": "If we misunderstood a point, tell us. This page is the basis for everything that follows.",
        "discussed_title": "Discussed, not decided",
        "discussed_text": "Solutions marked as discussed were mentioned in the conversation. Decisions are listed separately.",
        "meeting_prefix": "From the meeting",
        "kicker_client": "From the meeting · Client statements",
        "heard_title": "What we heard in our first meeting",
        "heard_lead": "The points {named} raised in our first meeting, as we understood them.",
        "needs_title": "What you need and what comes first",
        "needs_lead": "Requirements and priorities as {named} described them in our first meeting.",
        "situation_title": "Where it is difficult today and where you see potential",
        "situation_lead": "Challenges and opportunities as {named} described them in our first meeting.",
        "kicker_discussed": "From the meeting · Solutions discussed",
        "discussed_slide_title": "Solutions we talked about",
        "discussed_lead": "Approaches that came up in our first meeting. They were discussed, not agreed.",
        "our_observation": "Our observation",
        "reviewed_by_us": "Reviewed by us",
        "observations_title": "Our observations",
        "observations_text": "From our own notes after the meeting, not from the transcript. We would like to verify each of them with you.",
        "reviewed_text": "Written or reworded by our team when we reviewed Discovery 2: our reading of the meeting, not a quote from the transcript and not from our notes.",
        "both_text": "'Our observation' is from our own notes; 'Reviewed by us' was written or reworded by our team in Discovery 2. Neither is a quote from the transcript.",
        "references_title": "Borek references",
        "references_text": "From Borek's approved reference library: examples of comparable work, not statements about your company.",
        "not_client": "Not client statements",
        "kicker_borek": "From Borek · Not client statements",
        "add_start_title": "What we add and where we would start",
        "add_start_lead": "Our own {kind} and opportunities from the approved Discovery analysis. Figures are industry benchmarks, not commitments.",
        "kind_observation": "observation",
        "kind_reference": "reference",
        "why_start_title": "Why these starting points",
        "add_title": "What we add from our side",
        "add_lead": "Our own observations after the meeting and approved Borek references. None of this was said by {named}.",
        "observations_prefix": "Our observations",
        "kicker_observations": "Our observations · Not client statements",
        "noted_title": "Our observations after the meeting",
        "noted_lead": "Points the Borek team noted after the meeting and would like to verify with you. They are our reading, not statements by {named}.",
        "not_transcript_title": "Not from the transcript",
        "not_transcript_text": "None of these points was said in this form during the meeting.",
        "kicker_references": "Borek references · Not client statements",
        "ref_slide_title": "Borek references that fit what we heard",
        "ref_lead": "Approved Borek reference use cases selected for this conversation.",
        "use_title": "How we use them",
        "use_text": "As examples of how comparable work was delivered, to be checked against your situation.",
        "borek_reference": "Borek reference",
        "recommendation": "Borek recommendation",
        "rec_title": "Where we would start, given what we heard",
        "rec_lead_d2": "Opportunities carried forward in the approved Discovery 2 that relate to points you raised in the meeting.",
        "rec_lead": "Opportunities from the approved Discovery analysis that relate to points you raised in the meeting.",
        "why_title": "Why these",
        "benchmarks_title": "Benchmarks, not commitments",
        "benchmark_note": BENCHMARK_NOTE,
        "open_question": "Open question",
        "source_d2": "the approved Discovery 2",
        "source_d1": "the Discovery analysis",
        "decisions_title": "Decisions and follow-ups",
        "decisions_text": "Supported by the transcript of our first conversation and confirmed for this presentation.",
        "open_title": "Open questions",
        "open_text": "Questions that are still open in {source}. We would like to clarify them with you.",
        "missing_text": "If a decision or follow-up is missing or wrong, tell us. We will update this page.",
        "kicker_closing_open": "Decisions · Next steps · Open questions",
        "kicker_closing": "From the meeting · Decisions and next steps",
        "agreed_open_title": "What was agreed and what is still open",
        "agreed_title": "What was agreed",
        "closing_lead_both": "Decisions and follow-ups come from the meeting. Open questions come from {source}.",
        "closing_lead_open": "Open questions from {source}.",
        "closing_lead": "Decisions and follow-ups as they were stated in our first meeting.",
        "decided": "Decided in the meeting",
        "next_kicker": "Borek recommendation · Next phase",
        "next_title": "How we suggest to continue",
        "updated_opportunity": "Updated opportunity",
        "evidence_names": {
            "requirements": "requirements", "challenges": "challenges", "priorities": "priorities", "opportunities": "opportunities",
            "discussed_solutions": "solutions we discussed", "decisions": "decisions", "follow_ups": "follow-ups",
        },
        "evidence": "They relate to the {listed} you described in our first meeting. Whether they fit is for you to confirm.",
        "and": " and ",
    },
    "de": {
        "category": {
            "requirements": "Anforderung", "challenges": "Herausforderung", "priorities": "Priorität", "opportunities": "Opportunity",
            "discussed_solutions": "Besprochene Lösung", "decisions": "Entscheidung", "follow_ups": "Nächster Schritt",
        },
        "named": "Ihr Unternehmen",
        "divider_kicker": "Anhang · Nach unserem ersten Gespräch",
        "divider_title": "Nach unserem ersten Gespräch",
        "divider_text": "Was wir in unserem ersten Gespräch gehört haben, was wir darauf aufbauend vorschlagen und was noch offen ist.",
        "said_title": "Im Gespräch gesagt",
        "said_text": "Jeder Punkt ist durch das Transkript unseres ersten Gesprächs belegt und wurde für diese Präsentation bestätigt. Er kann das Gesagte zusammenfassen, statt es wörtlich zu zitieren.",
        "correct_title": "Bitte korrigieren Sie uns",
        "correct_text": "Wenn wir einen Punkt falsch verstanden haben, sagen Sie es uns. Diese Seite ist die Grundlage für alles Weitere.",
        "discussed_title": "Besprochen, nicht entschieden",
        "discussed_text": "Als besprochen markierte Lösungen wurden im Gespräch erwähnt. Entscheidungen stehen gesondert.",
        "meeting_prefix": "Aus dem Gespräch",
        "kicker_client": "Aus dem Gespräch · Aussagen des Kunden",
        "heard_title": "Was wir in unserem ersten Gespräch gehört haben",
        "heard_lead": "Die Punkte, die {named} in unserem ersten Gespräch angesprochen hat, so wie wir sie verstanden haben.",
        "needs_title": "Was Sie brauchen und was zuerst kommt",
        "needs_lead": "Anforderungen und Prioritäten, wie {named} sie in unserem ersten Gespräch beschrieben hat.",
        "situation_title": "Wo es heute schwierig ist und wo Sie Potenzial sehen",
        "situation_lead": "Herausforderungen und Opportunities, wie {named} sie in unserem ersten Gespräch beschrieben hat.",
        "kicker_discussed": "Aus dem Gespräch · Besprochene Lösungen",
        "discussed_slide_title": "Lösungen, über die wir gesprochen haben",
        "discussed_lead": "Ansätze, die in unserem ersten Gespräch aufkamen. Sie wurden besprochen, nicht vereinbart.",
        "our_observation": "Unsere Beobachtung",
        "reviewed_by_us": "Von uns überarbeitet",
        "observations_title": "Unsere Beobachtungen",
        "observations_text": "Aus unseren eigenen Notizen nach dem Gespräch, nicht aus dem Transkript. Wir möchten jede davon mit Ihnen prüfen.",
        "reviewed_text": "Von unserem Team bei der Durchsicht von Discovery 2 formuliert oder umformuliert: unsere Lesart des Gesprächs, kein Zitat aus dem Transkript und nicht aus unseren Notizen.",
        "both_text": "„Unsere Beobachtung“ stammt aus unseren Notizen; „Von uns überarbeitet“ hat unser Team in Discovery 2 formuliert oder umformuliert. Beides ist kein Zitat aus dem Transkript.",
        "references_title": "Borek-Referenzen",
        "references_text": "Aus der freigegebenen Referenzbibliothek von Borek: Beispiele vergleichbarer Arbeit, keine Aussagen über Ihr Unternehmen.",
        "not_client": "Keine Aussagen des Kunden",
        "kicker_borek": "Von Borek · Keine Aussagen des Kunden",
        "add_start_title": "Was wir ergänzen und wo wir beginnen würden",
        "add_start_lead": "Unsere eigene {kind} und Opportunities aus der freigegebenen Discovery-Analyse. Zahlen sind Branchen-Benchmarks, keine Zusagen.",
        "kind_observation": "Beobachtung",
        "kind_reference": "Referenz",
        "why_start_title": "Warum diese Ansatzpunkte",
        "add_title": "Was wir von unserer Seite ergänzen",
        "add_lead": "Unsere eigenen Beobachtungen nach dem Gespräch und freigegebene Borek-Referenzen. Nichts davon hat {named} gesagt.",
        "observations_prefix": "Unsere Beobachtungen",
        "kicker_observations": "Unsere Beobachtungen · Keine Aussagen des Kunden",
        "noted_title": "Unsere Beobachtungen nach dem Gespräch",
        "noted_lead": "Punkte, die das Borek-Team nach dem Gespräch festgehalten hat und gern mit Ihnen prüfen möchte – unsere Lesart, nicht das, was {named} gesagt hat.",
        "not_transcript_title": "Nicht aus dem Transkript",
        "not_transcript_text": "Keiner dieser Punkte wurde in dieser Form im Gespräch gesagt.",
        "kicker_references": "Borek-Referenzen · Keine Aussagen des Kunden",
        "ref_slide_title": "Borek-Referenzen, die zu dem passen, was wir gehört haben",
        "ref_lead": "Freigegebene Borek-Referenzfälle, ausgewählt für dieses Gespräch.",
        "use_title": "Wie wir sie nutzen",
        "use_text": "Als Beispiele, wie vergleichbare Arbeit umgesetzt wurde – zu prüfen an Ihrer Situation.",
        "borek_reference": "Borek-Referenz",
        "recommendation": "Borek-Empfehlung",
        "rec_title": "Wo wir beginnen würden – nach dem, was wir gehört haben",
        "rec_lead_d2": "Opportunities aus der freigegebenen Discovery 2, die sich auf Punkte beziehen, die Sie im Gespräch angesprochen haben.",
        "rec_lead": "Opportunities aus der freigegebenen Discovery-Analyse, die sich auf Punkte beziehen, die Sie im Gespräch angesprochen haben.",
        "why_title": "Warum diese",
        "benchmarks_title": "Benchmarks, keine Zusagen",
        "benchmark_note": "Zahlen sind Branchen-Benchmarks und Arbeitshypothesen aus der freigegebenen Analyse, keine Zusagen.",
        "open_question": "Offene Frage",
        "source_d2": "der freigegebenen Discovery 2",
        "source_d1": "der Discovery-Analyse",
        "decisions_title": "Entscheidungen und nächste Schritte",
        "decisions_text": "Durch das Transkript unseres ersten Gesprächs belegt und für diese Präsentation bestätigt.",
        "open_title": "Offene Fragen",
        "open_text": "Fragen, die in {source} noch offen sind. Wir möchten sie mit Ihnen klären.",
        "missing_text": "Wenn eine Entscheidung oder ein nächster Schritt fehlt oder falsch ist, sagen Sie es uns. Wir aktualisieren diese Seite.",
        "kicker_closing_open": "Entscheidungen · Nächste Schritte · Offene Fragen",
        "kicker_closing": "Aus dem Gespräch · Entscheidungen und nächste Schritte",
        "agreed_open_title": "Was vereinbart wurde und was noch offen ist",
        "agreed_title": "Was vereinbart wurde",
        "closing_lead_both": "Entscheidungen und nächste Schritte stammen aus dem Gespräch, offene Fragen aus {source}.",
        "closing_lead_open": "Offene Fragen aus {source}.",
        "closing_lead": "Entscheidungen und nächste Schritte, wie sie in unserem ersten Gespräch genannt wurden.",
        "decided": "Im Gespräch entschieden",
        "next_kicker": "Borek-Empfehlung · Nächste Phase",
        "next_title": "Wie wir das weitere Vorgehen vorschlagen",
        "updated_opportunity": "Aktualisierte Opportunity",
        "evidence_names": {
            "requirements": "Anforderungen", "challenges": "Herausforderungen", "priorities": "Prioritäten", "opportunities": "Opportunities",
            "discussed_solutions": "besprochenen Lösungen", "decisions": "Entscheidungen", "follow_ups": "nächsten Schritte",
        },
        "evidence": "Sie beziehen sich auf die {listed}, die Sie in unserem ersten Gespräch beschrieben haben. Ob sie passen, bestätigen Sie.",
        "and": " und ",
    },
}


def snapshot_language(snapshot: dict[str, Any]) -> str:
    """The language of a frozen snapshot: recorded since BAP-13, English before."""
    return stored_language(snapshot.get("language"))


def v2_text(snapshot: dict[str, Any]) -> dict[str, Any]:
    return V2_TEXT[snapshot_language(snapshot)]


def confirmed_findings(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Every confirmed finding of the snapshot as {category, text, source}, in category order."""
    return [
        {"category": category, "text": str(item["text"]), "source": str(item["source"])}
        for category in CATEGORIES
        for item in snapshot["findings"].get(category, [])
    ]


def plan_v2_appendix(snapshot: dict[str, Any], *, first_order: int) -> list[dict[str, Any]]:
    """Appendix slides for one frozen post-meeting snapshot, numbered from ``first_order``."""
    if not set(PLANNER_LAYOUTS) <= implemented_layouts():
        raise AppendixPlanError("The V2 appendix planner refers to a layout that is not implemented")
    findings = confirmed_findings(snapshot)
    if not findings:
        raise AppendixPlanError("Master Presentation V2 needs at least one confirmed meeting finding")
    paper = snapshot["approved_discovery"]
    analysis = paper["analysis"]
    client = str(snapshot.get("client_name") or "").strip()
    t = v2_text(snapshot)
    language = snapshot_language(snapshot)
    named = client or t["named"]
    slides: list[tuple[str, str, dict[str, Any], list[str]]] = []
    omitted: list[str] = []

    slides.append(
        (
            "L06",
            "Open the post-meeting appendix",
            {
                "kicker": t["divider_kicker"],
                "title": client if client and _fits(client, 105, 300, 1690, 1, -2.5) else t["divider_title"],
                "text": t["divider_text"],
            },
            ["meeting_review"],
        )
    )

    client_notes = [
        {"title": t["said_title"], "text": t["said_text"]},
        {"title": t["correct_title"], "text": t["correct_text"]},
    ]
    discussed_notes = [
        {"title": t["discussed_title"], "text": t["discussed_text"]},
        client_notes[0],
    ]
    needs = _finding_rows(findings, ("requirements", "priorities"), CLIENT_SOURCES, omitted, labels=t["category"])
    situation = _finding_rows(findings, ("challenges", "opportunities"), CLIENT_SOURCES, omitted, labels=t["category"])
    discussed = _finding_rows(findings, ("discussed_solutions",), CLIENT_SOURCES, omitted, labels=t["category"])
    heard = needs + situation + discussed
    if heard and _fits_one_matrix(heard):
        # A short meeting record reads better on one page than on three nearly empty ones.
        _matrix_slides(
            slides,
            heard,
            kicker=t["kicker_client"],
            title=t["heard_title"],
            lead=t["heard_lead"].format(named=named),
            notes=discussed_notes if discussed else client_notes,
            purpose="Client statements confirmed from the first meeting",
        )
    else:
        _matrix_slides(
            slides,
            needs,
            kicker=t["kicker_client"],
            title=t["needs_title"],
            lead=t["needs_lead"].format(named=named),
            notes=client_notes,
            purpose="Requirements and priorities confirmed in the first meeting",
        )
        _matrix_slides(
            slides,
            situation,
            kicker=t["kicker_client"],
            title=t["situation_title"],
            lead=t["situation_lead"].format(named=named),
            notes=client_notes,
            purpose="Challenges and opportunities confirmed in the first meeting",
        )
        # Solutions that were talked about: discussed, not agreed.
        _matrix_slides(
            slides,
            discussed,
            kicker=t["kicker_discussed"],
            title=t["discussed_slide_title"],
            lead=t["discussed_lead"],
            notes=discussed_notes,
            purpose="Solutions discussed in the first meeting",
        )
    # What Borek adds - none of it a client statement, all of it labelled row by row:
    #   the owner's own observations, the approved references selected for this client, and
    #   Discovery opportunities with clear support in what the client said (the recommendation).
    # Fewer than MIN_SHARED_TERMS shared subject words means no recommendation rather than a weak one.
    noted = _finding_rows(findings, CATEGORIES, frozenset({OWNER_SOURCE}), omitted, prefix="N", title=t["our_observation"])
    reviewed = _finding_rows(findings, CATEGORIES, frozenset({REVIEWED_SOURCE}), omitted, prefix="R", title=t["reviewed_by_us"])
    observations = noted + reviewed
    references = _use_case_rows(snapshot, omitted, title=t["borek_reference"])
    matches = _matching_opportunities(analysis, findings, snapshot.get("approved_discovery2"), language=language)
    recommended = _recommended(matches)
    recommendation_rows = [
        {"code": f"A{position}", "title": match["title"], "text": match["text"], "reference": match["reference"]}
        for position, match in enumerate(recommended, start=1)
    ]
    recommendation_rows = [row for row in recommendation_rows if _row_height(row) <= _MATRIX_BOTTOM - _MATRIX_TOP]
    evidence = sorted({reference for match in recommended for reference in match["evidence"]})
    observation_note = (
        {"title": t["observations_title"], "text": t["observations_text"]}
        if not reviewed
        else {"title": t["reviewed_by_us"], "text": t["reviewed_text"]}
        if not noted
        else {"title": t["observations_title"], "text": t["both_text"]}
    )
    reference_note = {"title": t["references_title"], "text": t["references_text"]}
    own = observations + references
    before = len(slides)
    if len(own) == 1 and recommendation_rows and _fits_one_matrix(own + recommendation_rows):
        # A single observation or reference does not fill a page: it opens the recommendation page.
        _matrix_slides(
            slides,
            own + recommendation_rows,
            kicker=t["kicker_borek"],
            title=t["add_start_title"],
            lead=t["add_start_lead"].format(kind=t["kind_observation"] if observations else t["kind_reference"]),
            notes=[observation_note if observations else reference_note, {"title": t["why_start_title"], "text": _evidence_sentence(evidence, t)}],
            purpose="Borek's own observation or reference and the recommended starting points",
        )
    else:
        if observations and references and _fits_one_matrix(own):
            _matrix_slides(
                slides,
                own,
                kicker=t["kicker_borek"],
                title=t["add_title"],
                lead=t["add_lead"].format(named=named),
                notes=[observation_note, reference_note],
                purpose="Owner observations and selected approved Borek use cases",
            )
        else:
            _matrix_slides(
                slides,
                observations,
                kicker=t["kicker_observations"],
                title=t["noted_title"],
                lead=t["noted_lead"].format(named=named),
                notes=[observation_note, {"title": t["not_transcript_title"], "text": t["not_transcript_text"]}],
                purpose="Owner observations and points reviewed by the owner",
            )
            _matrix_slides(
                slides,
                references,
                kicker=t["kicker_references"],
                title=t["ref_slide_title"],
                lead=t["ref_lead"],
                notes=[reference_note, {"title": t["use_title"], "text": t["use_text"]}],
                purpose="Selected approved Borek use cases",
            )
        before = len(slides)
        _matrix_slides(
            slides,
            recommendation_rows,
            kicker=t["recommendation"],
            title=t["rec_title"],
            lead=t["rec_lead_d2"] if snapshot.get("approved_discovery2") else t["rec_lead"],
            notes=[
                {"title": t["why_title"], "text": _evidence_sentence(evidence, t) if evidence else ""},
                {"title": t["benchmarks_title"], "text": t["benchmark_note"]},
            ],
            purpose="Recommended starting points in light of the meeting",
        )
    # A page with recommendations cites the client statements they rest on.
    for index in range(before, len(slides)):
        layout_id, purpose, content, cited = slides[index]
        if any(row["code"].startswith("A") for row in content["rows"]):
            slides[index] = (layout_id, purpose, content, cited + evidence)

    # What was decided, what follows, and what is still open. Open questions are the Discovery
    # analysis' own questions for the opportunities that relate to the meeting - full questions,
    # never a bare topic label.
    closing_rows = _finding_rows(findings, ("decisions", "follow_ups"), CLIENT_SOURCES, omitted, labels=t["category"])
    questions: list[dict[str, str]] = []
    for text, reference in _open_question_candidates(snapshot, matches):
        row = {"code": f"Q{len(questions) + 1}", "title": t["open_question"], "text": text, "reference": reference}
        if _is_question(text) and text not in [entry["text"] for entry in questions] and _row_height(row) <= _MATRIX_BOTTOM - _MATRIX_TOP:
            questions.append(row)
        if len(questions) == MAX_OPEN_QUESTIONS:
            break
    questions_source = t["source_d2"] if snapshot.get("approved_discovery2") else t["source_d1"]
    closing_notes = [{"title": t["decisions_title"], "text": t["decisions_text"]}]
    closing_notes.append(
        {"title": t["open_title"], "text": t["open_text"].format(source=questions_source)}
        if questions
        else {"title": t["correct_title"], "text": t["missing_text"]}
    )
    _matrix_slides(
        slides,
        closing_rows + questions,
        kicker=t["kicker_closing_open"] if questions else t["kicker_closing"],
        title=t["agreed_open_title"] if questions else t["agreed_title"],
        lead=(
            t["closing_lead_both"].format(source=questions_source)
            if closing_rows and questions
            else t["closing_lead_open"].format(source=questions_source)
            if questions
            else t["closing_lead"]
        ),
        notes=closing_notes,
        purpose="Decisions, follow-ups and open questions",
    )

    # Proposed way forward: a Borek recommendation from the approved analysis.
    closing = analysis["closing"]
    decision = next((item for item in findings if item["category"] == "decisions" and item["source"] in CLIENT_SOURCES), None)
    statement = {"kicker": t["recommendation"], "text": closing["headline"]}
    references_for_closing = ["discovery.closing"]
    if decision and _fits(decision["text"], 25, 500, 1776 - 72, 2):
        statement = {"kicker": t["decided"], "text": decision["text"]}
        references_for_closing.append(_finding_reference(findings, decision))
    slides.append(
        (
            "L07",
            "Proposed approach for the next phase",
            {
                "kicker": t["next_kicker"],
                "title": t["next_title"],
                "lead": analysis["framing"]["leads"]["closing_steps"],
                "stages": [
                    {"number": f"{position:02d}", "title": step["title"], "text": _stage_text(step["text"])}
                    for position, step in enumerate(closing["steps"], start=1)
                ],
                "statement": statement,
            },
            references_for_closing,
        )
    )

    planned = [
        {
            "order": first_order + index,
            "layout_id": layout_id,
            "purpose": purpose,
            "content": {"layout": layout_id, **content},
            "source_references": references,
        }
        for index, (layout_id, purpose, content, references) in enumerate(slides)
    ]
    validate_v2_appendix(planned, snapshot, first_order=first_order, omitted=omitted)
    return planned


# ---------------------------------------------------------------------------------- building blocks


def _finding_reference(findings: list[dict[str, str]], finding: dict[str, str]) -> str:
    position = [item for item in findings if item["category"] == finding["category"]].index(finding)
    return f"finding:{finding['category']}:{position}"


def _row_height(row: dict[str, str]) -> float:
    """Height of one matrix row, exactly as the L08 layout computes it."""
    code_width = engine().bp.text_w(row["code"], 22, 600) + 16
    title_height = measured_height(row["title"], 22, 600, 360 - code_width - 12, 1.3)
    return 20 + max(title_height, measured_height(row["text"], 22, 400, 720, 1.3)) + 20


def _finding_rows(
    findings: list[dict[str, str]],
    categories: tuple[str, ...],
    sources: frozenset[str],
    omitted: list[str],
    *,
    prefix: str | None = None,
    title: str | None = None,
    labels: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Matrix rows for the confirmed findings of the given categories and sources, in their approved wording.

    A finding that cannot fit one slide on its own is left out and reported - it is never cut,
    because a shortened client statement is no longer the statement that was confirmed.
    """
    rows: list[dict[str, str]] = []
    for category in categories:
        position = 0
        for index, item in enumerate(entry for entry in findings if entry["category"] == category):
            if item["source"] not in sources:
                continue
            position += 1
            code = f"{prefix}{len(rows) + 1}" if prefix else f"{CATEGORY_CODE[category]}{position}"
            row = {"code": code, "title": title or (labels or CATEGORY_LABEL)[category], "text": item["text"], "reference": f"finding:{category}:{index}"}
            if _row_height(row) > _MATRIX_BOTTOM - _MATRIX_TOP:
                omitted.append(row["reference"])
                continue
            rows.append(row)
    return rows


def _use_case_rows(snapshot: dict[str, Any], omitted: list[str], *, title: str = "Borek reference") -> list[dict[str, str]]:
    rows = []
    for item in snapshot["use_cases"]:
        row = {
            "code": f"B{len(rows) + 1}",
            "title": title,
            "text": str(item["statement"]),
            "reference": f"use_case:{item['fact_id']}",
        }
        if _row_height(row) > _MATRIX_BOTTOM - _MATRIX_TOP:
            omitted.append(row["reference"])
            continue
        rows.append(row)
    return rows


def _matrix_slides(
    slides: list[tuple[str, str, dict[str, Any], list[str]]],
    rows: list[dict[str, str]],
    *,
    kicker: str,
    title: str,
    lead: str,
    notes: list[dict[str, str]],
    purpose: str,
) -> None:
    """Add as many matrix slides as the rows need: at most six rows and never past the content zone."""
    groups: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    height = 0.0
    for row in rows:
        needed = _row_height(row) + (1 if current else 0)
        if current and (len(current) == 6 or _MATRIX_TOP + height + needed > _MATRIX_BOTTOM):
            groups.append(current)
            current, height, needed = [], 0.0, _row_height(row)
        current.append(row)
        height += needed
    if current:
        groups.append(current)
    for index, group in enumerate(groups):
        slides.append(
            (
                "L08",
                purpose,
                {
                    "kicker": kicker,
                    "title": _numbered(title, index, len(groups)),
                    "lead": lead,
                    "rows": [{"code": row["code"], "title": row["title"], "text": row["text"]} for row in group],
                    "notes": copy.deepcopy(notes),
                },
                [row["reference"] for row in group],
            )
        )


def _fits_one_matrix(rows: list[dict[str, str]]) -> bool:
    return len(rows) <= 6 and _MATRIX_TOP + sum(_row_height(row) for row in rows) + len(rows) - 1 <= _MATRIX_BOTTOM


def _keywords(text: str, language: str = "en") -> set[str]:
    """Content words of a text, reduced to a simple stem so that "quotes" meets "quote"."""
    if language == "de":
        return _keywords_de(text)
    stems = set()
    for word in _WORD_RE.findall(text):
        word = word.lower()
        if word in _STOPWORDS:
            continue
        if word.endswith("ies") and len(word) > 5:
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith("ss") and len(word) > 4:
            word = word[:-1]
        for suffix in ("ing", "ed"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        stems.add(word)
    return stems - _STOPWORDS


def _keywords_de(text: str) -> set[str]:
    """German content words with a simple stem, so that "Angebote" meets "Angebot"."""
    stems = set()
    for word in _WORD_RE.findall(text):
        word = word.lower()
        if word in _STOPWORDS_DE:
            continue
        for suffix in ("ungen", "en", "er", "es", "e", "n", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        stems.add(word)
    return stems - _STOPWORDS_DE


def _opportunity_candidates(analysis: dict[str, Any], discovery2: dict[str, Any] | None, language: str = "en") -> list[dict[str, Any]]:
    """The opportunities V2 may recommend, as {title, text, words, reference, questions}.

    With an approved Discovery 2 (BAP-7) only the opportunities it carries forward, in its
    approved wording: an opportunity the owner removed is not offered, and one the owner
    reworded is matched and shown with the new wording only - the old wording never reappears.
    Snapshots frozen before Discovery 2 use every opportunity of the Discovery analysis.
    """
    content = (discovery2 or {}).get("content")
    if not isinstance(content, dict):
        return [
            {
                "title": item["title"],
                "text": item["result"],
                "words": _keywords(" ".join([item["title"], item["result"], item["opportunity_signal"]]), language),
                "reference": f"discovery.opportunity:{area['id']}/{item['id']}",
                "questions": list(item.get("discovery_questions") or []),
            }
            for area, item in _opportunity_pool(analysis)
        ]
    carried = {str(entry.get("discovery1_ref")): entry for entry in content.get("opportunities") or []}
    candidates = []
    # Discovery 1's order, so an unedited Discovery 2 recommends exactly what Discovery 1 would.
    for area, original in _opportunity_pool(analysis):
        entry = carried.get(f"area:{area['id']}/opportunity:{original['id']}")
        if entry is None:
            continue  # removed in Discovery 2: not offered
        if entry.get("edited"):
            title, text, words = V2_TEXT[language]["updated_opportunity"], str(entry["text"]), _keywords(str(entry["text"]), language)
        else:
            title, text = original["title"], original["result"]
            words = _keywords(" ".join([original["title"], original["result"], original["opportunity_signal"]]), language)
        candidates.append({
            "title": title, "text": text, "words": words, "reference": f"discovery2.item:{entry['id']}", "questions": [],
            "discovery1_ref": str(entry.get("discovery1_ref") or ""),
        })
    return candidates


def _open_question_candidates(snapshot: dict[str, Any], matches: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(question, reference) in the order they are offered."""
    content = (snapshot.get("approved_discovery2") or {}).get("content")
    if isinstance(content, dict):
        # The questions Discovery 2 keeps open for the opportunities the meeting relates to, then
        # the questions raised in the meeting or added by the owner - all in the approved wording.
        items = list(content.get("open_questions") or [])
        chosen: list[dict[str, Any]] = []
        for match in matches:
            prefix = f"{match.get('discovery1_ref')}/" if match.get("discovery1_ref") else None
            chosen.extend(item for item in items if prefix and str(item.get("discovery1_ref") or "").startswith(prefix) and item not in chosen)
        chosen.extend(item for item in items if item.get("origin") != "discovery1" and item not in chosen)
        return [(str(item["text"]).strip(), f"discovery2.item:{item['id']}") for item in chosen]
    return [(str(question).strip(), match["reference"]) for match in matches for question in match["questions"]]


def _is_question(text: str) -> bool:
    return text.endswith("?") and len(text.split()) >= 5


def _matching_opportunities(
    analysis: dict[str, Any], findings: list[dict[str, str]], discovery2: dict[str, Any] | None = None, *, language: str = "en"
) -> list[dict[str, Any]]:
    """Opportunities clearly supported by what the client said, best supported first.

    Only client statements count: an owner observation never steers a recommendation. Every
    returned opportunity shares at least one subject word with the client's statements and is
    therefore related; it is recommended only with ``MIN_SHARED_TERMS`` or more (see
    ``_recommended``) - one shared word is a coincidence, not a reason. Each match names the
    statements it rests on, so a recommendation can show its evidence.
    """
    heard = [
        (reference, _keywords(item["text"], language))
        for reference, item in ((_finding_reference(findings, item), item) for item in findings)
        if item["source"] in CLIENT_SOURCES
    ]
    scored = []
    for position, candidate in enumerate(_opportunity_candidates(analysis, discovery2, language)):
        words = candidate["words"]
        shared = set().union(*(terms & words for _reference, terms in heard), set())
        if not shared:
            continue
        evidence = [reference for reference, terms in heard if terms & words]
        scored.append((-len(shared), position, {**candidate, "evidence": evidence, "shared_terms": sorted(shared)}))
    return [match for _score, _position, match in sorted(scored, key=lambda entry: entry[:2])]


def _recommended(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The matches with enough support to be recommended, at most ``MAX_RECOMMENDATIONS``."""
    return [match for match in matches if len(match["shared_terms"]) >= MIN_SHARED_TERMS][:MAX_RECOMMENDATIONS]


def _evidence_sentence(references: list[str], text: dict[str, Any] | None = None) -> str:
    """Which kinds of client statements a recommendation rests on, in plain words."""
    text = text or V2_TEXT["en"]
    names = text["evidence_names"]
    kinds = [names[category] for category in CATEGORIES if any(reference.startswith(f"finding:{category}:") for reference in references)]
    listed = kinds[0] if len(kinds) == 1 else ", ".join(kinds[:-1]) + text["and"] + kinds[-1]
    return text["evidence"].format(listed=listed)


# ---------------------------------------------------------------------------------- validation


def resolve_v2_reference(snapshot: dict[str, Any], reference: str) -> Any:
    """The part of the frozen snapshot a reference points at; ``None`` when it does not exist."""
    analysis = snapshot["approved_discovery"]["analysis"]
    kind, _, ref = reference.partition(":")
    if kind == "meeting_review":
        return snapshot["meeting_review"]
    if kind == "finding":
        category, _, position = ref.partition(":")
        items = snapshot["findings"].get(category, [])
        return items[int(position)] if position.isdigit() and int(position) < len(items) else None
    if kind == "use_case":
        return next((item for item in snapshot["use_cases"] if item["fact_id"] == ref), None)
    if kind == "discovery.closing":
        return analysis["closing"]
    if kind == "discovery2.item":
        content = (snapshot.get("approved_discovery2") or {}).get("content") or {}
        return next((item for items in content.values() for item in items if item.get("id") == ref), None)
    if kind == "discovery.opportunity":
        area_id, _, opportunity_id = ref.partition("/")
        area = next((item for item in analysis["areas"] if item["id"] == area_id), None)
        return next((item for item in (area or {}).get("opportunities", []) if item["id"] == opportunity_id), None)
    return None


def shown_finding_references(slides: list[dict[str, Any]], snapshot: dict[str, Any]) -> set[str]:
    """References of the confirmed findings whose text is actually on a slide."""
    on_slides = {str(row.get("text")) for slide in slides for row in slide.get("content", {}).get("rows", [])}
    return {
        f"finding:{category}:{position}"
        for category in CATEGORIES
        for position, item in enumerate(snapshot["findings"].get(category, []))
        if str(item["text"]) in on_slides
    }


def validate_v2_appendix(
    slides: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    first_order: int,
    omitted: list[str] | None = None,
) -> None:
    """Planning contract, source integrity, provenance separation and layout rules."""
    problems: list[str] = []
    t = v2_text(snapshot)
    findings = confirmed_findings(snapshot)
    client_texts = {item["text"] for item in findings if item["source"] in CLIENT_SOURCES}
    owner_texts = {item["text"] for item in findings if item["source"] in OWNER_SOURCES}
    reference_texts = {str(item["statement"]) for item in snapshot["use_cases"]}
    excluded = {str(text) for text in snapshot.get("excluded_findings", [])} - {item["text"] for item in findings}
    source_numbers = set(
        _NUMBER_RE.findall(
            json.dumps(
                {
                    "findings": snapshot["findings"],
                    "use_cases": snapshot["use_cases"],
                    "analysis": snapshot["approved_discovery"]["analysis"],
                    "brief": snapshot["approved_discovery"]["presentation_brief"],
                    "discovery2": (snapshot.get("approved_discovery2") or {}).get("content"),
                    "client": snapshot.get("client_name"),
                },
                ensure_ascii=False,
            )
        )
    )
    used_references: set[str] = set()
    if not slides:
        problems.append("the appendix has no slides")
    for index, slide in enumerate(slides):
        where = f"appendix slide {index + 1}"
        missing = [key for key in ("order", "layout_id", "purpose", "content", "source_references") if key not in slide]
        if missing:
            problems.append(f"{where} lacks {', '.join(missing)}")
            continue
        layout_id = str(slide["layout_id"])
        if slide["order"] != first_order + index:
            problems.append(f"{where} has order {slide['order']}, expected {first_order + index}")
        if layout_id not in LAYOUT_IDS or layout_id not in implemented_layouts():
            problems.append(f"{where} uses '{layout_id}', which is not an implemented master layout")
            continue
        if slide["content"].get("layout") != layout_id:
            problems.append(f"{where}: content does not belong to layout {layout_id}")
        references = slide["source_references"]
        if not isinstance(references, list) or not references:
            problems.append(f"{where} cites no source in the frozen snapshot")
            references = []
        for reference in references:
            if resolve_v2_reference(snapshot, str(reference)) is None:
                problems.append(f"{where} cites '{reference}', which is not part of the frozen snapshot")
        used_references.update(str(reference) for reference in references)
        texts = _texts(slide["content"])
        if excluded & set(texts):
            problems.append(f"{where} uses a finding the owner excluded")
        invented = sorted({n for text in texts for n in _NUMBER_RE.findall(_COUNTER_RE.sub("", text))} - source_numbers)
        if invented:
            problems.append(f"{where} states figures that are not in the frozen sources: {', '.join(invented)}")
        # One kind of content per slide: an owner observation never sits on a client-statement page.
        kicker = str(slide["content"].get("kicker") or "")
        row_texts = {str(row.get("text")) for row in slide["content"].get("rows", [])}
        if kicker.startswith(t["meeting_prefix"]) and row_texts & (owner_texts - client_texts):
            problems.append(f"{where} presents an owner observation as a client statement")
        if kicker.startswith(t["observations_prefix"]) and row_texts - owner_texts:
            problems.append(f"{where} mixes other content into the owner observations")
        if t["not_client"] in kicker and row_texts & (client_texts - owner_texts - reference_texts):
            problems.append(f"{where} presents a client statement as Borek's own content")
        recommends = any(str(row.get("code", "")).startswith("A") for row in slide["content"].get("rows", []))
        if (kicker == t["recommendation"] or recommends) and not any(str(reference).startswith("finding:") for reference in references):
            problems.append(f"{where} recommends without naming the client statements it rests on")
        for row in slide["content"].get("rows", []):
            if row.get("title") == t["open_question"] and (not str(row.get("text")).strip().endswith("?") or len(str(row.get("text")).split()) < 5):
                problems.append(f"{where} lists a topic label as an open question: '{row.get('text')}'")
    # Every confirmed finding is either shown or reported as omitted - none disappears silently.
    # "Shown" means its text is on a slide: being cited as evidence for a recommendation is not enough.
    shown = shown_finding_references(slides, snapshot)
    for category in CATEGORIES:
        for position, _item in enumerate(snapshot["findings"].get(category, [])):
            reference = f"finding:{category}:{position}"
            if reference not in shown and reference not in (omitted or []):
                problems.append(f"confirmed finding {reference} is neither shown nor reported as omitted")
    if not problems:
        problems.extend(layout_fit_problems(slides))
    if problems:
        raise AppendixPlanError("; ".join(problems))
