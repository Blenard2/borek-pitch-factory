"""JJ-35: PPT #2 planning keeps BT-46 sources separate and writes them into slides."""

from __future__ import annotations

from services.presentation.post_meeting import (
    deterministic_post_meeting_plan,
    plan_post_meeting_from_context,
    ppt2_generation_manifest,
)
from services.presentation.post_meeting_slide_content import build_post_meeting_slide_specs

NOTE = "Revision A owner observation. The price is EUR 500."
REQUIREMENT = "Keep slotting manual"


def _context() -> dict:
    return {
        "source_kind": "ppt2_context",
        "approved_discovery": {
            "version_id": "11111111-1111-4111-8111-111111111111",
            "pages": [
                {
                    "key": key,
                    "order": index,
                    "title": key,
                    "content": {"client_name": "Harbor Mills"} if key == "cover" else {"text": f"{key} for Harbor Mills"},
                }
                for index, key in enumerate(
                    (
                        "cover",
                        "client_context",
                        "opportunity",
                        "borek_approach",
                        "relevant_use_case",
                        "pilot_proposal",
                        "next_steps",
                    ),
                    start=1,
                )
            ],
        },
        "personal_notes": {"text": NOTE, "updated_at": "2026-01-01T00:00:00Z"},
        "meeting_extraction": {
            "transcript_id": "22222222-2222-4222-8222-222222222222",
            "generated_at": "2026-01-02T00:00:00Z",
            "personal_notes_updated_at": "2026-01-01T00:00:00Z",
            "extraction": {
                "generated_at": "2026-01-02T00:00:00Z",
                "requirements": [REQUIREMENT],
                "challenges": [],
                "priorities": [],
                "opportunities": [],
                "discussed_solutions": [],
                "decisions": [],
                "follow_ups": [],
                "item_sources": {
                    "requirements": ["transcript"], "challenges": [], "priorities": [], "opportunities": [],
                    "discussed_solutions": [], "decisions": [], "follow_ups": [],
                },
            },
        },
        "selected_use_cases": {
            "use_case_ids": ["reference.warehouse.delivery-pattern", "reference.invoice-3way.delivery-pattern"],
            "use_cases": [
                {
                    "fact_id": "reference.warehouse.delivery-pattern",
                    "statement": "Warehouse pattern statement",
                    "status": "resolved",
                },
                {
                    "fact_id": "reference.invoice-3way.delivery-pattern",
                    "statement": "Invoice pattern statement",
                    "status": "resolved",
                },
            ],
        },
        "source_priority": [
            "approved_discovery",
            "personal_notes",
            "meeting_extraction",
            "selected_use_cases",
        ],
        "notes_revision_matches_extraction": False,
        "missing_sources": [],
        "warnings": [{"code": "MEETING_EXTRACTION_NOTES_STALE"}],
    }


class _Spy:
    def __init__(self) -> None:
        self.seen = None

    def complete_planning(self, **kwargs):
        self.seen = kwargs["planning_input"]["ppt2Context"]
        return deterministic_post_meeting_plan(self.seen)


def test_planner_receives_distinct_sources_and_allows_more_than_eight_slides() -> None:
    source = _context()
    planner = _Spy()
    plan = plan_post_meeting_from_context(source, planner=planner)
    seen = planner.seen
    assert seen["source_priority"] == source["source_priority"]
    # Audit 2026-10-27 (BUG-02): the owner's notes are internal; the planner never receives them.
    assert seen["personal_notes"] is None and NOTE not in str(seen)
    assert seen["meeting_extraction"]["extraction"]["requirements"] == [REQUIREMENT]
    assert seen["selected_use_cases"]["use_case_ids"] == source["selected_use_cases"]["use_case_ids"]
    assert seen["approved_discovery"]["pages"][0]["content"]["client_name"] == "Harbor Mills"
    assert seen["warnings"] == [{"code": "MEETING_EXTRACTION_NOTES_STALE"}]
    dumped = plan.model_dump(mode="json")
    assert len(dumped["slides"]) == 9
    families = [slide["frameworkReferences"][0].split(".", 1)[0] for slide in dumped["slides"]]
    assert families[:7] == ["discovery"] * 7
    assert all("notes" not in slide["frameworkReferences"] for slide in dumped["slides"])
    assert dumped["slides"][7]["frameworkReferences"] == ["meeting.requirements"]
    assert dumped["slides"][8]["frameworkReferences"] == [
        "use_case.reference.warehouse.delivery-pattern",
        "use_case.reference.invoice-3way.delivery-pattern",
    ]


def test_slide_text_comes_from_each_source_and_pricing_is_not_banned() -> None:
    source = _context()
    source["meeting_extraction"]["extraction"]["requirements"].append("The budget is EUR 500 per month.")
    source["meeting_extraction"]["extraction"]["item_sources"]["requirements"].append("both")
    specs = build_post_meeting_slide_specs(deterministic_post_meeting_plan(source), source)
    rendered = __import__("json").dumps(specs)
    assert specs[0]["title"] == "Harbor Mills"
    assert specs[0]["sourceChapterIds"] == ["discovery.cover"]
    # The owner's notes are internal (audit 2026-10-27, BUG-02): no notes slide, no notes text.
    assert not [spec for spec in specs if spec["sourceChapterIds"] == ["notes"]]
    assert "Revision A owner observation" not in rendered
    assert "EUR 500" in rendered, "pricing said in the meeting is not banned"
    extraction = next(spec for spec in specs if spec["layoutId"] == "REQUIREMENTS_MATRIX_01")
    assert extraction["sourceChapterIds"] == ["meeting.requirements"]
    assert REQUIREMENT in rendered
    use_cases = next(spec for spec in specs if spec["layoutId"] == "ARCHITECTURE_01")
    assert use_cases["components"][0]["description"] == "Warehouse pattern statement"
    assert use_cases["components"][1]["description"] == "Invoice pattern statement"


def test_missing_optional_sources_still_plan_and_manifest_has_no_bodies() -> None:
    source = _context()
    source["personal_notes"] = None
    source["meeting_extraction"] = None
    source["selected_use_cases"] = {"use_case_ids": [], "use_cases": []}
    source["missing_sources"] = ["personal_notes", "meeting_extraction"]
    source["warnings"] = [{"code": "MEETING_EXTRACTION_MISSING"}]
    source["notes_revision_matches_extraction"] = "not_applicable"
    plan = deterministic_post_meeting_plan(source)
    assert len(plan["slides"]) == 7
    assert all(ref.startswith("discovery.") for slide in plan["slides"] for ref in slide["frameworkReferences"])
    context = {
        "sources": {
            "approved_discovery": {
                "status": "available",
                "version_id": source["approved_discovery"]["version_id"],
            },
            "personal_notes": {"status": "missing", "text": None, "updated_at": None},
            "meeting_extraction": {
                "status": "missing",
                "transcript_id": None,
                "personal_notes_updated_at": None,
                "extraction": None,
            },
            "selected_use_cases": {"status": "empty", "use_case_ids": [], "use_cases": []},
        }
    }
    manifest = ppt2_generation_manifest(context)
    assert manifest["transcript_id"] is None
    assert manifest["selected_use_case_ids"] == []
    assert manifest["kind"] == "ppt2"
    blob = __import__("json").dumps(manifest)
    assert "Harbor Mills" not in blob
    assert NOTE not in blob
    assert "paper_json" not in manifest
