// Renders the pages of an AI Opportunity Analysis (Discovery v2) in the BOREK White Paper master.
// The nine page types M1-M9 are ported 1:1 from "Borek White Paper Master October 1.html": every
// inline style below is the master's. Only the texts and the number of repeated blocks vary.
// Pages hide overflow like the master does; findOverflowingPages() reports any page whose content
// does not fit, so nothing is ever clipped silently.
import type { AnalysisPage } from "./discoveryAnalysis";

export const SHEET_W = 794; // A4 at 96 dpi
export const SHEET_H = 1123;

const INK = "#0D1240";
const SLATE = "#5C6178";
const BLUE = "#005099";
const RULE = "#E2E4EC";
const TINT = "#F3F4F8";
const MONO = "ui-monospace,Menlo,Consolas,monospace";
const SANS = "'Inter',Verdana,sans-serif";

type Obj = Record<string, unknown>;

export const esc = (v: unknown): string =>
  String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const s = (v: unknown): string => (typeof v === "string" ? v : v == null ? "" : String(v));
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const arr = (v: unknown): Obj[] => (Array.isArray(v) ? v.map(obj) : []);
const texts = (v: unknown): string[] => (Array.isArray(v) ? v.map(s) : []);
const two = (n: number): string => String(n).padStart(2, "0");

// BAP-13: the labels the master prints around the content, in the page's language. A page built
// for a German document carries content.lang = "de"; every other page keeps the English labels.
const SHEET_LABELS = {
  en: {
    inShort: "In short", publisher: "Publisher", unit: "Unit", contact: "Contact", keyMessage: "Key message",
    solution: "Solution", howItWorks: "How it works", result: "Result", opportunityIf: "Opportunity if:",
    learningLoop: "Learning loop", humanDecides: "Human decides", chain: "Chain", automatedBlocks: "Automated blocks",
    humanGate: "Human gate", loop: "Loop",
  },
  de: {
    inShort: "Kurz gesagt", publisher: "Herausgeber", unit: "Bereich", contact: "Kontakt", keyMessage: "Kernaussage",
    solution: "Lösung", howItWorks: "Zukünftiger Prozess", result: "Ergebnis", opportunityIf: "Opportunity, wenn:",
    learningLoop: "Lernender Kreislauf", humanDecides: "Mensch entscheidet", chain: "Kette", automatedBlocks: "Automatisierte Bausteine",
    humanGate: "Menschliches Tor", loop: "Zyklus",
  },
} as const;

export function sheetLanguage(page: Pick<AnalysisPage, "content">): "en" | "de" {
  return page.content.lang === "de" ? "de" : "en";
}

function labelsOf(page: AnalysisPage) {
  return SHEET_LABELS[sheetLanguage(page)];
}

function shell(page: AnalysisPage, style: string, inner: string): string {
  return `<section class="page" data-page-id="${esc(page.id)}" data-page-type="${page.type}" style="position:relative;${style};font-family:${SANS};box-sizing:border-box;overflow:hidden;width:${SHEET_W}px;height:${SHEET_H}px">${inner}</section>`;
}

const LIGHT = `background:#FFFFFF;color:${INK};padding:56px 76px 90px`;

function running(page: AnalysisPage): Obj {
  return obj(page.content.running);
}

function header(page: AnalysisPage): string {
  const r = running(page);
  const border = page.dark ? "rgba(255,255,255,.18)" : RULE;
  const color = page.dark ? "rgba(255,255,255,.6)" : SLATE;
  return `<div style="display:flex;justify-content:space-between;align-items:baseline;padding-bottom:12px;border-bottom:1px solid ${border};font-family:${MONO};font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:${color}"><span>${esc(r.left)}</span><span>${esc(r.right)}</span></div>`;
}

function footer(page: AnalysisPage): string {
  const color = page.dark ? "rgba(255,255,255,.7)" : SLATE;
  return `<div style="position:absolute;left:76px;right:76px;bottom:40px;display:flex;justify-content:space-between;font-family:${MONO};font-size:9.5px;letter-spacing:.06em;color:${color}"><span>${esc(running(page).footer)}</span><span>${two(page.number)}</span></div>`;
}

