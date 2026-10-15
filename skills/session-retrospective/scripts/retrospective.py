#!/usr/bin/env python3
"""Private, local guardrails for session review. No inference, networking or scheduler."""
import argparse
import contextlib
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid

SKILLS = ("dialogue-explore", "dialogue-validate", "model-router")
CONTROLS = {"factual-direct", "authorized-execution", "no-recursive-delegation",
            "privacy", "authority", "relevant-improvement"}
BOUNDARIES = {"permissions", "root-only-delegation", "privacy", "scope"}
DEFAULT_STATE = Path.home() / ".local/state/codex-dialogue-kit/retrospective"
DEFAULT_SKILLS = Path.home() / ".codex/skills"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\Z")
ACTOR_PATH = re.compile(r"/root(?:/[a-z0-9][a-z0-9_-]{0,63})*\Z")
SECRET = re.compile(r"(?i)(?:bearer\s+|(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+|sk-[a-z0-9]{12,}|-----BEGIN .*PRIVATE KEY)")


class Rejected(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Rejected(message)


def valid_actor_id(value):
    """Actor identities are JSON values, never filesystem components."""
    return isinstance(value, str) and len(value) <= 512 and bool(ID.fullmatch(value) or ACTOR_PATH.fullmatch(value))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    require(path.is_file() and not path.is_symlink(), "missing or symlink artifact")
    require(path.stat().st_size <= 262144, "artifact too large")
    return json.loads(path.read_text())


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".retro-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def prose(value):
    require(isinstance(value, str) and 8 <= len(value) <= 1200, "expected concise paraphrase")
    require(not SECRET.search(value), "possible secret in paraphrase")
    return value


def exact_keys(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), "unexpected/missing artifact fields")


def version(data):
    text = data.decode("utf-8")
    front = text.split("---", 2)[1] if text.startswith("---\n") else ""
    match = re.search(r'^  version: *[\"\']?([0-9]+(?:\.[0-9]+)*)[\"\']? *$', front, re.M)
    return match.group(1) if match else None


def markdown(data, target):
    require(0 < len(data) <= 100000 and b"\x00" not in data, "invalid Markdown size/content")
    text = data.decode("utf-8")
    if target.endswith("SKILL.md"):
        require(text.startswith("---\n") and "\n---\n" in text[4:], "missing skill frontmatter")
        front = text.split("---", 2)[1]
        require(re.search(r"^name: *\S+", front, re.M) and re.search(r"^description: *\S+", front, re.M), "missing skill metadata")


