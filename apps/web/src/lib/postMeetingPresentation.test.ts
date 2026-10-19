import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, test } from "node:test";
import {
  adaptPostMeetingPresentation, downloadPostMeetingPresentation, loadExistingPostMeetingPresentation,
  requestPostMeetingSlidePreview, type LivePreviewState,
} from "./postMeetingPresentation.js";
import type { DeckResponse, SlideResponse } from "./presentationLive.js";

const token = "ppt2-test-token";
const opportunityId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const identity = { presentationId: "deck-2", presentationVersionId: "ppt2-version-1" };
const prefix = "/presentations/deck-2/versions/ppt2-version-1";
const metadata: SlideResponse[] = [5, 0, 2].map((index) => ({
  id: `ppt2-slide-${index}`, presentation_version_id: identity.presentationVersionId,
  slide_index: index, layout_id: `LAYOUT_${index}`, slide_spec: { title: index === 5 ? "" : `Meeting title ${index}` },
}));
const deck: DeckResponse = {
  presentation_id: identity.presentationId, presentation_name: "Meeting-informed deck", version_number: 1, status: "ready",
  slides: [2, 0].map((index) => ({
    slide_id: `ppt2-slide-${index}`, slide_index: index, layout_id: `LAYOUT_${index}`,
    preview_url: `${prefix}/preview/slides/${index}.png`,
  })),
  pptx_download_url: `${prefix}/download/pptx`, pdf_download_url: `${prefix}/download/pdf`,
};
function workflow(version: string | null = identity.presentationVersionId) {
  return { opportunity_id: opportunityId, documents: {
    ppt1: { presentation_id: "deck-1", latest_ready_version_id: "ppt1-version-99", journey_stage: "first_contact", status: "ready" },
    ppt2: { presentation_id: "deck-2", latest_ready_version_id: version, journey_stage: "post_meeting", status: "ready" },
  } };
}
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function image() { return new Response("rendered image bytes", { headers: { "Content-Type": "image/png" } }); }
function file(format: "pptx" | "pdf") {
  return new Response(`generated ${format} bytes`, { headers: { "Content-Type": format === "pdf" ? "application/pdf" : "application/vnd.openxmlformats-officedocument.presentationml.presentation" } });
}
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
    const url = new URL(String(input));
    const path = url.pathname;
    calls.push(path);
    assert.equal(new Headers(init?.headers).get("Authorization"), `Bearer ${token}`);
    assert.equal(init?.cache, "no-store");
    assert.equal(init?.method ?? "GET", "GET", "this workspace never generates or mutates documents");
    const response = await override?.(path, init);
    if (response) return response;
    if (path === `/opportunities/${opportunityId}/workflow-status`) return json(workflow());
    if (path === `${prefix}/deck`) return json(deck);
    if (path === `${prefix}/slides`) return json(metadata);
    if (path.startsWith(`${prefix}/preview/slides/`) || path.startsWith(`${prefix}/download/`)) {
      assert.equal(init?.redirect, "error", "authenticated assets cannot follow redirects");
      if (path.includes("/preview/")) return image();
      if (path.endsWith("/pptx")) return file("pptx");
      if (path.endsWith("/pdf")) return file("pdf");
    }
    throw new Error(`Unexpected authenticated request: ${url}`);
  };
  return calls;
}

test("resolves only workflow.documents.ppt2 when PPT1 also exists", async () => {
  const calls = mockFetch();
  const result = await loadExistingPostMeetingPresentation(token, opportunityId);
  assert.equal(result.deck?.presentationId, "deck-2");
  assert.equal(result.deck?.presentationVersionId, "ppt2-version-1");
  assert.equal(result.deck?.versionNumber, 1);
  assert.deepEqual(result.deck?.slides.map((slide) => slide.index), [0, 2, 5]);
  assert.deepEqual(result.deck?.slides.map((slide) => slide.label), ["Meeting title 0", "Meeting title 2", "Slide 6 - LAYOUT_5"]);
  assert.equal(result.deck?.slides[2].previewPath, null);
  assert.equal(calls.some((path) => path.includes("deck-1") || path.includes("generate")), false);
  assert.equal(calls.filter((path) => path.endsWith("/workflow-status")).length, 2);
});

