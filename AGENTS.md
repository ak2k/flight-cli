# Agent instructions

Contract for agents working in this repo. Read first; overrides defaults.
This is the single source of agent guidance — `CLAUDE.md` is a symlink to this
file, so Claude Code, Codex, and any other agent read the same contract.

This project follows the [`ak2k/python-starter`](https://github.com/ak2k/python-starter)
template (Profile A — distributable CLI). It is a power-user CLI wrapping ITA
Matrix's undocumented Alkali backend: a pydantic discriminated union →
match-based wire adapters → thin typer CLI shells, with golden-file regression
tests and `basedpyright`-strict type checking. As of 2026-05 an exhaustive
web search turned up zero other public wrappers of this endpoint
(see [`docs/memories/public_alkali_wrapper.md`](./docs/memories/public_alkali_wrapper.md));
our fixtures are the de facto spec.

## Stack (one per concern; substitutes are bans)

| Concern | Use | flight-cli uses | Not |
|---|---|---|---|
| Package manager | `uv` | ✓ | pip, poetry, pipenv, pyenv |
| Lint / format | `ruff` + `ruff format` | ✓ | black, isort, flake8, pylint |
| Type checker | `basedpyright` strict | ✓ (was `pyright`) | mypy, pyright |
| Boundary validation | Pydantic v2 | ✓ (see `wire.py`, `models.py`, `domain.py`) | dataclasses, attrs, TypedDict |
| HTTP | `httpx` | ✓ | requests, aiohttp, urllib |
| Async runtime | `anyio` | ✓ | raw asyncio |
| Logging | `structlog` | ✓ (see `log.py`) | `print()` |
| Paths | `pathlib.Path` | ✓ | `os.path` |
| Tests | `pytest` + `hypothesis` | `pytest` (no hypothesis yet) | unittest |
| Errors | subclass project-local error hierarchy | `MatrixApiError`, `ApiKeyResolutionError` | bare `Exception`, string errors |

## When you need it, use

Not every project hits these concerns. When yours does, this is the
choice that fits the rest of the stack — don't substitute.

| Concern | Use | flight-cli uses |
|---|---|---|
| Retry / backoff | `stamina` | ✓ (see `_http.py`) |
| CLI framework | `typer` (paired with `rich` for human-facing output) | ✓ (see `cli.py`) |
| Time injection in tests | `time-machine` | — |
| HTTP rate limiting (outbound) | `aiolimiter` | ✓ (see `_http.py`) |
| HTTP fingerprint evasion | `httpx-curl-cffi` transport (keep httpx API + `MockTransport` tests) | ✓ (see `_http.py`) |
| SQL | `sqlalchemy 2.0` Core | — |
| Async file I/O | `anyio.Path` | — |

## Workflow

- **Install**: `uv venv && uv pip install -e .` (Python 3.12+).
- **Test**: `pytest tests/` — golden-file regression net, runs in <0.1s.
- **Inner loop**:
  ```
  make fix     # autofix + full check
  make check   # full check (CI runs this)
  ```
  Both must be green. `make check` runs `ruff check`, `ruff format --check`,
  `basedpyright src tests`, and `pytest` — exactly the GitHub Actions `check`
  job, so green locally ⇒ green in CI. `filterwarnings = ["error"]` and
  `xfail_strict = true` are load-bearing: deprecation warnings and unexpected
  passes are real failures, not noise.
- **Use `basedpyright`, not vanilla `pyright`.** `basedpyright` is stricter
  (catches `reportUnknownVariableType`, `reportUnusedFunction`, etc.). Running
  `pyright` alone is a false-green; always gate on `make check`.
- **Smoke test against the live backend**:
  ```
  flight search JFK LHR --dep 2026-08-15 --return 2026-08-22 -n 2
  ```
  If `make check` is green but the smoke test fails, suspect the API key
  cache (`~/.cache/flight-cli/.matrix-key`, 30-day TTL) or a Matrix brownout
  (see quirk #7 below). The key is resolved at runtime from Matrix's SPA
  bundle — never hardcode it; override via the `FLIGHT_API_KEY` env var.

## Editing rules

- **Run pytest after any change to `wire.py`, `links.py`, `domain.py`.** The
  golden-file tests at `tests/fixtures/` catch field-name and ordering
  regressions in <100ms — exactly the class of bugs that hit us during the
  initial build.
- **New SPA captures go in `research/`** (gitignored). Use
  `research/record_user_session.py` to drive a real browser, capture a wire
  body, drop it into `tests/fixtures/`, and write a reconstruction test.
- **Never commit anything under `research/`.** It's the paper trail;
  `tests/fixtures/` is the tracked canonical set.
- **No hardcoded credentials** — `_api_key.py` does runtime resolution.

## Principles

1. **Boundaries fail loudly.** Pydantic at every external edge. Wire
   models in `wire.py` AND response models in `models.py` both use
   `extra="ignore"` — Matrix is reverse-engineered and adds fields
   unannounced in directions that aren't load-bearing for us
   (Profile-B-edge divergence; see "Appropriate divergence" below).
   Domain models in `domain.py` use `extra="forbid"` — that's where
   *we* control the contract. Domain errors (`MatrixApiError`,
   `ApiKeyResolutionError`) wrap third-party exceptions —
   `httpx.HTTPStatusError` never leaks to a caller.

2. **Suppress with cost.** Every `# pyright: ignore[code]` and `# noqa: code`
   names the specific rule and a one-line reason. Suppression is annotated
   debt — visible, greppable, justified — not a workaround.

3. **Copy the canonical example.** Inventing a new shape for any of these
   is a deliberate choice. The defaults are:
   - **Wire boundary (request)**: `src/flight_cli/wire.py` — pydantic
     models with `populate_by_name`, discriminated-union dispatch via
     `match` + `assert_never`.
   - **Wire boundary (response)**: `src/flight_cli/models.py` — `_Loose`
     base with `extra="ignore"`, `from_api()` classmethod constructors.
   - **Domain types (intent)**: `src/flight_cli/domain.py` — frozen
     pydantic models, discriminated by `kind` Literal, validators with
     domain-meaningful error messages.
   - **HTTP client**: `src/flight_cli/client.py` — single `execute()`
     entry point; wraps `httpx.HTTPStatusError` in `MatrixApiError` at
     the boundary.
   - **HTTP transport**: `src/flight_cli/_http.py` — `stamina.retry`
     decorator, `aiolimiter.AsyncLimiter`, `httpx-curl-cffi` for
     fingerprinting, on-disk JSON cache. structlog `BoundLogger` for
     cache-hit / retry / rate-limit diagnostics.
   - **Logging config**: `src/flight_cli/log.py` — idempotent
     `configure(level)` with ConsoleRenderer; stderr-bound for CLI use.
   - **Env + disk-cache config**: `src/flight_cli/_api_key.py` — env-var
     override → disk cache (TTL) → live resolution → cache write.
   - **Test pattern**: `tests/test_wire_round_trip.py` — captured SPA
     bodies as golden-file fixtures; reconstruct via `to_wire()` and
     compare.

4. **Diverge with a comment.** When tuning a default below (or deviating
   from the stack table), leave `# DIVERGE: <reason>` so future readers
   don't "fix" it back. Existing divergences from the template default:
   - Coverage gate at 0% — golden-file regression tests; fixture parity
     is the signal, not line coverage.
   - See "Appropriate divergence" table for the Profile-A→A/B-edge
     tunings (reportAny, Pydantic `extra` on boundary models).

5. **Ask when guessing.** Unknown Matrix wire shape, new SPA capture
   needed, irresolvable type error → ask. Don't invent the shape; capture
   a real body via `research/record_user_session.py` first.

6. **Tests assert behavior, not implementation.** Golden-file fixtures
   for wire-format round-trips (`tests/fixtures/`). `httpx.MockTransport`
   is the substitution point for hermetic HTTP tests. `monkeypatch.setenv`
   for env-driven settings. Assert on domain errors (`MatrixApiError`),
   not on HTTP status codes leaking through.

7. **Imports declare intent.** `from __future__ import annotations` at
   the top of every module; runtime-only third-party types inside
   `if TYPE_CHECKING:`.

8. **Async runtime is anyio.** Not raw asyncio. Compose with sync at the
   edges via `anyio.from_thread` / `anyio.to_thread`. CLI entrypoints use
   `anyio.run(coro_fn)` (not `asyncio.run(coro_fn())`). Concurrency
   primitives: `anyio.Semaphore`, `anyio.create_task_group()`. Providers
   and clients that hold async transports must be created, used, and
   closed within a single `anyio.run` — spanning their lifecycle across
   two `anyio.run` calls tears their sockets down on a dead loop.

## Appropriate divergence

This project is **Profile A — distributable CLI** with a Profile-B edge
(reverse-engineering / scraping) — the CLI surface is typed strictly,
but the integration boundaries with `fli`, `fast_flights`, Matrix's
undocumented JSON, and pydantic internals are inherently `Any`-typed.
Tunings applied versus the strict-service default:

| Knob | Default | flight-cli |
|---|---|---|
| `requires-python` | `>=3.13` | `>=3.12` |
| Ruff `D*` (docstrings) | enabled | omitted |
| `pythonVersion` (basedpyright) | `"3.13"` | `"3.12"` |
| Coverage `fail_under` | 80 | 0 (golden-file suite) |
| `PLR0913` (too many args) | strict | ignored (CLI verbs are wide) |
| `N815` (camelCase) | strict | per-file-ignored in `wire.py` only — direct camelCase attrs mirror Matrix's JSON without per-field `alias=`. `models.py` uses snake_case + `Field(alias="…")` so N815 doesn't fire there; `domain.py` is snake_case throughout. |
| `reportAny` | `"warning"` | `"none"` — Profile-B edge: fli/fast_flights ship no stubs; Matrix response is `dict[str, Any]` by design; `ValidationInfo.data` and `Field` overloads are Any internally. `reportUnknown*` stays on (higher-signal "can't type this"). |
| Pydantic `extra` on boundary models | `"forbid"` | `"ignore"` on wire + response (`_Wire`, `_Loose`) — Profile-B edge: Matrix is reverse-engineered and adds fields unannounced in directions that aren't load-bearing. Domain models stay `extra="forbid"` (we control that contract). |

## flight-cli load-bearing quirks (read before touching wire.py)

Full detail at [`docs/memories/MEMORY.md`](./docs/memories/MEMORY.md).

1. **`routeLanguage` ≠ `commandLine`.** Matrix slice has TWO distinct fields:
   `routeLanguage` for routing language (`"LH+"`, `"BA AA"`, `"[F* X F*]"`)
   and `commandLine` for extension codes (`"MAXCONNECT 2:00"`, `"MAXSTOPS 1"`).
   Confusing them returns `QPX Warning. Illegal COMMAND-LINE prefix`. Full
   references: [`routing_language.md`](./docs/memories/routing_language.md) +
   [`extension_codes.md`](./docs/memories/extension_codes.md).
2. **Per-mode field rules.** Specific-date emits `dateModifier` +
   `isArrivalDate` always; calendar + followup omit them. Followup omits
   `inputs.filter`. Calendar has `page: {size}`; specific + followup have
   `page: {current, size}`.
3. **`maxLegsRelativeToMin` defaults to 1**, not 10. Matches the SPA's
   "No limit" UI default. User override via `--stops N`.
4. **`timeRanges` is more flexible than the 6 named buckets.** Matrix
   accepts arbitrary `{min: "HH:MM", max: "HH:MM"}` ranges and multi-range
   arrays. The 6-bucket UI is one interface; the underlying API takes any
   well-formed range with `min < max`. Zero-padded and single-digit hours
   both work.
5. **Summarizer order is asserted by golden-file tests.** The captured
   SPA order is `["carrierStopMatrix", "currencyNotice", "solutionList", ...]`.
   Don't reorder unless you also rebase the fixtures.
6. **Multiple AIza keys in the SPA bundle.** Only the entry tagged
   `matrix` (e.g. `.matrix="AIza..."` or `"matrix":"AIza..."`) is the prod
   search key — siblings `matrix-nightly` / `matrix-uat` / `matrix-dev`
   share the prefix but route to different backends. Bootstrap regex
   anchors on the bare `matrix` label so it excludes the variants; a
   first-match-any-AIzaSy approach would pick the People API key and 403.
7. **Calendar compute-budget sheds ("brownouts").** Matrix's calendar engine
   silently returns 0 solutions (HTTP 200, no error or warning) once a query
   exceeds its per-query compute budget — cost grows roughly as
   (origins × destinations) × (departure days) × (routing complexity), and the
   ceiling is load-dependent (the same query that sheds under load prices fine
   later). It is *not* our encoding (byte-matches the SPA fixture) and *not*
   transient-per-request (retrying the same query in-window doesn't recover it).
   `flight calendar` auto-recovers: on an empty multi-airport result it splits
   per-destination, runs the sub-searches in parallel, and merges the grids
   (`_calendar_split.py` + `cli._run_calendar`). The gflight backend has an
   analogous empty-failure mode (cold curl_cffi session) handled separately by
   retry + NID-cookie persistence in `_gflight_ids.py`.
8. **Two-phase calendar flow.** `name: "calendar"` returns the date grid;
   user picks a date in the UI; `name: "calendarFollowup"` returns full
   itineraries for that date. Both use the same `/v1/search` endpoint.

When in doubt: capture a real SPA body via `research/record_user_session.py`
and write a reconstruction test in `tests/test_wire_round_trip.py`.

## Adding a search mode

1. New pydantic class in `domain.py` (extend `_SearchBase`, add `kind`
   Literal, register in the `Search` union).
2. Add a `case` in `wire.to_wire()`, `client._parse_response`,
   `links.matrix_deep_link`, `links.google_flights_url`, `fli_bridge.to_fli_filter`.
3. `assert_never` ensures pyright catches forgotten branches.
4. Capture a real SPA body to use as a golden-file fixture.
5. Write a reconstruction test in `tests/test_wire_round_trip.py`.

## Adding a search backend or award provider

The backend (Matrix vs Google Flights) is selected by `_pick_backend` in
`cli.py`, not encoded in the command name. Award providers (PointsPath,
seats.aero) live behind the `AwardProvider` protocol in `providers/base.py`
and are fanned out per leg by `providers/registry.py:gather_awards`. A new
provider implements the protocol, registers in `_construct_enabled`, and the
leg fan-out picks it up — the matcher and renderers stay provider-blind.

## Known gotchas (non-obvious from the toolchain)

- `extra="forbid"` Pydantic models raise on any unknown upstream field.
  Wire models use `extra="ignore"` instead — Matrix adds fields without
  notice and we don't want every shape addition to be a P0.
- `http.HTTPStatus.NOT_FOUND` — stdlib, well-typed. Not
  `httpx.codes.NOT_FOUND` (mis-typed as tuple) and not bare `404`
  (PLR2004).
- Domain `InputValidationError`-style errors should never shadow
  `pydantic.ValidationError` — keep our errors named distinctly
  (`MatrixApiError`, `ApiKeyResolutionError`).

## Agent skill

`.claude/skills/flight-search/SKILL.md` packages the routing-language grammar,
extension-code reference, airport-group expansions, and worked examples —
enough that an agent can translate a user's flight-search intent into the right
CLI invocation on the first try. It triggers on flight-search-shaped requests
in any Claude Code session working in this repo.

## Project memory

See [`docs/memories/MEMORY.md`](./docs/memories/MEMORY.md) for the topic index.
