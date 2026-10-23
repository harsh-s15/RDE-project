# Implementation Summary

This document complements the pre-development design spec (*Agentic State-Traversal Tester: Project Spec*). The spec explains the idea, the architecture and the intended build; this document records what was actually built, every place the build differs from the spec, and the low-level details and findings the spec doesn't contain. Read the spec first for the "why"; read this for the "what exactly".

## 1. Status

The **core scope** from the spec's "Scope cut to 200–500 lines" section is built and has been run against a real LLM. The **stretch items** are not built.

| Spec milestone | Status | Notes |
|---|---|---|
| 1. Toy app + 3 bugs | Done | Single HTML page serving both views |
| 2. Browser layer + checks | Done | Verified with a scripted no-LLM model (`tests/scripted_walk.py`) |
| 3. Route planner | Done (reduced) | Chain validation + `--until` slicing + `--dry-run`, as the scope cut specifies |
| 4. Executor | Done | Explicit LangGraph graph (see §4.3) |
| 5. Masking | Done | Unit-tested |
| 6. Rules retrieval | Done | Keyword scoring, as the scope cut specifies |
| 7. Reporter | Done | One structured call; skipped when everything passes |
| 8. Recorder + replay + self-heal | **Not built** | Stretch |
| 9. LLM-parsed targeted goals | **Not built** | Stretch; `--until <transition>` covers targeted runs without an LLM |
| 10. GitHub Actions | **Not built** | Stretch |
| 11. README / AI_USAGE / sample report | Done | `README_v2.md`, `AI_USAGE.md`, `samples/` |

All four samples (S0–S3) produce the expected result with the real model (§7).

## 2. Environment and stack as built

| Item | Spec | Built | Why |
|---|---|---|---|
| Python | 3.11+ | **3.10** | Only 3.9/3.10 were installed; nothing needs 3.11 |
| Runtime LLM | Configurable (Gemini/Groq/Anthropic) | **Gemini `gemini-flash-lite-latest`** (free tier), configurable via `LLM_MODEL` | Free key, native tool calling and JSON-schema output; see §6 for why not `gemini-flash-latest` |
| Local LLM (Ollama) | – | Rejected | Dev laptop has 4 GB VRAM; models good enough for multi-step tool calling (8B+) would run partly on CPU, and reviewers would need a ~5 GB download |
| Agent framework | `create_react_agent` or a two-node graph | **Explicit two-node LangGraph `StateGraph`** | Makes the 8-call cap and the stop-on-`finish` rule visible in code |
| Playwright API | not specified | **Async** API throughout | The agent's tools run inside LangGraph's async loop; the sync Playwright API is bound to its creating thread and breaks if a framework runs tools on worker threads |
| Retrieval | `rank_bm25` (later: keyword scoring) | Keyword overlap with crude 5-character stemming | 5 rules; the scope cut asked for no library |
| Extras | – | `python-dotenv` for `.env`; `pytest` | |

Pinned versions are in `requirements.txt` (LangGraph 1.2, LangChain 1.4 / core 1.6, Playwright 1.64, FastAPI 0.147, Pydantic 2.14).

## 3. Toy app (system under test), low-level

**Pages.** One file, `app/static/index.html`, served at `/` (customer view) and `/admin` (warehouse and courier view); JavaScript shows the right `<main>` based on the path.
- **Customer view:** a form (Name, Email, Phone, Address, **Place order**), then an order card with order ID, status badge, **Pay now** and a message line.
- **Admin view:** an **Order ID** input, **Mark shipped**, **Mark delivered** and a message line.
- Buttons are plain `<button>` elements with visible text; inputs are wrapped in `<label>`s; messages use `role="alert"`.
- The badge and order ID carry `data-testid` attributes. These are read **only by the tester's code** (for checks and the page-state builder), never chosen by the agent.
- A `data:` favicon prevents a `/favicon.ico` 404, which Chromium would log as a console error and which would fail every step's `no_errors` check.

**UI behaviour:**
- The customer page polls a status endpoint every second.
- **Pay now** is disabled once the status is no longer CREATED.
- After a successful payment the badge updates immediately from the API response rather than waiting for the next poll. This was added after the agent misread a not-yet-refreshed badge as a failure.

**API:**

