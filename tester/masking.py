"""PII redaction applied to every text before it reaches an LLM or a report.

Masking runs before the model, not after: once PII is in a prompt it has left our control.
Known test values get stable tokens (<EMAIL_1>); regexes catch anything else that looks like PII.
Order IDs, statuses and timestamps are deliberately left visible because diagnosis needs them.
"""
import re

PATTERNS = [
    (re.compile(r"(?i)authorization:\s*\S+(\s+\S+)?"), "Authorization: <SECRET>"),
    (re.compile(r"Bearer\s+[\w.~+/=-]+"), "Bearer <TOKEN>"),
    (re.compile(r"\b(?:sk|gsk|AIza)[\w-]{16,}|\bAQ\.[\w-]{20,}"), "<API_KEY>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){12,15}\d(?!\d)"), "<CARD>"),
    (re.compile(r"(?<![\w+])(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)"), "<PHONE>"),
]


class Masker:
    def __init__(self):
        self.known: dict[str, str] = {}

    def register(self, kind: str, value: str) -> None:
        if value and value not in self.known:
            n = sum(token.startswith(f"<{kind.upper()}_") for token in self.known.values()) + 1
            self.known[value] = f"<{kind.upper()}_{n}>"

    def __call__(self, text: str) -> str:
        for value in sorted(self.known, key=len, reverse=True):  # longest first: address before name
            text = text.replace(value, self.known[value])
        for pattern, token in PATTERNS:
            text = pattern.sub(token, text)
        return text
