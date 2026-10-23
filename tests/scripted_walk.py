"""Pipeline check with no LLM: a scripted model replays fixed tool calls against each BUGS setting.

Exercises the real app, browser, capture, checks and masking without an API key or quota.
Run from the repo root:  python tests/scripted_walk.py
"""
import asyncio
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.getcwd())
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from tester.__main__ import load_route  # noqa: E402
from tester.browser import BrowserSession  # noqa: E402
from tester.executor import run_route  # noqa: E402

PLANS = {  # keyed by a phrase from each step's hint
    "place the order": [("fill", {"label": f, "data_key": f.lower()}) for f in ["Name", "Email", "Phone", "Address"]]
    + [("click", {"label": "Place order"}), ("finish", {"outcome": "done", "note": "placed"})],
    "pay for": [("click", {"label": "Pay now"}), ("finish", {"outcome": "done", "note": "paid"})],
    "shipped": [("switch_page", {"name": "admin"}), ("fill", {"label": "Order ID", "data_key": "order_id"}),
                ("click", {"label": "Mark shipped"}), ("finish", {"outcome": "done", "note": "shipped"})],
    "delivered": [("click", {"label": "Mark delivered"}), ("finish", {"outcome": "done", "note": "delivered"})],
}
EXPECTED = {"": None, "allocation": "allocate", "stale_ui": "ship", "deliver_500": "deliver"}


class ScriptedModel(BaseChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        task = messages[1].content.splitlines()[0]
        turn = sum(isinstance(m, AIMessage) for m in messages)
        name, args = next(plan for key, plan in PLANS.items() if key in task)[turn]
        return ChatResult(generations=[ChatGeneration(message=AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"c{turn}"}]))])

    @property
    def _llm_type(self):
        return "scripted"


async def walk(port: int) -> str | None:
    session = BrowserSession(f"http://127.0.0.1:{port}")
    await session.start()
    try:
        results = await run_route(session, ScriptedModel(), load_route())
    finally:
        await session.close()
    leaked = [v for k, v in session.data.items() if k != "order_id" and v in str([r.model_dump() for r in results])]
    assert not leaked, f"PII leaked into step results: {leaked}"
    return next((r.transition for r in results if not r.passed), None)


def main() -> int:
    procs, ok = [], True
    for i, bugs in enumerate(EXPECTED):
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(8101 + i)],
                                      env={**os.environ, "BUGS": bugs}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        for i, (bugs, expected) in enumerate(EXPECTED.items()):
            for _ in range(40):  # wait for the server to come up
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{8101 + i}/")
                    break
                except Exception:
                    time.sleep(0.25)
            failed_at = asyncio.run(walk(8101 + i))
            ok &= failed_at == expected
            print(f"BUGS={bugs or '(none)':12} failed at: {failed_at or '-':9} expected: {expected or '-':9} "
                  f"{'OK' if failed_at == expected else 'MISMATCH'}")
    finally:
        for p in procs:
            p.terminate()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
