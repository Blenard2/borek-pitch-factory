import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  clearPostAuthPath,
  DEFAULT_POST_AUTH_PATH,
  PRE_MEETING_PATH,
  readAuthCallbackError,
  rememberPostAuthPath,
  resolveAuthMode,
  resolvePostAuthPath,
  sanitizeNextPath,
  withoutAuthCallbackError,
  type AuthModeInput,
} from "./authMode.js";
import { beginAuthSession, clearAuthSession, isAuthSessionEnded, isDevAuthSessionActive, isPreviewSessionActive } from "./authSession.js";

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, value); },
  };
}

Object.defineProperty(globalThis, "sessionStorage", { value: memoryStorage(), configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: memoryStorage(), configurable: true });

const base: AuthModeInput = {
  supabaseConfigured: false,
  bypassFlag: undefined,
  devAccessToken: undefined,
  runtimeProfile: "development",
  localHost: true,
  deployedDevBypassFlag: undefined,
  deployedDevHost: undefined,
  hostname: "localhost",
};

// Mode resolution: Microsoft, development sign-in, preview and unconfigured stay distinct.
assert.equal(resolveAuthMode({ ...base, supabaseConfigured: true }).mode, "supabase");
assert.equal(resolveAuthMode(base).mode, "preview");
assert.equal(resolveAuthMode({ ...base, localHost: false }).mode, "unconfigured");
assert.deepEqual(resolveAuthMode({ ...base, bypassFlag: "true" }), {
  mode: "dev", devAccessToken: null, bypassIgnoredReason: null, deployedDevPreview: false,
});
assert.equal(resolveAuthMode({ ...base, bypassFlag: "true", runtimeProfile: "test" }).mode, "dev");
assert.equal(resolveAuthMode({ ...base, bypassFlag: "true", runtimeProfile: undefined }).mode, "dev");
assert.equal(resolveAuthMode({ ...base, bypassFlag: "true", supabaseConfigured: true }).mode, "dev");
for (const flag of ["false", "", "1", "TRUE", undefined]) {
  assert.equal(resolveAuthMode({ ...base, bypassFlag: flag }).mode, "preview", `flag ${String(flag)} must not enable dev auth`);
}

// Production fails closed: the flag and the tooling token are both ignored and reported.
assert.deepEqual(resolveAuthMode({ ...base, bypassFlag: "true", devAccessToken: "token", runtimeProfile: "production" }), {
  mode: "preview", devAccessToken: null, bypassIgnoredReason: "production_profile", deployedDevPreview: false,
});
assert.deepEqual(
  resolveAuthMode({ ...base, bypassFlag: "true", runtimeProfile: "production", supabaseConfigured: true, localHost: false }),
  { mode: "supabase", devAccessToken: null, bypassIgnoredReason: "production_profile", deployedDevPreview: false },
);
assert.deepEqual(resolveAuthMode({ ...base, bypassFlag: "true", devAccessToken: "token", localHost: false }), {
  mode: "unconfigured", devAccessToken: null, bypassIgnoredReason: "non_local_host", deployedDevPreview: false,
});
assert.deepEqual(resolveAuthMode({ ...base, devAccessToken: " token " }), {
  mode: "preview", devAccessToken: "token", bypassIgnoredReason: null, deployedDevPreview: false,
});

// Return paths stay inside the app.
assert.equal(sanitizeNextPath("/clients"), "/clients");
assert.equal(sanitizeNextPath("/opportunities/abc/discovery?tab=2#top"), "/opportunities/abc/discovery?tab=2#top");
assert.equal(sanitizeNextPath("  /clients?phase=pre_meeting "), "/clients?phase=pre_meeting");
for (const unsafe of [
  null, undefined, "", "clients", "https://evil.example/x", "//evil.example", "/\\evil.example", "\\\\evil.example",
  "/\t/evil.example", "/%0a/x\n", "javascript:alert(1)", "/login", "/login?next=/clients", "/login/again",
  "/../../evil", "http:/evil.example",
]) {
  const result = sanitizeNextPath(unsafe);
  assert.ok(result === null || (result.startsWith("/") && !result.startsWith("//") && !result.startsWith("/login")),
    `${String(unsafe)} must not leave the app`);
}
assert.equal(sanitizeNextPath("//evil.example"), null);
assert.equal(sanitizeNextPath("https://evil.example/x"), null);
assert.equal(sanitizeNextPath("/\\evil.example"), null);
assert.equal(sanitizeNextPath("/login?next=/clients"), null);
assert.equal(sanitizeNextPath("/../../evil"), "/evil");

