"""CLI: python -m tester walk [--until TRANSITION] [--headed] [--verbose]

Loads the lifecycle from spec/states.yaml, plans the route (whole chain, or up to --until),
runs it, and writes a markdown report. Exit code 1 when any step fails, so CI fails too.
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.rate_limiters import InMemoryRateLimiter

from tester.browser import BrowserSession
from tester.executor import CREATE_STEP, run_route
from tester.reporter import diagnose, render

STATES_FILE = Path(__file__).parent.parent / "spec" / "states.yaml"
log = logging.getLogger("tester")


def load_route(path: Path = STATES_FILE, until: str | None = None) -> list[dict]:
    """Every route starts at CREATED: seeding an order mid-lifecycle would skip the
    transitions where bugs hide and could create states impossible in real life."""
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    states, transitions = spec["states"], spec["transitions"]
    chain = [(t["from"], t["to"]) for t in transitions]
    if chain != list(zip(states, states[1:])):
        raise ValueError(f"{path.name}: transitions must form one chain through {states}, got {chain}")
    if until:
        ids = [t["id"] for t in transitions]
        if until not in ids:
            raise SystemExit(f"Unknown transition {until!r}. Choose from: {', '.join(ids)}")
        transitions = transitions[: ids.index(until) + 1]
    return transitions


class Usage(BaseCallbackHandler):
    """Counts LLM calls and tokens for the report footer."""

    def __init__(self):
        self.calls = self.tokens = 0

    def on_llm_end(self, response, **kwargs):
        self.calls += 1
        for gen in (g for batch in response.generations for g in batch):
            usage = getattr(getattr(gen, "message", None), "usage_metadata", None) or {}
            self.tokens += usage.get("total_tokens", 0)


def make_llm(usage: Usage):
    rpm = float(os.getenv("LLM_RPM", "4"))  # pace calls to stay inside free-tier per-minute limits
    limiter = InMemoryRateLimiter(requests_per_second=rpm / 60, check_every_n_seconds=0.1, max_bucket_size=1)
    return init_chat_model(os.environ["LLM_MODEL"], temperature=0, max_retries=2, rate_limiter=limiter, callbacks=[usage])


async def walk(args) -> int:
    route = load_route(until=args.until)
    session = BrowserSession(os.getenv("APP_URL", "http://localhost:8000"),
                             headless=not args.headed and os.getenv("HEADLESS", "true").lower() != "false")
    log.info("[run %s] route: %s", session.run_id, " -> ".join(t["id"] for t in route))
    if args.dry_run:
        return 0
    usage = Usage()
    llm = make_llm(usage)
    await session.start()
    try:
        results = await run_route(session, llm, route)
        planned = [CREATE_STEP["id"]] + [t["id"] for t in route]
        not_reached = planned[len(results):]
        report = await diagnose(llm, results, not_reached, session.mask)
    except Exception as e:  # a tester/LLM failure is not an app finding: say so and exit 2
        log.error("[run %s] aborted: %s: %s", session.run_id, type(e).__name__, session.mask(str(e))[:300])
        return 2
    finally:
        await session.close()
    footer = f"LLM calls: {usage.calls} · tokens: {usage.tokens / 1000:.1f}k"
    path = render(report, results, not_reached, session.run_id, os.environ["LLM_MODEL"], footer, session.mask)
    log.info("[report] %d finding(s) -> %s | %s", len(report.findings), path, footer)
    return 0 if all(r.passed for r in results) and not not_reached else 1


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m tester", description="Agentic state-traversal tester")
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("walk", help="walk a fresh order through the lifecycle")
    w.add_argument("--until", help="stop after this transition id (targeted run)")
    w.add_argument("--headed", action="store_true", help="show the browser")
    w.add_argument("--verbose", action="store_true", help="log every tool call")
    w.add_argument("--dry-run", action="store_true", help="print the route and exit")
    args = parser.parse_args()

    logging.basicConfig(format="%(message)s", level=logging.WARNING)
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    sys.exit(asyncio.run(walk(args)))


if __name__ == "__main__":
    main()
