"""Retrieval over spec/rules.md (the RAG tool). One chunk per rule, scored by keyword overlap.

Five rules would fit in any prompt; retrieval is here to show the pattern. A real
requirements corpus would swap this for BM25 or embeddings behind the same lookup().
"""
import re
from pathlib import Path

RULES_FILE = Path(__file__).parent.parent / "spec" / "rules.md"
STOP = {"the", "a", "an", "to", "of", "and", "or", "is", "be", "it", "how", "what", "should", "must", "with", "no", "any"}


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP}  # crude stem: allocation ~ allocated


RULES = re.findall(r"^- \*\*(R\d+)\.\*\* (.+)$", RULES_FILE.read_text(encoding="utf-8"), re.M)


def lookup(query: str, k: int = 3) -> list[str]:
    q = _stems(query)
    scored = sorted(RULES, key=lambda r: len(q & _stems(r[1])), reverse=True)
    return [f"{rid}. {text}" for rid, text in scored[:k] if q & _stems(text)]
