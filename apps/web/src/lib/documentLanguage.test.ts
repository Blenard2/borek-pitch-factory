import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync as readFileFromDisk } from "node:fs";

import { ApiRequestError, ensureBackendOpportunityId, generateDiscoveryPaper, generatePpt2, generateStage1Outputs, regeneratePpt2, regenerateStage1Presentation } from "./api";
import {
  DEFAULT_DOCUMENT_LANGUAGE, DOCUMENT_LANGUAGES, deckLanguageSummary, documentLanguageErrorMessage, documentLanguageLabel, downstreamLanguageGate,
  emailLanguageRemedy, failedLookup, finalizedPackageLanguage, languageChangeMessage, languageChangeMode, languageChangeNotice,
  loadApprovedDiscovery1Language, lookupFrom, normalizeDocumentLanguage, otherLanguageReviewText, saveDocumentLanguage, sourceLanguageGate,
} from "./documentLanguage";
import { parseIntakeDraft } from "./intakeDraft";
import { generateAndAwaitMasterV2 } from "./masterPresentationV2";
import { loadLiveOpportunity } from "./opportunityResolution";
import { approvedDiscovery2Language, generatePostMeetingDiscovery, parsePostMeetingDiscovery } from "./postMeetingDiscovery";
import { generatePostMeetingEmail, parsePostMeetingEmail } from "./postMeetingEmail";
import { versionLabel } from "./presentationLive";
import { parsePreviewJourney } from "./previewJourney";

// Sources are compared as text below. A Windows checkout may hold them with CRLF line ends.
const readFileSync = (path: URL | string): string => readFileFromDisk(path, "utf8").replace(/\r\n/g, "\n");
const source = (relative: string) => readFileSync(new URL(relative, import.meta.url));

const backendId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const otherBackendId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const localId = "opp-acme-1a2b3c4d";

interface Call { path: string; method: string; body: unknown }

/** Runs ``body`` with a fake browser (owner, backend id map) and a fetch that records every call. */
async function withApi<T>(respond: (call: Call) => unknown, body: (calls: Call[]) => Promise<T>, map: Record<string, string> = {}): Promise<T> {
  const originalFetch = globalThis.fetch;
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalSession = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
  delete process.env.NEXT_PUBLIC_SUPABASE_URL;
  const stored: Record<string, string> = {};
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    localStorage: {
      getItem: (key: string) => stored[key] ?? JSON.stringify(map),
      setItem: (key: string, value: string) => { stored[key] = value; },
    },
  } });
  Object.defineProperty(globalThis, "sessionStorage", { configurable: true, value: {
    getItem: (key: string) => key === "borek.authUserId" ? "test-owner" : null,
  } });
  const calls: Call[] = [];
  globalThis.fetch = async (input, init) => {
    const call = { path: new URL(String(input)).pathname, method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined };
    calls.push(call);
    const answer = respond(call);
    if (answer instanceof Response) return answer;
    return new Response(JSON.stringify(answer), { status: 200 });
  };
  try {
    return await body(calls);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow); else Reflect.deleteProperty(globalThis, "window");
    if (originalSession) Object.defineProperty(globalThis, "sessionStorage", originalSession); else Reflect.deleteProperty(globalThis, "sessionStorage");
    if (supabaseUrl !== undefined) process.env.NEXT_PUBLIC_SUPABASE_URL = supabaseUrl;
  }
}

const refused = (status: number, code: string, message: string) =>
  new Response(JSON.stringify({ error: { code, message } }), { status });

// ------------------------------------------------------------------------------------ basics

test("English is the default and the only fallback; German is offered as Deutsch", () => {
  assert.equal(DEFAULT_DOCUMENT_LANGUAGE, "en");
  assert.deepEqual(DOCUMENT_LANGUAGES.map((option) => [option.code, option.label]), [["en", "English"], ["de", "Deutsch"]]);
  for (const value of [undefined, null, "", "fr", 42, "EN", "english"]) assert.equal(normalizeDocumentLanguage(value), "en", String(value));
  for (const value of ["de", "DE", "de-DE", "German", "deutsch"]) assert.equal(normalizeDocumentLanguage(value), "de", value);
  assert.equal(documentLanguageLabel("de"), "Deutsch");
  assert.equal(documentLanguageLabel(undefined), "English");
});