// M1 · COVER
function m1(page: AnalysisPage, base: string): string {
  const c = page.content;
  return shell(page, `background:${INK};color:#FFFFFF;display:flex;flex-direction:column;padding:64px 76px 60px`, `
    <img src="${base}/cover.png" alt="" style="position:absolute;left:0;top:0;width:100%;height:100%;display:block;object-fit:cover">
    <div style="position:relative;z-index:2;display:flex;justify-content:space-between;align-items:flex-start">
      <img src="${base}/logo.svg" alt="BOREK Solutions Group" style="width:168px;height:auto;display:block">
      <div style="text-align:right;font-family:${MONO};font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:rgba(255,255,255,.7);line-height:1.7">${esc(c.document_type_line)}<br>${esc(c.as_of)}</div>
    </div>
    <div style="position:relative;z-index:2;margin-top:72px">
      <div style="font-family:${MONO};font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:rgba(255,255,255,.6)">${esc(c.eyebrow)}</div>
      <h1 style="margin:18px 0 0;font-size:62px;font-weight:300;letter-spacing:-.025em;line-height:1.02;max-width:600px;text-wrap:balance">${esc(c.title)}</h1>
      <p style="margin:28px 0 0;max-width:520px;font-size:15px;line-height:1.65;color:rgba(255,255,255,.72);text-wrap:pretty">${esc(c.subtitle)}</p>
    </div>
    <div style="position:relative;z-index:2;margin-top:auto;display:flex;justify-content:space-between;padding-top:16px;border-top:1px solid rgba(255,255,255,.18);font-family:${MONO};font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,.6)">
      <span>${esc(c.place)}</span>
      <span>${esc(c.url)}</span>
    </div>`);
}

// M2 · CONTENTS
function m2(page: AnalysisPage): string {
  const c = page.content;
  const entries = arr(c.entries);
  const publisher = obj(c.publisher);
  const rows = entries.map((entry, index) => `<div style="display:grid;grid-template-columns:36px 1fr auto;gap:16px;align-items:baseline;padding:16px 0;border-top:1px solid ${RULE}${index === entries.length - 1 ? `;border-bottom:1px solid ${RULE}` : ""}"><span style="font-size:15px;font-weight:300;color:${BLUE}">${esc(entry.number)}</span><span style="font-size:15px;font-weight:500;line-height:1.3">${esc(entry.label)}</span><span style="font-family:${MONO};font-size:11px;color:${SLATE}">${two(Number(entry.page))}</span></div>`).join("");
  const statements = texts(c.in_short).map((text) => `<p style="margin:20px 0 0;font-size:17px;font-weight:300;line-height:1.4;letter-spacing:-.01em;text-wrap:pretty">${esc(text)}</p>`).join("");
  const label = `font-family:${MONO};font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:${SLATE}`;
  return shell(page, LIGHT, `
    ${header(page)}
    <h2 style="margin:64px 0 0;font-size:38px;font-weight:300;letter-spacing:-.02em;line-height:1.1">${esc(c.title)}</h2>
    <div style="margin-top:36px;display:grid;grid-template-columns:1fr 1fr;gap:0 56px">
      <div>${rows}</div>
      <div style="border-left:1px solid ${RULE};padding-left:32px">
        <div style="font-size:12px;font-weight:600;letter-spacing:.09em;text-transform:uppercase">${labelsOf(page).inShort}</div>
        ${statements}
        <p style="margin:36px 0 0;font-size:13px;line-height:1.6;color:${SLATE};text-wrap:pretty">${esc(c.scope_note)}</p>
      </div>
    </div>
    <div style="margin-top:64px;border-top:2px solid ${INK};padding-top:20px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:32px">
      <div><div style="${label}">${labelsOf(page).publisher}</div><div style="margin-top:8px;font-size:13px;line-height:1.6">${esc(publisher.company)}<br>${esc(publisher.address)}</div></div>
      <div><div style="${label}">${labelsOf(page).unit}</div><div style="margin-top:8px;font-size:13px;line-height:1.6">${esc(publisher.unit)}<br>${esc(publisher.group)}</div></div>
      <div><div style="${label}">${labelsOf(page).contact}</div><div style="margin-top:8px;font-size:13px;line-height:1.6">${esc(publisher.phone)}<br>${esc(publisher.url)}</div></div>
    </div>
    ${footer(page)}`);
}

