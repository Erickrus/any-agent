"""
First-run bootstrap: install bundled skills into the working directory.

The `send-file` skill (and any future bundled skills) live inside the package under
`any_agent/skills/<name>/SKILL.md`. On startup we copy them into the current working
directory's agent skill folders if they are missing:

  ./.claude/skills/<name>/SKILL.md     (Claude Code discovery)
  ./.opencode/skills/<name>/SKILL.md   (OpenCode discovery)

Existing files are left untouched (idempotent).
"""

import logging
import os
from importlib import resources

logger = logging.getLogger("any_agent.bootstrap")

# Agent skill roots, relative to the working directory.
_SKILL_ROOTS = [".claude/skills", ".opencode/skills"]


def _bundled_skills() -> list[str]:
    """Names of skills bundled in the package (subdirs of any_agent/skills)."""
    try:
        root = resources.files("any_agent") / "skills"
    except (ModuleNotFoundError, FileNotFoundError):
        return []
    names = []
    for entry in root.iterdir():
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            names.append(entry.name)
    return names


def _read_bundled(skill: str) -> str | None:
    try:
        return (resources.files("any_agent") / "skills" / skill / "SKILL.md").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError):
        return None


def install_skills(cwd: str | None = None) -> list[str]:
    """
    Ensure bundled skills exist under the working directory's agent skill roots.
    Returns the list of file paths that were created (empty if all present).
    """
    base = cwd or os.getcwd()
    created: list[str] = []
    for skill in _bundled_skills():
        content = _read_bundled(skill)
        if content is None:
            continue
        for root in _SKILL_ROOTS:
            dest_dir = os.path.join(base, root, skill)
            dest = os.path.join(dest_dir, "SKILL.md")
            if os.path.exists(dest):
                continue
            os.makedirs(dest_dir, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
            created.append(dest)
    if created:
        logger.info(f"Installed bundled skills: {', '.join(created)}")
    return created