test("a preview change is never presented as saved; only a server opportunity is saved", () => {
  assert.equal(languageChangeMode(false, backendId), "preview", "preview mode never sends, even for a server id");
  assert.equal(languageChangeMode(true, "opp-unmapped-1234"), "pending", "a live client not yet on the server");
  assert.equal(languageChangeMode(true, backendId), "server");
  assert.match(languageChangeMessage("preview", "de"), /^Preview only: Deutsch .*not saved to the server/);
  assert.doesNotMatch(languageChangeMessage("preview", "de"), /\bsaved:/);
  assert.match(languageChangeMessage("pending", "de"), /saved with this client when its first Discovery is generated/);
  assert.match(languageChangeMessage("server", "de"), /saved: Deutsch\. Nothing was generated or changed\./);
});

// ------------------------------------------------------------------------- persistence (API)

test("a new client is created on the server with the chosen language", async () => {
  await withApi(() => ({ id: backendId }), async (calls) => {
    const created = await ensureBackendOpportunityId("test-token", localId, { company_name: "Müller & Söhne GmbH" }, "de");
    assert.equal(created, backendId);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].path, "/opportunities");
    const sent = calls[0].body as Record<string, unknown>;
    assert.equal(sent.language, "de");
    assert.equal(sent.client_name, "Müller & Söhne GmbH", "German characters are sent as they are");
  });
  // Without a choice the payload stays as before, so the server default (English) applies.
  await withApi(() => ({ id: backendId }), async (calls) => {
    await ensureBackendOpportunityId("test-token", "opp-other-1a2b3c4d", { company_name: "Acme" });
    assert.equal("language" in (calls[0].body as Record<string, unknown>), false);
  });
});

test("changing the language saves only that opportunity's setting and generates nothing", async () => {
  await withApi(() => ({ id: backendId, language: "de" }), async (calls) => {
    assert.equal(await saveDocumentLanguage("test-token", localId, "de"), "de");
    assert.deepEqual(calls, [{ path: `/opportunities/${backendId}`, method: "PATCH", body: { language: "de" } }]);
  }, { [localId]: backendId });
  // Another client keeps its own setting: the request goes to that client only.
  await withApi(() => ({ id: otherBackendId, language: "en" }), async (calls) => {
    assert.equal(await saveDocumentLanguage("test-token", otherBackendId, "en"), "en");
    assert.equal(calls[0].path, `/opportunities/${otherBackendId}`);
  });
  // An answer for another opportunity is refused, not stored.
  await withApi(() => ({ id: otherBackendId, language: "de" }), async () => {
    await assert.rejects(saveDocumentLanguage("test-token", backendId, "de"), (error) => error instanceof ApiRequestError && error.code === "OPPORTUNITY_IDENTITY_MISMATCH");
  });
  // A client that is not on the server yet is never created just to save the language.
  await withApi(() => assert.fail("no request"), async (calls) => {
    await assert.rejects(saveDocumentLanguage("test-token", "opp-unmapped-1234", "de"), (error) => error instanceof ApiRequestError && error.code === "OPPORTUNITY_NOT_SYNCED");
    assert.equal(calls.length, 0);
  });
});

test("the server's language is restored on every load (refresh, reopen, Pre/Post switch)", async () => {
  const row = (language: unknown) => ({ id: backendId, client_name: "Müller AG", opportunity_name: "KI", stage1_intake: {}, ...(language === undefined ? {} : { language }) });
  for (const [stored, expected] of [["de", "de"], ["en", "en"], [undefined, "en"]] as const) {
    await withApi((call) => call.path.endsWith("/workflow-status") ? { current_status: "client_information", steps: [] } : row(stored), async () => {
      const loaded = await loadLiveOpportunity("test-token", localId, backendId);
      assert.equal(loaded.document_language, expected, String(stored));
      assert.equal(loaded.client.values.company_name, "Müller AG");
    });
  }
});

test("browser-held records keep each client's own language; older records read as English", () => {
  const record = (id: string, language?: string) => ({
    opportunity_id: id, created_at: "", updated_at: "", client: { opportunity_id: id, revision: 1, source: "preview", values: {} },
    discovery: { document_state: "draft", revision: 1 }, presentation: { state: "waiting" }, workflow: {},
    ...(language ? { document_language: language } : {}),
  });
  const parsed = parsePreviewJourney({ schema_version: "1.0", opportunities: { a: record("a", "de"), b: record("b"), c: record("c", "xx") } });
  assert.equal(parsed.opportunities.a.document_language, "de");
  assert.equal(parsed.opportunities.b.document_language, "en");
  assert.equal(parsed.opportunities.c.document_language, "en");
});

