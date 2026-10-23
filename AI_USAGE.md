# AI usage log

## Tools used

- **Claude:** design phase (problem framing, architecture, scope cuts).
- **Claude Code:** implementation, debugging and test runs.
- **Gemini (`gemini-flash-lite-latest`, free tier):** runtime LLM for the Executor agent and the Reporter.

## Runtime model choice

- Ollama was rejected. The dev laptop has 4 GB VRAM, so a model capable enough for reliable multi-step tool calling (8B+) would run partly on CPU, and reviewers would need a ~5 GB download.
- Gemini's free tier was chosen because the key is free and it has native tool calling and JSON-schema output. Groq remains a one-line `.env` alternative.
- `gemini-flash-latest` was tried first, but its free tier allows only **5 requests/minute and 20/day**. The daily cap ran out after about two walks. Quotas are per model, so the tester moved to `gemini-flash-lite-latest`, which still made correct tool calls. Calls are paced with `LLM_RPM`.

## Implementation bugs found by running it

- **Transient PAID state.** Allocation runs ~1 s after payment, so the `pay` check saw ALLOCATED and failed on a clean run. The check now asks whether the order *reached* the expected state, using the backend status history, rather than comparing the current status. This was found with a scripted (no-LLM) model before any API key was used.

## Prompt iterations

| # | Prompt | What went wrong | Change | Result |
|---|---|---|---|---|
| 1 | Executor | "One action per turn" meant 6 LLM calls just to fill the order form; with a 5 RPM limit, `create_order` took 60 s and the run hit the rate limit. | Allow several tool calls in one turn when the page already shows every needed control. | Full walk dropped from ~20 to 9 calls; form filled in one turn. |
| 2 | Executor | Flash-Lite called `finish("blocked")` after clicking Pay now because the badge had not refreshed yet (it polls every second): a false failure. | Added: "You only report whether the action could be performed. Code checks the order status afterwards." The app also updates the badge immediately after payment. | S2 then ran to its real failure at `ship`. |
| 3 | Reporter | Evidence items were whole step records pasted as JSON. | Added "each evidence item is one short quote"; the failing step is now passed as one fact per line instead of JSON. | Evidence became quoted lines, e.g. `backend status: SHIPPED; UI status: ALLOCATED`. |
