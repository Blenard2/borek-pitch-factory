import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  adaptEarlierVersions, adaptLivePresentation, downloadLivePresentation, downloadPresentationVersion, loadExistingFirstPitch,
  requestLiveSlidePreview, type DeckResponse, type LivePreviewState, type SlideResponse,
} from "./presentationLive.js";

const token = "presentation-test-token";
const opportunityId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const identity = { presentationId: "deck-1", presentationVersionId: "version-1", jobId: null };
const prefix = "/presentations/deck-1/versions/version-1";
const metadata: SlideResponse[] = [5, 0, 2].map((index) => ({
  id: `slide-${index}`, presentation_version_id: "version-1", slide_index: index,
  layout_id: `LAYOUT_${index}`, slide_spec: { title: index === 5 ? "" : `Actual title ${index}` },
}));
const deck: DeckResponse = {
  presentation_id: "deck-1", presentation_name: "Actual generated deck", version_number: 1, status: "ready",
  slides: [2, 0, 8].map((index) => ({ slide_id: `slide-${index}`, slide_index: index, layout_id: `LAYOUT_${index}`, preview_url: `${prefix}/preview/slides/${index}.png` })),
  pptx_download_url: `${prefix}/download/pptx`, pdf_download_url: `${prefix}/download/pdf`,
};
function workflow(version = "version-1") {
  return { documents: {
    ppt1: { presentation_id: "deck-1", latest_ready_version_id: version, status: "ready", journey_stage: "first_contact" },
    ppt2: null, approved_discovery: { version_id: "approved-1" },
  } };
}
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function image() { return new Response("image bytes", { headers: { "Content-Type": "image/png" } }); }
const originalFetch = globalThis.fetch;
const originalCreate = URL.createObjectURL;
const originalRevoke = URL.revokeObjectURL;
afterEach(() => {
  globalThis.fetch = originalFetch;
  URL.createObjectURL = originalCreate;
  URL.revokeObjectURL = originalRevoke;
});

function mockFetch(override?: (path: string, init?: RequestInit) => Response | undefined | Promise<Response | undefined>) {
  const calls: string[] = [];
  globalThis.fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    calls.push(path);
    assert.equal(new Headers(init?.headers).get("Authorization"), `Bearer ${token}`);
    const response = await override?.(path, init);
    if (response) return response;
    if (path.endsWith("/workflow-status")) return json(workflow());
    if (path === `${prefix}/deck`) return json(deck);
    if (path === `${prefix}/slides`) return json(metadata);
    if (path.includes("/preview/slides/")) return image();
    if (path.includes("/download/")) return new Response("generated deck");
    throw new Error(`Unexpected request: ${path}`);
  };
  return calls;
}

test("live deck uses actual sorted indices, slide titles, and missing-preview rows, not seven Discovery pages", () => {
  const result = adaptLivePresentation(identity, deck, metadata);
  assert.equal(result.slides.length, 4);
  assert.deepEqual(result.slides.map((slide) => slide.index), [0, 2, 5, 8]);
  assert.deepEqual(result.slides.map((slide) => slide.label), ["Actual title 0", "Actual title 2", "Slide 6 - LAYOUT_5", "Slide 9 - LAYOUT_8"]);
  assert.equal(result.slides[2].previewPath, null);
  const coverOnly = adaptLivePresentation(identity, { ...deck, slides: [deck.slides[1]] }, metadata);
  assert.equal(coverOnly.slides.length, 3, "cover-only rendering must not hide the other actual slides");
  assert.deepEqual(coverOnly.slides.map((slide) => slide.previewPath), [`${prefix}/preview/slides/0.png`, null, null]);
  const foreignUrl = adaptLivePresentation(identity, { ...deck, slides: [{ ...deck.slides[0], preview_url: "https://other.example/image.png" }] }, metadata);
  assert.equal(foreignUrl.slides[1].previewPath, null, "never forward auth to untrusted preview URLs");
});

test("reload loads an existing PPT1 through workflow-status without generating or local fixture state", async () => {
  const calls = mockFetch();
  const loaded = await loadExistingFirstPitch(token, opportunityId);
  assert.equal(loaded.deck?.presentationId, "deck-1");
  assert.equal(loaded.deck?.presentationVersionId, "version-1");
  assert.equal(loaded.deck?.slides.length, 4);
  assert.equal(loaded.approvedSourceId, "approved-1");
  assert.equal(calls[0], `/opportunities/${opportunityId}/workflow-status`);
  assert.ok(calls.includes(`${prefix}/deck`));
  assert.ok(calls.includes(`${prefix}/slides`));
  assert.equal(calls.some((path) => path.includes("generate")), false);
});

