# Website research for Discovery (BAP-5)

Discovery can read the client's public website - the address entered in Client Information - and
cite what it says. The research is **opt-in per environment** and off unless it is switched on.

## Configuration

| Variable | Values | Default |
| --- | --- | --- |
| `WEBSITE_RESEARCH_ENABLED` | `true` / `false` | `false` (also when the variable is absent) |

Set it on the **API** (the worker does not read websites). Only `true` (or `1`, `yes`, `on`)
activates it; absent or `false` (`0`, `no`, `off`) keeps it off, and any other value stops the API
at start-up with a settings validation error. The setting is read at start-up, so a change needs an
API restart.

- **Off** (default): no website is requested. Discovery is built from the user's input only,
  `generation.research_mode` is `user_context_only`, and the analysis states that no web or company
  research was performed. This is the workflow before BAP-5.
- **On**: Discovery reads the website and stores the outcome in `generation.website_research`
  (status `complete`, `partial`, `failed` or `not_provided`, the pages read, cited facts, signals and
  errors). Only research that produced cited facts is reported as performed; a site that could not
  be analysed is reported with the reason, and Discovery continues with the user's input.

Unit and integration tests always run with the research off; research tests use fixture websites
and never touch the network.

## What is read

The homepage and at most six relevant internal pages of the same site (about, services, products,
solutions, industries, imprint, careers), one link deep. Legal pages, logins, shops, files and other
sites are skipped. No model call is made for the research itself.

- **Facts** are quoted verbatim from the site with the page they come from (`SOURCE_FACT`).
- **Signals** ("the website mentions logistics") are interpretations (`AI_INFERENCE`, to validate).
  They only order the opportunity areas; they are never presented as facts or verified challenges.

## Operational requirements and limitations

- **Outbound HTTPS.** The API container needs outbound internet access on ports 443 (and 80 for
  sites that redirect from http). Without it every site fails with `WEBSITE_UNREACHABLE` and
  Discovery continues with the user's input.
- **Latency.** The research runs inside the Discovery generation request: up to **25 s** in total
  (5 s connect and 8 s read per request, at most 7 pages). A successful result is cached for 10
  minutes per address, so Stage 1 research and a regenerated Discovery do not read the site again.
  Check that gateway or proxy timeouts in front of the API allow for this.
- **Limits.** At most 1.5 MB per response (the rest is not downloaded), 4 redirects, 18,000
  characters of content per site, about 9,000 characters of it passed to each live model stage.
- **Restricted sites.** `robots.txt`, logins (401), refusals (403) and rate limits (429) are
  respected and reported, never retried or worked around. Many large corporate sites refuse
  automated access; those clients get a user-input-only Discovery with a note saying why.
- **TLS.** Certificates are always verified. A site with a broken certificate chain fails with
  `WEBSITE_TLS_ERROR`.
- **JavaScript-only sites** are not rendered (no headless browser). Their metadata is used and the
  research is marked `partial` with `WEBSITE_REQUIRES_JAVASCRIPT`.
- **Network safety.** Only public addresses are requested: every host and redirect is resolved and
  checked, private, loopback, link-local, metadata and other reserved addresses are refused, and
  the connection goes to the checked address (no DNS rebinding). IP-address URLs and non-standard
  ports are refused.
- **Untrusted content.** Website text reaches the model only as fenced data with the instruction
  never to follow it; scripts, navigation, forms and cookie banners are removed beforehand.
- **Identification.** Requests use the user agent
  `BorekPitchFactory-WebsiteResearch/1.0 (+https://boreksolutions.de; sales preparation research)`.

## Error codes

`WEBSITE_URL_INVALID`, `WEBSITE_URL_BLOCKED`, `WEBSITE_UNREACHABLE`, `WEBSITE_TIMEOUT`,
`WEBSITE_TLS_ERROR`, `WEBSITE_ACCESS_RESTRICTED`, `WEBSITE_RATE_LIMITED`,
`WEBSITE_BLOCKED_BY_ROBOTS`, `WEBSITE_REDIRECT_BLOCKED`, `WEBSITE_TOO_MANY_REDIRECTS`,
`WEBSITE_TOO_LARGE`, `WEBSITE_UNSUPPORTED_CONTENT`, `WEBSITE_REQUIRES_JAVASCRIPT`,
`WEBSITE_TIME_BUDGET_EXCEEDED`, `WEBSITE_PAGE_TRUNCATED`, `WEBSITE_PAGE_NOT_FOUND` (a linked page
only), `WEBSITE_NO_CONTENT`, `WEBSITE_LIMITED_CONTENT`.