| Endpoint | Behaviour |
|---|---|
| `POST /api/orders` | Creates an order (6-hex-char id) in CREATED |
| `POST /api/orders/{id}/pay` | CREATED → PAID, then schedules allocation (~1 s later, background task) |
| `POST /api/orders/{id}/ship` | ALLOCATED → SHIPPED |
| `POST /api/orders/{id}/deliver` | SHIPPED → DELIVERED |
| `GET /api/orders/{id}` | Backend ground truth: full order with `status` and `history` (list of `{status, at}`) |
| `GET /api/orders/{id}/view` | **Customer-facing status cache**, polled by the customer page. *Not in the spec*; it is where the `stale_ui` bug lives |
| `GET /api/debug/logs?run_id=` | Log entries `{at, run_id, level, msg}` for that run |

Out-of-order moves return **409** with a message; unknown ids return **404**.

**Logging and run tagging:**
- Middleware reads `X-Test-Run-Id` from every request.
- The order stores the run id at creation, so the **background allocation job**, which has no request, can still tag its log lines.
- Only non-GET requests are logged, because the 1-second polling would otherwise drown the signal.
- An unhandled exception in an endpoint is caught by the middleware, logged as `ERROR <method> <path> -> 500 <ExceptionType>: <message>`, and returned to the client as a bare `{"detail": "Internal Server Error"}`. The cause is visible only in server logs, as in a real app.
- The creation log line deliberately contains the customer's name and email, to show masking at work.

**Planted bugs** (env `BUGS`, read once at startup, so restart the app to change it; `.env` is also read, and real env vars take precedence):

| Bug | Implementation | What the tester sees |
|---|---|---|
| `allocation` | Background job raises `RuntimeError("AllocationError: no warehouse slot for order <id>")` and logs it | Order stays PAID; only a server-log ERROR line |
| `stale_ui` | `ship` updates the order but skips refreshing the `/view` cache | Backend SHIPPED, customer badge stays ALLOCATED |
| `deliver_500` | `deliver` reads a missing `order["courier_id"]` → `KeyError` → middleware 500 | HTTP 500, Chromium console error, UI shows "Error: Internal Server Error", status stays SHIPPED |

State is in memory and lost when the app restarts.

## 4. Tester, low-level

### 4.1 Run lifecycle (`tester/__main__.py`)

1. Load `.env`, parse CLI (`walk [--until T] [--headed] [--verbose] [--dry-run]`).
2. Load `states.yaml` and validate that the transitions' `(from, to)` pairs equal consecutive pairs of `states` exactly. Any gap, branch or reordering raises. Slice at `--until`.
3. Create the `BrowserSession` (run id = 4 hex chars) and log the route. `--dry-run` stops here.
4. Build the LLM, run the route, call the Reporter, render the report.
5. Exit `0` (all passed), `1` (a step failed), or `2` (the tester itself failed, e.g. LLM quota exhausted). Exit code 2 is not in the spec; it keeps "the app is broken" separate from "the tester is broken".

**LLM setup:**
- `init_chat_model(LLM_MODEL, temperature=0, max_retries=2, rate_limiter=…, callbacks=[Usage])`.
- The rate limiter is LangChain's `InMemoryRateLimiter` at `LLM_RPM`/60 requests per second (default 4/min), with a bucket size of 1, so calls are evenly spaced.
- Every LLM runnable is also wrapped in `.with_retry(stop_after_attempt=2, initial backoff 15 s)`, which is the spec's "retry once on rate limits".
- A `Usage` callback counts LLM calls and total tokens for the report footer and the final log line.

### 4.2 Browser session (`tester/browser.py`)

**Setup:**
- One Chromium browser and one context per run. The context has `base_url=APP_URL` and the `X-Test-Run-Id` header, which also applies to the page's own `fetch` calls.
- The session opens **two pages** in the same context: `customer` (`/`) and `admin` (`/admin`). The agent switches between them with a tool; the session tracks the "current" page.

**Test data:**
- `Faker("en_IN")` seeded with 42 gives name, email and address. The phone number is generated as `+91 [6-9]` followed by 9 digits.
- All four values are registered with the masker.
- After the order is created, code reads the order id from the page and adds it as the data key `order_id`, so the agent can fill the admin page's Order ID field by key.

**Error capture** (the DevTools equivalent), attached to both pages:

