# GF throttle and transport retry: the shared single-prober ladder, `retry_throttled`, curl error classification, `auto`'s escalation to Chrome, Google's client-context rate budget

How the search retries a Google Flights throttle or an unreachable network:
one ladder per fan-out with a single prober, what ends a waiter's park, which
successes refill which rungs, which curl failures are retried, and the measured
throttle behind the reactive design. Read before touching
`_gflight_ids.retry_throttled`, `shared_throttle_ladder`, `_one_call_auto`,
`GfTransportError`, or `cli._run_gflight_multi`.

**One ladder per fan-out, not per cabin.** The multi-cabin path runs a cabin per
thread; laddering separately, four cabins spend 4 x 5 = 20 multi-megabyte GETs
against an IP already refusing us to learn what the first ladder learned.
`_gflight_ids.shared_throttle_ladder` — armed by `cli._run_gflight_multi` around
the fan-out — hands the group one ladder, and it is a **single prober**: the
first worker throttled owns the backoff and its retry is the probe, while any
other worker throttled meanwhile waits on that outcome instead of sleeping a
schedule of its own. A probe that gets through releases every waiter to retry,
so a wall that lifts inside the ladder serves the whole fan-out rather than
whichever cabin happened to be probing. When the rungs run out the waiters raise
without spending a request on a wall just measured. The transport budget rides
the same object because the network is one network, and it probes the same way:
the classifier admits only the curl failures that DO clear, so a waiter has an
outcome worth waiting for. Each arm keeps its own round; only the lock is
shared. One worker can own both at once, so standing down releases both — a
SUCCESS does not, and the next paragraph is where that asymmetry is stated.

**A waiter's park ends on the owner's report and on nothing else.** The wait
carries no clock, because there is nothing for one to decide: `release()` sets
the very event the waiter holds, so waking is the report arriving. Any rule that
lets a waiter go earlier — a timeout read as an answer, a poll — puts three more
multi-megabyte GETs in flight beside the prober's, which is the amplification
the shared budget exists to remove; and an owner IS slow by construction, since
every attempt of its ladder can burn the full request timeout. What bounds a
waiter is its own attempt count: it meets the wall at most
`_THROTTLE_RETRY_ATTEMPTS` = 4 times and the network at most
`_TRANSPORT_RETRY_ATTEMPTS` = 2, because the next meeting is `final` and returns
without parking. One park ends no later than the owner's remaining ladder —
`4 x REQUEST_TIMEOUT (60 s) + b1..b4 (<= 22.5 s) = 262.5 s` on the wall and
`124.5 s` on the network — so one wall waiter's whole call is bounded at
`5 x 60 + 4 x 262.5 = 1350 s` and one network waiter's at 429 s. What guarantees
the report arrives at all is `retry_throttled`'s `finally`, which stands an owner
down whatever door it leaves by. A round whose owner thread DIED without doing
so is released by the next worker to meet the same wall, which costs that worker
one GET against a wall this round had already measured. The case none of them
covers is a GET that never returns: the owner is then a worker thread the task
group is waiting on, so the command is wedged whatever its waiters do — that is
a request timeout's job, not a ladder's.

A call whose own attempts are spent never parks at all. It cannot use a backoff,
so waiting for one is latency it will throw away, and it takes no round it will
not probe. It does still book the rung of a round it already owns: that booking
is how the group learns the wall has been measured to the end, and an owner that
walked away without it leaves every waiter to spend a GET proving what the call
already knew — measured at 9 GETs for a four-cabin outage, against the
`3 + (cabins - 1)` the table above bounds one at, which is 6 for four cabins.
The table is in [gf_request_budget.md](gf_request_budget.md).

**Release before park.** A worker that is about to wait on another arm's round
gives up any round it still owns first. Two workers can otherwise each hold what
the other waits for, and nothing ends it: no rung is spent, so nothing exhausts.
The other half is `retry_throttled`'s `finally`, for the worker that crosses and
takes the second round instead of parking on it.

A successful call REFILLS the WALL's rungs: the wall is per-IP, so any call
getting through is evidence it lifted whoever made it, and a wall that returns
later is a different one. It does NOT refill the network's rungs for everybody —
fli's session is a `threading.local`, so the socket that carried a sibling's
call is no evidence about this one's, and crediting it let every healthy cabin
hand a failing one another rung. Only the worker that met a transport failure
gets those back.