test("optional workflow fields and absent PPT2 never fall back to PPT1 or Discovery slides", async () => {
  for (const body of [{}, { documents: { ppt1: workflow().documents.ppt1, ppt2: null } }, workflow(null)]) {
    const calls = mockFetch(() => json(body));
    const result = await loadExistingPostMeetingPresentation(token, opportunityId);
    assert.equal(result.deck, null);
    assert.equal(calls.length, 1);
  }
  mockFetch(() => json({ documents: { ppt2: { ...workflow(null).documents.ppt2, status: "failed" } } }));
  assert.equal((await loadExistingPostMeetingPresentation(token, opportunityId)).status, "failed");
});

test("selected non-cover slide requests its real index and cleans up its object URL", async () => {
  const calls = mockFetch();
  URL.createObjectURL = () => "blob:ppt2-selected";
  const revoked: string[] = [];
  URL.revokeObjectURL = (url) => revoked.push(url);
  const live = adaptPostMeetingPresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  const request = requestPostMeetingSlidePreview(token, opportunityId, live, live.slides[1], (state) => states.push(state));
  await request.done;
  assert.deepEqual(states, [{ state: "loading" }, { state: "ready", url: "blob:ppt2-selected" }]);
  assert.ok(calls.includes(`${prefix}/preview/slides/2.png`));
  assert.equal(calls.includes(`${prefix}/preview/slides/0.png`), false);
  assert.equal(calls.filter((path) => path.endsWith("/workflow-status")).length, 2);
  request.cancel();
  request.cancel();
  assert.deepEqual(revoked, ["blob:ppt2-selected"]);
});

test("missing images preserve actual slides and a reload can advertise a newly rendered preview", async () => {
  const live = adaptPostMeetingPresentation(identity, { ...deck, slides: [] }, metadata);
  assert.equal(live.slides.length, 3);
  assert.ok(live.slides.every((slide) => slide.previewPath === null));
  const calls = mockFetch();
  const states: LivePreviewState[] = [];
  const missing = requestPostMeetingSlidePreview(token, opportunityId, live, live.slides[1], (state) => states.push(state));
  await missing.done;
  assert.equal(states.at(-1)?.state, "failed");
  assert.equal(calls.length, 0);
  const reloaded = await loadExistingPostMeetingPresentation(token, opportunityId);
  const retry = requestPostMeetingSlidePreview(token, opportunityId, reloaded.deck!, reloaded.deck!.slides[1], (state) => states.push(state));
  await retry.done;
  assert.equal(states.at(-1)?.state, "ready");
  retry.cancel();
});

test("failed, unauthorized, empty and non-image previews fail explicitly; retry can succeed", async () => {
  let response = () => json({ error: { code: "SLIDE_PREVIEW_NOT_FOUND", message: "missing" } }, 404);
  mockFetch((path) => path.includes("/preview/") ? response() : undefined);
  const live = adaptPostMeetingPresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  const run = () => requestPostMeetingSlidePreview(token, opportunityId, live, live.slides[0], (state) => states.push(state));
  await run().done;
  assert.match(JSON.stringify(states.at(-1)), /not rendered/);
  response = () => json({ error: { message: "expired" } }, 401);
  await run().done;
  assert.match(JSON.stringify(states.at(-1)), /Sign in/);
  for (const invalid of [() => new Response("html", { headers: { "Content-Type": "text/html" } }), () => new Response(null, { headers: { "Content-Type": "image/png" } })]) {
    response = invalid;
    await run().done;
    assert.match(JSON.stringify(states.at(-1)), /not an image/);
  }
  response = image;
  const retry = run();
  await retry.done;
  assert.equal(states.at(-1)?.state, "ready");
  retry.cancel();
});

