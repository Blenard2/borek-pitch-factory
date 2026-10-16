import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  DEPLOYED_DEV_PREVIEW_HOST,
  resolveAuthMode,
  resolvePostAuthPath,
  type AuthModeInput,
} from "./authMode.js";
import { clearAuthSession, getAuthOwnerId, isAuthSessionEnded, isDevAuthSessionActive } from "./authSession.js";
import { restoreDevAuthSession, startDevAuthSession } from "./devAuthSession.js";
import { performSignIn, signInAction } from "./signIn.js";

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

const HOST = "web.gentletree-93d291c0.germanywestcentral.azurecontainerapps.io";
assert.equal(DEPLOYED_DEV_PREVIEW_HOST, HOST);

// The deployed development build: Supabase is configured there too, and the host is not localhost.
const deployed: AuthModeInput = {
  supabaseConfigured: true,
  bypassFlag: "true",
  devAccessToken: undefined,
  runtimeProfile: "development",
  localHost: false,
  deployedDevBypassFlag: "true",
  deployedDevHost: HOST,
  hostname: HOST,
};
const inactive = (overrides: Partial<AuthModeInput>, why: string) => {
  const result = resolveAuthMode({ ...deployed, ...overrides });
  assert.equal(result.deployedDevPreview, false, why);
  assert.equal(result.mode, "supabase", `${why}: Microsoft sign-in stays in place`);
  assert.equal(signInAction(result.mode), "microsoft", why);
};

// A. Exact host, development profile and both flags: preview bypass is active.
assert.deepEqual(resolveAuthMode(deployed), {
  mode: "dev", devAccessToken: null, bypassIgnoredReason: null, deployedDevPreview: true,
});
assert.equal(resolveAuthMode({ ...deployed, supabaseConfigured: false }).mode, "dev");
assert.equal(resolveAuthMode({ ...deployed, devAccessToken: "token" }).devAccessToken, null,
  "the tooling token is never honoured on a deployed host");

// B. Same host without the deployed-preview flag, or without the general bypass flag.
for (const flag of [undefined, "", "false", "TRUE", "1", " true", "true "]) {
  inactive({ deployedDevBypassFlag: flag }, `deployed flag ${JSON.stringify(flag)}`);
}
inactive({ bypassFlag: undefined }, "NEXT_PUBLIC_BYPASS_LOGIN missing");
inactive({ bypassFlag: "false" }, "NEXT_PUBLIC_BYPASS_LOGIN false");

// C. A different host, in the browser or in the build configuration.
inactive({ hostname: "app.example.com" }, "different browser host");
inactive({ hostname: "web.other-env.germanywestcentral.azurecontainerapps.io" }, "another container app");
inactive({ hostname: "" }, "no browser host (server render)");
inactive({ deployedDevHost: undefined }, "host not configured");
inactive({ deployedDevHost: "" }, "empty configured host");
inactive({ deployedDevHost: "app.example.com", hostname: "app.example.com" },
  "build configuration cannot move the preview to another host");

// D. Wildcards and lookalikes never match.
for (const lookalike of [
  `${HOST}.evil.example`, `evil.${HOST}`, `x${HOST}`, `${HOST}.`, `${HOST}:443`, ` ${HOST}`, HOST.toUpperCase(),
  "web.gentletree-93d291c0.germanywestcentral.azurecontainerapps.io.", "gentletree-93d291c0.germanywestcentral.azurecontainerapps.io",
  "web-gentletree-93d291c0.germanywestcentral.azurecontainerapps.io", "web.gentletree-93d291c0.germanywestcentral.azurecontainerapps.io.evil",
  "*.azurecontainerapps.io", "*", "web.gentletree-93d291c1.germanywestcentral.azurecontainerapps.io",
]) {
  inactive({ hostname: lookalike }, `browser host ${lookalike}`);
  inactive({ deployedDevHost: lookalike }, `configured host ${lookalike}`);
  inactive({ deployedDevHost: lookalike, hostname: lookalike }, `both hosts ${lookalike}`);
}

// E. Any profile other than an explicit "development" keeps it off.
for (const profile of ["production", "test", "staging", "Development", "", undefined]) {
  inactive({ runtimeProfile: profile }, `profile ${JSON.stringify(profile)}`);
}
assert.equal(resolveAuthMode({ ...deployed, runtimeProfile: "production" }).bypassIgnoredReason, "production_profile");
assert.equal(resolveAuthMode({ ...deployed, hostname: "app.example.com" }).bypassIgnoredReason, "non_local_host");

