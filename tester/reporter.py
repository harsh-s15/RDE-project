"""Reporter: one structured LLM call turns masked step evidence into a BugReport; code renders markdown.

The LLM judges evidence; it never formats the report, so the layout and the hand-off block
are always the same shape. A clean run needs no judgement and skips the LLM entirely.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from tester import rules
from tester.checks import StepResult
from tester.executor import RETRY

REPORTS = Path("reports")
CHECK_TOPICS = {  # what each failed check is about, used as the rules query
    "backend_status": "order must reach status",
    "ui_matches_backend": "status shown to the customer match backend",
    "no_errors": "server error 5xx browser console error",
    "agent_outcome": "who ships delivers pays order",
}

PROMPT = """You are writing a bug report from an automated end-to-end test run.
Input: step results (masked) and the business rules that apply.

Rules for the report:
- One finding per failing transition.
- Every claim must cite evidence from the input (step, log line, response).
- Each evidence item is one short quote: a single log line, response or observation,
  never a whole step record.
- Cite the violated rule by ID (e.g. R2).
- Do not guess file names or code locations; you have no code access.
- If evidence is insufficient, say so instead of guessing.

Input:
{evidence}"""


class Finding(BaseModel):
    title: str
    severity: Literal["high", "medium", "low"]
    failing_transition: str
    failed_checks: list[str]
    rule_violated: str = Field(description="Rule ID, e.g. R2")
    expected: str
    actual: str
    evidence: list[str] = Field(description="Quoted log lines, responses or observations from the input")
    repro_steps: list[str]
    suspected_area: str = Field(description="Black-box hint, e.g. 'background allocation never completes'")
    next_step: str


class BugReport(BaseModel):
    summary: str = Field(description="Two or three sentences")
    findings: list[Finding]


def evidence_for(results: list[StepResult], not_reached: list[str]) -> str:
    """Compact input: one line per passing step, full evidence only for the failing one."""
    lines = []
    for r in results:
        if r.passed:
            lines.append(f"PASS {r.transition}: backend={r.backend_status} ui={r.ui_status}")
        else:
            query = f"{r.transition} {r.expected} " + " ".join(CHECK_TOPICS[c] for c in r.failed_checks)
            # One fact per line, so the model quotes lines as evidence instead of a whole JSON record
            lines += [f"FAIL {r.transition} (actor {r.actor}, expected {r.expected})",
                      f"  failed checks: {', '.join(r.failed_checks)}",
                      f"  backend status: {r.backend_status}; UI status: {r.ui_status}",
                      f"  agent: {r.agent_outcome}: {r.agent_note}",
                      *[f"  agent action: {a}" for a in r.actions],
                      *[f"  failed request: {x}" for x in r.failed_requests],
                      *[f"  console error: {x}" for x in r.console_errors],
                      *[f"  server log: {x}" for x in r.server_logs]]
            lines.append("Relevant rules:\n" + "\n".join(rules.lookup(query)))
    lines.append(f"Not reached: {', '.join(not_reached) or 'none'}")
    return "\n".join(lines)


async def diagnose(llm, results: list[StepResult], not_reached: list[str], mask) -> BugReport:
    if all(r.passed for r in results):
        return BugReport(summary="Every transition in the route passed. No findings.", findings=[])
    prompt = mask(PROMPT.format(evidence=evidence_for(results, not_reached)))  # defence in depth: mask again
    return await llm.with_structured_output(BugReport).with_retry(**RETRY).ainvoke(prompt)


def render(report: BugReport, results: list[StepResult], not_reached: list[str], run_id: str,
           model: str, usage: str, mask) -> Path:
    passed = all(r.passed for r in results) and not not_reached
    out = [f"# E2E run {run_id}: {'PASS' if passed else 'FAIL'}", "",
           f"{datetime.now():%Y-%m-%d %H:%M} · model `{model}`", "", "## Summary", "", report.summary, "",
           "## Steps", "", "| Step | Result | Backend | UI | Duration | Failed checks |", "| --- | --- | --- | --- | --- | --- |"]
    for r in results:
        out.append(f"| {r.transition} | {'PASS' if r.passed else 'FAIL'} | {r.backend_status} | {r.ui_status} "
                   f"| {r.duration_s}s | {', '.join(r.failed_checks) or '-'} |")
    out += [f"| {t} | not reached | | | | |" for t in not_reached]
    for i, f in enumerate(report.findings, 1):
        failing = next((r for r in results if r.transition == f.failing_transition), results[-1])
        out += ["", f"## Finding {i}: {f.title}", "",
                f"- **Severity:** {f.severity}", f"- **Transition:** `{f.failing_transition}`",
                f"- **Rule violated:** {f.rule_violated}", f"- **Failed checks:** {', '.join(f.failed_checks)}",
                f"- **Expected:** {f.expected}", f"- **Actual:** {f.actual}",
                f"- **Suspected area:** {f.suspected_area}", f"- **Next step:** {f.next_step}"]
        if failing.screenshot:
            out.append(f"- **Screenshot:** `{failing.screenshot}`")
        out += ["", "**Evidence**", ""] + [f"- {e}" for e in f.evidence]
        out += ["", "**Hand-off to coding agent**", "", "```text",
                f"Bug: {f.title} (rule {f.rule_violated})",
                f"Failing transition: {f.failing_transition}. Expected: {f.expected}. Actual: {f.actual}",
                "Repro steps:", *[f"  {n}. {s}" for n, s in enumerate(f.repro_steps, 1)],
                "Runtime evidence (masked):", *[f"  {line}" for line in failing.server_logs],
                *[f"  {req}" for req in failing.failed_requests], *[f"  console: {c}" for c in failing.console_errors],
                f"Suspected area: {f.suspected_area}", "```"]
    out += ["", "---", "", usage, ""]

    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{datetime.now():%Y-%m-%dT%H-%M-%S}-{run_id}.md"
    path.write_text(mask("\n".join(out)), encoding="utf-8")
    return path