test("the create-flow draft keeps the chosen language, and drafts without one are unchanged", () => {
  const values = { company_name: "Acme", contact_person: "", website_url: "", meeting_purpose: "", additional_information: "" };
  const extras = { company_logo_name: "", business_industry: "", contact_phone: "", contact_position: "", pitch_notes: "", pitch_file_names: [], additional_opportunity_information: "" };
  assert.equal(parseIntakeDraft({ step: 1, values, extras, document_language: "de" })?.document_language, "de");
  assert.equal("document_language" in (parseIntakeDraft({ step: 1, values, extras }) ?? {}), false);
});

// ---------------------------------------------------------------------- generation payloads

test("every generate call names the language when given, and sends nothing extra when not", async () => {
  await withApi(() => ({ status: "ready" }), async (calls) => {
    await generateDiscoveryPaper("test-token", backendId, null, "de");
    await generateDiscoveryPaper("test-token", backendId, null);
    await generateStage1Outputs("test-token", backendId, "de");
    await regenerateStage1Presentation("test-token", backendId, "de");
    await regenerateStage1Presentation("test-token", backendId);
    assert.deepEqual(calls.map((call) => [call.path.replace(`/opportunities/${backendId}`, ""), call.body]), [
      ["/discovery-paper/generate", { language: "de" }],
      ["/discovery-paper/generate", undefined],
      ["/stage1-outputs/generate", { language: "de" }],
      ["/stage1-outputs/presentation/regenerate", { language: "de" }],
      ["/stage1-outputs/presentation/regenerate", undefined],
    ]);
  });
  // The first Discovery of a new client creates it with the language and generates in it.
  await withApi((call) => call.path === "/opportunities" ? { id: backendId } : { status: "generating" }, async (calls) => {
    await generateDiscoveryPaper("test-token", localId, { company_name: "Acme" }, "de");
    assert.deepEqual(calls.map((call) => [call.method, call.path, (call.body as { language?: string }).language]), [
      ["POST", "/opportunities", "de"],
      ["POST", `/opportunities/${backendId}/discovery-paper/generate`, "de"],
    ]);
  });
});

const d2Envelope = (language: string | undefined, approved = true) => ({
  opportunity_id: backendId,
  state: { status: approved ? "approved" : "draft", stale_reasons: [], approved_version: approved ? { version_id: "v-1", version_number: 1, approved_at: null } : null },
  document: { ...(language ? { language } : {}), status: "ready", sections: [], title: "Discovery 2", client_name: "Acme" },
  current_version: { id: "v-1", version_number: 1, status: approved ? "approved" : "draft" },
  versions: [], input_blockers: [], finalized: false, ready_for_v2: approved,
  language_check: { language: language ?? "en", other_language_item_ids: ["m-requirements-1"] },
});

test("post-meeting payloads: Discovery 2 and V2 name the language; the email inherits the package's", async () => {
  await withApi(() => d2Envelope("de"), async (calls) => {
    await generatePostMeetingDiscovery("test-token", backendId, "de");
    await generatePostMeetingDiscovery("test-token", backendId);
    assert.deepEqual(calls.map((call) => call.body), [{ language: "de" }, undefined]);
  });
  await withApi((call) => call.method === "POST"
    ? { presentation_id: otherBackendId, product_version: "V2", status: "ready", job_id: null, presentation_version_id: null, is_existing_job: false, is_existing_version: false }
    : { opportunity_id: backendId, product_version: "V2", presentation_id: otherBackendId, state: "ready", can_generate: true, ready_for_v2: true, blockers: [], finalized: false, latest_ready: { presentation_version_id: "x", version_number: 2, generation_fingerprint: null }, job: null },
  async (calls) => {
    await generateAndAwaitMasterV2("test-token", backendId, undefined, undefined, "de");
    assert.deepEqual(calls[0].body, { language: "de" });
  });
  await withApi(() => Response.json({ opportunity_id: backendId, journey_stage: "deepening", draft: null }), async (calls) => {
    await assert.rejects(generatePostMeetingEmail("test-token", backendId, false));
    assert.deepEqual(calls[0].body, { journey_stage: "deepening", overwrite_edits: false }, "no language: the finalized package decides");
  });
});

// ------------------------------------------------------------------- safe language changes