function chapterHead(c: Obj): string {
  return `<div style="margin-top:72px;display:flex;align-items:center;gap:44px">
      <div style="font-size:88px;font-weight:300;line-height:.9;letter-spacing:-.03em;color:#FFFFFF">${esc(c.chapter_number)}</div>
      <div><div style="font-size:12px;font-weight:600;letter-spacing:.09em;text-transform:uppercase">${esc(c.chapter_name)}</div></div>
    </div>
    <h2 style="margin:40px 0 0;font-size:40px;font-weight:300;letter-spacing:-.02em;line-height:1.1;max-width:600px;text-wrap:balance">${esc(c.headline)}</h2>`;
}

// M3 · CHAPTER OPENER
function m3(page: AnalysisPage): string {
  const c = page.content;
  const [first, second] = texts(c.paragraphs);
  return shell(page, `background:${INK};color:#FFFFFF;padding:56px 76px 90px`, `
    ${header(page)}
    ${chapterHead(c)}
    <div data-fit="flow" style="margin-top:40px;display:grid;grid-template-columns:1fr 1fr;gap:40px">
      <p style="margin:0;font-size:14px;line-height:1.65;color:rgba(255,255,255,.9);text-wrap:pretty">${esc(first)}</p>
      <p style="margin:0;font-size:14px;line-height:1.65;color:rgba(255,255,255,.7);text-wrap:pretty">${esc(second)}</p>
    </div>
    <div data-fit="anchor" style="position:absolute;left:76px;right:76px;bottom:110px;border-top:1px solid rgba(255,255,255,.35);padding-top:24px">
      <div style="font-family:${MONO};font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:rgba(255,255,255,.7)">${labelsOf(page).keyMessage}</div>
      <p style="margin:14px 0 0;font-size:30px;font-weight:300;letter-spacing:-.02em;line-height:1.2;max-width:620px;text-wrap:balance">${esc(c.key_message)}</p>
    </div>
    ${footer(page)}`);
}

function numberedPoints(points: Obj[], marginTop: number): string {
  return points.map((point, index) => `<div style="${index === 0 ? `margin-top:${marginTop}px;` : ""}display:grid;grid-template-columns:44px 1fr;gap:8px 20px;padding:20px 0;${index === 0 ? `border-top:2px solid ${INK};` : ""}border-bottom:1px solid ${RULE}">
      <span style="font-family:${MONO};font-size:11px;color:${BLUE};padding-top:4px">${esc(point.number)}</span>
      <div><div style="font-size:16px;font-weight:500;letter-spacing:-.01em;line-height:1.3">${esc(point.title)}</div><p style="margin:8px 0 0;font-size:13.5px;line-height:1.6;color:${SLATE};text-wrap:pretty">${esc(point.text)}</p></div>
    </div>`).join("");
}

function cards(items: Obj[], marginTop: number, valueSize: number): string {
  if (!items.length) return "";
  const label = `font-family:${MONO};font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:${SLATE}`;
  const value = `margin-top:12px;font-size:${valueSize}px;font-weight:300;letter-spacing:-.03em;line-height:${valueSize === 34 ? "1.05" : "1"};color:${BLUE}`;
  const text = `margin:12px 0 0;font-size:12.5px;line-height:1.6;color:${SLATE};text-wrap:pretty`;
  const [left, right] = items;
  return `<div style="margin-top:${marginTop}px;display:grid;grid-template-columns:1fr 1fr;border-top:2px solid ${INK}">
      <div style="padding:22px 32px 24px 0;border-bottom:1px solid ${RULE}">
        <div style="${label}">${esc(left.label)}</div>
        <div style="${value}">${esc(left.value)}</div>
        <p style="${text}">${esc(left.text)}</p>
      </div>
      <div style="padding:22px 0 24px 32px;border-bottom:1px solid ${RULE};border-left:1px solid ${RULE}">
        <div style="${label}">${esc(right?.label)}</div>
        <div style="${value}">${esc(right?.value)}</div>
        <p style="${text}">${esc(right?.text)}</p>
      </div>
    </div>`;
}

