# BAP-9 Phase 2 — Reference-based Discovery content calibration

**Status:** engineering change for PR review. Not PM acceptance.
**Base:** `origin/test` @ `3ce520a` · branch `feat/BAP-9-phase2-reference-calibration`.

## Inputs

| Input | Status |
| --- | --- |
| `Quotient Sciences – AI Opportunity Analysis – EN_2 (2).pdf` (28 pages, English, pharma CRDMO) | Reviewed in full; local copy only, not committed |
| `Schnellecke – KI-Potenzialanalyse – DE 1.pdf` (28 pages, German, automotive contract logistics) | Reviewed in full; local copy only, not committed |
| `BAP9_Phase2_Calibration_Brief.md` (page map, quality contract, rubric) | Used as the specification |
| Original system-generated and approved Wahl & CO documents | **Not supplied** — no Wahl & CO before/after comparison was made |

The two PDFs are quality examples from other companies. Nothing in them is used as a fact about any other company, and no reference text, opportunity list or layout was copied into code or prompts.

## What the references do (shared rules, not their content)

| Section (reference pages) | Common rule, generalised |
| --- | --- |
| Cover, contents (1–2) | An industry label in the eyebrow and running head; the in-short thesis names the business's economic lever; a disclaimer that figures are benchmarks and people keep decisions. |
| Overview (3–5) | Company paragraph from stated facts, then the economics of the business (what is sold, what constrains it, where days or margin are lost). Areas follow the company's own value chain (program lifecycle / contract lifecycle), named in the industry's vocabulary. Business case ordered by payback, figures framed as benchmarks. |
| Deep dives (6–20) | Per opportunity: one concrete step or document; solution naming its inputs; the mechanism; the future process ending with the role that decides, approves or releases; an operational result; 2–3 questions answerable with a number, duration, share or cost; a conditional signal. The German reference adds a "today" hypothesis per item; the English one does not. |
| Processes outside the systems (21–23) | 8 industry-specific judgement calls (scarce asset conflicts, customer commitments, exceptions), how they are typically steered, AI preparation with the person deciding; the chapter intro names those calls in the industry's terms. |
| Target picture (24–27) | 5–6 chains named after the business's value streams, built from part-2 blocks, each ending at a consequential human gate; a learning loop; maturity and measurability. |
| Conclusion (28) | Baseline on named metrics from the questions, a two-opportunity pilot, no promises. |

Industry specificity is the decisive difference between the two references: the same skeleton carries entirely different areas, vocabulary, economics and human gates (GxP release vs. price and staffing decisions).

## Baseline (origin/test @ 3ce520a)

Five cases in fixture mode, inputs stored with the outputs (scratch evaluation, not committed):

| Case | Inputs | Pages |
| --- | --- | --- |
| Manufacturing, website research on | Hansa Präzisionsteile (fictional CNC parts maker, repo website fixture) + meeting purpose | 34 |
| Logistics, website research on | Nordlicht Logistik (fictional e-commerce fulfilment company, repo website fixture) + meeting purpose | 33 |
| Manufacturing, research off | user description only | 32 |
| Research failed | website answers 403 | 32 |
| Limited input | company name only | 32 |

**Measured gap:** after replacing the company names, **96 %** of the printed text of the manufacturing and the logistics document is identical. Only the quoted website background differs. Fixture mode is industry-neutral by design (the library may not carry customer or reference content), and production cannot run in fixture mode (`RUNTIME_PROFILE=production` requires `AI_EXECUTION_MODE=live`). Customer documents therefore depend on the live prompts and on the fixed texts that live mode keeps.

No live baseline was generated: it needs a paid model call, which was not authorised. The live gaps below come from the prompts and from which texts live mode leaves to the library.

## Gap matrix

