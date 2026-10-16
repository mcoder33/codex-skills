"""Validate portable skill metadata and repository-local Markdown targets."""
import argparse
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
import yaml


def tracked_files(root):
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                            capture_output=True, text=True)
    return [root / name for name in result.stdout.split("\0") if name]


def validate_structure(root, files):
    errors = []
    skill_directories = {p.relative_to(root).parts[1] for p in files
                         if len(p.relative_to(root).parts) > 2
                         and p.relative_to(root).parts[0] == "skills"}
    if not skill_directories:
        errors.append("no tracked skills found")
    for name in sorted(skill_directories):
        path = root / "skills" / name / "SKILL.md"
        if path not in files or not path.is_file():
            errors.append(f"skills/{name}: missing tracked SKILL.md")
            continue
        text = path.read_text(encoding="utf-8")
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
        try:
            metadata = yaml.safe_load(match.group(1)) if match else None
        except yaml.YAMLError:
            metadata = None
        if not isinstance(metadata, dict):
            errors.append(f"skills/{name}: invalid YAML frontmatter")
            continue
        if metadata.get("name") != name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            errors.append(f"skills/{name}: name must match its lowercase directory")
        if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
            errors.append(f"skills/{name}: nonempty description required")
        if not text[match.end():].strip():
            errors.append(f"skills/{name}: empty skill body")
    return errors


def validate_local_links(root, files):
    errors = []
    tracked = {p.resolve() for p in files}
    markdown = MarkdownIt()
    for path in files:
        if path.suffix.lower() != ".md":
            continue
        for block in markdown.parse(path.read_text(encoding="utf-8")):
            for token in block.children or []:
                if token.type not in {"link_open", "image"}:
                    continue
                target = token.attrGet("href" if token.type == "link_open" else "src")
                parsed = urlsplit(target)
                if parsed.scheme in {"http", "https", "mailto"} or parsed.netloc:
                    continue
                if not parsed.path and not parsed.scheme:
                    continue
                destination = (path.parent / unquote(parsed.path)).resolve()
                if parsed.scheme or not destination.is_relative_to(root) or destination not in tracked:
                    errors.append(f"{path.relative_to(root)}: missing or nonportable local target {target}")
        # Obsidian links in the supplied vault must resolve; skill prose may show examples.
        if path.is_relative_to(root / "vault-template"):
            text = re.sub(r"`[^`]*`", "", path.read_text(encoding="utf-8"))
            for target in re.findall(r"\[\[([^]\n]+)\]\]", text):
                name = target.split("|", 1)[0].split("#", 1)[0]
                if not name:
                    continue
                destination = (root / "vault-template" / (name if name.endswith(".md") else name + ".md")).resolve()
                if destination not in tracked or not destination.is_relative_to(root / "vault-template"):
                    errors.append(f"{path.relative_to(root)}: unresolved vault link {target}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=["structure", "links"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    files = tracked_files(root)
    errors = (validate_structure if args.check == "structure" else validate_local_links)(root, files)
    for error in errors:
        print(error)
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