**That is what the "one ladder" budget is bounded by — no success getting
through, not elapsed time.** And because the wall's refill is shared and
correct, the ladder alone cannot bound a single call: each `retry_throttled`
call carries its own attempt count as well, so a flapping link costs a bounded
number of requests per call whatever the siblings are doing. There is no
time floor: a success five milliseconds old refills the budget exactly as one
from half an hour ago does. So a wall that lets the prober past and closes again
refills on each probe and costs more than one ladder. No single count is quoted
for that here, because it turns on what "only the prober gets through" means: a
wall that stays open for as long as a prober is through costs about one ladder,
while one that admits only the owner's own probe costs roughly three times that
at three cabins. Each reading is deterministic; they are different questions.
What is bounded whatever the wall does is the per-call cost in the table above.
The table is in [gf_request_budget.md](gf_request_budget.md).

The rejected alternative is worth recording, because it is the lever if a
request bound is ever traded away. A shared DEADLINE — every worker retries on
its own schedule until one clock expires — recovers every cabin just as well
and does not bound requests at all, since each worker keeps spending until the
deadline. Bounding AND recovering needs a shared budget plus a broadcast of the
probe's outcome, which is a counter and a condition variable; that is what this
is.

A decorator cannot express this, which is why the loop is written out: retry
decorators bound ONE call against a counter of its own, while this budget
belongs to the per-IP wall and is shared sideways across worker threads.

Owning the ladder re-homes one thing fli's `Client.get` does for us: it also
retries transport errors three times. `retry_throttled` carries a third arm
for a curl-level failure — a reset connection, a read timeout — on a
deliberately smaller budget than the throttle arm. A throttle is a wall that
lifts on its own; a transport failure that survives three attempts is usually
the network being down, and a long backoff there only delays the Matrix
fallback the user is going to get anyway. When the budget is spent it becomes
`GfTransportError`, so the enriched path degrades to Matrix, `--backend gflight`
prints a typed line rather than a curl traceback, and the pin loop can tell an
unreachable network apart from a board that refused for its own reasons.

Only a failure to REACH Google is retried — `curl_cffi`'s `ConnectionError` and
`Timeout` (DNS, a reset socket, connect and read timeouts), **plus four
result codes those classes do not cover**: `PARTIAL_FILE`, `HTTP2`,
`HTTP2_STREAM` and `HTTP3`. curl_cffi maps several codes onto classes that also
carry permanent faults, so the class alone cannot decide — a multi-megabyte body
cut short arrives as `IncompleteRead` and the HTTP/2 and HTTP/3 stream errors
all arrive as `HTTPError`, which is otherwise a status never to retry.

Classify by class OR code, **minus a deny-list read first**. `SSLError`
subclasses `ConnectionError`, so the class arm sweeps in seven codes that name
this machine's own TLS setup: a CA bundle or CRL it cannot read, a crypto engine
it does not have, a pin that does not match, a client certificate the server
would not take. Those are identical on the third attempt, and reporting them as
"Google Flights could not be reached" sends the reader to the network for a
fault that is local. So TLS is both retried and not, by code — which is why the
decision is enumerated per code in the test rather than re-derived from the
rule: a test that restates the rule agrees with it even where it is wrong.

Everything else propagates on the first try, including the rest of curl's own
`CurlError` tree (`InvalidURL`, `InvalidSchema`, `SessionClosed`,
`CookieConflict`, `ImpersonateError`, `TooManyRedirects`). Those name a request
WE built wrongly — the shape a `build_search_tfs` regression takes — and
retrying a bug three times and relabelling it "Google could not be reached" is
how a defect becomes unfindable.

The request timeout is fli's own `REQUEST_TIMEOUT`, imported rather than copied:
it is the value that reads and validates `FLI_TIMEOUT`, and a duplicate constant
here silently ignores whatever the user set.

## `auto`: a throttle the ladder cannot clear moves the search to Chrome

Under `--gf-transport auto` a search runs rung 1 under the ladder above. The
order, from `_gflight_ids._one_call_auto` out:

1. http under the ladder;
2. the ladder spent on a throttle (`GfThrottledError`);
3. if the search has not escalated yet: one stderr line (`Google Flights
   rate-limited the request; opening Chrome (rung 2) for the rest of this
   search…`), the same request on Chrome, and the search's flag set;
4. Chrome answers, or its failure is that request's, never retried on http;
5. a pin loop that stopped after serving keeps its rows and carries the stop
   (`Board.stopped`);
