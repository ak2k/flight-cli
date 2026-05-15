# Agent instructions

Contract for agents working in this repo. Read first; overrides defaults.

This project follows the [`ak2k/python-starter`](https://github.com/ak2k/python-starter)
template (Profile A — distributable CLI). See also `CLAUDE.md` for
flight-cli-specific load-bearing quirks (Matrix wire-format gotchas, SPA
capture workflow, two-phase calendar flow) — read both before non-trivial
work.

## Stack (one per concern; substitutes are bans)

| Concern | Use | flight-cli uses | Not |
|---|---|---|---|
| Package manager | `uv` | ✓ | pip, poetry, pipenv, pyenv |
| Lint / format | `ruff` + `ruff format` | ✓ | black, isort, flake8, pylint |
| Type checker | `basedpyright` strict | ✓ (was `pyright`) | mypy, pyright |
| Boundary validation | Pydantic v2 | ✓ (see `wire.py`, `models.py`, `domain.py`) | dataclasses, attrs, TypedDict |
| HTTP | `httpx` | ✓ | requests, aiohttp, urllib |
| Async runtime | `anyio` | asyncio (DIVERGE — see below) | raw asyncio |
| Logging | `structlog` | stdlib `logging` (DIVERGE — see below) | `print()` |
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

## Inner loop

```
make fix     # autofix + full check
make check   # full check (CI runs this)
```

Both must be green. `filterwarnings = ["error"]` and `xfail_strict = true`
are load-bearing — deprecation warnings and unexpected passes are real
failures, not noise.

Smoke test against the live backend:

```
flight fare JFK LHR --dep 2026-08-15 --return 2026-08-22 -n 2
```

If `make check` is green but the smoke test fails, suspect the API key
cache (`~/.cache/flight-cli/.matrix-key`) or a Matrix brownout (see
`CLAUDE.md` quirk #7).

## Principles

1. **Boundaries fail loudly.** Pydantic at every external edge. Wire
   models in `wire.py` use `extra="ignore"` for forward-compat (Matrix
   adds fields unannounced); domain models in `domain.py` and response
   models in `models.py` validate strictly. Domain errors
   (`MatrixApiError`, `ApiKeyResolutionError`) wrap third-party
   exceptions — `httpx.HTTPStatusError` never leaks to a caller. Shape
   drift is an alarm, not a silent fallthrough.

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
     fingerprinting, on-disk JSON cache.
   - **Env + disk-cache config**: `src/flight_cli/_api_key.py` — env-var
     override → disk cache (TTL) → live resolution → cache write.
   - **Test pattern**: `tests/test_wire_round_trip.py` — captured SPA
     bodies as golden-file fixtures; reconstruct via `to_wire()` and
     compare.

4. **Diverge with a comment.** When tuning a default below (or deviating
   from the stack table), leave `# DIVERGE: <reason>` so future readers
   don't "fix" it back. Existing divergences from the template default:
   - `asyncio` instead of `anyio` — flight-cli is single-runtime; no
     library-author concern. Cost of switching is real, value is small.
   - stdlib `logging` instead of `structlog` — CLI tool with rich-handler
     output; structured logging would target a sink we don't have.
   - Coverage gate at 0% — golden-file regression tests; fixture parity
     is the signal, not line coverage.

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

8. **Async runtime is asyncio (DIVERGE).** Not anyio. See divergence note
   in principle 4. Stick with asyncio unless we add a library-author
   concern.

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
| `N815` (camelCase) | strict | per-file-ignored in `wire.py`, `domain.py`, `models.py` — Matrix wire JSON dictates field names |
| `reportAny` | `"warning"` | `"none"` — Profile-B edge: fli/fast_flights ship no stubs; Matrix response is `dict[str, Any]` by design; `ValidationInfo.data` and `Field` overloads are Any internally. `reportUnknown*` stays on (higher-signal "can't type this"). |

## flight-cli load-bearing quirks (read before touching wire.py)

Full detail in [`CLAUDE.md`](./CLAUDE.md) and
[`docs/memories/MEMORY.md`](./docs/memories/MEMORY.md). One-line summaries:

- `routeLanguage` ≠ `commandLine` (two distinct wire fields).
- Per-mode field rules: specific-date emits `dateModifier` + `isArrivalDate`; calendar omits.
- `maxLegsRelativeToMin` defaults to 1, not 10.
- Calendar brownouts are real; not our bug.
- Two-phase calendar flow: `calendar` returns grid; `calendarFollowup` returns itineraries.
- Multiple AIza keys in the SPA bundle — only the bare `matrix` label is the prod key.

When in doubt: capture a real SPA body via `research/record_user_session.py`
and write a reconstruction test in `tests/test_wire_round_trip.py`.

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
