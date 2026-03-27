"""Scan ~/.claude for all skills and commands.

Sources:
  1. User commands:   ~/.claude/commands/*.md
  2. Plugin skills:   ~/.claude/plugins/cache/{marketplace}/{plugin}/{version}/skills/*/SKILL.md
  3. Plugin commands:  ~/.claude/plugins/cache/{marketplace}/{plugin}/{version}/commands/*.md
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class SkillEntry:
    name: str
    description: str
    source: str  # "user", marketplace name, or plugin name
    source_type: str  # "command" | "skill"
    plugin: Optional[str] = None
    version: Optional[str] = None
    path: str = ""
    content_preview: str = ""
    tags: List[str] = field(default_factory=list)


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML-ish frontmatter from a markdown file."""
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip()
    result = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def _extract_description_from_body(text: str, max_len: int = 200) -> str:
    """Extract a description from the markdown body if no frontmatter desc."""
    # Skip frontmatter
    body = text
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            body = text[end + 3:].strip()

    # Skip the first heading
    lines = body.splitlines()
    desc_lines = []
    past_heading = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if past_heading and desc_lines:
                break
            continue
        if stripped.startswith("#"):
            past_heading = True
            continue
        if past_heading or not stripped.startswith("#"):
            desc_lines.append(stripped)
            if len(" ".join(desc_lines)) > max_len:
                break

    desc = " ".join(desc_lines)[:max_len]
    return desc.rstrip(".")


def _extract_tags(text: str) -> List[str]:
    """Pull simple tags from content: headings, bold keywords."""
    tags = set()
    for match in re.finditer(r"^##\s+(.+)", text, re.MULTILINE):
        tag = match.group(1).strip().lower()
        if len(tag) < 30:
            tags.add(tag)
    return sorted(tags)[:8]


def scan_skills(claude_dir: Optional[Path] = None) -> List[SkillEntry]:
    """Scan all skill sources and return a flat list."""
    if claude_dir is None:
        claude_dir = Path.home() / ".claude"

    skills: List[SkillEntry] = []

    # 1. User commands
    cmd_dir = claude_dir / "commands"
    if cmd_dir.is_dir():
        for md in sorted(cmd_dir.glob("*.md")):
            text = md.read_text(errors="replace")
            fm = _parse_frontmatter(text)
            name = fm.get("name", md.stem)
            desc = fm.get("description", "") or _extract_description_from_body(text)

            # First line after heading as preview
            preview = ""
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("---"):
                    preview = line[:150]
                    break

            skills.append(SkillEntry(
                name=name,
                description=desc,
                source="You",
                source_type="command",
                path=str(md),
                content_preview=preview,
                tags=_extract_tags(text),
            ))

    # 2. Plugin skills + commands (from cache — these are the active versions)
    cache_dir = claude_dir / "plugins" / "cache"
    if cache_dir.is_dir():
        for marketplace_dir in sorted(cache_dir.iterdir()):
            if not marketplace_dir.is_dir():
                continue
            marketplace = marketplace_dir.name

            for plugin_dir in sorted(marketplace_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                plugin = plugin_dir.name

                for version_dir in sorted(plugin_dir.iterdir()):
                    if not version_dir.is_dir():
                        continue
                    version = version_dir.name

                    # Skills
                    skills_dir = version_dir / "skills"
                    if skills_dir.is_dir():
                        for skill_dir in sorted(skills_dir.iterdir()):
                            skill_md = skill_dir / "SKILL.md"
                            if not skill_md.exists():
                                continue
                            text = skill_md.read_text(errors="replace")
                            fm = _parse_frontmatter(text)
                            name = fm.get("name", skill_dir.name)
                            desc = fm.get("description", "") or _extract_description_from_body(text)

                            skills.append(SkillEntry(
                                name=name,
                                description=desc,
                                source=marketplace,
                                source_type="skill",
                                plugin=plugin,
                                version=version,
                                path=str(skill_md),
                                content_preview=desc[:150],
                                tags=_extract_tags(text),
                            ))

                    # Commands
                    cmds_dir = version_dir / "commands"
                    if cmds_dir.is_dir():
                        for cmd_md in sorted(cmds_dir.glob("*.md")):
                            text = cmd_md.read_text(errors="replace")
                            fm = _parse_frontmatter(text)
                            name = fm.get("name", cmd_md.stem)
                            desc = fm.get("description", "") or _extract_description_from_body(text)

                            skills.append(SkillEntry(
                                name=name,
                                description=desc,
                                source=marketplace,
                                source_type="command",
                                plugin=plugin,
                                version=version,
                                path=str(cmd_md),
                                content_preview=desc[:150],
                                tags=_extract_tags(text),
                            ))

    return skills


def group_by_source(skills: List[SkillEntry]) -> dict:
    """Group skills by source for display."""
    groups = {}
    for s in skills:
        key = s.source
        if s.plugin:
            key = s.plugin
        if key not in groups:
            groups[key] = {"name": key, "skills": [], "commands": []}
        if s.source_type == "skill":
            groups[key]["skills"].append(s)
        else:
            groups[key]["commands"].append(s)
    return groups


def get_stats(skills: List[SkillEntry]) -> dict:
    """Summary stats for the skills page."""
    user_count = sum(1 for s in skills if s.source == "You")
    plugin_count = sum(1 for s in skills if s.source != "You")
    skill_count = sum(1 for s in skills if s.source_type == "skill")
    command_count = sum(1 for s in skills if s.source_type == "command")
    plugins = set(s.plugin for s in skills if s.plugin)
    return {
        "total": len(skills),
        "user": user_count,
        "plugin": plugin_count,
        "skills": skill_count,
        "commands": command_count,
        "plugin_count": len(plugins),
    }