test("workflow with no ready deck stays empty and does not synthesize fixture slides", async () => {
  const calls = mockFetch((path) => path.endsWith("/workflow-status") ? json({ documents: { ppt1: null, approved_discovery: null } }) : undefined);
  const loaded = await loadExistingFirstPitch(token, opportunityId);
  assert.equal(loaded.deck, null);
  assert.equal(loaded.approvedSourceId, null);
  assert.equal(calls.length, 1);
});

test("a failed latest version retains approval information for explicit generation recovery", async () => {
  mockFetch((path) => path === `${prefix}/deck` ? json({ error: { message: "Latest version is not ready" } }, 409) : undefined);
  const loaded = await loadExistingFirstPitch(token, opportunityId);
  assert.equal(loaded.deck, null);
  assert.equal(loaded.approvedSourceId, "approved-1");
  assert.match(loaded.loadError!, /Latest version is not ready/);
});

test("selected non-cover slide fetches its actual index with auth and revokes its object URL", async () => {
  const calls = mockFetch();
  const revoked: string[] = [];
  URL.createObjectURL = () => "blob:selected-slide";
  URL.revokeObjectURL = (url) => revoked.push(url);
  const live = adaptLivePresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  const request = requestLiveSlidePreview(token, opportunityId, live, live.slides[1], (state) => states.push(state));
  await request.done;
  assert.deepEqual(states, [{ state: "loading" }, { state: "ready", url: "blob:selected-slide" }]);
  assert.ok(calls.includes(`${prefix}/preview/slides/2.png`));
  assert.equal(calls.includes(`${prefix}/preview/slides/0.png`), false);
  request.cancel();
  request.cancel();
  assert.deepEqual(revoked, ["blob:selected-slide"]);
});

test("unadvertised preview is unavailable, never ready, and a deck reload can restore it", async () => {
  const calls = mockFetch();
  const live = adaptLivePresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  const missing = requestLiveSlidePreview(token, opportunityId, live, live.slides[2], (state) => states.push(state));
  await missing.done;
  assert.equal(states.at(-1)?.state, "failed");
  assert.equal(calls.length, 0);
  const updated = adaptLivePresentation(identity, { ...deck, slides: [...deck.slides, {
    slide_id: "slide-5", slide_index: 5, layout_id: "LAYOUT_5", preview_url: `${prefix}/preview/slides/5.png`,
  }] }, metadata);
  const retry = requestLiveSlidePreview(token, opportunityId, updated, updated.slides[2], (state) => states.push(state));
  await retry.done;
  assert.equal(states.at(-1)?.state, "ready");
  assert.ok(calls.includes(`${prefix}/preview/slides/5.png`));
  retry.cancel();
});

test("404 and auth errors remain explicit failures; retry can load the image", async () => {
  let response: () => Response = () => json({ error: { code: "SLIDE_PREVIEW_NOT_FOUND", message: "missing" } }, 404);
  mockFetch((path) => path.includes("/preview/slides/") ? response() : undefined);
  const live = adaptLivePresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  const run = () => requestLiveSlidePreview(token, opportunityId, live, live.slides[1], (state) => states.push(state));
  await run().done;
  assert.match(JSON.stringify(states.at(-1)), /not rendered/);
  response = () => json({ error: { message: "Expired token" } }, 401);
  await run().done;
  assert.match(JSON.stringify(states.at(-1)), /Sign in/);
  response = () => new Response("not an image", { headers: { "Content-Type": "text/html" } });
  await run().done;
  assert.match(JSON.stringify(states.at(-1)), /not an image/);
  response = image;
  const retry = run();
  await retry.done;
  assert.equal(states.at(-1)?.state, "ready");
  retry.cancel();
});

test("cancelled/out-of-order selection cannot publish or allocate an obsolete preview", async () => {
  let resolveOld!: (response: Response) => void;
  let entered!: () => void;
  const oldStarted = new Promise<void>((resolve) => { entered = resolve; });
  const oldResponse = new Promise<Response>((resolve) => { resolveOld = resolve; });
  let oldSignal: AbortSignal | null | undefined;
  mockFetch((path, init) => {
    if (path === `${prefix}/preview/slides/0.png`) {
      oldSignal = init?.signal;
      entered();
      return oldResponse; // Deliberately ignore abort to exercise the late-response guard.
    }
  });
  let created = 0;
  const revoked: string[] = [];
  URL.createObjectURL = () => `blob:preview-${++created}`;
  URL.revokeObjectURL = (url) => revoked.push(url);
  const live = adaptLivePresentation(identity, deck, metadata);
  const oldStates: LivePreviewState[] = [];
  const newStates: LivePreviewState[] = [];
  const old = requestLiveSlidePreview(token, opportunityId, live, live.slides[0], (state) => oldStates.push(state));
  await oldStarted;
  old.cancel();
  assert.equal(oldSignal?.aborted, true);
  const current = requestLiveSlidePreview(token, opportunityId, live, live.slides[1], (state) => newStates.push(state));
  await current.done;
  resolveOld(image());
  await old.done;
  assert.deepEqual(oldStates, [{ state: "loading" }]);
  assert.equal(newStates.at(-1)?.state, "ready");
  assert.equal(created, 1);
  current.cancel();
  assert.deepEqual(revoked, ["blob:preview-1"]);
});