| Playwright event | Captured | DevTools equivalent |
|---|---|---|
| `console` with type `error` | `"[page] <text>"` | Console errors, including Chromium's own "Failed to load resource: … status of 500" |
| `pageerror` | Uncaught JS exceptions | Red exceptions in Console |
| `response` with status ≥ 400 | `"<status> <method> <url>: <first 300 chars of body>"` | Network tab, failed requests |

Captured errors are cleared at the start of each step ("drain"), so each step is judged only on errors it caused.

**Page state** (the agent's only view of the page) is JSON built by one `page.evaluate` script over visible elements:

```json
{"page": "customer", "headings": ["Toy Shop", "Your order"],
 "buttons": [{"label": "Pay now", "enabled": true}], "inputs": [],
 "order_id": "948e40", "status": "CREATED", "messages": ["Order placed"]}
```

Inputs report only `{label, filled: bool}`, never their values. The whole string is masked before it is returned.

**Tools** (LangChain `@tool`s closed over the session):

| Tool | Behaviour |
|---|---|
| `get_page_state()` | Returns the page state above |
| `switch_page(name)` | `customer` or `admin`; returns that page's state |
| `click(label)` | `get_by_role("button", name=label, exact=True)` with a 3 s timeout, then waits 800 ms for the request and DOM update; returns the new state. On failure it returns "No clickable button labelled …" plus the state, instead of raising, so the agent can recover or block |
| `fill(label, data_key)` | Looks up the value by key (unknown keys get an error listing valid keys) and fills `get_by_label(label, exact=True)`. Returns `Filled 'Email' with <email>`; the value never reaches the LLM |
| `lookup_rules(query)` | Top 3 matching rules from `rules.md` |
| `finish(outcome, note)` | `done` or `blocked`; intercepted by the graph to end the step |

**Other code-only helpers:**
- `backend_order()` / `backend_status()` call `GET /api/orders/{id}` through the context's request client.
- `ui_status()` reads the customer badge.
- `server_logs()` returns the last 10 log lines for the run id.
- `screenshot(step, page)` saves a screenshot under `reports/`.

### 4.3 Executor (`tester/executor.py`)

**Route:** a synthetic first step, `create_order` (actor customer, target CREATED), is prepended to every route, followed by the planned transitions. The run stops at the first failing step.

**System steps** (`by: system`) use no LLM. Code polls the backend every 0.25 s until the status equals `to`, or until `timeout_s` (5 s for allocation) passes, which gives outcome `timeout`.

**UI steps** run a fresh LangGraph graph per transition:

```text
START -> agent --(tool calls?)--> act --(finish called or 8 tool calls)--> END
           ^                       |
           +-------- otherwise ----+
```

- **State:** `messages`, `calls` (tool calls made so far), `finished` (the arguments of `finish`, if called).
- **`agent` node:** the model with all six tools bound, plus retry.
- **`act` node:** runs **every** tool call from the last model turn **sequentially**, because the browser is one shared resource. It appends `ToolMessage`s, records a human-readable trail such as `click("Pay now")` with arguments in signature order (Gemini sometimes reorders them), and counts calls.
- **Limits:** a cap of 8 tool calls per transition and a LangGraph recursion limit of 20.
- **Ending:**
  - If the model stops without calling `finish`, or hits the cap, the outcome is `blocked` with an explanatory note.
  - Otherwise the outcome and note come from `finish`.

**Prompts:**
- The system prompt follows the spec's draft, with two additions from testing: batching is allowed when every needed control is visible, and "you only report whether the action could be performed; code checks the status".
- The task message includes the **current page state**, which saves a `get_page_state` call per step. In the spec, `get_page_state` was always the first call.
- Full prompt text and the reasons for each change are in `AI_USAGE.md`.

Typical agent trails with the real model:

```text
create_order: fill×4 -> click("Place order") -> finish(done)            (1–2 LLM turns)
pay:          click("Pay now") -> finish(done)
ship:         switch_page("admin") -> fill("Order ID","order_id") -> click("Mark shipped") -> finish(done)
deliver:      click("Mark delivered") -> finish(blocked, "…Internal Server Error")   [with deliver_500]
```

### 4.4 Checks (`tester/checks.py`)

After each step:
1. Read backend and UI status. If they differ, re-read every 0.5 s for up to 2 s (R4's allowance).
2. Drain the captured errors.
3. Fetch the order history.
4. Evaluate the four checks:

| Check | Fails when |
|---|---|
| `backend_status` | The target state is **not in the order's history**. *Spec said "backend status equals `to`"*; that failed clean runs, because allocation moves PAID → ALLOCATED about 1 s after payment, before `pay` is verified. Using history checks that the order *reached* the state, allowing a system transition to move it on |
| `ui_matches_backend` | Badge ≠ backend status after the 2 s grace period |
| `no_errors` | Any console error, or any 5xx response. Note: Chromium logs 4xx responses as console errors too, so a 409/404 also fails this check |
| `agent_outcome` | Agent reported `blocked` |

**Output:** a `StepResult` (Pydantic) with every field masked. Server logs and a screenshot are collected **only for failing steps**. If `ui_matches_backend` failed, the screenshot is of the **customer** page, where the stale badge is visible; otherwise it is of the page the agent was on.

### 4.5 Masking (`tester/masking.py`)

1. Registered values (name, email, phone, address) are replaced first, longest first, with stable numbered tokens (`<NAME_1>`, `<EMAIL_1>`, `<ADDRESS_1>`, `<PHONE_1>`).
2. Then regexes, in this order:
   - `Authorization:` header values → `<SECRET>`
   - `Bearer …` → `<TOKEN>`
   - API keys (`sk…`, `gsk…`, `AIza…`, and Google's newer `AQ.…` format) → `<API_KEY>`
   - any email → `<EMAIL>`
   - 13–16-digit card-like numbers → `<CARD>`
   - Indian mobile numbers (optional `+91`, 10 digits starting 6–9) → `<PHONE>`
3. Order ids (6 hex chars), statuses and ISO timestamps pass through untouched.

Masking is applied in four places:
- every tool output and the initial page state, before they reach the Executor
- every `StepResult` field
- the Reporter prompt (again, as defence in depth)
- the final markdown file

### 4.6 Reporter (`tester/reporter.py`)

- **All steps passed:** no LLM call. The report says "No findings".
- **Otherwise, one `with_structured_output(BugReport)` call.** Its input is:
  - one line per passing step
  - the failing step as **one fact per line** (failed checks, backend/UI status, agent outcome and note, each action, each failed request, console error and server log)
  - the top rules retrieved for that step. The query is built from the transition, its target state and a topic phrase per failed check, e.g. `ui_matches_backend` → "status shown to the customer match backend".
- **Schema:** `BugReport{summary, findings[]}`. `Finding` has the spec's fields (title, severity, failing_transition, failed_checks, rule_violated, expected, actual, evidence[], repro_steps[], suspected_area, next_step). *Differs from the spec:* `passed_transitions` and "not reached" are computed by code, not asked of the LLM.
- **Rendering (code):**
  - title with PASS/FAIL, date and model
  - summary
  - a step table, including "not reached" rows
  - per finding: details, screenshot path, the LLM's evidence quotes, and a fenced **"Hand-off to coding agent"** block
  - a footer with LLM calls and tokens
- **The hand-off block's runtime evidence comes from the captured `StepResult`**, not from the LLM, so it is exact even if the model's wording is weak.
- **Output file:** `reports/<YYYY-MM-DDTHH-MM-SS>-<run>.md`.

## 5. What a run costs

| | S0 full pass | S1 | S2 | S3 |
|---|---|---|---|---|
| LLM calls | 9 | 5 | 8 | 10 |
| Tokens | 8.2k | 4.4k | 7.5k | 9.2k |
| Wall time | ~2.5 min | ~1.2 min | ~2 min | ~2.5 min |

Wall time is dominated by the 4-per-minute pacing (15 s per call). Model latency and the app are a few seconds in total.

## 6. Provider findings (free tier, 2026-10-19)

- `gemini-flash-latest` resolved to `gemini-3.8-flash`, which allows **5 requests/minute and 20 requests/day** on the free tier. Two full walks exhaust a day.
- Quotas are **per model**. `gemini-flash-lite-latest` and `gemma-4-31b-it` were each checked with a single tool-calling request and both called the right tool. Flash-Lite was chosen and passed S0–S3. Its exact daily cap is not documented here; about 40 calls in one day did not hit it.
- A 429 error's `quotaId` says which limit was hit: `…PerMinute…` vs `…PerDay…`.
- Gemini returns `AIMessage.content` as a list of content blocks (with "thought signatures"), not a plain string. The code never parses free-text content; it uses only tool calls and structured output.
- Groq (`groq:llama-3.3-70b-versatile`, package installed) is the documented fallback; switching is one `.env` line. It has not been run end to end.

## 7. Verification

- **Unit tests** (`pytest -q tests`, 5 tests):
  - the loader accepts the chain, slices at `--until`, and rejects a gap and a branch
  - masking hides name, email, phone, Bearer token and a stray email while keeping order id, status and timestamp
  - `lookup_rules("how fast should allocation happen")` returns R2 first
- **Scripted walk** (`python tests/scripted_walk.py`): a fake chat model replays fixed tool calls. It starts four app instances (ports 8101–8104, one per BUGS setting), checks each run fails at the expected transition (none / allocate / ship / deliver), and asserts no PII appears in the step results. No API key is needed.
- **Real-model runs:** S0–S3 all correct (failing transition, rule and LLM usage per sample are in §5 and in `README_v2.md`); a real S3 report is in `samples/`.
- **Not yet done:** reproducing each bug by hand in a browser; three consecutive S0 runs for consistency (limited by daily quota).

## 8. Bugs found during development

| Symptom | Cause | Fix |
|---|---|---|
| Clean run failed at `pay` (backend ALLOCATED) | Exact-status check vs a ~1 s system transition | Check "reached" against status history |
| Run aborted with 429 mid-walk | Pacing (8/min) above the 5/min free limit; 6 LLM turns just for the order form | Pace at 4/min; allow batched tool calls; retry with 15 s backoff; exit 2 with a clear message |
| S2 failed at `pay` instead of `ship` | Agent blocked because the badge hadn't refreshed after Pay now | Prompt: code checks status, not the agent; app updates the badge immediately after payment |
| Evidence items were whole JSON step records | Failing step was given to the Reporter as one JSON blob | One-fact-per-line input + "each evidence item is one short quote" |
| S2 screenshot showed the admin page | Screenshot taken of the agent's current page | Use the customer page when the UI check fails |

## 9. Known limitations

- **Model quality.** Flash-Lite is a light model. Wording varies between runs, and it once labelled an evidence quote `PASS` for a failed step. Verification never depends on the LLM, so this affects report prose, not pass/fail.
- **Speed and quota.** Each walk takes minutes and uses about 10 requests of a small daily allowance.
- **Single-run assumptions.** One run at a time per browser session. Runs against the same app are isolated by run id, but nothing has been tested in parallel.
- **No replay.** Every run uses the LLM; the "LLM explores, script replays" idea from the spec is not built.
- **Debug endpoint.** `/api/debug/logs` is unauthenticated, which is acceptable only for a test fixture.
- **Windows noise.** On Windows, an aborted run may print a harmless `RuntimeError: Event loop is closed` from asyncio's Proactor transport after exit.
- **Size.** About 440 lines of tester logic plus about 100 of prompts and docstrings, and about 100 lines of app Python plus 90 of HTML. That is above the brief's 500-line target; the remainder is the agent's prompts and the decision comments the brief asks for.
- **The design spec file is git-ignored.** Share it alongside this document, since this summary assumes it has been read.

## 10. File map

| File | Code / text lines* | Role |
|---|---|---|
| `app/main.py` | 96 / 5 | Toy shop API, bugs, logging |
| `app/static/index.html` | 89 | Both views |
| `tester/__main__.py` | ~80 / 7 | CLI, route planner, LLM + usage |
| `tester/browser.py` | ~108 / 27 | Session, capture, page state, tools |
| `tester/executor.py` | ~90 / 26 | Agent graph, prompts, route loop |
| `tester/checks.py` | ~45 / 7 | Four checks, `StepResult` |
| `tester/reporter.py` | ~85 / 18 | Diagnosis schema, prompt, rendering |
| `tester/masking.py` | 22 / 5 | PII redaction |
| `tester/rules.py` | 11 / 4 | Rule retrieval |
| `spec/states.yaml`, `spec/rules.md` | – | Lifecycle and business rules |
| `tests/test_units.py`, `tests/scripted_walk.py` | – | Unit tests, no-LLM pipeline check |

\* Non-blank lines; "text" means docstrings, comments and prompt strings.