| Dimension | Reference (pages) | Baseline output | Change | Remaining gap |
| --- | --- | --- | --- | --- |
| Industry and commercial thesis | Q 2–3, S 2–3: thesis names the industry's economics; eyebrow names the industry | Live: thesis prompt asks only "where it loses time and margin". Eyebrow and in-short thesis are fixed library text ("AI & Automation · Discussion basis", "the lever lies less in the core work …") in every document | System prompt requires the industry's vocabulary and economics and rejects text that works for any industry; research stage writes the in-short thesis and an industry label, kept only when every word is backed by the user's context or the website material | Needs a live run to judge |
| Area and opportunity taxonomy | Q 4, S 4: 7 areas along the client's lifecycle, concrete step titles | Live prompt: "areas along the value chain"; generic titles possible. Fixture: 7 generic departments | Overview task: value chain from winning work to getting paid, industry names, titles naming a step/document/decision; code flags capability titles ("AI analytics") and repeated titles and retries once | Fixture stays generic (by design) |
| Technical solution and future process | Q 7–20, S 7–20: inputs, mechanism, human role at the end | Live prompt: "solution (what is built), ai_technology (the mechanism)". Fixture: 12 of 24 items had no human step | Deep-dive task names inputs, mechanism types and the deciding role; code checks the human step; the 12 library items now end with the role that decides | — |
| Business case and impact framing | Q 5, S 5: ranges as benchmarks, payback horizon | Already framed (footnote, `basis`, figure check) | Ranges only, no precise single values; abbreviations spelt out | — |
| Discovery questions and signals | every deep dive | Live prompt asked for "numbers, durations or how it is done today"; signal could repeat the printed "Opportunity if:" label | Questions must be answerable from the company's own operations, ≥1 measurable, no yes/no; signal is a condition without the label; all checked in code | — |
| Shadow processes | Q 21–23, S 21–23: industry judgement calls and roles | Live rows were model-written, but the chapter intro stayed library text about "which order gets the contested slot" in every industry | Shadow stage writes the chapter intro and key message; rows name the decision and who makes it | — |
| Target chains and human gates | Q 25–27, S 25–27 | Live chapter intro stayed "the inquiry becomes the quote, the quote the order …"; blocks not tied to part-2 opportunities | Target stage writes the chapter intro and key message, names chains after value streams, uses part-2 opportunities as blocks (checked), ends with a learning loop and consequential gates | — |
| Writing quality | throughout | Phase 1 protections in place | Repetition rule; duplicate results/signals/processes flagged; Phase 1 internal-language check covers the new fields | Tone needs PM review |
| Honest sourcing | Q 2, 5; S 2, 5 | Phase 1 + BAP-5/10 rules | Today's situation only as a typical pattern or a question; the industry label is never guessed (grounding check); figure check covers the new chapter texts | — |

## Implementation boundary kept

* No schema, layout, page-type, page-limit or count change (24-opportunity and 28-page counts in the references are descriptive). The only new limit is a bound on the cover eyebrow (52 characters), which now can carry an industry label.
* No "today" field: the English reference has none, and a new field would change the contract, layout and stored documents. The deep-dive task asks for today's situation through the questions instead.
* English only; the German reference informed structure and quality, not language.
* Stored and approved documents are not touched; the layout is unchanged, so stored page manifests stay valid.
* Presentation V1 takes areas, opportunities and gates, as before.
* Website research stays opt-in (`WEBSITE_RESEARCH_ENABLED` default false).

## Quality bar in code (`live.quality_problems`)

Checked per model answer; a miss is retried once with the reasons. It is a quality bar, not a contract: if the retry fails, the first valid answer is used and generation never fails because of it. What the used answer still misses is logged and stored in `generation.quality_warnings` (optional field, never printed, at most 40 entries). Checks: thesis framing; repeated or capability-only titles; ≥1 measurable question per opportunity (a quantity asked for, or an open question about cost or time — yes/no questions do not count); a human decision in the future process (the deciding subject must be a person, role, team or function, not the AI or the system); no "Opportunity if" in the signal; no duplicate results, signals or processes; chain blocks taken from the part-2 opportunities (learning loop exempt). The fixture library passes all checks.

PR #12 review: the first version accepted automation verbs without a person ("the system approves clean cases"), yes/no questions containing "cost" or "days", and kept no record of answers still below the bar. All three were reproduced in tests and fixed; two library texts that named no decider (pricing check, shift hand-over) now do.

## Validation

* Unit tests: `tests/unit/framework/test_bap9_phase2_calibration.py` (quality checks, retry and fallback, industry label grounding, chapter texts, figure and internal-language checks on the new fields, V1 hand-off).
* Fixture and model-mock runs of both sectors, rendered with the production print code: no page overflows (12 documents plus a maximum-length stress case); the longest eyebrow fits the running head on one line.
* Mock runs show that the new fields reach the printed pages. They do not show what a real model writes.

## Outstanding

1. **Live run (paid, needs permission):** two sectors, 12 model calls each (up to 24 with retries), to score the rubric (target ≥ 14/16, zero invented facts, zero leakage, zero overflow).
2. **PM review** of real generated documents against the references.
3. **Wahl & CO:** original and approved documents, and the company's identity, are needed for the Jira before/after criterion — or a PM-approved change to the acceptance criteria.
4. Optional product decision: industry packs for fixture mode (local and demo only), and whether a "today" hypothesis per opportunity is wanted.