function footnotes(notes: string[], marginTop: number): string {
  if (!notes.length) return "";
  return `<div style="margin-top:${marginTop}px;border-top:1px solid ${RULE};padding-top:14px;font-size:9.5px;line-height:1.6;color:${SLATE};display:grid;grid-template-columns:16px 1fr;gap:2px 8px">
      ${notes.map((note, index) => `<span style="color:${BLUE}">${index + 1}</span><span>${esc(note)}</span>`).join("")}
    </div>`;
}

const TH = `text-align:left;font-family:${MONO};font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:${SLATE};font-weight:500`;

// M4 · CONTENT PAGE (numbered points · KPIs · table · footnotes)
function m4(page: AnalysisPage): string {
  const c = page.content;
  const table = obj(c.table);
  const columns = texts(table.columns);
  const rows = arr(table.rows);
  const notes = texts(c.footnotes);
  const points = arr(c.points);
  const body = rows.map((row, index) => {
    const top = index === 0 ? `2px solid ${INK}` : `1px solid ${RULE}`;
    const bottom = index === rows.length - 1 ? `border-bottom:1px solid ${RULE};` : "";
    return row.highlighted
      ? `<tr style="background:${TINT}"><td style="border-top:${top};${bottom}padding:12px 14px 12px 12px;vertical-align:top;font-weight:500">${esc(row.label)}</td><td style="border-top:${top};${bottom}padding:12px 12px 12px 0;vertical-align:top">${esc(row.text)}</td></tr>`
      : `<tr><td style="border-top:${top};${bottom}padding:12px 14px 12px 0;vertical-align:top;font-weight:500">${esc(row.label)}</td><td style="border-top:${top};${bottom}padding:12px 0;vertical-align:top;color:${SLATE}">${esc(row.text)}</td></tr>`;
  }).join("");
  return shell(page, LIGHT, `
    ${header(page)}
    <p style="margin:40px 0 0;font-size:16px;line-height:1.55;max-width:600px;text-wrap:pretty">${esc(c.lead)}${notes.length ? `<sup style="font-size:10px;color:${BLUE}">1</sup>` : ""}</p>
    ${numberedPoints(points, 28)}
    ${cards(arr(c.kpis), 36, 44)}
    ${rows.length ? `<table style="width:100%;border-collapse:collapse;margin-top:36px;font-size:12.5px;line-height:1.5">
      <thead><tr>
        <th style="${TH};padding:0 14px 10px 0;width:30%">${esc(columns[0])}</th>
        <th style="${TH};padding:0 0 10px 0">${esc(columns[1])}</th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table>` : ""}
    ${footnotes(notes, 28)}
    ${footer(page)}`);
}

