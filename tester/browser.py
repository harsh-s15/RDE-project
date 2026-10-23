"""Browser layer: one Playwright session per route, error capture, and the Executor's tools.

The agent never sees HTML: it gets a compact JSON page state (buttons, inputs, status, messages),
which costs fewer tokens and leaves only what a user could act on.
"""
import json
import uuid
from pathlib import Path
from typing import Literal

from faker import Faker
from langchain_core.tools import tool
from playwright.async_api import async_playwright

from tester import rules
from tester.masking import Masker

PAGES = {"customer": "/", "admin": "/admin"}
STATE_JS = """() => {
  const q = s => [...document.querySelectorAll(s)].filter(e => e.getClientRects().length > 0);
  const text = e => e.innerText.trim();
  return {
    headings: q('h1, h2').map(text),
    buttons: q('button').map(b => ({label: text(b), enabled: !b.disabled})),
    inputs: q('input').map(i => ({label: i.labels[0] ? text(i.labels[0]) : i.name, filled: i.value !== ''})),
    order_id: q('[data-testid=order-id]').map(text)[0] || null,
    status: q('[data-testid=status]').map(text)[0] || null,
    messages: q('[role=alert]').map(text).filter(Boolean),
  };
}"""


class BrowserSession:
    def __init__(self, app_url: str, headless: bool = True, seed: int = 42):
        self.app_url, self.headless, self.run_id = app_url.rstrip("/"), headless, uuid.uuid4().hex[:4]
        fake = Faker("en_IN")
        fake.seed_instance(seed)  # synthetic, reproducible test data; never real customers
        self.data = {"name": fake.name(), "email": fake.email(), "address": fake.address().replace("\n", ", "),
                     "phone": f"+91 {fake.random_element('6789')}{fake.numerify('#########')}"}
        self.mask = Masker()
        for key, value in self.data.items():
            self.mask.register(key, value)
        self.current, self.console_errors, self.failed_requests = "customer", [], []

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(base_url=self.app_url)
        await self.context.set_extra_http_headers({"X-Test-Run-Id": self.run_id})  # lets us pull this run's server logs
        self.pages = {}
        for name, path in PAGES.items():
            page = await self.context.new_page()
            # What DevTools would show: console errors, uncaught exceptions, failed network calls
            page.on("console", lambda m, n=name: m.type == "error" and self.console_errors.append(f"[{n}] {m.text}"))
            page.on("pageerror", lambda e, n=name: self.console_errors.append(f"[{n}] {e}"))
            page.on("response", self._on_response)
            await page.goto(path)
            self.pages[name] = page

    async def _on_response(self, response) -> None:
        if response.status >= 400:
            try:
                body = (await response.text())[:300]
            except Exception:
                body = ""
            self.failed_requests.append(f"{response.status} {response.request.method} {response.url}: {body}")

    async def close(self) -> None:
        await self.browser.close()
        await self._pw.stop()

    def drain(self) -> tuple[list[str], list[str]]:
        """Return and clear errors captured since the last drain (one step's worth)."""
        out = (self.console_errors, self.failed_requests)
        self.console_errors, self.failed_requests = [], []
        return out

    async def page_state(self) -> str:
        state = await self.pages[self.current].evaluate(STATE_JS)
        return self.mask(json.dumps({"page": self.current, **state}))

    async def ui_status(self) -> str | None:
        badge = self.pages["customer"].get_by_test_id("status")
        return ((await badge.inner_text()).strip() or None) if await badge.is_visible() else None

    async def backend_order(self) -> dict | None:
        if "order_id" not in self.data:
            return None
        response = await self.context.request.get(f"/api/orders/{self.data['order_id']}")
        return await response.json() if response.ok else None

    async def backend_status(self) -> str | None:
        order = await self.backend_order()
        return order["status"] if order else None

    async def read_order_id(self) -> None:
        oid = (await self.pages["customer"].get_by_test_id("order-id").inner_text()).strip()
        if oid:
            self.data["order_id"] = oid

    async def server_logs(self) -> list[str]:
        response = await self.context.request.get("/api/debug/logs", params={"run_id": self.run_id})
        return [f"{e['at']} {e['level']} {e['msg']}" for e in await response.json()][-10:]

    async def screenshot(self, name: str, page: str | None = None) -> str:
        page = page or self.current
        Path("reports").mkdir(exist_ok=True)
        path = f"reports/{self.run_id}-{name}-{page}.png"
        await self.pages[page].screenshot(path=path)
        return path

    def tools(self) -> list:
        """LangChain tools bound to this session. Every output is masked before the LLM sees it."""

        @tool
        async def get_page_state() -> str:
            """Return the current page as compact JSON: buttons, inputs, order id, status and messages."""
            return await self.page_state()

        @tool
        async def switch_page(name: Literal["customer", "admin"]) -> str:
            """Switch to the 'customer' page or the 'admin' (warehouse and courier) page. Returns its state."""
            self.current = name
            return await self.page_state()

        @tool
        async def click(label: str) -> str:
            """Click the button with this visible label. Returns the new page state."""
            page = self.pages[self.current]
            try:
                await page.get_by_role("button", name=label, exact=True).click(timeout=3000)
            except Exception:
                return f"No clickable button labelled {label!r}. " + await self.page_state()
            await page.wait_for_timeout(800)  # let the request finish and the page update
            return await self.page_state()

        @tool
        async def fill(label: str, data_key: str) -> str:
            """Type test data into the input with this label. data_key names the value (e.g. 'email');
            code resolves it, so you never type real values yourself."""
            if data_key not in self.data:
                return f"Unknown data_key {data_key!r}; use one of {sorted(self.data)}."
            try:
                await self.pages[self.current].get_by_label(label, exact=True).fill(self.data[data_key], timeout=3000)
            except Exception:
                return f"No input labelled {label!r}. " + await self.page_state()
            return f"Filled {label!r} with <{data_key}>."

        @tool
        def lookup_rules(query: str) -> str:
            """Look up the business rules relevant to a question, e.g. 'when should shipping happen'."""
            return "\n".join(rules.lookup(query)) or "No matching rule."

        @tool
        def finish(outcome: Literal["done", "blocked"], note: str) -> str:
            """End this step. 'done' = the action was performed; 'blocked' = the control was missing,
            disabled, or the app showed an error. note = one short sentence on what you saw."""
            return "ok"

        return [get_page_state, switch_page, click, fill, lookup_rules, finish]