test("rejects foreign deck identity, version metadata, slide IDs/layouts and duplicate indices", () => {
  assert.throws(() => adaptPostMeetingPresentation(identity, { ...deck, presentation_id: "foreign-deck" }, metadata), /does not match/);
  assert.throws(() => adaptPostMeetingPresentation(identity, deck, metadata.map((slide) => ({ ...slide, presentation_version_id: "ppt1-version-99" }))), /does not match/);
  for (const changed of [{ slide_id: "foreign-slide" }, { layout_id: "OTHER" }, { slide_index: 99 }]) {
    assert.throws(() => adaptPostMeetingPresentation(identity, { ...deck, slides: [{ ...deck.slides[0], ...changed }] }, metadata), /does not match/);
  }
  assert.throws(() => adaptPostMeetingPresentation(identity, deck, [...metadata, metadata[0]]), /duplicate/);
  assert.throws(() => adaptPostMeetingPresentation(identity, deck, metadata.map((slide) => ({ ...slide, slide_index: -1 }))), /invalid/);
  assert.throws(() => adaptPostMeetingPresentation(identity, { ...deck, version_number: 0 }, metadata), /invalid deck/);
  assert.throws(() => adaptPostMeetingPresentation(identity, { ...deck, status: "failed" }, metadata), /not ready/);
  assert.throws(() => adaptPostMeetingPresentation(identity, deck, []), /does not match/);
});

test("rejects cross-opportunity workflow, wrong family and a deck endpoint returning another deck", async () => {
  mockFetch((path) => path.endsWith("/workflow-status") ? json({ ...workflow(), opportunity_id: "another-opportunity" }) : undefined);
  await assert.rejects(loadExistingPostMeetingPresentation(token, opportunityId), /does not match/);
  mockFetch((path) => path.endsWith("/workflow-status") ? json({ documents: { ppt2: workflow().documents.ppt1 } }) : undefined);
  await assert.rejects(loadExistingPostMeetingPresentation(token, opportunityId), /does not match/);
  mockFetch((path) => path.endsWith("/deck") ? json({ ...deck, presentation_id: "another-opportunity-deck" }) : undefined);
  await assert.rejects(loadExistingPostMeetingPresentation(token, opportunityId), /does not match/);
});

test("rejects latest metadata mismatch and version changes during deck loading", async () => {
  mockFetch((path) => path.endsWith("/slides") ? json(metadata.map((slide) => ({ ...slide, presentation_version_id: "ppt2-new" }))) : undefined);
  await assert.rejects(loadExistingPostMeetingPresentation(token, opportunityId), /version changed/);
  let reads = 0;
  mockFetch((path) => path.endsWith("/workflow-status") ? json(workflow(++reads === 1 ? identity.presentationVersionId : "ppt2-new")) : undefined);
  await assert.rejects(loadExistingPostMeetingPresentation(token, opportunityId), /version changed/);
});

