import assert from "node:assert/strict";
import { test } from "node:test";

import type { AnalysisPage } from "./discoveryAnalysis.js";
import { sampleAnalysis } from "./discoveryAnalysisSample.js";
import { renderAnalysisDocument, renderAnalysisSheet, sheetLanguage } from "./discoveryAnalysisSheets.js";

// BAP-13: the print renderer writes its own labels in the page's language. A page built for a
// German document carries content.lang = "de"; every other page keeps the English labels.

const pages = (sampleAnalysis("00000000-0000-4000-8000-000000000000") as { page_manifest: AnalysisPage[] }).page_manifest;
const german = (page: AnalysisPage): AnalysisPage => ({ ...page, content: { ...page.content, lang: "de" } });
const firstOf = (type: AnalysisPage["type"]) => pages.find((page) => page.type === type)!;

test("a page without a language keeps the English labels", () => {
  assert.equal(sheetLanguage(firstOf("M5")), "en");
  const html = renderAnalysisSheet(firstOf("M5"), "/wp");
  for (const label of ["Solution", "How it works", "Result", "Opportunity if:"]) assert.ok(html.includes(label), label);
  assert.match(renderAnalysisDocument(pages, "x", "/wp"), /<html lang="en">/);
});

test("a German page prints German labels and the document is marked German", () => {
  const deepDive = renderAnalysisSheet(german(firstOf("M5")), "/wp");
  for (const label of ["Lösung", "Zukünftiger Prozess", "Ergebnis", "Opportunity, wenn:"]) assert.ok(deepDive.includes(label), label);
  for (const label of [">Solution<", ">How it works<", ">Result<", "Opportunity if:"]) assert.ok(!deepDive.includes(label), label);
  const contents = renderAnalysisSheet(german(firstOf("M2")), "/wp");
  for (const label of ["Kurz gesagt", "Herausgeber", "Bereich", "Kontakt"]) assert.ok(contents.includes(label), label);
  const diagram = renderAnalysisSheet(german(firstOf("M7")), "/wp");
  for (const label of ["Lernender Kreislauf", "Mensch entscheidet", "Kette", "Automatisierte Bausteine", "Menschliches Tor"]) assert.ok(diagram.includes(label), label);
  assert.ok(renderAnalysisSheet(german(firstOf("M3")), "/wp").includes("Kernaussage"));
  assert.match(renderAnalysisDocument(pages.map(german), "x", "/wp"), /<html lang="de">/);
});

test("German characters are written as they are", () => {
  const page = german({ ...firstOf("M5"), content: { ...firstOf("M5").content, title: "Größere Prüfläufe für Übergaben" } });
  assert.ok(renderAnalysisSheet(page, "/wp").includes("Größere Prüfläufe für Übergaben"));
});
