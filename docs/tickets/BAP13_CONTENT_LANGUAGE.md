# BAP-13 — English and German document and email generation (backend)

**Status:** implementation for PR review. Not PM acceptance.
**Base:** `origin/test` @ `cc3b806` · branch `feat/BAP-13-german-content-generation`.

## Language model

* One contract for every generated artifact: `en` or `de` (`services/language.py`). Missing means English; anything else is refused (HTTP 422), never guessed.
* **Discovery 1 sets the language** — from the request, else the opportunity's `language`, else English. An explicit choice is stored as the opportunity's `language`, so Post Meeting follows it.
* **Everything after inherits it from the approved version it is built from:**

  | Artifact | Language source | Different explicit language |
  | --- | --- | --- |
  | Discovery 1 | request → opportunity → `en` | — (sets the journey language) |
  | Master Presentation V1 | approved Discovery 1 | 409 `LANGUAGE_MISMATCH` |
  | Discovery 2 | approved Discovery 1 | 409 `LANGUAGE_MISMATCH` |
  | Master Presentation V2 | approved Discovery 2 (= Discovery 1) | 409 `LANGUAGE_MISMATCH` |
  | Follow-up email (Master journey) | finalized V2 package | 409 `LANGUAGE_MISMATCH` |
  | Follow-up email (earlier flows) | English only | 409 `LANGUAGE_UNSUPPORTED` for `de` |

* **Changing the language** = generate Discovery 1 again in the other language (a new version, new document id), approve it, then regenerate downstream. Approved versions are never changed; their language is stored with them. A mismatch is refused rather than producing a document in two languages, because carried-forward items and confirmed findings are used in their own words.
* **Input material in either language** is accepted. Generated and fixed texts follow the selected language; user-supplied facts, quoted website statements, confirmed meeting findings and names are kept verbatim.

## API contract for BAP-12

All bodies are optional; omitting `language` keeps today's behaviour.

| Endpoint | Body | Response carries |
| --- | --- | --- |
| `POST /opportunities` · `PATCH /opportunities/{id}` | `language: "en" \| "de"` (default `en`) | `language` |
| `POST /opportunities/{id}/discovery-paper/generate` | `{ "language"?: "en" \| "de", roles_employees?, decision_map?, system_interfaces? }` | `language` (paper) |
| `GET …/discovery-paper/versions/{vid}` | — | `paper_json.language` |
| `POST /opportunities/{id}/stage1-outputs/generate` · `…/stage1-outputs/presentation/regenerate` | `{ "language"?: … }` | version `generation_source_manifest.language` (appendix) and `.master_language` (canonical slides) |
| `POST /opportunities/{id}/post-meeting-discovery/generate` | `{ "language"?: … }` | `document.language`, every version `language` |
| `POST /opportunities/{id}/master-presentation/v2/generate` | `{ "language"?: … }` | version manifest `language`, `master_language`; plan `content_language` |
| `POST /opportunities/{id}/email-drafts/generate` | `{ journey_stage, overwrite_edits?, "language"?: … }` | `draft.source.language` |

Errors: `422` invalid language · `409 LANGUAGE_MISMATCH` (explicit language ≠ approved source) · `409 LANGUAGE_UNSUPPORTED` (German on an English-only email flow). Ownership, workflow readiness, approval and finalization checks are unchanged and run as before (404 for another user's opportunity, 401 without a token).

Suggested UI for BAP-12: one language selector on the client (PATCH `language`) or on the Discovery generate action; downstream actions need no selector and should show the inherited language read from the responses above.

## Coverage per artifact

| Artifact | German output | Notes |
| --- | --- | --- |
| Discovery 1 | **Complete** — fixture library (`library_de.json`), live prompts, fixed texts, page labels, dates, PDF print labels, Word export | Website quotes stay as published (often English); labels around them are German |
| Discovery 2 | **Complete** — section titles, summary, Word export, live prompt | Carried items keep Discovery 1's (German) wording; confirmed findings keep the words they were confirmed in |
| Master Presentation V1 | **Appendix only** | **Blocker:** the 26 canonical slides are the immutable English master `borek_ai_tech_en_v1`; no German master exists in the repository or on file. Manifests record `language` (appendix) and `master_language: "en"` |
| Master Presentation V2 | **Appendix only** | Same canonical-slide blocker. Appendix, provenance labels and recommendation matching (German stop words) are German |
| Follow-up email | **Complete for the Master journey (rule-based writer)** | The AI writer stays English-only: its grounding checks rely on English (commitments, dates, capitalised names); a German draft is written by the rules and records `AI_WRITER_LANGUAGE_UNSUPPORTED`. Earlier email flows are English-only and refuse `de` |

## Persistence

No migration. The language lives in the stored JSON: `discovery_paper_versions.paper_json.language` (already in the schema), `discovery2_versions.document_json.language` (new optional property), presentation version `generation_source_manifest.language`/`master_language`, V2 snapshot `language`, email draft `source.language` (new optional property). Anything stored before BAP-13 reads as English.

## Remaining before full Jira acceptance

1. A German canonical master deck (or a PM decision that the canonical slides stay English) — needed for a fully German Master Presentation V1/V2.
2. German grounding checks for the AI email writer, if AI-written German emails are wanted.
3. BAP-12: the language selector in the UI.
4. A paid live-model run in German (prompts are covered by deterministic mocks only) and PM language review of real German output.