class Store:
    def __init__(self, state=DEFAULT_STATE, skills=DEFAULT_SKILLS):
        self.state = Path(state).absolute()
        self.skills = Path(skills).absolute()
        for root in (self.state, self.skills):
            require(not root.is_symlink(), "symlink root forbidden")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        self.state = self.state.resolve()
        self.skills = self.skills.resolve()

    @contextlib.contextmanager
    def lock(self):
        path = self.state / "lock"
        require(not path.is_symlink(), "symlink lock forbidden")
        with path.open("a") as stream:
            os.chmod(path, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def internal(self, relative):
        parts = PurePosixPath(relative).parts
        require(not str(relative).startswith("/") and ".." not in parts, "state escape")
        path = self.state / relative
        require(path.is_relative_to(self.state), "state escape")
        for ancestor in (path, *path.parents):
            if ancestor == self.state:
                break
            require(not ancestor.is_symlink(), "symlink state path")
        return path

    def target(self, relative):
        require(isinstance(relative, str), "target must be relative")
        parts = PurePosixPath(relative).parts
        require(relative == str(PurePosixPath(relative)) and not relative.startswith("/") and ".." not in parts, "unsafe target path")
        require(len(parts) >= 2 and parts[0] in SKILLS, "skill not allowlisted")
        require(parts[1:] == ("SKILL.md",) or (len(parts) >= 3 and parts[1] == "references" and parts[-1].endswith(".md")), "only skill Markdown/references allowed")
        path = self.skills.joinpath(*parts)
        for ancestor in (path, *path.parents):
            if ancestor == self.skills:
                break
            require(not ancestor.is_symlink(), "symlink target path")
        require(path.is_file() and path.resolve().is_relative_to(self.skills), "target must already exist within skills")
        return path

    def candidate_dir(self, candidate_id):
        require(bool(ID.fullmatch(candidate_id)), "unsafe candidate id")
        return self.internal("candidates/" + candidate_id)

    def snapshot(self, relative):
        target = self.target(relative)
        data = target.read_bytes()
        sha = digest(data)
        backup = self.internal("versions/" + sha + ".md")
        if backup.exists():
            require(digest(backup.read_bytes()) == sha, "corrupt version backup")
        else:
            atomic(backup, data)
        record = {"target": relative, "sha256": sha, "version": version(data),
                  "mode": target.stat().st_mode & 0o777, "backup": "versions/" + sha + ".md"}
        return record

    def baseline(self):
        records = []
        for skill in SKILLS:
            folder = self.skills / skill
            files = [folder / "SKILL.md", *sorted((folder / "references").rglob("*.md"))]
            for path in files:
                records.append(self.snapshot(str(path.relative_to(self.skills))))
        result = {"status": "no-change", "reason": "Initial hashes only; no improvement evidence evaluated", "files": records}
        save(self.internal("baseline.json"), result)
        return result

    def prepare(self, target, candidate_file, analysis_file):
        baseline = self.snapshot(target)
        candidate = Path(candidate_file).read_bytes()
        markdown(candidate, target)
        original = self.target(target).read_bytes()
        require(candidate != original, "candidate has no change")
        changed = sum(line[0] in "+-" for line in difflib.ndiff(original.decode().splitlines(), candidate.decode().splitlines()))
        require(changed <= 80 and abs(len(candidate) - len(original)) <= 12000, "candidate is not a small change")
        old_version, new_version = version(original), version(candidate)
        if old_version:
            require(new_version is not None and tuple(map(int, new_version.split('.'))) > tuple(map(int, old_version.split('.'))), "increment declared version")
        analysis = read_json(Path(analysis_file))
        exact_keys(analysis, {"analyst_id", "reason", "expected_effect", "comparison_criteria", "evidence"})
        require(valid_actor_id(analysis["analyst_id"]), "invalid analyst id")
        for key in ("reason", "expected_effect", "comparison_criteria"):
            prose(analysis[key])
        evidence = analysis["evidence"]
        require(isinstance(evidence, list) and 1 <= len(evidence) <= 6, "missing/bounded evidence required")
        episodes = set()
        for item in evidence:
            exact_keys(item, {"session_id", "turn_id", "events", "summary", "kind"})
            require(ID.fullmatch(item["session_id"]) and ID.fullmatch(item["turn_id"]), "invalid evidence id")
            require(isinstance(item["events"], list) and len(item["events"]) == 2 and all(type(x) is int and x >= 0 for x in item["events"]) and item["events"][0] <= item["events"][1], "invalid event range")
            prose(item["summary"])
            require(item["kind"] in ("episode", "current-user-correction"), "invalid evidence kind")
            episodes.add((item["session_id"], item["turn_id"]))
        require(len(episodes) >= 2 or any(x["kind"] == "current-user-correction" for x in evidence), "two independent episodes or current correction required")
        candidate_id = uuid.uuid4().hex
        folder = self.candidate_dir(candidate_id)
        folder.mkdir(mode=0o700, parents=True)
        atomic(folder / "candidate.md", candidate)
        save(folder / "analysis.json", analysis)
        manifest = {"id": candidate_id, "status": "prepared", "baseline": baseline,
                    "candidate_sha256": digest(candidate), "version": new_version,
                    "analysis_sha256": digest((folder / "analysis.json").read_bytes()),
                    "prepared_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        save(folder / "manifest.json", manifest)
        return manifest

    def validate(self, manifest, validation, review):
        exact_keys(validation, {"candidate_sha256", "baseline_sha256", "runner_id", "syntax_passed", "cases"})
        require(validation["candidate_sha256"] == manifest["candidate_sha256"] and validation["baseline_sha256"] == manifest["baseline"]["sha256"], "validation hash mismatch")
        require(validation["syntax_passed"] is True, "syntax validation failed")
        require(valid_actor_id(validation["runner_id"]), "invalid runner")
        cases = validation["cases"]
        require(isinstance(cases, list) and 6 <= len(cases) <= 20, "behavior cases required")
        seen = set()
        improved = False
        for case in cases:
            exact_keys(case, {"control", "expected", "baseline_observed", "candidate_observed", "baseline_passed", "candidate_passed"})
            require(case["control"] in CONTROLS, "unknown control")
            seen.add(case["control"])
            for key in ("expected", "baseline_observed", "candidate_observed"):
                prose(case[key])
            require(type(case["baseline_passed"]) is bool and case["candidate_passed"] is True, "candidate behavior failed")
            if case["control"] == "relevant-improvement" and case["baseline_passed"] is False:
                improved = True
        require(seen == CONTROLS and improved, "missing controls or no observed improvement")
        exact_keys(review, {"reviewer_id", "analyst_id", "candidate_sha256", "verdict", "reason", "boundaries_preserved", "independent"})
        require(review["candidate_sha256"] == manifest["candidate_sha256"] and review["verdict"] == "accept" and review["independent"] is True, "review not accepted")
        require(valid_actor_id(review["reviewer_id"]) and review["reviewer_id"] not in (review["analyst_id"], validation["runner_id"]), "independent reviewer required")
        require(set(review["boundaries_preserved"]) == BOUNDARIES and all(x is True for x in review["boundaries_preserved"].values()), "boundary regression")
        prose(review["reason"])

    def post_apply(self, target, expected):
        require(digest(target.read_bytes()) == expected, "post-apply hash mismatch")
        markdown(target.read_bytes(), str(target))

    def apply(self, candidate_id, validation_file, review_file, day=None):
        folder = self.candidate_dir(candidate_id)
        manifest = read_json(folder / "manifest.json")
        require(manifest["status"] == "prepared", "candidate not prepared")
        analysis = read_json(folder / "analysis.json")
        require(digest((folder / "analysis.json").read_bytes()) == manifest["analysis_sha256"], "analysis changed")
        validation, review = read_json(Path(validation_file)), read_json(Path(review_file))
        require(isinstance(review, dict) and review.get("analyst_id") == analysis["analyst_id"], "analyst mismatch")
        self.validate(manifest, validation, review)
        baseline = manifest["baseline"]
        target = self.target(baseline["target"])
        candidate = (folder / "candidate.md").read_bytes()
        require(digest(candidate) == manifest["candidate_sha256"], "candidate changed")
        require(digest(target.read_bytes()) == baseline["sha256"], "concurrent target change")
        backup = self.internal(baseline["backup"]).read_bytes()
        require(digest(backup) == baseline["sha256"], "corrupt baseline backup")
        day = day or dt.datetime.now(dt.timezone(dt.timedelta(hours=4))).date().isoformat()
        daily = self.internal("days/" + day + ".json")
        require(not daily.exists(), "one apply attempt per Samara day")
        save(folder / "validation.json", validation)
        save(folder / "review.json", review)
        save(daily, {"candidate": candidate_id})
        manifest["status"] = "applying"
        save(folder / "manifest.json", manifest)
        try:
            # Recheck immediately before mutation; lock serializes this helper, not other editors.
            target = self.target(baseline["target"])
            require(digest(target.read_bytes()) == baseline["sha256"], "concurrent target change")
            atomic(target, candidate, baseline["mode"])
            self.post_apply(target, manifest["candidate_sha256"])
            manifest["status"] = "applied-pending-verification"
            save(folder / "manifest.json", manifest)
        except BaseException:
            current = digest(target.read_bytes())
            if current == manifest["candidate_sha256"]:
                atomic(target, backup, baseline["mode"])
                manifest["status"] = "rolled-back"
            elif current == baseline["sha256"]:
                manifest["status"] = "rolled-back"
            else:
                manifest["status"] = "recovery-conflict"
            save(folder / "manifest.json", manifest)
            raise
        return manifest

    def rollback(self, candidate_id):
        folder = self.candidate_dir(candidate_id)
        manifest = read_json(folder / "manifest.json")
        require(manifest["status"] in ("applied", "applied-pending-verification", "applying", "recovery-conflict"), "not an applied version")
        baseline = manifest["baseline"]
        target = self.target(baseline["target"])
        require(digest(target.read_bytes()) == manifest["candidate_sha256"], "later edit preserved; rollback refused")
        backup = self.internal(baseline["backup"]).read_bytes()
        require(digest(backup) == baseline["sha256"], "corrupt rollback backup")
        atomic(target, backup, baseline["mode"])
        manifest["status"] = "rolled-back"
        save(folder / "manifest.json", manifest)
        return manifest

    def verify_applied(self, candidate_id, result_file):
        folder = self.candidate_dir(candidate_id)
        manifest = read_json(folder / "manifest.json")
        require(manifest["status"] == "applied-pending-verification", "not pending verification")
        try:
            result = read_json(Path(result_file))
            exact_keys(result, {"candidate_sha256", "runner_id", "passed", "reason"})
            require(result["candidate_sha256"] == manifest["candidate_sha256"], "post-apply result hash mismatch")
            require(valid_actor_id(result["runner_id"]) and result["passed"] is True, "post-apply behavior failed")
            prose(result["reason"])
            self.post_apply(self.target(manifest["baseline"]["target"]), manifest["candidate_sha256"])
            save(folder / "post-apply.json", result)
        except BaseException:
            self.rollback(candidate_id)
            raise
        manifest["status"] = "applied"
        save(folder / "manifest.json", manifest)
        return manifest

    def recover(self, skip_id=None):
        """Recover interrupted replacements on next invocation, preserving subsequent edits."""
        recovered = []
        folder = self.internal("candidates")
        if not folder.exists():
            return recovered
        for directory in folder.iterdir():
            candidate = self.candidate_dir(directory.name)
            manifest = read_json(candidate / "manifest.json")
            if directory.name == skip_id or manifest["status"] not in ("applying", "applied-pending-verification"):
                continue
            baseline = manifest["baseline"]
            target = self.target(baseline["target"])
            current = digest(target.read_bytes())
            if current == manifest["candidate_sha256"]:
                self.rollback(directory.name)
            elif current == baseline["sha256"]:
                manifest["status"] = "rolled-back"
                save(candidate / "manifest.json", manifest)
            else:
                manifest["status"] = "recovery-conflict"
                save(candidate / "manifest.json", manifest)
            recovered.append(directory.name)
        return recovered

    def scan(self, sessions, exclude, baseline=False, now=None, days=3, max_files=100, max_bytes=32000000):
        require(exclude, "explicit current workflow thread exclusions required")
        require(1 <= days <= 7 and 1 <= max_files <= 100, "scan budget exceeded")
        sessions = Path(sessions).resolve()
        pending = self.internal("pending-scan.json")
        if pending.exists():
            result = read_json(pending)
            result["excluded_threads"] = sorted(set(result["excluded_threads"]) | set(exclude))
            result["episodes"] = [e for e in result["episodes"] if e["session_id"] not in result["excluded_threads"]]
            save(pending, result)
            return result
        index_path = self.internal("scan-index.json")
        index = read_json(index_path) if index_path.exists() else {"cursors": {}, "reviewed": []}
        exclude = set(exclude) | set(index.get("excluded_threads", []))
        now = now or dt.datetime.now(dt.timezone.utc)
        files = []
        for offset in range(days):
            folder = sessions / (now - dt.timedelta(days=offset)).strftime("%Y/%m/%d")
            files.extend(sorted(folder.glob("*.jsonl"), key=lambda p: p.name, reverse=True))
        cursors = dict(index["cursors"])
        episodes = []
        seen_episodes = set(index["reviewed"])
        used = 0
        selected_bytes = 0
        skipped_oversize = 0
        metadata_bytes = 0
        unknown_identity = 0
        for source in files[:max_files]:
            if len(episodes) >= 6:
                break
            if source.is_symlink() or not source.resolve().is_relative_to(sessions):
                continue
            # File suffixes may be continuation UUIDs, not logical thread IDs.
            # Metadata IDs can be inherited and reordered. They are conservative
            # exclusion hints, never proof of the logical owner of a turn.
            with source.open("rb") as identity_stream:
                first = identity_stream.readline(65537)
            metadata_bytes += len(first)
            if len(first) > 65536:
                continue
            try:
                header = json.loads(first)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(header, dict):
                continue
            metadata = header.get("payload", {})
            if header.get("type") != "session_meta" or not isinstance(metadata, dict):
                continue
            session_id = metadata.get("id")
            if not isinstance(session_id, str) or not ID.fullmatch(session_id):
                continue
            source_meta = metadata.get("source")
            subagent = source_meta.get("subagent", {}) if isinstance(source_meta, dict) else {}
            spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
            if isinstance(spawn, dict) and spawn.get("parent_thread_id") in exclude:
                exclude.add(session_id)
            relative = str(source.relative_to(sessions))
            size = source.stat().st_size
            if baseline or session_id in exclude:
                cursors[relative] = size
                continue
            start = cursors.get(relative, 0)
            if size < start:  # replacement/truncation: never replay automatically
                continue
            budget = min(4000000, max_bytes - used)
            if budget <= 0:
                break
            active = None
            own = False
            proposed = []
            turn_threads = {}
            committed = start
            with source.open("rb") as stream:
                stream.seek(start)
                while used < max_bytes and stream.tell() - start < budget:
                    line_start = stream.tell()
                    line = stream.readline(min(1000000, max_bytes - used) + 1)
                    if not line or not line.endswith(b"\n") or len(line) > 1000000:
                        break
                    used += len(line)
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        break
                    if not isinstance(event, dict):
                        break
                    payload = event.get("payload", {})
                    if not isinstance(payload, dict):
                        if active is None:
                            committed = stream.tell()
                        continue
                    event_type = event.get("type")
                    if event_type == "session_meta":
                        metadata_id = payload.get("id")
                        source_value = payload.get("source")
                        sub = source_value.get("subagent", {}) if isinstance(source_value, dict) else {}
                        spawn_value = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
                        if (isinstance(metadata_id, str) and metadata_id in exclude) or (isinstance(spawn_value, dict) and spawn_value.get("parent_thread_id") in exclude):
                            own = True
                            if isinstance(metadata_id, str) and ID.fullmatch(metadata_id):
                                exclude.add(metadata_id)
                    if event_type == "token_usage_record":
                        thread_id, turn_id = payload.get("thread_id"), payload.get("turn_id")
                        if isinstance(thread_id, str) and ID.fullmatch(thread_id) and isinstance(turn_id, str) and ID.fullmatch(turn_id):
                            turn_threads.setdefault(turn_id, set()).add(thread_id)
                    if event_type == "turn_context":
                        # A later turn implicitly supersedes an uncompleted earlier turn.
                        committed = line_start
                        active = {"turn_id": payload.get("turn_id") or payload.get("root_turn_id"), "start": line_start, "relevant": False}
                        if not isinstance(active["turn_id"], str):
                            active["turn_id"] = None
                    if event_type == "response_item" and payload.get("role") == "user":
                        content = payload.get("content", [])
                        text = " ".join(c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)) if isinstance(content, list) else ""
                        if "session-retrospective" in text.lower():
                            own = True
                        if active and re.search(r"(?i)(dialogue|model-router|skill|навык|обсуд|план|модел|делег)", text):
                            active["relevant"] = True
                    if event_type == "event_msg" and payload.get("type") in ("task_complete", "turn_aborted"):
                        end = stream.tell()
                        if payload.get("type") == "task_complete" and active and active["relevant"] and active["turn_id"]:
                            key = session_id + ":" + active["turn_id"]
                            if ID.fullmatch(active["turn_id"]):
                                span = end - active["start"]
                                if selected_bytes + span <= 120000:
                                    proposed.append({"key": key, "session_id": session_id, "turn_id": active["turn_id"], "source": relative, "byte_range": [active["start"], end]})
                                    selected_bytes += span
                                else:
                                    skipped_oversize += 1
                        active = None
                        committed = end
                    if active is None:
                        committed = stream.tell()
                    if len(episodes) + len(proposed) >= 6:
                        break
            if own:
                exclude.add(session_id)
                proposed = []
                committed = size
            for episode in proposed:
                logical = turn_threads.get(episode["turn_id"], set())
                if len(logical) != 1:  # absent/ambiguous runtime identity is not evidence
                    unknown_identity += 1
                    continue
                episode["session_id"] = next(iter(logical))
                episode["key"] = episode["session_id"] + ":" + episode["turn_id"]
                if episode["session_id"] not in exclude and episode["key"] not in seen_episodes:
                    episodes.append(episode)
                    seen_episodes.add(episode["key"])
            cursors[relative] = committed
        episodes = [episode for episode in episodes if episode["session_id"] not in exclude]
        result = {"id": uuid.uuid4().hex, "status": "no-change" if baseline else "review-pending",
                  "baseline": baseline, "episodes": episodes, "cursors": cursors,
                  "bytes_scanned": used, "selected_bytes": selected_bytes,
                  "skipped_oversize": skipped_oversize, "metadata_bytes_read": metadata_bytes, "unknown_identity": unknown_identity,
                  "files_considered": min(len(files), max_files),
                  "excluded_threads": sorted(exclude)}
        save(pending, result)
        return result

    def acknowledge(self, scan_id, outcome, candidate_id=None):
        require(outcome in ("no-change", "applied", "rejected", "rolled-back"), "invalid review outcome")
        candidate = None
        if outcome in ("applied", "rolled-back"):
            require(candidate_id is not None, "candidate id required for mutation outcome")
        if candidate_id is not None:
            candidate = read_json(self.candidate_dir(candidate_id) / "manifest.json")
            if outcome in ("applied", "rolled-back"):
                require(candidate["status"] == outcome, "candidate outcome not confirmed")
        pending = self.internal("pending-scan.json")
        scan = read_json(pending)
        require(scan["id"] == scan_id, "scan id mismatch")
        path = self.internal("scan-index.json")
        old = read_json(path) if path.exists() else {"reviewed": []}
        reviewed = sorted(set(old["reviewed"]) | {e["key"] for e in scan["episodes"]})
        save(path, {"cursors": scan["cursors"], "reviewed": reviewed, "excluded_threads": scan["excluded_threads"]})
        report = {"scan_id": scan_id, "candidate_id": candidate_id,
                  "candidate": candidate, "coverage": {key: scan.get(key) for key in ("files_considered", "bytes_scanned", "selected_bytes", "skipped_oversize", "unknown_identity")}, "status": outcome, "episodes_reviewed": len(scan["episodes"]),
                  "source_ranges": scan["episodes"], "limits": "Heuristic relevance; semantic review and provenance require native independent agents."}
        save(self.internal("reports/" + scan_id + ".json"), report)
        pending.unlink()
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("baseline")
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("target")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("target")
    prepare.add_argument("candidate", type=Path)
    prepare.add_argument("analysis", type=Path)
    apply = commands.add_parser("apply")
    apply.add_argument("id")
    apply.add_argument("validation", type=Path)
    apply.add_argument("review", type=Path)
    verify = commands.add_parser("verify-applied")
    verify.add_argument("id")
    verify.add_argument("result", type=Path)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("id")
    scan = commands.add_parser("scan")
    scan.add_argument("--exclude-thread", action="append", required=True)
    scan.add_argument("--baseline", action="store_true")
    ack = commands.add_parser("acknowledge")
    ack.add_argument("id")
    ack.add_argument("outcome", choices=["no-change", "applied", "rejected", "rolled-back"])
    ack.add_argument("--candidate")
    args = parser.parse_args()
    store = Store()
    with store.lock():
        store.recover(skip_id=args.id if args.command in ("verify-applied", "rollback") else None)
        if args.command == "baseline":
            result = store.baseline()
        elif args.command == "snapshot":
            result = store.snapshot(args.target)
        elif args.command == "prepare":
            result = store.prepare(args.target, args.candidate, args.analysis)
        elif args.command == "apply":
            result = store.apply(args.id, args.validation, args.review)
        elif args.command == "verify-applied":
            result = store.verify_applied(args.id, args.result)
        elif args.command == "rollback":
            result = store.rollback(args.id)
        elif args.command == "scan":
            result = store.scan(Path.home() / ".codex/sessions", set(args.exclude_thread), args.baseline)
        else:
            result = store.acknowledge(args.id, args.outcome, args.candidate)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