// The requested page survives the identity-provider round trip, which returns to /login without a query.
clearPostAuthPath();
// BAP-1: without a requested page, sign-in lands on the Pre-Meeting page, not the client list.
assert.equal(PRE_MEETING_PATH, "/opportunities/new/client-information");
assert.equal(DEFAULT_POST_AUTH_PATH, PRE_MEETING_PATH);
assert.equal(resolvePostAuthPath(""), "/opportunities/new/client-information");
assert.equal(resolvePostAuthPath("?next="), "/opportunities/new/client-information");
assert.equal(resolvePostAuthPath("?next=%2Flogin"), "/opportunities/new/client-information", "a /login loop falls back to Pre-Meeting");
assert.equal(resolvePostAuthPath("?next=%2Fclients"), "/clients", "an explicit deep link to the client list is still honoured");
assert.equal(resolvePostAuthPath("?next=%2Fclients%3Fphase%3Dpost_meeting"), "/clients?phase=post_meeting");
assert.equal(resolvePostAuthPath("?next=%2Fopportunities%2Fabc%2Fdiscovery"), "/opportunities/abc/discovery");
rememberPostAuthPath("/opportunities/abc/presentations");
assert.equal(resolvePostAuthPath(""), "/opportunities/abc/presentations");
assert.equal(resolvePostAuthPath("?next=/profile"), "/profile", "an explicit next wins over the remembered path");
assert.equal(resolvePostAuthPath("?next=https://evil.example"), "/opportunities/abc/presentations");
rememberPostAuthPath("https://evil.example");
assert.equal(resolvePostAuthPath(""), "/opportunities/new/client-information", "an unsafe path is never remembered");
globalThis.sessionStorage.setItem("borek.authNext", "//evil.example");
assert.equal(resolvePostAuthPath(""), "/opportunities/new/client-information", "a tampered remembered path is rejected");
clearPostAuthPath();

// A failed provider round trip is reported instead of silently showing the login page again.
assert.equal(readAuthCallbackError("", ""), null);
assert.equal(readAuthCallbackError("?next=%2Fclients", "#"), null);
assert.deepEqual(
  readAuthCallbackError("", "#error=server_error&error_code=unexpected_failure&error_description=Error+getting+user+email+from+external+provider"),
  { code: "unexpected_failure", description: "Error getting user email from external provider" },
);
assert.deepEqual(
  readAuthCallbackError("?error=access_denied&error_description=AADSTS50105%3A+user+not+assigned", ""),
  { code: "access_denied", description: "AADSTS50105: user not assigned" },
);
assert.deepEqual(readAuthCallbackError("?error=access_denied", ""), { code: "access_denied", description: "" });
assert.equal(readAuthCallbackError("", `#error_description=${"x".repeat(900)}`)?.description.length, 300);
assert.equal(readAuthCallbackError("", "#error_description=line%0Abreak%00")?.description, "line break");
assert.equal(readAuthCallbackError("", "#access_token=abc&token_type=bearer"), null, "a successful return is not an error");
assert.equal(withoutAuthCallbackError("/login", "?error=access_denied&error_description=x&next=%2Fclients"), "/login?next=%2Fclients");
assert.equal(withoutAuthCallbackError("/login", "?error=access_denied&error_code=x"), "/login");
assert.equal(withoutAuthCallbackError("/login", ""), "/login");

// Development and preview sessions are separate flags, and sign-out clears both.
beginAuthSession(false, true);
assert.equal(isDevAuthSessionActive(), true);
assert.equal(isPreviewSessionActive(), false);
beginAuthSession(true, true);
assert.equal(isPreviewSessionActive(), true);
assert.equal(isDevAuthSessionActive(), false, "a preview session is never also a development session");
beginAuthSession(false, true);
clearAuthSession("dev-user");
assert.equal(isDevAuthSessionActive(), false);
assert.equal(isAuthSessionEnded(), true);
beginAuthSession();
assert.equal(isDevAuthSessionActive(), false);

// Wiring: the login card never turns the Microsoft button into a preview or development sign-in.
const source = (name: string) => readFileSync(fileURLToPath(new URL(name, import.meta.url)), "utf8");
const card = source("../components/AuthCard.tsx");
assert.match(card, /disabled=\{busy \|\| signInAction\(authMode\) === null\}/);
assert.doesNotMatch(card.slice(card.indexOf("async function handleMicrosoftSignIn"), card.indexOf("async function handleDevSignIn")),
  /startPreviewSession|startDevSession/);
assert.match(card, /rememberPostAuthPath\(/);
assert.match(card, /readAuthCallbackError\(window\.location\.search, window\.location\.hash\)/);
assert.match(card, /authCopy\.signInFailed/);
assert.match(card, /redirectTo: `\$\{window\.location\.origin\}\/login`/);
// BAP-1: a signed-in user leaves /login through the sanitised return path, once, replacing history.
assert.match(card, /const target = resolvePostAuthPath\(window\.location\.search\);\s*clearPostAuthPath\(\);\s*router\.replace\(target\);/);
// The default destination is the same page as the sidebar's Pre-meeting entry.
const sidebar = source("../components/SiteHeader.tsx");
assert.ok(sidebar.includes(`{ id: "pre_meeting", href: "${PRE_MEETING_PATH}"`), "sidebar Pre-meeting link and post-login default agree");
const provider = source("../components/AuthProvider.tsx");
assert.doesNotMatch(provider, /process\.env\.NEXT_PUBLIC_(BYPASS_LOGIN|DEV_ACCESS_TOKEN)/, "bypass env is read only through authMode");
assert.match(provider, /await startDevAuthSession\(/);
assert.match(source("./supabase.ts"), /currentAuthMode\(\)\.mode === "dev"/);

console.log("Auth mode, return path and development session tests passed");
