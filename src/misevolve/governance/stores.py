"""Per-method GovernableStore adapters (paper RQ3).

Each adapter presents the six governable-store operations over a target method's
OWN skill store, preserving native authoring/retrieval/update. The methods are NOT
edited — the adapter reaches their store the same way the method itself does.

  EvoSkill  / SkillsVote : store is a raw <name>/SKILL.md dir tree re-read fresh on
                           every retrieval, so set_skill_status = physically move the
                           bundle dir out of the active dir (reversible sidecar).
  AutoSkill              : retrieval is served from an in-memory LocalSkillStore cache,
                           so hiding a bundle also refreshes the store from disk (or
                           falls back to the SDK's native delete).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import GovernableStore, SkillBundle, Status


def _frontmatter(text: str) -> dict:
    s = (text or "").lstrip()
    if not s.startswith("---"):
        return {}
    end = s[3:].find("\n---")
    if end == -1:
        return {}
    out = {}
    for line in s[3 : 3 + end].splitlines():
        k, _, v = line.partition(":")
        if k.strip():
            out[k.strip().lower()] = v.strip().strip("\"'")
    return out


def _read_bundle(dir_path: Path, key: str, version: str | None = None) -> SkillBundle:
    md = dir_path / "SKILL.md"
    text = md.read_text(encoding="utf-8", errors="replace") if md.exists() else ""
    refs: dict[str, str] = {}
    if dir_path.is_dir():
        for f in sorted(dir_path.rglob("*")):
            if f.is_file() and f.name != "SKILL.md":
                try:
                    refs[str(f.relative_to(dir_path))] = f.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    refs[str(f.relative_to(dir_path))] = ""
    fm = _frontmatter(text)
    return SkillBundle(key=key, text=text, referenced_files=refs, version=version or fm.get("version"))


def _structurally_valid(text: str) -> bool:
    """Native structural check ONLY: a loadable SKILL.md with a name. No content/safety."""
    if not text or not text.strip():
        return False
    fm = _frontmatter(text)
    if fm.get("name"):
        return True

    return any(line.lstrip().startswith("#") for line in text.splitlines())


class EvoSkillStoreControl:
    def __init__(self, method):
        self.m = method
        self._skills_dir: Path = method._skills_dir
        self._hidden = Path(method._root) / "_governance_hidden"
        self._hidden.mkdir(parents=True, exist_ok=True)
        self._seeded = set(getattr(method, "_seeded", set()))

    def _dirs(self):
        for d in sorted(self._skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists() and d.name not in self._seeded:
                yield d

    def snapshot_skill_bundles(self):
        return [_read_bundle(d, d.name) for d in self._dirs()]

    def is_valid(self, key):
        md = self._skills_dir / key / "SKILL.md"
        return _structurally_valid(md.read_text(encoding="utf-8", errors="replace")) if md.exists() else False

    def replace_skill(self, key, text, referenced_files=None):
        d = self._skills_dir / key
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        for rel, content in (referenced_files or {}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    def set_skill_status(self, key, status):
        active = self._skills_dir / key
        held = self._hidden / key
        if status == Status.ACTIVE:
            if held.exists():
                if active.exists():
                    shutil.rmtree(active)
                shutil.move(str(held), str(active))
        else:
            if active.exists():
                if held.exists():
                    shutil.rmtree(held)
                shutil.move(str(active), str(held))

    def active_skills(self):
        return self.m.authored_skills()

    def last_retrieved_skill_keys(self):

        return [s.key for s in self.m.authored_skills()]

    def export_active_skills(self):
        return self.m.authored_skills()


class SkillsVoteStoreControl:
    def __init__(self, method):
        self.m = method
        self._store_dir: Path = method._store_dir
        self._hidden = self._store_dir.parent / "skillsvote_store_hidden"
        self._hidden.mkdir(parents=True, exist_ok=True)
        self._chroma = getattr(method, "_chroma_dir", None) or (self._store_dir.parent / "skillsvote_chroma")

    def _dirs(self):
        for md in sorted(self._store_dir.glob("*/SKILL.md")):
            yield md.parent

    def snapshot_skill_bundles(self):
        return [_read_bundle(d, d.name) for d in self._dirs()]

    def is_valid(self, key):
        md = self._store_dir / key / "SKILL.md"
        return _structurally_valid(md.read_text(encoding="utf-8", errors="replace")) if md.exists() else False

    def replace_skill(self, key, text, referenced_files=None):
        d = self._store_dir / key
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        for rel, content in (referenced_files or {}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self._bust_index()

    def set_skill_status(self, key, status):
        active = self._store_dir / key
        held = self._hidden / key
        if status == Status.ACTIVE:
            if held.exists():
                if active.exists():
                    shutil.rmtree(active)
                shutil.move(str(held), str(active))
        else:
            if active.exists():
                if held.exists():
                    shutil.rmtree(held)
                shutil.move(str(active), str(held))
        self._bust_index()

    def _bust_index(self):

        try:
            if self._chroma and Path(self._chroma).exists():
                shutil.rmtree(self._chroma)
        except Exception:
            pass

    def active_skills(self):
        return self.m.authored_skills()

    def last_retrieved_skill_keys(self):
        lr = getattr(self.m, "_last_retrieved", None) or []
        return [getattr(s, "key", None) for s in lr if getattr(s, "key", None)]

    def export_active_skills(self):
        return self.m.authored_skills()


def _reach_store(sdk):
    """Best-effort reach the underlying LocalSkillStore (for refresh_from_disk)."""
    for chain in ("store", "_store", "client.store", "_client.store", "client._store", "_client._store"):
        obj = sdk
        try:
            for part in chain.split("."):
                obj = getattr(obj, part)
            if obj is not None and hasattr(obj, "refresh_from_disk"):
                return obj
        except Exception:
            continue
    return None


class AutoSkillStoreControl:
    def __init__(self, method):
        self.m = method
        self.sdk = method.sdk
        self._uid = method._UID
        self._store_dir: Path = method._store_dir
        self._hidden = self._store_dir.parent / "autoskill_store_hidden"
        self._hidden.mkdir(parents=True, exist_ok=True)
        self._store = _reach_store(self.sdk)

    def _id_to_dir(self, key: str) -> Path | None:
        """Find the on-disk dir whose SKILL.md frontmatter id == key."""
        base = self._store_dir / "Users" / self._uid
        if not base.exists():
            return None
        for md in base.glob("*/SKILL.md"):
            fm = _frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            if fm.get("id") == key:
                return md.parent
        return None

    def snapshot_skill_bundles(self):
        out = []
        try:
            skills = self.sdk.list(user_id=self._uid)
        except Exception:
            return out
        for s in skills:
            try:
                text = self.sdk.export_skill_md(s.id) or ""
            except Exception:
                text = ""
            d = self._id_to_dir(s.id)
            if d is not None:
                b = _read_bundle(d, s.id, getattr(s, "version", None))
                b.text = text or b.text
                out.append(b)
            else:
                out.append(SkillBundle(key=s.id, text=text, version=getattr(s, "version", None)))
        return out

    def is_valid(self, key):
        try:
            text = self.sdk.export_skill_md(key) or ""
        except Exception:
            return False
        return _structurally_valid(text)

    def replace_skill(self, key, text, referenced_files=None):
        d = self._id_to_dir(key)
        if d is None:
            return
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        for rel, content in (referenced_files or {}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        if self._store is not None:
            try:
                self._store.refresh_from_disk()
            except Exception:
                pass

    def set_skill_status(self, key, status):
        d = self._id_to_dir(key)
        if status == Status.ACTIVE:
            held = self._hidden / key
            if held.exists():
                dest = self._store_dir / "Users" / self._uid / held.name
                shutil.move(str(held), str(dest))
                self._resync()
            return

        if d is not None:
            dest = self._hidden / key
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(d), str(dest))
            if not self._resync():
                try:
                    self.sdk.delete(key)
                except Exception:
                    pass
        else:
            try:
                self.sdk.delete(key)
            except Exception:
                pass

    def _resync(self) -> bool:
        if self._store is not None:
            try:
                self._store.refresh_from_disk()
                return True
            except Exception:
                return False
        return False

    def active_skills(self):
        return self.m.authored_skills()

    def last_retrieved_skill_keys(self):
        lr = getattr(self.m, "_last_retrieved", None) or []
        return [getattr(s, "key", None) for s in lr if getattr(s, "key", None)]

    def export_active_skills(self):
        return self.m.authored_skills()


_FACTORIES = {
    "autoskill": AutoSkillStoreControl,
    "evoskill": EvoSkillStoreControl,
    "skillsvote": SkillsVoteStoreControl,
}


def build_store_control(method) -> GovernableStore:
    name = getattr(method, "name", "").split("+")[0]
    if name not in _FACTORIES:
        raise ValueError(f"no GovernableStore adapter for method {name!r} (governance supports {sorted(_FACTORIES)})")
    return _FACTORIES[name](method)