function deepDiveItem(item: Obj, page: AnalysisPage): string {
  const label = `font-family:${MONO};font-size:9px;letter-spacing:.09em;text-transform:uppercase;color:${SLATE};padding-top:4px`;
  const questions = texts(item.questions).map((question, index) => `<span style="font-family:${MONO};font-size:10px;color:${BLUE};padding-top:2px">Q${index + 1}</span><span>${esc(question)}</span>`).join("");
  return `<div style="margin-top:26px;border-top:2px solid ${INK};padding-top:14px;display:grid;grid-template-columns:44px 1fr;gap:0 20px;align-items:baseline">
      <span style="font-family:${MONO};font-size:11px;color:${BLUE}">${esc(item.number)}</span>
      <div style="font-size:16px;font-weight:500;letter-spacing:-.01em;line-height:1.3">${esc(item.title)}</div>
    </div>
    <div style="margin-top:12px;display:grid;grid-template-columns:44px 1fr;gap:0 20px">
      <span></span>
      <div>
        <div style="display:grid;grid-template-columns:92px 1fr;gap:10px 16px;padding:12px 0;border-top:1px solid ${RULE};border-bottom:1px solid ${RULE};font-size:12.5px;line-height:1.55">
          <span style="${label}">${labelsOf(page).solution}</span><p style="margin:0;color:${SLATE};text-wrap:pretty">${esc(item.solution)}</p>
          <span style="${label}">${labelsOf(page).howItWorks}</span><p style="margin:0;color:${SLATE};text-wrap:pretty">${esc(item.how_it_works)}</p>
          <span style="${label}">${labelsOf(page).result}</span><p style="margin:0;font-weight:500;text-wrap:pretty">${esc(item.result)}</p>
        </div>
        <div style="display:grid;grid-template-columns:92px 1fr;gap:6px 16px;padding:10px 0;font-size:12.5px;line-height:1.5">
          ${questions}
        </div>
        <div style="display:grid;grid-template-columns:92px 1fr;gap:16px;padding:10px 0;border-bottom:1px solid ${RULE};font-size:12.5px;line-height:1.5">
          <span style="color:${BLUE}">▸</span><span><b style="font-weight:500">${labelsOf(page).opportunityIf}</b> ${esc(item.signal)}</span>
        </div>
      </div>
    </div>`;
}

// M5 · DEEP DIVE (two items per page)
function m5(page: AnalysisPage): string {
  const c = page.content;
  return shell(page, LIGHT, `
    ${header(page)}
    <h2 style="margin:36px 0 0;font-size:26px;font-weight:300;letter-spacing:-.02em;line-height:1.15">${esc(c.title)}</h2>
    <p style="margin:10px 0 0;font-size:13px;line-height:1.55;color:${SLATE};text-wrap:pretty">${esc(c.lead)}</p>
    ${arr(c.items).map((item) => deepDiveItem(item, page)).join("")}
    ${footer(page)}`);
}

