#!/usr/bin/env python3
"""Preview/apply/rollback the optional skill runtime, preserving local skill text."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import shlex
import sys
import tomllib

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills/dialogue-explore/scripts"))
from runtime_state import atomic, digest, encode, locked, safe_path  # noqa: E402

BEGIN = "<!-- BEGIN SKILL RUNTIME -->"
END = "<!-- END SKILL RUNTIME -->"
OWN = "codex-skills-runtime-v1"
MANAGED = ("skills/dialogue-explore/scripts/runtime_state.py",
           "skills/dialogue-explore/references/runtime-state.md",
           "skills/dialogue-explore/references/runtime-state.schema.json")


def snapshot(path):
    safe_path(path)
    if not path.exists():
        return None
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("installation target too large")
    return path.read_bytes()


def sha(data):
    return digest(data) if data is not None else None


def skill_block(name):
    content = (REPO / f"skills/{name}/SKILL.md").read_text()
    return content[content.index(BEGIN):content.index(END) + len(END)]


def merge_skill(data, name):
    content = data.decode()
    if BEGIN in content or END in content:
        if content.count(BEGIN) != 1 or content.count(END) != 1:
            raise ValueError("ambiguous runtime markers")
        start, end = content.index(BEGIN), content.index(END) + len(END)
        if end < start:
            raise ValueError("invalid runtime markers")
        return (content[:start] + skill_block(name) + content[end:]).encode()
    return (content.rstrip() + "\n\n" + skill_block(name) + "\n").encode()


def remove_ours(config):
    for kind, groups in list(config.get("hooks", {}).items()):
        kept = []
        for group in groups:
            handlers = group.get("hooks", [])
            remaining = [h for h in handlers if f"--package={OWN}" not in h.get("command", "")]
            if any("runtime_state.py" in h.get("command", "") for h in remaining):
                raise ValueError("another skill runtime registration exists; choose one source")
            if remaining or not handlers:
                kept.append({**group, "hooks": remaining})
        if kept:
            config["hooks"][kind] = kept
        else:
            del config["hooks"][kind]
    return config


def planned(home):
    files = {name: (REPO / name).read_bytes() for name in MANAGED}
    for name in ("dialogue-explore", "dialogue-validate", "nasa"):
        rel = f"skills/{name}/SKILL.md"
        existing = snapshot(home / rel)
        if existing is None:
            raise ValueError(f"install the existing {name} skill first")
        files[rel] = merge_skill(existing, name)
    toml = snapshot(home / "config.toml")
    if toml and "runtime_state.py" in json.dumps(tomllib.loads(toml.decode()).get("hooks", {})):
        raise ValueError("inline runtime hooks already registered; choose one source")
    raw = snapshot(home / "hooks.json")
    config = json.loads(raw) if raw else {"hooks": {}}
    config.setdefault("hooks", {})
    command = shlex.join([sys.executable, str(home / MANAGED[0]), f"--package={OWN}", "hook"])
    for kind in ("SessionStart", "UserPromptSubmit", "Stop"):
        group = {"hooks": [{"type": "command", "command": command, "timeout": 3}]}
        if kind == "SessionStart":
            group["matcher"] = "startup|resume|compact"
            group["hooks"][0]["additionalContextLimit"] = 1000
        groups = config["hooks"].setdefault(kind, [])
        matches = [(g, i) for g in groups for i, h in enumerate(g.get("hooks", []))
                   if f"--package={OWN}" in h.get("command", "")]
        if len(matches) > 1:
            raise ValueError("duplicate runtime handler")
        if any("runtime_state.py" in h.get("command", "") and f"--package={OWN}" not in h.get("command", "")
               for g in groups for h in g.get("hooks", [])):
            raise ValueError("another runtime handler exists; choose one source")
        if matches:
            g, i = matches[0]
            if {k: v for k, v in g.items() if k != "hooks"} != {k: v for k, v in group.items() if k != "hooks"}:
                raise ValueError("existing runtime matcher changed; reconcile before reinstall")
            g["hooks"][i] = group["hooks"][0]
        else:
            groups.append(group)
    files["hooks.json"] = encode(config)
    return files


def install(home, apply=False):
    home = safe_path(home)
    manifest = home / ".skill-runtime-install.json"
    with locked(manifest):
        receipt = json.loads(manifest.read_text()) if manifest.exists() else None
        files = planned(home)
        if receipt:
            if receipt.get("owner") != OWN:
                raise ValueError("unknown install manifest")
            if receipt.get("status") != "installed":
                raise ValueError("interrupted install; rollback before retry")
            # Installed files can contain local edits; never replace them on a repeat.
            for rel, entry in receipt["files"].items():
                if rel != "hooks.json" and sha(snapshot(home / rel)) != entry["after"]:
                    raise ValueError(f"local edit since installation: {rel}")
            if any(receipt["files"].get(rel, {}).get("after") != sha(data)
                   for rel, data in files.items() if rel != "hooks.json"):
                raise ValueError("different runtime version; rollback/reconcile before upgrading")
        before = {rel: snapshot(home / rel) for rel in files}
        changed = [rel for rel, data in files.items() if before[rel] != data]
        if not apply:
            return {"mode": "preview", "changes": changed, "trust": "review changed definitions in /hooks"}
        if not changed:
            return {"mode": "unchanged", "changes": []}
        if not receipt:
            receipt = {"owner": OWN, "hooks_after": base64.b64encode(files["hooks.json"]).decode(),
                       "files": {rel: {"before": base64.b64encode(data).decode() if data is not None else None,
                       "after": sha(files[rel])} for rel, data in before.items()}, "status": "pending"}
            atomic(manifest, encode(receipt))
        elif receipt.get("status") != "installed":
            raise ValueError("interrupted install; rollback before retry")
        for rel in changed:
            if snapshot(home / rel) != before[rel]:
                raise ValueError(f"concurrent edit: {rel}; rollback available")
            atomic(home / rel, files[rel])
        receipt["status"] = "installed"
        atomic(manifest, encode(receipt))
        return {"mode": "installed", "changes": changed, "trust": "review changed definitions in /hooks"}


def rollback(home):
    home = safe_path(home)
    manifest = home / ".skill-runtime-install.json"
    with locked(manifest):
        receipt = json.loads(manifest.read_text())
        if receipt.get("owner") != OWN:
            raise ValueError("unknown install manifest")
        changes = {}
        for rel, entry in receipt["files"].items():
            if rel not in {*MANAGED, "hooks.json", *(f"skills/{s}/SKILL.md" for s in ("dialogue-explore", "dialogue-validate", "nasa"))}:
                raise ValueError("unexpected manifest path")
            original = base64.b64decode(entry["before"]) if entry["before"] is not None else None
            current = snapshot(home / rel)
            if current == original:
                continue  # includes files never written during interrupted install
            if rel == "hooks.json" and sha(current) != entry["after"]:
                config = json.loads(current)
                wanted = json.loads(base64.b64decode(receipt["hooks_after"]))
                for kind, groups in config.get("hooks", {}).items():
                    expected_handlers = [h for g in wanted.get("hooks", {}).get(kind, []) for h in g.get("hooks", [])
                                         if f"--package={OWN}" in h.get("command", "")]
                    for g in groups:
                        for h in g.get("hooks", []):
                            if f"--package={OWN}" in h.get("command", "") and h not in expected_handlers:
                                raise ValueError("modified runtime handler; preserve and reconcile manually")
                # Preserve unrelated hook additions made after installation.
                config = remove_ours(config)
                changes[rel] = encode(config)
            elif sha(current) != entry["after"]:
                raise ValueError(f"local edit prevents rollback: {rel}")
            else:
                changes[rel] = original
        expected = {rel: snapshot(home / rel) for rel in changes}
        for rel, data in changes.items():
            if snapshot(home / rel) != expected[rel]:
                raise ValueError(f"concurrent edit: {rel}")
            if data is None:
                safe_path(home / rel).unlink()
            else:
                atomic(home / rel, data)
        manifest.unlink()
        return {"mode": "rolled-back", "changes": list(changes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    try:
        result = rollback(args.codex_home) if args.rollback else install(args.codex_home, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"runtime installation: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
