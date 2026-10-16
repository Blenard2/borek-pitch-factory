import { isLocalUiPreviewAvailable } from "./uiPreview";

/**
 * supabase     - Microsoft sign-in through Supabase.
 * dev          - development-only sign-in against an API running with AUTH_BYPASS (memory backend):
 *                on localhost, or on the one deployed development host as a temporary preview.
 * preview      - browser-only sample data on localhost; the API is never called.
 * unconfigured - no sign-in is available for this build.
 */
export type AuthMode = "supabase" | "dev" | "preview" | "unconfigured";

export type BypassIgnoredReason = "production_profile" | "non_local_host";

/**
 * The only deployed host that may use development sign-in. It is fixed in code on purpose:
 * build configuration can switch the preview off, but can never point it at another host.
 */
export const DEPLOYED_DEV_PREVIEW_HOST = "web.gentletree-93d291c0.germanywestcentral.azurecontainerapps.io";

export interface AuthModeInput {
  supabaseConfigured: boolean;
  bypassFlag: string | undefined;
  devAccessToken: string | undefined;
  runtimeProfile: string | undefined;
  localHost: boolean;
  deployedDevBypassFlag: string | undefined;
  deployedDevHost: string | undefined;
  /** window.location.hostname, or "" outside the browser. */
  hostname: string;
}

export interface AuthModeResult {
  mode: AuthMode;
  /** Pre-issued Supabase token for local tooling; honoured only where development auth is allowed. */
  devAccessToken: string | null;
  bypassIgnoredReason: BypassIgnoredReason | null;
  /** True only for development sign-in on the deployed development host. */
  deployedDevPreview: boolean;
}

/** The API ignores the token value while AUTH_BYPASS is active; it only marks the request as dev auth. */
export const DEV_AUTH_TOKEN = "dev-bypass";
/** The Pre-Meeting page of the main navigation: where a new pitch starts. */
export const PRE_MEETING_PATH = "/opportunities/new/client-information";
/** Where sign-in lands when no safe ?next= or remembered path asks for another page. */
export const DEFAULT_POST_AUTH_PATH = PRE_MEETING_PATH;

const POST_AUTH_PATH_KEY = "borek.authNext";
const PATH_BASE = "http://borek.invalid";

export function resolveAuthMode(input: AuthModeInput): AuthModeResult {
  const bypassRequested = input.bypassFlag?.trim() === "true";
  const token = input.devAccessToken?.trim() || null;
  // Mirrors the API default: an unset profile is development, only "production" refuses the bypass.
  const production = (input.runtimeProfile?.trim() || "development") === "production";
  const developmentAllowed = !production && input.localHost;
  // Every condition is an exact match: an explicit development profile, both flags, and the one
  // allowed host named in the build and seen by the browser. No defaults, patterns or subdomains.
  const deployedDevPreview = bypassRequested &&
    input.deployedDevBypassFlag === "true" &&
    input.runtimeProfile === "development" &&
    input.deployedDevHost === DEPLOYED_DEV_PREVIEW_HOST &&
    input.hostname === DEPLOYED_DEV_PREVIEW_HOST;
  const bypassIgnoredReason = (bypassRequested || token) && !developmentAllowed && !deployedDevPreview
    ? production ? "production_profile" : "non_local_host"
    : null;
  const fallback: AuthMode = input.supabaseConfigured ? "supabase" : input.localHost ? "preview" : "unconfigured";
  return {
    mode: bypassRequested && (developmentAllowed || deployedDevPreview) ? "dev" : fallback,
    devAccessToken: developmentAllowed ? token : null,
    bypassIgnoredReason,
    deployedDevPreview,
  };
}

export function currentAuthMode(): AuthModeResult {
  return resolveAuthMode({
    supabaseConfigured: Boolean(process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY),
    bypassFlag: process.env.NEXT_PUBLIC_BYPASS_LOGIN,
    devAccessToken: process.env.NEXT_PUBLIC_DEV_ACCESS_TOKEN,
    runtimeProfile: process.env.NEXT_PUBLIC_RUNTIME_PROFILE,
    localHost: isLocalUiPreviewAvailable(),
    deployedDevBypassFlag: process.env.NEXT_PUBLIC_DEPLOYED_DEV_BYPASS,
    deployedDevHost: process.env.NEXT_PUBLIC_DEPLOYED_DEV_HOST,
    hostname: typeof window === "undefined" ? "" : window.location.hostname,
  });
}

/** Returns a same-origin app path, or null when the value could leave the app or loop back to /login. */
export function sanitizeNextPath(value: string | null | undefined): string | null {
  const candidate = value?.trim();
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//")) return null;
  if (/[\\\u0000-\u001f\u007f]/.test(candidate)) return null;
  let parsed: URL;
  try {
    parsed = new URL(candidate, PATH_BASE);
  } catch {
    return null;
  }
  if (parsed.origin !== PATH_BASE || parsed.pathname.startsWith("//")) return null;
  if (parsed.pathname === "/login" || parsed.pathname.startsWith("/login/")) return null;
  return `${parsed.pathname}${parsed.search}${parsed.hash}`;
}

export interface AuthCallbackError {
  code: string;
  description: string;
}

const CALLBACK_ERROR_PARAMS = ["error", "error_code", "error_description"] as const;

function printable(value: string | null): string {
  return (value ?? "").replace(/[\u0000-\u001f\u007f]+/g, " ").trim().slice(0, 300);
}

/** Supabase and Microsoft report a failed sign-in as error parameters on the return URL, in the query or the hash. */
export function readAuthCallbackError(search: string, hash: string): AuthCallbackError | null {
  for (const source of [hash.replace(/^#/, ""), search.replace(/^\?/, "")]) {
    const params = new URLSearchParams(source);
    const code = printable(params.get("error_code") ?? params.get("error"));
    const description = printable(params.get("error_description"));
    if (code || description) return { code, description };
  }
  return null;
}

/** The same URL without the sign-in error parameters, so a reload does not repeat a stale error. */
export function withoutAuthCallbackError(pathname: string, search: string): string {
  const params = new URLSearchParams(search);
  for (const name of CALLBACK_ERROR_PARAMS) params.delete(name);
  const query = params.toString();
  return query ? `${pathname}?${query}` : pathname;
}

function storage(): Storage | null {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

/** Keeps the return path across the identity-provider round trip, which only returns to /login. */
export function rememberPostAuthPath(value: string | null | undefined): void {
  const path = sanitizeNextPath(value);
  try {
    if (path) storage()?.setItem(POST_AUTH_PATH_KEY, path);
    else storage()?.removeItem(POST_AUTH_PATH_KEY);
  } catch {
    // Without storage the user lands on the default page after sign-in.
  }
}

export function clearPostAuthPath(): void {
  try {
    storage()?.removeItem(POST_AUTH_PATH_KEY);
  } catch {
    // Nothing to clear when storage is unavailable.
  }
}

/** The ?next= of the current URL wins; the remembered path covers the provider round trip. */
export function resolvePostAuthPath(search: string): string {
  const fromQuery = sanitizeNextPath(new URLSearchParams(search).get("next"));
  if (fromQuery) return fromQuery;
  let remembered: string | null = null;
  try {
    remembered = storage()?.getItem(POST_AUTH_PATH_KEY) ?? null;
  } catch {
    remembered = null;
  }
  return sanitizeNextPath(remembered) ?? DEFAULT_POST_AUTH_PATH;
}