test("a downstream artifact is not generated against an approved source in another language", () => {
  const english = { language: "en" as const, versionNumber: 3 };
  const gate = downstreamLanguageGate("de", english, { artifact: "Master Presentation V1", source: "Discovery" });
  assert.equal(gate.blocked, true);
  assert.equal(gate.language, "en", "the artifact would be in its source's language");
  assert.match(gate.message!, /approved Discovery \(version 3\) is in English/);
  assert.match(gate.message!, /Generate a new Discovery in Deutsch and approve it first/);
  assert.match(gate.message!, /approved English version stays unchanged in the history/);
  assert.deepEqual(downstreamLanguageGate("en", english, { artifact: "V1", source: "Discovery" }), { blocked: false, language: "en", message: null });
  assert.deepEqual(downstreamLanguageGate("de", null, { artifact: "V1", source: "Discovery" }), { blocked: false, language: "de", message: null });
  assert.equal(downstreamLanguageGate("de", { language: "de", versionNumber: 4 }, { artifact: "V1", source: "Discovery" }).blocked, false, "after the German Discovery is approved");
});

test("changing the language explains that a new Discovery version is needed and changes nothing", () => {
  const notice = languageChangeNotice("de", { language: "en", status: "approved", versionNumber: 2 })!;
  assert.match(notice, /approved Discovery version 2 is in English/);
  assert.match(notice, /new Discovery version in Deutsch is needed/);
  assert.match(notice, /review and approve it before the presentation and post-meeting documents/);
  assert.match(notice, /Earlier versions stay in the history unchanged/);
  assert.equal(languageChangeNotice("en", { language: "en", status: "approved", versionNumber: 2 }), null);
  assert.equal(languageChangeNotice("de", null), null);
});

test("the approved Discovery 1 language comes from its version, also for versions without one", async () => {
  await withApi(() => ({ versions: [
    { id: "v1", version_number: 1, status: "approved", language: "en" },
    { id: "v3", version_number: 3, status: "draft", language: "de" },
    { id: "v2", version_number: 2, status: "approved", language: "de" },
  ] }), async () => {
    assert.deepEqual(await loadApprovedDiscovery1Language("test-token", backendId), { versionId: "v2", versionNumber: 2, language: "de" });
  });
  // An older API lists no language: the approved paper is read instead.
  await withApi((call) => call.path.endsWith("/approved") ? { paper_json: { language: "de" } } : { versions: [{ id: "v1", version_number: 1, status: "approved" }] }, async (calls) => {
    assert.equal((await loadApprovedDiscovery1Language("test-token", backendId))?.language, "de");
    assert.equal(calls.length, 2);
  });
  await withApi(() => ({ versions: [{ id: "v1", version_number: 1, status: "draft" }] }), async () => {
    assert.equal(await loadApprovedDiscovery1Language("test-token", backendId), null);
  });
  await withApi(() => assert.fail("no request for a local client"), async () => {
    assert.equal(await loadApprovedDiscovery1Language("test-token", "opp-unmapped-1234"), null);
  });
});

test("refused language requests get actionable messages", () => {
  const mismatch = documentLanguageErrorMessage(new ApiRequestError("Master Presentation V1 is generated in the language of the approved Discovery document (English).", 409, "LANGUAGE_MISMATCH"))!;
  assert.match(mismatch, /Nothing was generated or changed\. Either switch the document language back, or generate and approve a new Discovery/);
  assert.match(documentLanguageErrorMessage(new ApiRequestError("German follow-up emails are written from a finalized package.", 409, "LANGUAGE_UNSUPPORTED"))!, /Nothing was generated\. Use English/);
  assert.match(documentLanguageErrorMessage(new ApiRequestError("x", 401))!, /Sign in again/);
  assert.match(documentLanguageErrorMessage(new ApiRequestError("x", 403))!, /not available to your account/);
  assert.match(documentLanguageErrorMessage(new ApiRequestError("x", 404))!, /not found on the server/);
  assert.match(documentLanguageErrorMessage(new ApiRequestError("x", 422))!, /Choose English or Deutsch/);
  assert.equal(documentLanguageErrorMessage(new ApiRequestError("Other conflict", 409, "PPT1_FAILED")), null, "other errors keep their own message");
  assert.equal(documentLanguageErrorMessage(new Error("network")), null);
});

test("a German mismatch from the API is surfaced from the real request, nothing is assumed", async () => {
  await withApi(() => refused(409, "LANGUAGE_MISMATCH", "Discovery 2 is generated in the language of the approved Discovery document (English)."), async () => {
    const error = await generatePostMeetingDiscovery("test-token", backendId, "de").catch((cause) => cause);
    assert.ok(error instanceof ApiRequestError && error.status === 409 && error.code === "LANGUAGE_MISMATCH");
    assert.match(documentLanguageErrorMessage(error)!, /approved Discovery document \(English\)/);
  });
});

// --------------------------------------------------------------- canonical deck and review