test("presentation/version mismatches are rejected rather than relabeled", async () => {
  assert.throws(() => adaptLivePresentation(identity, { ...deck, presentation_id: "another-deck" }, metadata), /does not match/);
  assert.throws(() => adaptLivePresentation(identity, deck, metadata.map((slide) => ({ ...slide, presentation_version_id: "version-2" }))), /does not match/);
  assert.throws(() => adaptLivePresentation(identity, { ...deck, slides: [{ ...deck.slides[0], slide_id: "other-version-slide" }] }, metadata), /does not match/);
  // Reads are pinned to the version the workflow names; a server answering with another version's slides is rejected.
  const calls = mockFetch((path) => path.endsWith("/workflow-status") ? json(workflow("version-2"))
    : path === "/presentations/deck-1/versions/version-2/deck" ? json(deck)
    : path === "/presentations/deck-1/versions/version-2/slides" ? json(metadata) : undefined);
  const mismatched = await loadExistingFirstPitch(token, opportunityId);
  assert.ok(calls.every((path) => !path.startsWith("/presentations/") || path.startsWith("/presentations/deck-1/versions/version-2/")), "no unversioned deck request");
  assert.equal(mismatched.deck, null);
  assert.match(mismatched.loadError!, /does not match/);
  assert.equal(mismatched.approvedSourceId, "approved-1", "retain authoritative approval so recovery stays available");
});

test("version changes during preview/download discard the latest-only response", async () => {
  let changed = false;
  mockFetch((path) => {
    if (path.endsWith("/workflow-status")) return json(workflow(changed ? "version-2" : "version-1"));
    if (path.includes("/preview/slides/") || path.includes("/download/")) {
      changed = true;
      return image();
    }
  });
  let created = 0;
  URL.createObjectURL = () => `blob:${++created}`;
  const live = adaptLivePresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  const request = requestLiveSlidePreview(token, opportunityId, live, live.slides[0], (state) => states.push(state));
  await request.done;
  assert.match(JSON.stringify(states.at(-1)), /latest presentation version changed/);
  assert.equal(created, 0);
  changed = false;
  await assert.rejects(downloadLivePresentation(token, opportunityId, live, "pptx"), /latest presentation version changed/);
});

test("cancelled mount loads stop before requesting a deck", async () => {
  const controller = new AbortController();
  const calls = mockFetch((path) => {
    if (path.endsWith("/workflow-status")) {
      controller.abort();
      return json(workflow());
    }
  });
  await assert.rejects(loadExistingFirstPitch(token, opportunityId, controller.signal), { name: "AbortError" });
  assert.equal(calls.length, 1);
  await assert.rejects(loadExistingFirstPitch(token, opportunityId, controller.signal), { name: "AbortError" });
  assert.equal(calls.length, 1);
});

