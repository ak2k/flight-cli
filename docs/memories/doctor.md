# `flight doctor`: checks, causes, exit codes, canary contract

`flight doctor` answers "why did my search break?" for a user and "is this a
shape change or flakiness?" for a scheduled canary. Code: `src/flight_cli/_doctor.py`
(checks and classification) and `cli.doctor` (rendering). Tests:
`tests/test_doctor.py`.

## Checks, in order

| id | kind | passes when |
|---|---|---|
| `config` | local | `config.toml` is absent, or parses, and the rps a search resolves (`FLIGHT_RPS`, then `[http].rps`) reads as a number |
| `matrix-key` | local | always, reporting the key a search would send: `FLIGHT_API_KEY`, the cache (with its age of 30 days), or none. Fails (`config`) when `FLIGHT_API_KEY` is not shaped like a Matrix key, or the cached key cannot be read |
| `cache` | local | the response cache opens and closes as `HttpTransport` opens it |
| `google-cookies` | local | the NID jar is absent, or parses (age of 14 days, NID count) |
| `matrix-spa-key` | live | Matrix's homepage and SPA bundle both answer 2xx and the bundle carries the key tagged `matrix`; says whether it is the key in use |
| `matrix-search` | live | one search (JFK-LAX one-way, today + 30 days) returns a solution priced `^[A-Z]{3}\d` whose first slice names a flight |
| `google-http` | live | the same leg on Google Flights' page over curl_cffi returns a row with a positive `flight.price` |
| `google-browser` | live | the same, in Chrome. Skipped when patchright is not installed or, with no override, no Chrome is at patchright's `channel="chrome"` path |
| `pointspath` | live | stored tokens are valid (refreshed if stale) and `/api/pricing-info` answers |
| `seats-aero` | live | the stored key passes the `whoami` probe. Costs one unit of the 1000-a-day quota |

A provider is checked when its credential is STORED, not when
`is_configured()` says so: that call refreshes, and turns a failed refresh
into "not configured", which would make a dead token a skip.

`matrix-search` sends the key in use, or `matrix-spa-key`'s key when none is
in use; with neither it is a skip naming `matrix-spa-key`. It sends with the
rps and impersonation profile a search resolves, the defaults when `config`
fails.

## Causes

| cause | retryable | from |
|---|---|---|
| `throttled` | yes | `GfThrottledError`, HTTP 429 anywhere |
| `unreachable` | yes | `GfTransportError`, an httpx transport error, `ApiKeyResolutionError` caused by one |
| `upstream` | yes | `GfUpstreamStatusError`, HTTP 5xx (after Matrix's three attempts) |
| `brownout` | yes | a Matrix timeout, a `solutionList` with no solution, a `MatrixApiError` of kind `INTERNAL` / `UNAVAILABLE` / `DEADLINE_EXCEEDED` or an internal-error message |
| `shape` | no | `GfPageShapeError`, `GfPinIgnoredError`, an empty Google board on the probe leg, a Matrix body without `solutionList` or one its parser rejects, solutions with no price or flight, an SPA page (2xx) without the bundle or the key |
| `rejected` | no | any other `MatrixApiError` |
| `consent` | no | `GfConsentError` |
| `auth` | no | Matrix refusing the key twice, `PPAuthError` (a Supabase 429 or 5xx on the token refresh is `throttled` or `upstream`), HTTP 401/403 from a provider |
| `config` | no | a local setting: unparseable config, an rps that is not a number, malformed `FLIGHT_API_KEY`, an unopenable cache, an unreadable jar, a stored PointsPath or seats.aero credential file that cannot be read, `FLIGHT_CLI_GF_BROWSER_BIN` naming no executable file |
| `browser` | no | `GfBrowserUnavailableError` (reason and remedy) |
| `error` | no | anything else, as `Type: message` |

The SPA check makes `_bootstrap_from_spa`'s two GETs itself and reads each
status first. The bootstrap reads the body of whatever answered, so a 503
homepage would otherwise read as "no bundle in the page", a shape change.
When Matrix refuses the key in use, the client refetches the page that way,
by its body alone. A refetch that finds no key therefore takes
`matrix-spa-key`'s cause. If `matrix-spa-key` passed, it is `upstream`: the
page served a key moments earlier, so it is failing now, not changed.

## Exit codes

- `0`: no check failed (skips do not count).
- `75` (EX_TEMPFAIL): at least one failure, and every failure is retryable.
- `1`: any failure that is not retryable.
- `2`: a bad `--format`.

A dead Matrix key is `auth`, never `unreachable`, so it can never exit 75.

## Canary contract

- Run `flight doctor --format json`; the document is
  `{"ok", "date", "checks"}`, `date` being the run date, each check
  `{id, status, detail, cause, retryable, seconds}` in the order above.
- Exit 75: re-run later; alert only when the same check fails across runs.
- Exit 1: read the failing check's `cause`. `shape` means a parser needs
  re-deriving (Google's page or Matrix's response moved); `auth` and `config`
  are the user's to fix.
- A `brownout` on `matrix-search` that persists across runs is a shape suspect.
  Matrix answers a body it rejects with the same HTTP 200 + "Internal server
  error" it gives an overloaded engine (`wire.py`'s trip-length note), so the
  cause alone cannot tell the two apart; persistence can.
- A live check passes only on an answer the CLI's own parser priced, so a
  pass is evidence the extract works end to end.

## What it writes and prints

- Writes only what a search writes: `matrix-spa-key` caches no key,
  `matrix-search` neither reads nor writes the response cache. A Matrix 403
  re-caches the key, `google-http` persists the NID jar, a stale PointsPath
  token is refreshed to disk, and `/api/pricing-info` is cached, exactly as a
  search does.
- Every detail is redacted before it is stored: each stored credential (the
  four env vars, the cached and resolved Matrix key, the PointsPath tokens,
  the seats.aero key) becomes `sha256:` + 8 hex, and any `key=` query value
  becomes `<redacted>`. A head of a secret that ends the text also becomes its
  fingerprint: the providers' errors quote only the first 200 characters of a
  body, which can cut a secret short. httpx quotes the whole request URL in its errors, and
  Matrix's carries the key.
- During the run, stamina's retry log is swapped for one that redacts the
  same way, since its default hook logs `repr` of the exception and so the URL.
- The table escapes every value through `_safe_text`; the JSON keeps the text
  and `json.dumps` escapes its control characters.