test("a German Master Presentation is never described as fully German", () => {
  const summary = deckLanguageSummary({ language: "de", master_language: "en", other_language_findings: 2 })!;
  assert.equal(summary.label, "Appendix: Deutsch · Master slides: English");
  assert.equal(summary.mixed, true);
  assert.equal(summary.otherLanguageFindings, 2);
  assert.match(summary.notice!, /canonical Borek master slides stay in English/);
  assert.match(summary.notice!, /nothing is translated automatically/);
  const english = deckLanguageSummary({ language: "en", master_language: "en" })!;
  assert.equal(english.label, "Language: English");
  assert.equal(english.notice, null);
  assert.equal(deckLanguageSummary({})!.label, "Language: English", "a version from before BAP-13 is English");
  assert.equal(deckLanguageSummary(null), null);
  const source = { product_version: "V1", language: "de", master_language: "en" } as never;
  assert.equal(versionLabel({ versionNumber: 4, source }), "Master Presentation V1 · revision 4 · Appendix: Deutsch · Master slides: English");
  assert.equal(versionLabel({ versionNumber: 4, source: { product_version: "V1" } as never }), "Master Presentation V1 · revision 4", "older versions keep their label");
});

test("mixed-language review: flagged, never claimed as translated", () => {
  const text = otherLanguageReviewText(2, "de", "Discovery 2")!;
  assert.match(text, /^2 statements read as another language than this Discovery 2 \(Deutsch\)/);
  assert.match(text, /not translated/);
  assert.match(text, /acknowledging this does not translate them/);
  assert.equal(otherLanguageReviewText(0, "de", "Discovery 2"), null);
  assert.equal(otherLanguageReviewText(1, "de", "Discovery 2"),
    "1 statement reads as another language than this Discovery 2 (Deutsch). It is kept in the words it was confirmed in, not translated. Review it and reword it if needed; acknowledging this does not translate it.");
  const discovery2 = parsePostMeetingDiscovery(d2Envelope("de"));
  assert.deepEqual(discovery2.language_check?.other_language_item_ids, ["m-requirements-1"]);
  assert.deepEqual(approvedDiscovery2Language(discovery2), { language: "de", versionNumber: 1 });
  assert.equal(approvedDiscovery2Language(parsePostMeetingDiscovery(d2Envelope("de", false))), null, "a draft is not a source");
  assert.deepEqual(approvedDiscovery2Language(parsePostMeetingDiscovery(d2Envelope(undefined))), { language: "en", versionNumber: 1 });
});

test("the email reports its language from the pinned package", () => {
  // The source as the API pins it for a finalized Master Presentation V2 package (followup_email.py).
  const response = (language?: string) => ({
    opportunity_id: backendId, journey_stage: "deepening",
    draft: {
      id: "d", revision: 1, updated_at: "2026-10-26T09:00:00Z", status: "draft", send_status: "not_sent",
      lengths: {
        short: { subject: "Betreff", body: "Grüße", word_count: 1 },
        medium: { subject: "Betreff", body: "Viele Grüße", word_count: 2 },
        extensive: { subject: "Betreff", body: "Mit freundlichen Grüßen", word_count: 3 },
      },
      source: {
        kind: "master_presentation_v2", product_version: "V2", finalized_at: "2026-10-26T08:00:00Z",
        presentation_id: otherBackendId, presentation_version_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", version_number: 2,
        extraction_execution_mode: "fixture", confirmed_finding_count: 3, excluded_finding_count: 0,
        ...(language ? { language } : {}),
      },
      source_status: "valid",
      review_flags: ["statements_not_in_email_language"],
    },
  });
  const german = parsePostMeetingEmail(response("de"), backendId)!;
  assert.ok(german.source, "the pinned package is kept");
  assert.equal(german.source.language, "de");
  assert.equal(german.lengths.short.body, "Grüße", "German characters are kept");
  assert.ok(german.reviewFlags.includes("statements_not_in_email_language"));
  assert.equal(parsePostMeetingEmail(response(), backendId)!.source?.language, "en", "a draft from before BAP-13 is English");
  assert.equal(parsePostMeetingEmail(response("fr"), backendId)!.source?.language, "en", "an unsupported value never reads as German");
});

// ---------------------------------------------------------------------- wiring (source level)