test("a Master Presentation lists the canonical deck and the client appendix with its approved source", () => {
  const total = 33;
  // BAP-4: canonical slides 1-25, the appendix, and the canonical closing slide ("Let's talk") last.
  const layoutAt = (index: number) => (index < 25 || index === total - 1 ? "CANONICAL" : "L08");
  const source = {
    kind: "master_presentation_v1", product_version: "V1", product_stage: "pre_meeting", revision: 2,
    master_id: "borek_ai_tech_en_v1", master_version: "1.0",
    canonical_slide_count: 26, appendix_slide_count: 7,
    approved_discovery_version_id: "approved-1", discovery_schema_version: "2.0",
  };
  const slides: SlideResponse[] = Array.from({ length: total }, (_, index) => ({
    id: `slide-${index}`, presentation_version_id: "version-1", slide_index: index,
    layout_id: layoutAt(index), slide_spec: { title: index === total - 1 ? "Let’s talk" : `Title ${index + 1}` },
  }));
  const master: DeckResponse = {
    ...deck, source,
    slides: slides.map((slide) => ({ slide_id: slide.id, slide_index: slide.slide_index, layout_id: slide.layout_id, preview_url: `${prefix}/preview/slides/${slide.slide_index}.png` })),
  };
  const result = adaptLivePresentation(identity, master, slides);
  assert.equal(result.slides.length, total, "no slide cap: every canonical and appendix slide is listed");
  assert.deepEqual(result.slides.map((slide) => slide.index), slides.map((slide) => slide.slide_index));
  assert.equal(result.slides.filter((slide) => slide.appendix).length, 7);
  assert.equal(result.slides[24].appendix, false, "slide 25 is canonical");
  assert.equal(result.slides[25].appendix, true, "the appendix begins at slide 26");
  assert.equal(result.slides[31].appendix, true, "the appendix ends at slide 32");
  assert.equal(result.slides[32].appendix, false, "the closing slide after the appendix is canonical");
  assert.equal(result.slides[32].label, "Let’s talk", "Let’s talk is the last slide");
  assert.equal(result.slides[32].previewPath, `${prefix}/preview/slides/32.png`);
  assert.equal(result.source?.approved_discovery_version_id, "approved-1");
  // Other decks carry no source and no appendix marker.
  const legacy = adaptLivePresentation(identity, deck, metadata);
  assert.equal(legacy.source, null);
  assert.ok(legacy.slides.every((slide) => !slide.appendix));
  // A deck version that changed underneath is still rejected.
  assert.throws(
    () => adaptLivePresentation(identity, master, slides.map((slide) => ({ ...slide, presentation_version_id: "version-2" }))),
    /latest presentation version changed/,
  );
});

test("the workspace shows the approved source and never substitutes fixture slides in a live session", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync("src/components/PresentationWorkspace.tsx", "utf8");
  assert.match(source, /approved Discovery \$\{liveDeck\.source\.approved_discovery_version_id\}/);
  assert.match(source, /canonical Borek deck, unchanged/);
  // No fixed slide ranges: the closing slide follows the appendix.
  assert.doesNotMatch(source, /slides 1–\{liveDeck\.source\.canonical_slide_count\}/);
  // The product stage comes from the server; the database version is shown as a revision only.
  assert.match(source, /Master Presentation \{liveDeck\.source\.product_version\}/);
  assert.match(source, /revision \{liveDeck\.source\.revision\}/);
  assert.doesNotMatch(source, /Master Presentation V\{liveDeck\.versionNumber\}/);
  assert.match(source, /generateAndAwaitFirstPitch\(accessToken, opportunityId, undefined, \{ regenerate \}\)/);
  assert.match(source, /approvedSourceId !== liveDeck\.source\.approved_discovery_version_id/);
  assert.match(source, /No fixture content is shown in a live session/);
  assert.match(source, /const slides = live \? \(liveDeck\?\.slides \?\? \[\]\)/, "live slides come from the server only");
});

test("earlier ready versions are listed and downloaded only through their own version endpoints", async () => {
  const presentationId = "11111111-1111-4111-8111-111111111111";
  const [first, second, third] = ["a", "b", "c"].map((letter) => `${letter.repeat(8)}-aaaa-4aaa-8aaa-aaaaaaaaaaaa`);
  const row = (id: string, version: number, extra: object = {}) => ({
    presentation_version_id: id, version_number: version, status: "ready", is_latest: false,
    pptx_download_url: `/presentations/${presentationId}/versions/${id}/download/pptx`,
    pdf_download_url: `/presentations/${presentationId}/versions/${id}/download/pdf`, ...extra,
  });
  const versions = adaptEarlierVersions(presentationId, [
    row(third, 3, { is_latest: true }), row(first, 1), row(second, 2),
    row("dddddddd-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 4, { pptx_download_url: "https://other.example/deck.pptx" }),
    row("eeeeeeee-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 5, { status: "failed" }),
  ]);
  assert.deepEqual(versions.map((version) => [version.versionId, version.versionNumber]), [[second, 2], [first, 1]]);
  const requested: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requested.push(String(input));
    assert.equal(new Headers(init?.headers).get("Authorization"), `Bearer ${token}`);
    return new Response(new Blob(["deck"]), { status: 200 });
  }) as typeof fetch;
  await downloadPresentationVersion(token, presentationId, first, "pdf");
  assert.equal(requested.length, 1);
  assert.ok(requested[0].endsWith(`/presentations/${presentationId}/versions/${first}/download/pdf`));
  await assert.rejects(downloadPresentationVersion(token, presentationId, "../other", "pdf"), /not valid/);
  assert.equal(requested.length, 1, "an invalid version id never reaches the API");
});