test("before checks reject stale previews/downloads without fetching asset bytes", async () => {
  const calls = mockFetch((path) => path.endsWith("/workflow-status") ? json(workflow("ppt2-new")) : undefined);
  const live = adaptPostMeetingPresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  await requestPostMeetingSlidePreview(token, opportunityId, live, live.slides[0], (state) => states.push(state)).done;
  assert.match(JSON.stringify(states.at(-1)), /version changed/);
  await assert.rejects(downloadPostMeetingPresentation(token, opportunityId, live, "pdf"), /version changed/);
  assert.equal(calls.some((path) => /\/(preview|download)\//.test(path)), false);
});

test("after checks discard preview and both download formats if latest changes during the request", async () => {
  let changed = false;
  mockFetch((path) => {
    if (path.endsWith("/workflow-status")) return json(workflow(changed ? "ppt2-new" : identity.presentationVersionId));
    if (/\/(preview|download)\//.test(path)) changed = true;
  });
  let created = 0;
  URL.createObjectURL = () => `blob:${++created}`;
  const live = adaptPostMeetingPresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  await requestPostMeetingSlidePreview(token, opportunityId, live, live.slides[0], (state) => states.push(state)).done;
  assert.match(JSON.stringify(states.at(-1)), /version changed/);
  assert.equal(created, 0);
  for (const format of ["pptx", "pdf"] as const) {
    changed = false;
    await assert.rejects(downloadPostMeetingPresentation(token, opportunityId, live, format), /version changed/);
  }
});

test("after checks also reject metadata-only version changes", async () => {
  let changed = false;
  mockFetch((path) => {
    if (path.includes("/preview/")) changed = true;
    if (path.endsWith("/slides") && changed) return json(metadata.map((slide) => ({ ...slide, presentation_version_id: "ppt2-new" })));
  });
  const live = adaptPostMeetingPresentation(identity, deck, metadata);
  const states: LivePreviewState[] = [];
  await requestPostMeetingSlidePreview(token, opportunityId, live, live.slides[0], (state) => states.push(state)).done;
  assert.match(JSON.stringify(states.at(-1)), /version changed/);
});

test("asset credentials only go to exact advertised PPT2 paths, never other origins, families or formats", async () => {
  const calls = mockFetch();
  for (const path of ["https://other.example/image.png", "//other.example/image.png", "/presentations/deck-1/preview/slides/2.png", `${prefix}/preview/slides/0.png`, `${prefix}/preview/slides/2.png?redirect=evil`]) {
    const live = adaptPostMeetingPresentation(identity, { ...deck, slides: [{ ...deck.slides[0], preview_url: path }] }, metadata);
    assert.equal(live.slides[1].previewPath, null);
    const states: LivePreviewState[] = [];
    await requestPostMeetingSlidePreview(token, opportunityId, live, { ...live.slides[1], previewPath: path }, (state) => states.push(state)).done;
    assert.equal(states.at(-1)?.state, "failed");
  }
  const unsafe = adaptPostMeetingPresentation(identity, { ...deck, pptx_download_url: "https://other.example/deck.pptx", pdf_download_url: `${prefix}/download/pptx` }, metadata);
  assert.deepEqual(unsafe.downloads, { pptx: null, pdf: null });
  await assert.rejects(downloadPostMeetingPresentation(token, opportunityId, unsafe, "pptx"), /safe PPTX download/);
  await assert.rejects(downloadPostMeetingPresentation(token, opportunityId, unsafe, "pdf"), /safe PDF download/);
  const live = adaptPostMeetingPresentation(identity, deck, metadata);
  await assert.rejects(downloadPostMeetingPresentation(token, opportunityId, live, "zip" as "pdf"), /Unsupported/);
  assert.throws(() => adaptPostMeetingPresentation({ ...identity, presentationId: "../deck-1" }, deck, metadata), /does not match/);
  assert.equal(calls.length, 0);
});

test("downloads real authenticated PPTX/PDF bytes with before/after checks and format validation", async () => {
  const calls = mockFetch();
  const live = adaptPostMeetingPresentation(identity, deck, metadata);
  for (const format of ["pptx", "pdf"] as const) {
    const blob = await downloadPostMeetingPresentation(token, opportunityId, live, format);
    assert.equal(await blob.text(), `generated ${format} bytes`);
    assert.ok(calls.includes(`${prefix}/download/${format}`));
  }
  assert.equal(calls.filter((path) => path.endsWith("/workflow-status")).length, 4);
  for (const response of [() => file("pptx"), () => new Response("login page", { headers: { "Content-Type": "text/html" } }), () => new Response(null, { headers: { "Content-Type": "application/pdf" } })]) {
    mockFetch((path) => path.includes("/download/") ? response() : undefined);
    await assert.rejects(downloadPostMeetingPresentation(token, opportunityId, live, "pdf"), /not a presentation file/);
  }
});

test("cancelled preview selections cannot publish or allocate obsolete images", async () => {
  let resolveOld!: (response: Response) => void;
  let entered!: () => void;
  const started = new Promise<void>((resolve) => { entered = resolve; });
  const response = new Promise<Response>((resolve) => { resolveOld = resolve; });
  let signal: AbortSignal | null | undefined;
  mockFetch((path, init) => {
    if (path === `${prefix}/preview/slides/0.png`) { signal = init?.signal; entered(); return response; }
  });
  let created = 0;
  URL.createObjectURL = () => `blob:${++created}`;
  const live = adaptPostMeetingPresentation(identity, deck, metadata);
  const oldStates: LivePreviewState[] = [];
  const old = requestPostMeetingSlidePreview(token, opportunityId, live, live.slides[0], (state) => oldStates.push(state));
  await started;
  old.cancel();
  assert.equal(signal?.aborted, true);
  const current = requestPostMeetingSlidePreview(token, opportunityId, live, live.slides[1], () => {});
  await current.done;
  resolveOld(image());
  await old.done;
  assert.deepEqual(oldStates, [{ state: "loading" }]);
  assert.equal(created, 1);
  current.cancel();
});

test("unmount cancellation reaches workflow requests and stops load/download work", async () => {
  for (const action of ["load", "download"] as const) {
    const controller = new AbortController();
    const calls = mockFetch((path, init) => {
      assert.equal(init?.signal, controller.signal);
      if (path.endsWith("/workflow-status")) { controller.abort(); return json(workflow()); }
    });
    const live = adaptPostMeetingPresentation(identity, deck, metadata);
    await assert.rejects(action === "load" ? loadExistingPostMeetingPresentation(token, opportunityId, controller.signal)
      : downloadPostMeetingPresentation(token, opportunityId, live, "pptx", controller.signal), { name: "AbortError" });
    assert.equal(calls.some((path) => path.endsWith("/deck") || path.includes("/download/")), false);
  }
});

test("separate route copies workspace structure, handles image decode errors and has no unsafe generation or fixture content", () => {
  const source = readFileSync(new URL("../components/PostMeetingPresentationWorkspace.tsx", import.meta.url), "utf8");
  const route = readFileSync(new URL("../app/opportunities/[opportunityId]/post-meeting-presentation/page.tsx", import.meta.url), "utf8");
  for (const text of ["Updating your pitch", "artifact-preview-workspace presentation-document-workspace", "workflow-page-list", "discovery-page-toolbar", "presentation-slide-canvas", "discovery-generation-card", "discovery-download-card", "onLoad=", "onError=", "Retry preview", "Reload deck", "No generated ${product}", "those of this exact version", "downloadOperation.current", "previewOperation.current?.cancel()", "<progress"]) assert.ok(source.includes(text), text);
  assert.equal((source.match(/onClick=\{\(\) => void download\(/g) ?? []).length, 2, "one action per download format");
  assert.doesNotMatch(source, /DISCOVERY_PAGE_CATALOG|presentationPreview|usePreviewJourney|generateAndAwait|WorkflowArtifactTabs|href=\{[^}]*\/discovery/);
  assert.match(route, /return <PostMeetingPresentationWorkspace opportunityId=\{opportunityId\}/);
});

test("BAP-4: a Master Presentation V2 marks its appendix by layout, with Let's talk as the last, canonical slide", () => {
  const total = 30;
  const layoutAt = (index: number) => (index < 25 || index === total - 1 ? "CANONICAL" : "L08");
  const slides: SlideResponse[] = Array.from({ length: total }, (_, index) => ({
    id: `v2-slide-${index}`, presentation_version_id: identity.presentationVersionId, slide_index: index,
    layout_id: layoutAt(index), slide_spec: { title: index === total - 1 ? "Let’s talk" : `Title ${index + 1}` },
  }));
  const source = {
    kind: "master_presentation_v2", product_version: "V2", product_stage: "post_meeting", revision: 2, master_id: "borek_ai_tech_en_v1",
    master_version: "1.0", canonical_slide_count: 26, appendix_slide_count: 4, approved_discovery_version_id: "d", discovery_schema_version: "2.0",
  };
  const master: DeckResponse = {
    ...deck, source,
    slides: slides.map((slide) => ({ slide_id: slide.id, slide_index: slide.slide_index, layout_id: slide.layout_id, preview_url: `${prefix}/preview/slides/${slide.slide_index}.png` })),
  };
  const result = adaptPostMeetingPresentation(identity, master, slides);
  assert.deepEqual(result.slides.filter((slide) => slide.appendix).map((slide) => slide.index), [25, 26, 27, 28]);
  assert.equal(result.slides.at(-1)?.label, "Let’s talk");
  assert.equal(result.slides.at(-1)?.appendix, false);
  const workspace = readFileSync("src/components/PostMeetingPresentationWorkspace.tsx", "utf8");
  assert.doesNotMatch(workspace, /slides 1–\{deck\.source\.canonical_slide_count\}/, "no fixed slide ranges");
});