test("the selector is an accessible radio group and separate from the interface language", () => {
  const selector = source("../components/DocumentLanguageSelector.tsx");
  assert.match(selector, /<fieldset className=\{`document-language-field/);
  assert.match(selector, /<legend>Document language<\/legend>/);
  assert.match(selector, /type="radio"/);
  assert.match(selector, /<span id=\{`\$\{id\}-\$\{option\.code\}-label`\} lang=\{option\.code\}>/, "each option is announced in its own language");
  assert.match(selector, /aria-labelledby=\{`\$\{id\}-\$\{option\.code\}-label`\}/, "each radio is named by its visible label");
  assert.match(selector, /aria-describedby=/);
  const library = source("./documentLanguage.ts");
  for (const text of [selector, library]) {
    assert.doesNotMatch(text, /localStorage|sessionStorage|borek-language|useLanguage\(/, "the document language is neither the UI language nor kept in browser storage");
  }
  const provider = source("../components/LanguageProvider.tsx");
  assert.doesNotMatch(provider, /document_language|DocumentLanguage/, "the interface language provider is unchanged");
  const css = source("../app/pitch-shell.css");
  assert.match(css, /@media \(max-width: 640px\) \{\n  \.document-language-options \{ display: grid; grid-template-columns: 1fr 1fr; width: 100%; \}/, "the options share the row on a phone");
  assert.match(css, /\.document-language-option:has\(input:focus-visible\)/, "keyboard focus is visible");
});

test("the workspaces wire the language without regenerating or approving on change", () => {
  const discovery = source("../components/DiscoveryAnalysisWorkspace.tsx");
  assert.match(discovery, /generateAnalysis\(accessToken, opportunityId, preview\?\.client\.values, documentLanguage\)/);
  assert.match(discovery, /<DocumentLanguageSelector\n\s+opportunityId=\{opportunityId\}/);
  assert.match(discovery, /lockedReason=\{generating \? "The language cannot change while a Discovery is being generated\." : null\}/);
  const selector = source("../components/DocumentLanguageSelector.tsx");
  assert.doesNotMatch(selector, /(generate|approve|finalize)\w*\(/i, "a selector change never generates, approves or finalizes");
  assert.doesNotMatch(selector, /from "@\/lib\/(api|discoveryAnalysis|ppt1Generation|postMeetingDiscovery|masterPresentationV2|postMeetingEmail)"/, "it cannot reach a generate call");
  const v1 = source("../components/PresentationWorkspace.tsx");
  assert.match(v1, /disabled=\{!approved \|\| languageGate\.blocked\}/);
  assert.match(v1, /<DeckLanguageNotice source=\{liveDeck\.source\} \/>/);
  const d2 = source("../components/PostMeetingDiscoveryPanel.tsx");
  assert.match(d2, /disabled=\{!live \|\| !state\.can_generate \|\| busy !== null \|\| languageGate\.blocked\}/);
  assert.match(d2, /generatePostMeetingDiscovery\(token, opportunityId, discovery1\.status === "verified" \? discovery1\.language : selectedLanguage\)/);
  const v2 = source("../components/PostMeetingPresentationWorkspace.tsx");
  assert.match(v2, /<DeckLanguageNotice source=\{deck\.source\} \/>/);
  const email = source("../components/FollowUpDraftPanel.tsx");
  assert.match(email, /prefix="Email language"/);
  assert.match(email, /acknowledging the review flag does not translate them/);
  const create = source("../components/ClientInformationEditor.tsx");
  assert.match(create, /normalizeClientInformationExtras\(extras\),\n\s+documentLanguage,\n\s+\);/, "the new client carries the chosen language");
});

// ------------------------------------------------------------------------ PR #14 review fixes

const v1Names = { artifact: "Master Presentation V1", source: "Discovery" };

test("review 2: an unresolved or failed source lookup keeps generation closed, with a retry", () => {
  const loading = sourceLanguageGate("de", { status: "loading" }, v1Names);
  assert.deepEqual([loading.blocked, loading.pending, loading.retry], [true, true, false]);
  assert.match(loading.message!, /^Checking the language of the approved Discovery before Master Presentation V1 can be generated/);
  const failed = sourceLanguageGate("en", failedLookup(new Error("network down")), v1Names);
  assert.deepEqual([failed.blocked, failed.pending, failed.retry], [true, false, true], "even when the selection would match");
  assert.match(failed.message!, /could not be checked \(network down\)/);
  assert.match(failed.message!, /not generated until it is verified, so it cannot come out in the wrong language\. Nothing was generated or changed\.$/);
  assert.match(sourceLanguageGate("en", failedLookup(new ApiRequestError("x", 401)), v1Names).message!, /Sign in again/);
  const none = sourceLanguageGate("de", lookupFrom(null), v1Names);
  assert.deepEqual([none.blocked, none.message], [false, null], "nothing approved yet: nothing to contradict");
  const verified = sourceLanguageGate("de", lookupFrom({ language: "de", versionNumber: 3 }), v1Names);
  assert.deepEqual([verified.blocked, verified.language], [false, "de"]);
  const mismatch = sourceLanguageGate("de", lookupFrom({ language: "en", versionNumber: 3 }), v1Names);
  assert.deepEqual([mismatch.blocked, mismatch.retry, mismatch.language], [true, false, "en"]);
});

test("review 2: every inheriting workspace starts its check as pending and fails closed", () => {
  const v1 = source("../components/PresentationWorkspace.tsx");
  assert.match(v1, /setApprovedLanguage\(\{ status: "loading" \}\)/);
  assert.match(v1, /: \{ status: "failed", message: "the approved Discovery version was not found" \}\);/, "an approved id without a version is not a pass");
  assert.match(v1, /setApprovedLanguage\(failedLookup\(cause\)\)/);
  assert.match(v1, /data-testid="v1-language-retry" onClick=\{\(\) => setLanguageCheck\(\(value\) => value \+ 1\)\}>Check again/);
  assert.doesNotMatch(v1, /downstreamLanguageGate|setApprovedLanguage\(null\)/, "no path turns an unknown language into a pass");
  const d2 = source("../components/PostMeetingDiscoveryPanel.tsx");
  assert.match(d2, /useState<SourceLanguageLookup>\(\{ status: "loading" \}\)/);
  assert.match(d2, /setDiscovery1\(failedLookup\(cause\)\)/);
  assert.match(d2, /data-testid="discovery2-language-retry"/);
  assert.doesNotMatch(d2, /downstreamLanguageGate|setDiscovery1\(null\)/);
  const v2 = source("../components/MeetingEvidencePanel.tsx");
  assert.match(v2, /setDiscovery2Language\(\{ status: "loading" \}\)/);
  assert.match(v2, /: \{ status: "failed", message: "the approved Discovery 2 was not found" \}\);/);
  assert.match(v2, /setDiscovery2Language\(failedLookup\(cause\)\)/);
  assert.match(v2, /data-testid="v2-language-retry"/);
  assert.doesNotMatch(v2, /downstreamLanguageGate|setDiscovery2Language\(null\)/);
});

test("review 1: the finalized package's language is read before any draft exists", () => {
  const master = (language?: string) => ({ ppt2_generation_source_manifest: { kind: "master_presentation_v2", ...(language ? { language } : {}) } });
  assert.deepEqual(finalizedPackageLanguage(master("de")), { status: "verified", language: "de", versionNumber: null });
  assert.deepEqual(finalizedPackageLanguage(master()), { status: "verified", language: "en", versionNumber: null }, "finalized before BAP-13: English");
  assert.deepEqual(finalizedPackageLanguage({ ppt2_generation_source_manifest: { kind: "ppt2" } }), { status: "verified", language: "en", versionNumber: null }, "earlier flows are English only");
  assert.deepEqual(finalizedPackageLanguage(null), { status: "none" });
  assert.equal(finalizedPackageLanguage({}).status, "failed");
  assert.equal(finalizedPackageLanguage("x").status, "failed");
  assert.equal(finalizedPackageLanguage(master("fr")).status, "failed", "an unknown language is never guessed");
});

test("review 1: an email in another language than the client's setting is not generated", () => {
  const names = { artifact: "The follow-up email", source: "finalized presentation package", remedy: emailLanguageRemedy };
  const gate = sourceLanguageGate("de", finalizedPackageLanguage({ ppt2_generation_source_manifest: { kind: "master_presentation_v2", language: "en" } }), names);
  assert.equal(gate.blocked, true);
  assert.match(gate.message!, /^The document language is Deutsch, but the finalized presentation package is in English\./);
  assert.match(gate.message!, /The finalized package and its approved documents stay unchanged\. For a Deutsch email, generate and approve Discovery, Discovery 2 and Master Presentation V2 in Deutsch and finalize that package/);
  assert.match(gate.message!, /or switch the document language back to English to prepare the email from this package\.$/);
  assert.equal(sourceLanguageGate("de", { status: "loading" }, names).blocked, true, "unknown package language: closed");
  const open = sourceLanguageGate("de", finalizedPackageLanguage({ ppt2_generation_source_manifest: { kind: "master_presentation_v2", language: "de" } }), names);
  assert.deepEqual([open.blocked, open.language], [false, "de"]);
});

test("review 1: the email request names the verified language; drafts, edits and confirmations are untouched", async () => {
  await withApi(() => Response.json({ opportunity_id: backendId, journey_stage: "deepening", draft: null }), async (calls) => {
    await assert.rejects(generatePostMeetingEmail("test-token", backendId, true, "de"));
    assert.deepEqual(calls[0].body, { journey_stage: "deepening", overwrite_edits: true, language: "de" });
  });
  const panel = source("../components/FollowUpDraftPanel.tsx");
  assert.match(panel, /const gate = emailLanguageGate\(selectedLanguage, latest\);\n\s+if \(gate\.blocked\) throw new Error/, "re-checked against the package as it is now");
  assert.match(panel, /generatePostMeetingEmail\(token, opportunityId, overwrite, gate\.language\)/);
  assert.match(panel, /workflow \? finalizedPackageLanguage\(workflow\.finalization\) : \{ status: "loading" \}/);
  for (const control of [
    'data-testid="email-generate" disabled={!live || !loaded || working || editStatics || emailGate.blocked}',
    'data-testid="email-regenerate" disabled={working || !eligible || emailGate.blocked}',
    'disabled={working || emailGate.blocked} onClick={() => void generate(true)}>Replace with a new draft',
  ]) assert.ok(panel.includes(control), control);
  // Only generation is gated: saving, attachments, review checks and confirmation keep their own gates.
  assert.ok(panel.includes('data-testid="email-save" disabled={working || !dirty.length || overLimit || conflict}'));
  assert.doesNotMatch(panel, /const (save|confirm|toggleAttachment|approve) = [^\n]*emailGate/);
  assert.match(panel, /data-testid="email-language-retry" onClick=\{refreshWorkflow\}/);
});

test("review 3: the language can be changed from every workflow page, never past an approved source", () => {
  const selector = source("../components/DocumentLanguageSelector.tsx");
  assert.match(selector, /export function DocumentLanguageControl/);
  assert.match(selector, /aria-expanded=\{open\} aria-controls=\{panelId\}/, "a disclosure button, announced as such");
  assert.match(selector, /\{open \? "Done" : "Change"\}/);
  const pre = source("../components/OpportunityWorkflowShell.tsx");
  assert.match(pre, /<DocumentLanguageControl opportunityId=\{fixture\.opportunity_id\} \/>/, "Discovery and Client Presentation pages");
  const post = source("../components/PostMeetingShell.tsx");
  assert.match(post, /<DocumentLanguageControl opportunityId=\{opportunityId\}/, "Discovery 2, V2 and email pages");
  // The control saves the setting only; each generate action keeps its gate (see review 2 and 1).
  assert.doesNotMatch(selector, /\b(generate|approve|finalize)\w*\(/i);
});

test("review 4: Client Presentation and the earlier flows carry or refuse the language", async () => {
  // Client Presentation = the pre-meeting Presentation page: Master Presentation V1 + the BAP-2 heading.
  const shell = source("../components/OpportunityWorkflowShell.tsx");
  assert.match(shell, /pathname\.endsWith\("\/presentations"\) \? \(\n\s+<PresentationHeadline/);
  assert.match(source("../app/opportunities/[opportunityId]/presentations/page.tsx"), /PresentationWorkspace/, "the Client Presentation route renders the V1 workspace");
  // Earlier standalone PPT #2 is English only: "en" is sent, "de" is refused by the API.
  await withApi(() => ({ job_id: "j", status: "queued", presentation_id: otherBackendId, presentation_version_id: null, journey_stage: "post_meeting" }), async (calls) => {
    await generatePpt2("test-token", backendId, "en");
    await regeneratePpt2("test-token", backendId, otherBackendId, "en");
    await generatePpt2("test-token", backendId);
    assert.deepEqual(calls.map((call) => call.body), [{ language: "en" }, { language: "en" }, undefined]);
  });
  const meeting = source("../components/MeetingEvidencePanel.tsx");
  assert.match(meeting, /const legacyLanguageBlocked = selectedLanguage !== "en";/);
  assert.match(meeting, /Boolean\(conflictingNotes\) \|\| legacyLanguageBlocked\} onClick=\{\(\) => void generateDocuments\(\)\}/);
  assert.match(meeting, /language: "en",\n\s+\}\);/);
  // The Discovery viewer for papers in the earlier format sends the language too.
  const legacy = source("../components/DiscoveryWorkspace.tsx");
  assert.match(legacy, /generateLiveDiscovery\(accessToken, initialVersion\.opportunity_id, preview\?\.client\.values, options, documentLanguage\)/);
  assert.match(source("./liveDiscovery.ts"), /generateDiscoveryPaper\(token, opportunityId, seed, language\)/);
});
