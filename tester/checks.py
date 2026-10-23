"""Deterministic verification after every transition.

Verification is code, never the LLM: the agent decides how to click, but whether the order
really moved is a fact read from the backend, the UI and the captured errors.
"""
import asyncio

from pydantic import BaseModel


class StepResult(BaseModel):
    transition: str
    actor: str
    expected: str
    passed: bool
    failed_checks: list[str]
    agent_outcome: str
    agent_note: str
    actions: list[str]
    backend_status: str | None
    ui_status: str | None
    console_errors: list[str]
    failed_requests: list[str]
    server_logs: list[str]
    screenshot: str | None
    duration_s: float


async def verify(session, step: dict, outcome: str, note: str, actions: list[str], duration: float) -> StepResult:
    backend, ui = await session.backend_status(), await session.ui_status()
    for _ in range(4):  # R4: the UI may lag the backend by up to 2 s (two polling cycles)
        if ui == backend:
            break
        await asyncio.sleep(0.5)
        backend, ui = await session.backend_status(), await session.ui_status()
    console, requests = session.drain()
    history = [h["status"] for h in (await session.backend_order() or {}).get("history", [])]
    checks = {
        # "Reached" uses the backend history, not the current status: a system transition
        # (PAID -> ALLOCATED after ~1 s) may legitimately have moved the order on already.
        "backend_status": step["to"] not in history,
        "ui_matches_backend": ui != backend,
        "no_errors": bool(console) or any(r.startswith("5") for r in requests),
        "agent_outcome": outcome == "blocked",
    }
    failed = [name for name, bad in checks.items() if bad]
    mask = session.mask
    return StepResult(
        transition=step["id"], actor=step["by"], expected=step["to"], passed=not failed, failed_checks=failed,
        agent_outcome=outcome, agent_note=mask(note), actions=[mask(a) for a in actions],
        backend_status=backend, ui_status=ui,
        console_errors=[mask(c) for c in console], failed_requests=[mask(r) for r in requests],
        server_logs=[mask(line) for line in await session.server_logs()] if failed else [],
        # a stale UI is only visible on the customer page, wherever the agent happened to be
        screenshot=await session.screenshot(step["id"], "customer" if checks["ui_matches_backend"] else None)
        if failed else None,
        duration_s=round(duration, 1),
    )
