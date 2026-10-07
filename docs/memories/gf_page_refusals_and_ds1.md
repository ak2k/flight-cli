# GF search page refusals and `ds:1` reading: typed `_gf_errors`, the blob contest, where rows sit, when an empty board is authoritative

How a fetched search page becomes rows or a typed refusal: the `Gf*Error`
types, which `ds:1` blob is served when a page carries several, the measured
row placements at `[2]`/`[3]`, and the positive scan that tells an empty board
from a moved layout. Read before touching `_gf_errors.py`,
`_gflight_ids._rows_from_page_html`, `_extract_ds1`, `_is_a_readable_board`, or
`_rows_from_ds1`.

**Refusals are typed** (`_gf_errors`):

- `GfThrottledError` — the captcha interstitial, which arrives three ways. A
  redirect puts `/sorry/` in the final URL; Google also serves the same page
  in place with HTTP 200, where the body marker ("Our systems have detected
  unusual traffic") is the only tell; and an outright **HTTP 429, which arrives
  as a RESPONSE**. `_get_search_page` goes through fli's session rather than
  `Client.get`, so nothing calls `raise_for_status()` on our behalf and
  `_rows_from_page_html` reads the status itself — which is what lets the ladder
  see a throttle instead of a wrapped transport error.
- `GfConsentError` — no `ds:1` *and* consent markers, checked in that order,
  because a real results page links to the consent domain in its footer.
- `GfPageShapeError` — no readable `ds:1`; or a payload too short to reach
  `[3]`; or a value at `[2]`/`[3]` that is neither absent nor row-shaped; or
  rows found outside `[2]`/`[3]` with none served; or rows present and none
  parsed (with sampled reasons).
- `GfUpstreamStatusError` — a non-2xx that is not a throttle. Typed apart from
  `GfPageShapeError` because "Google declined to serve this" and "the extract
  is broken" send a reader to different work, and either rung can raise it:
  rung 1 reads the status off the response, rung 2 off the navigation.
- `GfSearchServerError` — a 200 whose `ds:1` holds Google's server error in
  place of a board, with the error's `code`. Typed apart from
  `GfPageShapeError` for the same reason: the page has not changed shape. See
  "A server error in place of the board" below.
- `GfBrowserUnavailableError` — rung 2 could not produce bytes at all: no
  patchright, no Chrome, a profile another `flight` holds, a dead navigation.
  Never a statement about the route. Its `remedy` is a separate attribute that
  BOTH renderings must carry: the message quotes `str(e)`, and the note the
  default enrich path prints has to append it explicitly.

A page that decodes with zero rows returns `[]` and is Google's authoritative
answer, so the search path passes `retry_empty=False` and spends exactly one GET
on it.

**A server error in place of the board.** Google sometimes answers a search
page with HTTP 200 and a `ds:1` that holds an RPC status rather than rows:
`AF_initDataCallback({key: 'ds:1', data:[13,null,[[...ErrorResponse...]]],
errorHasStatus: true,});`. The blob ends on `errorHasStatus`, not on
`sideChannel`, so `_DS_BLOB_RE` never matches it, and read as a page with no
`ds:1` it would be reported as a page-shape change. `_ds1_error_status` reads
it (`_DS_ERROR_BLOB_RE`): the first blob keyed `ds:1` whose `data:` decodes to a
list with an int, not a bool, at `[0]`. It runs only when `_extract_ds1` found
no board, before the consent check, so a page that carries a board reads as
before. Measured on NYC-LON in 2026-10, the search fell back to Matrix this way
on 3 of 8 probe loads and 1 of 10 sampler loads; the three failing reads
captured, one load's reads of one URL, all carry code 13, and none of 300 clean
bodies or the 27 committed fixtures matches. Only 13 has been seen; any other
code is read as the same error by choice.

The error does not last, and its length is not known: three reads of one URL
inside 0.65 s all failed, and a read ~150 s later carried the board. Rung 1
reads such a page again after 2 s, then 6 s (`_SERVER_ERROR_PAUSES_S`, an arm of
`retry_throttled`: each re-read is one of the call's wall attempts and books no
rung of the shared ladder; see [gf_throttle_ladder.md](gf_throttle_ladder.md)).
A search pauses 8 s at most in all, whatever its page count
(`_Escalation.pause`); after that, a page's error is read again at once, three
reads, then refused. Rung 2 navigates it once more at once, as it does a page
with no `ds:1`. The refusal names the error and its code ("Google Flights
answered with a server error (status 13)") on every rendering, and the
fallback to Matrix is otherwise the one a re-shaped page gets.

**A page may carry more than one `ds:1` blob**, and this one hydrates in stages,
so `_extract_ds1` decodes them ALL and serves the one carrying the most rows at
`[2]`/`[3]`, earliest blob on a tie. Choosing by position is a bet: a
placeholder emitted above the populated blob reads as an authoritative empty (or
trips the arity guard) while the real board sits further down the document,
unexamined. Choosing on "carries a row block" is the same bet one level down,
because a staged blob can carry an empty husk `[[]]`, or one row where the
settled board carries thirty.

Only a blob the row scan could actually SERVE competes on rows
(`_is_a_readable_board`, which mirrors `_rows_from_ds1`'s two refusals: the
arity floor, and a value at `[2]`/`[3]` that is neither absent nor row-shaped).
A staged blob can be truncated above `[3]` or carry rows beside a placeholder at
`[3]`, and either holds MORE rows than the finished board — counting them turned
a served page into a typed refusal.

The count is **structural**, exactly like the scan at those indices: rows are
counted, never parsed, so a board whose rows have all changed shape still wins
and reaches the 0-of-N guard as a layout change rather than losing to a husk.
With no blob carrying rows the fallback takes the first one long enough to reach
`[3]`, then the first decodable one at all — so a genuinely flight-less page
stays flight-less and a truncated placeholder above it does not become a shape
error.

The accepted cost, measured: a readable blob whose `[2]` holds many row-shaped
NON-flight entries outranks the real board in either order, and the search then
refuses with "none of N Google Flights rows parsed". That fails loud and
degrades to Matrix, which is the right side of the trade — the alternative is a
selector that parses rows to choose between blobs, and it would drop a real
board whose row layout has just changed.

Loud only while NONE of the decoy's rows parse. A decoy carrying four rows one
of which is a genuine flight row beats a real three-row board and serves that
one flight, with no warning at any level — the 0-of-N guard never fires because
one of N parsed. Measured; unchanged by the readability floor. The trade is the
same one and still worth taking, but the failure it degrades to is a short
table rather than a refusal, which is the quieter half.

**Where a served page puts its rows varies, so no count of blocks is a validity
test.** Six live pages, measured 2026-09-02:

| page | `ds:1[2]` | `ds:1[3]` | arity |
|---|---|---|---|
| JFK-LAX one-way | 3 rows | 27 rows | 32 |
| HNL-MIA round-trip outbound, business | 3 rows | 5 rows | 31 |
| HNL-MIA round-trip outbound, first | 2 rows | 4 rows | 31 |
| HNL-MIA pinned return (business) | **`None`** | 3 rows | 27 |
| HNL-MIA pinned return (first) | 3 rows | 2 rows | 27 |
| HNL-MIA one-way nonstop, no nonstop exists | **`None`** | **`None`** | 32 |

Two things follow, and both cost a release-blocking bug to learn. A pinned leg
**may** omit `[2]` — the business return did, the first-class return did not, so
do not build a rule on it or on a story about top-flights ranking. And a
genuinely flight-less board is an ordinary results page with no flight cards and
**no block at either index** — refusing that reports "the page shape changed"
for a route that simply has nothing matching. Two synthetic shapes are also
pinned in the fixtures (an empty block `[[]]` at both indices, and at one); they
have never been seen in the wild and are labelled as synthetic, but an empty
block at those indices with nothing misplaced must not read as a refusal if
Google starts sending one. It must not win a blob contest either — an empty husk
carries no rows, which is why `_extract_ds1` counts rows rather than blocks.

So the guard is a POSITIVE scan rather than a count. `_rows_from_ds1` collects
rows from `[2]`/`[3]` structurally, and separately probes every OTHER top-level
index for a block whose leading rows actually parse as flight rows:

- rows only at `[2]`/`[3]` → those rows (however many blocks carried them)
- nothing row-shaped anywhere → `[]`, an authoritative empty
- rows found outside `[2]`/`[3]` and **none served from them** →
  `GfPageShapeError`
- rows found outside `[2]`/`[3]` **with rows also served** → the served rows,
  and a `log.warning` naming the indices
- a payload too short to reach `[3]`, or a value at `[2]`/`[3]` that is neither
  absent nor row-shaped → `GfPageShapeError`

`None` and a bare `[]` both count as ABSENT at `[2]`/`[3]`: neither carries rows
and neither claims anything, and `None` is the shape Google actually sends.
`[[]]` is a different fact — a block that exists and holds no rows. Either way
it serves no rows, and served rows are the only thing the refusal predicate
turns on; `blocks_seen` is carried for the message and the debug line, not for
the decision.

That last case is not pedantry. Enumerating a list never visits an index that
isn't there, so a truncated or junk `ds:1` (`[]`, `[null]`) would otherwise fall
straight through the scan and be served to the user as "no flights on this
route" at exit 0.

The two probes differ on purpose. Away from `[2]`/`[3]` the test must PARSE a
row, because `ds:1` carries other list-of-list-of-list structures on every page
(indices 1, 6, 7, 11, 14, 17, 25, 26 and 30 across the three captures) and a
nesting-depth test would report a relocation on every ordinary page. It reads
EVERY row, not a leading window: unparseable rows at the head of a moved block
are exactly what a layout change looks like, so any fixed depth is a number some
payload sits just past. The decoys hold 2-7 rows and the scan is sub-millisecond,
so full depth costs nothing worth a cutoff.
At `[2]`/`[3]` the test must NOT require a parse, or a block whose rows have all
changed shape would drop to an empty board instead of reaching the 0-of-N parse
guard above, which is what catches a moved ROW layout. Both share one tuple of
"this did not decode" exception types (`_ROW_PARSE_ERRORS`), so a widening —
`OverflowError` from an absurd price, `TypeError` from a null legs field — can't
land in one and miss the other.

**What this does not detect, stated plainly:** a partial relocation — rows
leaving `[2]` while `[3]` still parses — yields a short board. There is no
signal that proves it: one block is an ordinary served shape (the business
pinned return), so a missing block cannot be told from a board that never had
one. Refusing a single-block page to catch it breaks every round trip, which is
the worse trade; under-returning is the accepted cost.

Refusing on the misplaced-block probe alone is worse for the same reason. Live
pages carry 4 to 9 blocks that are row-shaped by structure (4, 9 and 7 across
the three captures), so a Google row-schema change that makes any ONE of them
parse would refuse a board we can already serve completely. A partial relocation
therefore under-returns **with a `log.warning` naming the indices**: the user
keeps their results, and the next maintainer has the indices to re-derive
from.

The refusal predicate is `misplaced and not rows` — rows found somewhere else
and none served from where we read. It deliberately does NOT also require
`not blocks_seen`: an empty block at `[2]`/`[3]` is not how Google answers a
flight-less search. The MEASURED flight-less shape is `None` at both indices,
and an empty husk `[[]]` has never been seen on a live page, so a husk plus
flight rows sitting elsewhere is far likelier a relocation than a coincidence —
and the two outcomes are not symmetric, since refusing degrades to Matrix while
reading it as an empty tells the user the route has no flights. A zero-row board
with nothing misplaced is still an authoritative empty.
