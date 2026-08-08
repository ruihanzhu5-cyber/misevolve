"""Audit a complete candidate skill bundle at SafeEvolve's write boundary."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from ._critic_llm import complete as _llm_complete

_HERE = Path(__file__).resolve().parent
_PROMPT_PATH = _HERE / "config" / "safeevolve_audit_prompt.md"

_DEFAULT_AUDIT_MODEL = "$BACKBONE"


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text()


def audit_prompt_hash() -> str:
    return "sha256:" + hashlib.sha256(_prompt().encode()).hexdigest()[:16]


def _bundle_text(bundle) -> str:
    parts = [bundle.text or ""]
    for rel, content in sorted((bundle.referenced_files or {}).items()):
        parts.append(f"\n\n===== referenced file: {rel} =====\n{content}")
    s = "\n".join(parts)
    return s[:24000]


def _parse(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
        t = t.split("```")[0]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(re.sub(r",(\s*[}\]])", r"\1", m.group(0)))
            except Exception:
                pass

    dec = re.search(r'"decision"\s*:\s*"(accept|repair|quarantine|reject)"', t)
    if dec:
        out: dict = {"decision": dec.group(1)}
        rk = re.search(r'"risk"\s*:\s*([0-9]*\.?[0-9]+)', t)
        if rk:
            out["risk"] = float(rk.group(1))
        cats = re.search(r'"risk_categories"\s*:\s*\[([^\]]*)', t)
        if cats:
            out["risk_categories"] = re.findall(r'"([^"]+)"', cats.group(1))
        return out
    raise ValueError("audit critic returned unparseable JSON")


def audit_candidate(bundle, ancestor_text: str | None, *, cfg) -> dict:
    """Run the independent critic. Returns a normalized audit dict; fail-safe -> quarantine."""
    model = cfg.extra.get("audit_model", _DEFAULT_AUDIT_MODEL)
    anc = ""
    if ancestor_text:
        anc = (
            "\nANCESTOR SKILL (the prior version — do NOT let this revision silently drop "
            "its safety requirements):\n---\n" + ancestor_text[:8000] + "\n---"
        )
    prompt = _prompt().replace("{bundle}", _bundle_text(bundle)).replace("{ancestor_section}", anc)
    try:
        raw = _llm_complete(prompt, model, max_tokens=2048)
        d = _parse(raw)
    except Exception as e:
        return _quarantine(f"critic error: {type(e).__name__}: {e}")
    dec = str(d.get("decision", "quarantine")).strip().lower()
    if dec not in ("accept", "repair", "quarantine", "reject"):
        dec = "quarantine"
    try:
        risk = float(d.get("risk", 0.5))
    except Exception:
        risk = 0.5
    return {
        "decision": dec,
        "risk": max(0.0, min(1.0, risk)),
        "risk_categories": list(d.get("risk_categories") or []),
        "unsafe_generalization": d.get("unsafe_generalization", ""),
        "omitted_verification": list(d.get("omitted_verification") or []),
        "authorization_required": list(d.get("authorization_required") or []),
        "irreversible_effects": list(d.get("irreversible_effects") or []),
        "egress_provenance": list(d.get("egress_provenance") or []),
        "secret_handling": list(d.get("secret_handling") or []),
        "evidence_spans": list(d.get("evidence_spans") or []),
        "required_repairs": list(d.get("required_repairs") or []),
        "confidence": d.get("confidence", 0.5),
        "model": model,
        "prompt_hash": audit_prompt_hash(),
    }


def _quarantine(reason: str) -> dict:
    return {
        "decision": "quarantine",
        "risk": 0.75,
        "risk_categories": ["audit_unavailable"],
        "unsafe_generalization": "",
        "omitted_verification": [],
        "authorization_required": [],
        "irreversible_effects": [],
        "egress_provenance": [],
        "secret_handling": [],
        "evidence_spans": [],
        "required_repairs": [],
        "confidence": 0.0,
        "reason": reason,
        "model": "none",
        "prompt_hash": audit_prompt_hash(),
    }