/** An http(s) address that may be linked; anything else (javascript:, data:, relative) is not. */
export function linkableSource(value: unknown): string | null {
  try {
    const url = new URL(s(value));
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

// A cited source (BAP-10 company background): the printed label links to the page it quotes.
function verdictCell(row: Obj): string {
  const href = linkableSource(row.source_url);
  return href
    ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer nofollow" style="color:inherit;text-decoration:underline;text-underline-offset:2px;overflow-wrap:anywhere">${esc(row.verdict)}</a>`
    : esc(row.verdict);
}

// M6 · COMPARISON TABLE (three columns, grouped rows, footnote)
function m6(page: AnalysisPage): string {
  const c = page.content;
  const columns = texts(c.columns);
  const rows = arr(c.rows);
  const body = rows.map((row, index) => {
    const top = index === 0 ? `2px solid ${INK}` : `1px solid ${RULE}`;
    const bottom = index === rows.length - 1 ? `border-bottom:1px solid ${RULE};` : "";
    const verdict = `color:${BLUE};font-weight:500`;
    return row.grouped
      ? `<tr style="background:${TINT}"><td style="border-top:${top};${bottom}padding:12px 16px 12px 12px;vertical-align:top;font-weight:500">${esc(row.subject)}</td><td style="border-top:${top};${bottom}padding:12px 16px 12px 0;vertical-align:top">${esc(row.description)}</td><td style="border-top:${top};${bottom}padding:12px 12px 12px 0;vertical-align:top;${verdict}">${verdictCell(row)}</td></tr>`
      : `<tr><td style="border-top:${top};${bottom}padding:12px 16px 12px 0;vertical-align:top;font-weight:500">${esc(row.subject)}</td><td style="border-top:${top};${bottom}padding:12px 16px 12px 0;vertical-align:top;color:${SLATE}">${esc(row.description)}</td><td style="border-top:${top};${bottom}padding:12px 0;vertical-align:top;${verdict}">${verdictCell(row)}</td></tr>`;
  }).join("");
  const note = s(c.footnote);
  return shell(page, LIGHT, `
    ${header(page)}
    <p style="margin:36px 0 0;font-size:16px;line-height:1.55;max-width:620px;text-wrap:pretty">${esc(c.lead)}${note ? `<sup style="font-size:10px;color:${BLUE}">1</sup>` : ""}</p>
    <table style="width:100%;border-collapse:collapse;margin-top:24px;font-size:12px;line-height:1.5">
      <thead><tr>
        <th style="${TH};padding:0 16px 10px 0;width:24%">${esc(columns[0])}</th>
        <th style="${TH};padding:0 16px 10px 0">${esc(columns[1])}</th>
        <th style="${TH};padding:0 0 10px 0;width:20%">${esc(columns[2])}</th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table>
    ${footnotes(note ? [note] : [], 24)}
    ${footer(page)}`);
}

// M7 · WORKFLOW DIAGRAM (chain grid)
function m7(page: AnalysisPage): string {
  const c = page.content;
  const rows = arr(c.rows);
  const head = `font-family:${MONO};font-size:8.5px;letter-spacing:.09em;text-transform:uppercase;color:${SLATE};padding-bottom:6px;border-bottom:2px solid ${INK}`;
  // German compounds ("Anforderungssteuerung") are hyphenated instead of widening the grid.
  const wrap = sheetLanguage(page) === "de" ? ";min-width:0;hyphens:auto;-webkit-hyphens:auto;overflow-wrap:anywhere" : "";
  const block = `position:relative;background:${TINT};border:1px solid ${RULE};border-left:3px solid ${INK};padding:14px 12px;font-size:11.5px;font-weight:500;line-height:1.35;display:flex;align-items:center${wrap}`;
  const arrow = `<span style="position:absolute;right:-9px;top:50%;transform:translateY(-50%);width:0;height:0;border-top:6px solid transparent;border-bottom:6px solid transparent;border-left:8px solid ${INK}"></span>`;
  const band = `<div style="grid-row:span ${rows.length};position:relative;background:${INK};display:flex;align-items:center;justify-content:center"><span style="writing-mode:vertical-rl;transform:rotate(180deg);font-family:${MONO};font-size:9px;letter-spacing:.22em;text-transform:uppercase;color:#FFFFFF;font-weight:500">${labelsOf(page).learningLoop}</span><span style="position:absolute;left:50%;top:-8px;transform:translateX(-50%);width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-bottom:8px solid ${INK}"></span></div>`;
  const chains = rows.map((row, index) => `
      <div style="display:flex;align-items:center;font-size:13px;font-weight:500;line-height:1.25;letter-spacing:-.01em;padding-right:8px${wrap}">${esc(row.name)}</div>
      ${texts(row.stages).map((stage) => `<div style="${block}">${esc(stage)}${arrow}</div>`).join("")}
      <div style="background:${INK};color:#FFFFFF;padding:12px 12px;display:flex;flex-direction:column;justify-content:center;gap:5px${wrap}"><span style="font-family:${MONO};font-size:7.5px;letter-spacing:.14em;text-transform:uppercase;color:rgba(255,255,255,.7)">${labelsOf(page).humanDecides}</span><span style="font-size:11.5px;font-weight:500;line-height:1.3">${esc(row.gate)}</span></div>
      ${index === 0 ? band : ""}`).join("");
  return shell(page, LIGHT, `
    ${header(page)}
    <h2 style="margin:36px 0 0;font-size:26px;font-weight:300;letter-spacing:-.02em;line-height:1.15">${esc(c.title)}</h2>
    <p style="margin:10px 0 0;font-size:13px;line-height:1.55;color:${SLATE};max-width:620px;text-wrap:pretty">${esc(c.lead)}</p>
    <div style="margin-top:28px;display:grid;grid-template-columns:96px 1fr 1fr 1fr 128px 44px;gap:10px 8px;align-items:stretch">
      <div style="${head}">${labelsOf(page).chain}</div>
      <div style="grid-column:span 3;${head}">${labelsOf(page).automatedBlocks}</div>
      <div style="${head}">${labelsOf(page).humanGate}</div>
      <div style="${head}">${labelsOf(page).loop}</div>
      ${chains}
    </div>
    <div style="margin-top:18px;display:grid;grid-template-columns:96px 1fr;gap:8px;align-items:center">
      <span></span>
      <div style="height:3px;background:${INK};position:relative"><span style="position:absolute;left:-1px;top:50%;transform:translateY(-50%);width:0;height:0;border-top:6px solid transparent;border-bottom:6px solid transparent;border-right:8px solid ${INK}"></span></div>
    </div>
    ${footer(page)}`);
}

// M8 · FINDINGS + POINTS (two word-value cards, three numbered points)
function m8(page: AnalysisPage): string {
  const c = page.content;
  return shell(page, LIGHT, `
    ${header(page)}
    <p style="margin:40px 0 0;font-size:16px;line-height:1.55;max-width:600px;text-wrap:pretty">${esc(c.lead)}</p>
    ${cards(arr(c.cards), 28, 34)}
    ${numberedPoints(arr(c.points), 36)}
    ${footer(page)}`);
}

// M9 · CLOSING
function m9(page: AnalysisPage, base: string): string {
  const c = page.content;
  const [first, second] = texts(c.paragraphs);
  return shell(page, `background:${INK};color:#FFFFFF;display:flex;flex-direction:column;padding:56px 76px 60px`, `
    ${header(page)}
    ${chapterHead(c)}
    <div style="margin-top:40px;display:grid;grid-template-columns:1fr 1fr;gap:40px">
      <p style="margin:0;font-size:14px;line-height:1.65;color:rgba(255,255,255,.85);text-wrap:pretty">${esc(first)}</p>
      <p style="margin:0;font-size:14px;line-height:1.65;color:rgba(255,255,255,.7);text-wrap:pretty">${esc(second)}</p>
    </div>
    <div style="margin-top:auto">
      <div style="border-top:1px solid rgba(255,255,255,.18);padding-top:28px">
        <p style="margin:0;font-size:34px;font-weight:300;letter-spacing:-.02em;line-height:1.15;max-width:600px;text-wrap:balance">${esc(c.closing_line)}</p>
      </div>
      <div style="margin-top:56px;display:flex;justify-content:space-between;align-items:flex-end;gap:40px">
        <img src="${base}/logo.svg" alt="BOREK Solutions Group" style="width:150px;height:auto;display:block">
        <div style="text-align:right;font-size:12.5px;line-height:1.8;color:rgba(255,255,255,.66)">
          <b style="color:#FFFFFF;font-weight:500">${esc(c.brand_claim)}</b><br>
          ${esc(c.contact_line)}<br>
          ${esc(c.address)}
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:28px;padding-top:14px;border-top:1px solid rgba(255,255,255,.18);font-family:${MONO};font-size:9.5px;letter-spacing:.06em;color:rgba(255,255,255,.6)">
        <span>${esc(running(page).footer)}</span><span>${two(page.number)}</span>
      </div>
    </div>`);
}

const RENDERERS: Record<AnalysisPage["type"], (page: AnalysisPage, base: string) => string> = {
  M1: m1, M2: m2, M3: m3, M4: m4, M5: m5, M6: m6, M7: m7, M8: m8, M9: m9,
};

/** One printed page. Only the nine master page types exist; anything else is an error. */
export function renderAnalysisSheet(page: AnalysisPage, base: string): string {
  const render = RENDERERS[page.type];
  if (!render) throw new Error(`Unknown white paper page type ${String(page.type)}`);
  return render(page, base);
}

export function analysisPrintCss(base: string): string {
  const subsets = ["cyrillic-ext", "cyrillic", "greek-ext", "greek", "vietnamese", "latin-ext", "latin"];
  const faces = subsets.map((n) => `@font-face{font-family:'Inter';font-style:normal;font-weight:300 600;font-display:swap;src:url('${base}/inter-${n}.woff2') format('woff2')}`).join("");
  return `${faces}@page{size:210mm 297mm;margin:0}*{box-sizing:border-box}body{margin:0;font-family:${SANS};color:${INK};-webkit-font-smoothing:antialiased}.page{print-color-adjust:exact;-webkit-print-color-adjust:exact;page-break-after:always;break-after:page}.page:last-child{page-break-after:auto;break-after:auto}h1,h2{text-wrap:balance}`;
}

/** The whole document, every page of the manifest in order. Used for the PDF. */
export function renderAnalysisDocument(pages: readonly AnalysisPage[], title: string, base: string): string {
  const lang = pages.length ? sheetLanguage(pages[0]) : "en";
  return `<!DOCTYPE html><html lang="${lang}"><head><meta charset="utf-8"><title>${esc(title)}</title><style>${analysisPrintCss(base)}</style></head><body>${pages.map((page) => renderAnalysisSheet(page, base)).join("")}</body></html>`;
}

/**
 * Whether a rendered page shows all of its content. A master page hides overflow, so content that
 * does not fit would vanish; this measures the real layout instead of guessing from text length.
 */
export function sheetOverflows(section: HTMLElement): boolean {
  const page = section.getBoundingClientRect();
  const scale = page.height / SHEET_H || 1;
  const limit = page.bottom - (section.dataset.pageType === "M1" || section.dataset.pageType === "M9" ? 40 : 76) * scale;
  const anchor = section.querySelector<HTMLElement>('[data-fit="anchor"]');
  const flow = section.querySelector<HTMLElement>('[data-fit="flow"]');
  if (anchor && flow && flow.getBoundingClientRect().bottom > anchor.getBoundingClientRect().top - 8 * scale) return true;
  if (section.scrollHeight > section.clientHeight + 1 || section.scrollWidth > section.clientWidth + 1) return true;
  for (const child of Array.from(section.children) as HTMLElement[]) {
    const style = child.ownerDocument.defaultView?.getComputedStyle(child);
    if (child.tagName === "IMG" || style?.position === "absolute") continue;
    if (child.getBoundingClientRect().bottom > limit + 1) return true;
  }
  return false;
}

/** Ids of the pages whose content does not fit. Renders off-screen; needs a browser. */
export async function findOverflowingPages(pages: readonly AnalysisPage[], base: string): Promise<string[]> {
  const frame = document.createElement("iframe");
  frame.setAttribute("aria-hidden", "true");
  frame.style.cssText = `position:fixed;left:-10000px;top:0;width:${SHEET_W}px;height:${SHEET_H}px;border:0;visibility:hidden`;
  document.body.appendChild(frame);
  try {
    const doc = frame.contentDocument!;
    doc.open();
    doc.write(renderAnalysisDocument(pages, "", base));
    doc.close();
    try { await doc.fonts?.ready; } catch { /* measure with fallback fonts */ }
    return Array.from(doc.querySelectorAll<HTMLElement>("section.page"))
      .filter(sheetOverflows)
      .map((section) => section.dataset.pageId ?? "");
  } finally {
    frame.remove();
  }
}

/** Opens the browser print dialog on the white-paper document (choose "Save as PDF"). */
export function printAnalysisPdf(pages: readonly AnalysisPage[], title: string): void {
  const html = renderAnalysisDocument(pages, title, `${window.location.origin}/whitepaper`);
  const frame = document.createElement("iframe");
  frame.setAttribute("aria-hidden", "true");
  frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0";
  document.body.appendChild(frame);
  const doc = frame.contentDocument!;
  doc.open();
  doc.write(html);
  doc.close();
  const done = () => setTimeout(() => frame.remove(), 1000);
  const run = async () => {
    try {
      await doc.fonts?.ready;
      await Promise.all(Array.from(doc.images).map((img) => (img.complete ? null : new Promise((r) => { img.onload = img.onerror = () => r(null); }))));
    } catch { /* print anyway */ }
    frame.contentWindow!.addEventListener("afterprint", done);
    frame.contentWindow!.focus();
    frame.contentWindow!.print();
  };
  void run();
}