async function main() {
  // G + H. One button, two behaviours: Microsoft OAuth unless the preview bypass is active.
  async function click(input: AuthModeInput) {
    const calls: string[] = [];
    const action = await performSignIn(resolveAuthMode(input).mode, {
      microsoft: async () => { calls.push("signInWithOAuth"); },
      dev: async () => { calls.push("devSignIn"); },
    });
    return { action, calls };
  }
  assert.deepEqual(await click(deployed), { action: "dev", calls: ["devSignIn"] }, "preview bypass never starts Microsoft OAuth");
  assert.deepEqual(await click({ ...deployed, deployedDevBypassFlag: "false" }), { action: "microsoft", calls: ["signInWithOAuth"] });
  assert.deepEqual(await click({ ...deployed, runtimeProfile: "production" }), { action: "microsoft", calls: ["signInWithOAuth"] });
  assert.deepEqual(await click({ ...deployed, hostname: `evil.${HOST}` }), { action: "microsoft", calls: ["signInWithOAuth"] });
  assert.deepEqual(
    await click({ ...deployed, bypassFlag: undefined, deployedDevBypassFlag: undefined, deployedDevHost: undefined, runtimeProfile: "production" }),
    { action: "microsoft", calls: ["signInWithOAuth"] },
    "a normal production build is unchanged",
  );
  assert.deepEqual(await click({ ...deployed, supabaseConfigured: false, deployedDevBypassFlag: "false" }), { action: null, calls: [] });

  const source = (name: string) => readFileSync(fileURLToPath(new URL(name, import.meta.url)), "utf8");
  const card = source("../components/AuthCard.tsx");
  assert.equal(card.match(/signInWithOAuth\(/g)?.length, 1, "OAuth is started in exactly one place");
  assert.match(card.slice(card.indexOf("async function handleMicrosoftSignIn"), card.indexOf("async function handleDevSignIn")), /signInWithOAuth\(/);
  assert.doesNotMatch(card.slice(card.indexOf("async function handleDevSignIn")), /signInWithOAuth|getSupabaseBrowserClient\(/);
  assert.match(card, /onClick=\{\(\) => void performSignIn\(authMode, \{ microsoft: handleMicrosoftSignIn, dev: handleDevSignIn \}\)\}/);
  assert.equal(card.match(/auth-microsoft"/g)?.length, 1, "there is one primary sign-in button");
  assert.match(card, /microsoftStyledButton \? authCopy\.continueMicrosoft : authCopy\.devSignIn/);
  assert.match(card, /const microsoftStyledButton = authMode !== "dev" \|\| deployedDevPreview;/);
  assert.match(source("./supabase.ts"), /currentAuthMode\(\)\.mode === "dev"/, "a stored Supabase session is never used in development mode");
  const badge = source("../components/DevPreviewBadge.tsx");
  assert.match(badge, /if \(!deployedDevPreview \|\| !isAuthenticated\) return null;/);
  assert.match(badge, />Development Preview</);

  // I. A successful /employees/me starts the development session.
  const profile = { user_id: "00000000-0000-4000-8000-0000000000d1", email: "preview@borek.localhost" };
  clearAuthSession();
  assert.equal(isDevAuthSessionActive(), false);
  assert.deepEqual(await startDevAuthSession(async () => profile), profile);
  assert.equal(isDevAuthSessionActive(), true);
  assert.equal(isAuthSessionEnded(), false);
  assert.equal(getAuthOwnerId(), profile.user_id);

  // K. A refresh keeps the session while the API still confirms it.
  let probes = 0;
  assert.deepEqual(await restoreDevAuthSession(async () => { probes += 1; return profile; }), profile);
  assert.equal(probes, 1);
  assert.equal(isDevAuthSessionActive(), true);
  assert.equal(await restoreDevAuthSession(async () => { throw new Error("401"); }), null, "a rejected probe does not restore");

  // L. Logout clears it completely; a later refresh does not even ask the API.
  clearAuthSession(profile.user_id);
  assert.equal(isDevAuthSessionActive(), false);
  assert.equal(isAuthSessionEnded(), true);
  assert.equal(getAuthOwnerId(), null);
  probes = 0;
  assert.equal(await restoreDevAuthSession(async () => { probes += 1; return profile; }), null);
  assert.equal(probes, 0);

  // J. A failed or malformed /employees/me never authenticates.
  await assert.rejects(startDevAuthSession(async () => { throw new Error("Employee request failed (401)."); }));
  assert.equal(isDevAuthSessionActive(), false);
  assert.equal(getAuthOwnerId(), null);
  await assert.rejects(startDevAuthSession(async () => ({ user_id: "", email: "" })));
  await assert.rejects(startDevAuthSession(async () => ({}) as typeof profile));
  assert.equal(isDevAuthSessionActive(), false);
  assert.equal(await startDevAuthSession(async () => profile, () => false), null, "a superseded attempt persists nothing");
  assert.equal(isDevAuthSessionActive(), false);
  const provider = source("../components/AuthProvider.tsx");
  assert.match(provider, /const profile = await startDevAuthSession\(\s*\(\) => getEmployeeMe\(DEV_AUTH_TOKEN\),/);
  assert.match(provider, /void restoreDevAuthSession\(\(\) => getEmployeeMe\(DEV_AUTH_TOKEN\)\)/);
  assert.match(provider, /if \(devUserId && error instanceof ApiRequestError && error\.status === 401\) \{\s*endPreviewSession\(\);/);

  // M. The post-sign-in destination stays sanitised in preview mode too.
  assert.equal(resolvePostAuthPath(""), "/opportunities/new/client-information");
  assert.equal(resolvePostAuthPath("?next=%2Fopportunities%2Fabc%2Fdiscovery"), "/opportunities/abc/discovery");
  for (const unsafe of ["https://evil.example/x", "//evil.example", "/\\evil.example", "/login?next=/clients", "javascript:alert(1)"]) {
    assert.equal(resolvePostAuthPath(`?next=${encodeURIComponent(unsafe)}`), "/opportunities/new/client-information", `${unsafe} is rejected`);
  }

  console.log("Deployed development preview bypass tests passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
