# API key bootstrap

The Matrix backend is a public REST API gated by Google's standard
"public web app key" (an `AIzaSy*` value). Matrix's web UI loads the
key from the same SPA bundle the public can read. We do the same.

## Resolution order

`_api_key.resolve_api_key()` tries, in order:

1. **`FLIGHT_API_KEY` env var** — highest precedence. Set this to short-circuit
   bootstrap (CI, sandbox, or if Google rotates and we haven't yet fixed
   the regex).
2. **`~/.cache/flight-cli/.matrix-key`** — written automatically on first
   bootstrap. 30-day TTL; older than that triggers a fresh fetch.
3. **Live bootstrap from Matrix's SPA bundle.**

## The bundle has 7 keys, not 1

The captured SPA bundle defines:

| Tag | Key | Service | Useful? |
|---|---|---|---|
| `DEFAULT` | `AIzaSyAtYgaO...A4cE` | Generic gapi bootstrap | No |
| **`matrix`** | `AIzaSyBH1mte...fX7g` | **Prod search backend (what we want)** | ✓ |
| `matrix-nightly` | `AIzaSyA-9pyR...6RRo` | Matrix nightly / canary | Potentially — earlier features |
| `matrix-uat` | `AIzaSyBt5zVp...IAK4` | UAT environment | Marginal |
| `people-api` | `AIzaSyBMkT3C...Yuq8` | Google People (contacts) | No |
| `DY.YA` | `AIzaSyC2Zf_O...9Tr0` | Telemetry / metrics | No |
| `waa-pa` | `AIzaSyBGb5fG...1HRE` | WAA anti-abuse token mint | Defensive (see below) |

If we just grab "the first AIzaSy* we see in the JS", we get the People
API key and get back HTTP 403 on `/v1/search`. The regex MUST target the
prod-matrix entry specifically:

```python
_KEY_PATTERN_MATRIX_PROD = re.compile(
    r'(?:[.])matrix\s*[=:]\s*["\'](AIzaSy[A-Za-z0-9_-]{33})["\']'
    r'|'
    r'["\']matrix["\']\s*:\s*["\'](AIzaSy[A-Za-z0-9_-]{33})["\']'
)
```

This intentionally does NOT match `matrix-nightly` or `matrix-uat`.

## Failure modes

| What broke | What user sees | Fix |
|---|---|---|
| Bundle URL pattern changed | "Couldn't locate Matrix's SPA bundle URL" | Update `_BUNDLE_PATTERN` regex (probably the `gstatic.com/alkali/...` segment shifted) |
| Key tag changed (e.g., minifier rename) | "Found AIzaSy* keys but none tagged as the prod 'matrix' key" | Inspect bundle, update `_KEY_PATTERN_MATRIX_PROD` |
| Key validation broke (Google really did rotate) | HTTP 403 on `/v1/search` | Re-fetch bundle and confirm key still matches what we extract |
| User offline / Matrix unreachable | "Network error contacting matrix.itasoftware.com" | Set `FLIGHT_API_KEY` manually |

The error message guides the user to set `FLIGHT_API_KEY` as a workaround
in every failure case, so users are never blocked.

## WAA — the defensive insurance

The bundle includes a key for `waa-pa.clients6.google.com` (Web App
Authenticity — Google's abuse-token-minting service). We've confirmed
that Matrix's prod `/v1/search` **does not currently validate** the
`bgProgramResponse` token, so we omit it.

**If Google starts enforcing it**: a request that worked yesterday will
return empty results today. The fix path is:
1. Add a WAA-token mint at session start (one HTTP call to the WAA
   endpoint with browser-fingerprint-shaped headers).
2. Include the returned token as `inputs.bgProgramResponse` in every
   search.

We don't implement this preemptively because:
- It adds 1 HTTP call to every session
- The token has a TTL we'd need to manage
- Our captures haven't shown server-side validation; building it now is
  speculative work

But the WAA key is in our cache file (commented out), the request shape
is documented in the captured HARs under `research/`, and the fix is a
30-minute exercise if/when it becomes necessary.

## Why no hardcoded key in source

Even though the key is fully public (anyone with devtools can see it in
30 seconds), shipping it in source:

- Triggers security scanners that flag `AIzaSy*` patterns
- Looks like "secret leaked in commit" in git-blame / archaeology
- Invites the (very small) risk of Google sending a polite-but-firm email
  about distributing the credential

The runtime-resolution approach has none of those costs and self-heals
if Google rotates.
