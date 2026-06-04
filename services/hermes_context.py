from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULE_FILES = ("identity.md", "agents.md", "memory.md")


def load_hermes_context(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    files: dict[str, str] = {}
    missing: list[str] = []
    for name in RULE_FILES:
        path = root / name
        if not path.exists():
            missing.append(name)
            continue
        files[name] = path.read_text(encoding="utf-8")

    joined = "\n\n".join(files[name] for name in RULE_FILES if name in files)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16] if joined else ""
    return {
        "loaded": len(files) == len(RULE_FILES),
        "files": files,
        "missing": missing,
        "digest": digest,
        "source_paths": [str((root / name).resolve()) for name in RULE_FILES if name in files],
    }


def build_hermes_system_prompt(root: Path | None = None) -> str:
    context = load_hermes_context(root)
    sections = []
    for name in RULE_FILES:
        body = context["files"].get(name)
        if body:
            sections.append(f"<!-- {name} -->\n{body}")
    return "\n\n".join(sections)