6. `cli._PageAsk` gives that page up and asks nothing more;
7. `cli._report_pages` names every page not asked.

Every Google request of the search after step 3 goes straight to Chrome: the
remaining pins, the other pages, the Cheapest tab, `--split`'s one-ways. One
escalation costs one ladder (five GETs) before Chrome, and nothing after it
spends rung 1.

Only a throttle escalates. A transport failure is the network, which Chrome
shares, and a refusal of the page (a consent wall, a 503, a re-shaped page) is
the page's own. A refusal met after the escalation is worded as rung 2's
(`cli._rung_reached`): "rate-limited the browser rung", with no advice to wait
for a ladder Chrome does not run.

The flag is one object per search (`search_escalation`, opened by `cli.search`
on the thread that starts the workers, and by `cli._open_jaw_tickets` around an
open jaw's two one-ways, which Matrix's search asks beside its own answer), in
a ContextVar beside `_fanout_ladder`
for the same reason: every worker of the search reads the same object, so the
line prints once. A rung-1 GET reads it first, so a thread still backing off
when another escalated takes its next request to Chrome (`_EscalatedError`).
Chrome's lifetime is separate and thread-local, as on `browser`: opened by the
first request that needs it, closed where the browser transport closes one
(`cli._gflight_query`'s `finally`, `cli._browser_scope`), a no-op on a thread
that never escalated. The enriched path arms its interrupt guard under `auto`
as under `browser`, so Ctrl-C stops an escalated Chrome; an `auto` search that
never escalates pays for it with a second Ctrl-C ignored while its worker
finishes. The enriched path's `--split` runs on a worker of its own, so after
an escalation it opens a second Chrome there.

A multi-cabin search does not escalate: it fans its cabins out on http under
`auto`, since a thread per cabin escalating would be a Chrome per cabin on one
profile.

## GF throttle (per client-context, dynamic) — handle reactively, not with a fixed cap

Everything measured below was measured against the **RPC** transport, which is
what the date grid still uses. The search page is a different endpoint with a
different budget and a different block signal (the captcha interstitial, by
redirect or in place; or an HTTP 429, which arrives as a response status), so treat the
numbers as the grid's and re-measure before quoting them for the page. The
reactive design carries over unchanged: both raise `GfThrottledError` into the
same `retry_throttled` backoff.

The budget is keyed on **client context, not just IP.** Verified 2026-06-15
(`research/experiment_gf_patchright.py` + `capture_gf_request.py`): a real Chrome
(patchright, `channel=chrome`) pulled 10/10 `GetShoppingResults` from an IP that
was *simultaneously* `code-13` throttling our curl_cffi client (re-probed the same
minute). curl_cffi's chrome146 TLS fingerprint passes the edge (we reach the
backend — a structured error, not a CAPTCHA), but the generous budget is gated
behind dynamic, JS-generated session proof the SPA sends and we don't: URL
`f.sid`/`bl`, a token embedded in `f.req`, and the `x-goog-batchexecute-bgr`
per-request integrity token (plus `x-same-domain`/`origin`/`referer`, `accept: */*`
vs our navigation `text/html`, high-entropy client hints, and `OTZ`/`__Secure-BUCKET`
cookies beyond `NID`). No `x-client-data` and no `at` XSRF token are involved. So a
thin curl_cffi client gets a deliberately small budget that static header mirroring
can't fully close (bd work-udpp1). Datacenter VPN exits — e.g. PIA — are also
pre-flagged and blocked on sight; only residential IPs work.

Measured 2026-06-14 for the curl_cffi path (instrumented, distinguishing genuine
`code-13` from transport errors): two limits — a per-second burst cap (~3–4 at
~10/s, but it floated as high as 30 a run earlier) and a rolling allowance (~25–30
calls per ~2–3 min ≈ 10–12/min) — and **fast recovery** (the call right after a
block often returns data). Because the ceiling moves, a fixed rate limiter is the
wrong tool. The design is a closed loop: `_classify` detects a real block (HTTP 200
+ `ErrorResponse`/code-13 body, vs a transport exception, vs cold-session empty),
and `_one_call_with_retry` backs off + retries on a genuine block (typed
`GfThrottledError` on exhaustion). One-shot `flight` processes can't share a
proactive budget, but they DO share the `code-13` signal, so per-process reactive
backoff self-regulates even across concurrent invocations. In the woven flow a
persistent GF throttle degrades to Matrix-only rather than erroring.
