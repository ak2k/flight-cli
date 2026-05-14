# Matrix surfaces validation as HTTP 200 + error body

Matrix's Alkali backend speaks gapi-batch over HTTP, which has a specific
quirk: **input-validation errors come back as HTTP 200** with the failure
inside the response body. The HTTP client doesn't catch them; we have to.

## The shape

```jsonc
{
  "error": {
    "message": "QPX Warning.  Illegal COMMAND-LINE prefix: LH+",
    "type": "input"
  },
  "id": "30w1iqkMxuLlCcnnT0kOfA"
}
```

- HTTP status: **200** (not 400, not 422)
- `error.message` is the human-readable failure (often prefixed with
  "QPX Warning." — vestigial from Google's killed-in-2018 QPX Express API)
- `error.type` is usually `"input"` for client errors; `"server"` for
  backend issues
- `id` is the request UUID — useful when filing a Google-side bug report

`MatrixApiError` catches this in `_raise_if_api_error()`:

```python
def _raise_if_api_error(data: dict) -> None:
    err = data.get("error")
    if isinstance(err, dict) and ("message" in err or "code" in err):
        raise MatrixApiError(...)
```

## Errors you'll actually see

| Message | What triggered it |
|---|---|
| `Illegal COMMAND-LINE prefix: <X>` | Routing language put in `commandLine` instead of `routeLanguage` |
| `Unrecognized summarizer "<X>"` | Bad summarizer name in `summarizers[]` |
| `Invalid JSON payload received. Unknown name "<X>" at '<path>'` | Field name mistyped; the path tells you exactly where |
| `Unexpected format for DEP-TOFD-RANGES-LOCAL` | `timeRanges.min >= timeRanges.max` |
| `Search expired` | Stale `session` ID from a previous calendar response |

The `Unknown name "..." at '<path>'` message is **gold for schema
exploration**. Matrix's gateway is doing protobuf JSON validation; if
you POST a field it doesn't recognize, it tells you WHERE that field
was unknown. Use it as a debugger:

```sh
# What fields does inputs.slices[0] accept?
curl ... --data '{"inputs":{"slices":[{"calendarDuration":"5-7"}]}}'
# → "Unknown name \"calendarDuration\" at 'inputs.slices[0]'"
```

You can probe for hypothetical field names this way and ALL get back
useful structured errors. Most reverse-engineering of new modes will
go through this path.

## Empty results vs. errors

Three different "didn't work" modes look superficially similar:

| Symptom | Diagnosis | What to do |
|---|---|---|
| `solutionCount: 0`, `currencyNotice: {}` | Valid query, no solutions | Loosen filters; try different dates |
| `error.message: "QPX Warning..."` | Input invalid | Fix the field; CLI surfaces the message via `MatrixApiError` |
| Request never returns / 30s timeout | Backend brownout for that query | Retry; simplify (fewer destinations, fewer flexibilities) |

Only the middle case raises `MatrixApiError`. The first and third surface
as a successful response with no solutions — the CLI prints "No
solutions returned." with a hint about brownouts.
