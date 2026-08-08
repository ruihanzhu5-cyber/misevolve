"""SafeEvolve targeted repair (paper RQ3).

Given a candidate SKILL.md + the auditor's required_repairs, produce a repaired SKILL.md
that KEEPS the useful procedure but restores omitted checks / narrows applicability /
re-gates consequential actions. The wrapper re-audits the repaired bundle before admission
(bounded by cfg.max_repair_attempts). Returns repaired text, or None if repair is unusable.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from ._critic_llm import complete as _llm_complete

_HERE = Path(__file__).resolve().parent
_PROMPT_PATH = _HERE / "config" / "safeevolve_repair_prompt.md"

_DEFAULT_REPAIR_MODEL = "$BACKBONE"


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text()


def repair_prompt_hash() -> str:
    return "sha256:" + hashlib.sha256(_prompt().encode()).hexdigest()[:16]


def _clean(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("markdown") or t.startswith("md"):
            t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.split("```")[0]
    return t.strip()


def _loadable(text: str) -> bool:
    """The repaired SKILL.md must still be a loadable skill (frontmatter name, or a heading)."""
    if not text or not text.strip():
        return False
    s = text.lstrip()
    if s.startswith("---") and re.search(r"(?im)^name\s*:", s):
        return True
    return any(line.lstrip().startswith("#") for line in text.splitlines())


def repair_candidate(bundle, audit: dict, ancestor_text: str | None, *, cfg) -> str | None:
    """Return a repaired SKILL.md, or None if the repair is unavailable/unusable."""
    model = cfg.extra.get("repair_model", _DEFAULT_REPAIR_MODEL)
    reqs = (
        "\n".join(f"- {r}" for r in (audit.get("required_repairs") or []))
        or "- (auditor gave no specific list; apply the general repairs above)"
    )
    anc = ""
    if ancestor_text:
        anc = "\nANCESTOR SKILL (keep every safety requirement it asserts):\n---\n" + ancestor_text[:6000] + "\n---"
    prompt = (
        _prompt()
        .replace("{required_repairs}", reqs)
        .replace("{skill}", (bundle.text or "")[:12000])
        .replace("{ancestor_section}", anc)
    )
    try:
        out = _clean(_llm_complete(prompt, model, max_tokens=1600))
    except Exception:
        return None
    return out if _loadable(out) else None
