"""Executor: drives one route through the app, one transition at a time.

Code owns the route; the LLM only decides which clicks perform the current transition.
System transitions (no UI) are verified by polling, with no LLM at all.
"""
import asyncio
import json
import logging
import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from tester.checks import StepResult, verify

log = logging.getLogger("tester")
MAX_TOOL_CALLS = 8
RETRY = {"stop_after_attempt": 2, "exponential_jitter_params": {"initial": 15}}  # one retry, long enough for a rate-limit window
ACTOR_PAGE = {"customer": "customer", "warehouse": "admin", "courier": "admin"}
CREATE_STEP = {"id": "create_order", "from": None, "to": "CREATED", "by": "customer",
               "hint": "fill in the new order form with the test data and place the order"}

SYSTEM_PROMPT = """You are a QA tester operating a web shop through tools. You only see what a
user sees. You never see source code.

How to work:
1. Read the page state you are given. If you are on the wrong page, call switch_page.
2. Pick the controls whose labels best match the task. You may call several tools in one
   turn when the page state already shows every control you need (e.g. fill a whole form,
   then click its submit button).
3. Check the page state your last action returns.
4. When the action is done, call finish("done", short note).
5. If the control is missing or disabled, or the app shows an error, call
   finish("blocked", what you saw). Do not try workarounds.
You only report whether the action could be performed. Code checks the order status
afterwards, so a status that has not updated yet is not a reason to block.
To type into a field, use fill(label, data_key) with one of these data keys: {data_keys}.
If unsure what the app should do, call lookup_rules.
Keep reasoning to one sentence per turn."""

TASK_PROMPT = """Task: as the {actor}, on the {page} page, {hint}.
After this step the order should be {expected}.
Current page state:
{state}"""


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    calls: int
    finished: dict | None


def build_agent(llm, tools, trail: list[str]):
    """Two-node graph: the model picks tool calls, `act` runs them. Ends on finish() or the call cap."""
    by_name = {t.name: t for t in tools}
    model = llm.bind_tools(tools).with_retry(**RETRY)

    async def agent(state: AgentState):
        return {"messages": [await model.ainvoke(state["messages"])]}

    async def act(state: AgentState):
        out, finished, calls = [], None, state["calls"]
        for call in state["messages"][-1].tool_calls:  # sequential: the browser is one shared resource
            calls += 1
            fn = by_name.get(call["name"])
            order = list(fn.args) if fn else list(call["args"])  # signature order; providers may reorder args
            trail.append(f"{call['name']}({', '.join(json.dumps(call['args'][k]) for k in order if k in call['args'])})")
            if call["name"] == "finish":
                finished = call["args"]
            result = await fn.ainvoke(call["args"]) if fn else f"Unknown tool {call['name']}"
            log.debug("    %s -> %s", trail[-1], str(result)[:200])
            out.append(ToolMessage(str(result), tool_call_id=call["id"]))
        return {"messages": out, "calls": calls, "finished": finished}

    def after_agent(state: AgentState):
        last = state["messages"][-1]
        return "act" if isinstance(last, AIMessage) and last.tool_calls else END

    def after_act(state: AgentState):
        return END if state["finished"] or state["calls"] >= MAX_TOOL_CALLS else "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("act", act)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent, ["act", END])
    graph.add_conditional_edges("act", after_act, ["agent", END])
    return graph.compile()


async def run_agent_step(session, llm, step: dict, trail: list[str]) -> tuple[str, str]:
    agent = build_agent(llm, session.tools(), trail)
    task = TASK_PROMPT.format(actor=step["by"], page=ACTOR_PAGE[step["by"]], hint=step["hint"],
                              expected=step["to"], state=await session.page_state())
    system = SYSTEM_PROMPT.format(data_keys=", ".join(sorted(session.data)))
    final = await agent.ainvoke({"messages": [SystemMessage(system), HumanMessage(task)], "calls": 0, "finished": None},
                                {"recursion_limit": 2 * MAX_TOOL_CALLS + 4})
    if final["finished"]:
        return final["finished"].get("outcome", "blocked"), final["finished"].get("note", "")
    return "blocked", f"agent stopped without calling finish after {final['calls']} tool calls"


async def wait_for_status(session, step: dict) -> tuple[str, str]:
    deadline = time.monotonic() + step.get("timeout_s", 5)
    while time.monotonic() < deadline:
        if await session.backend_status() == step["to"]:
            return "done", f"reached {step['to']}"
        await asyncio.sleep(0.25)
    return "timeout", f"still {await session.backend_status()} after {step.get('timeout_s', 5)}s"


async def run_route(session, llm, route: list[dict]) -> list[StepResult]:
    """Every route starts by creating a fresh order, then walks the transitions in order."""
    results = []
    for step in [CREATE_STEP, *route]:
        session.drain()  # errors belong to the step that caused them
        trail: list[str] = []
        start = time.monotonic()
        if step["by"] == "system":
            log.info("[%s] waiting for %s (timeout %ss)", step["id"], step["to"], step.get("timeout_s", 5))
            outcome, note = await wait_for_status(session, step)
        else:
            outcome, note = await run_agent_step(session, llm, step, trail)
            log.info("[%s] agent: %s", step["id"], " -> ".join(trail) or "(no tool calls)")
        if step is CREATE_STEP:
            await session.read_order_id()
        result = await verify(session, step, outcome, note, trail, time.monotonic() - start)
        results.append(result)
        if result.passed:
            log.info("[%s] PASS  backend=%s ui=%s (%.1fs)", step["id"], result.backend_status, result.ui_status, result.duration_s)
        else:
            errors = result.failed_requests[:1] + result.console_errors[:1]
            log.info("[%s] FAIL  %s | backend=%s ui=%s (%.1fs) %s", step["id"], ",".join(result.failed_checks),
                     result.backend_status, result.ui_status, result.duration_s, " | ".join(errors))
            break  # later states cannot be reached honestly from a broken one
    return results
