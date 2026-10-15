#!/usr/bin/env python3
"""Private, incremental Codex metadata collector. Python 3.11+, standard library only."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
ZONE = ZoneInfo("Europe/Samara")
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
              "output_tokens", "reasoning_output_tokens", "total_tokens")
RATES = {"gpt-6-luna": (0.10, 0.01, 0.50),
         "gpt-6.1-sol": (2.00, 0.10, 10.00), "gpt-6-astra": (10.00, 1.00, 50.00)}
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
SAFE = re.compile(r"^[A-Za-z0-9_.:/-]{1,180}$")
ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


def safe(value):
    return value if isinstance(value, str) and SAFE.fullmatch(value) else None


def identifier(value):
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def stamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return dt.datetime.fromtimestamp(value, UTC).timestamp()
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".telemetry-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path)
    os.chmod(path, 0o600)
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS cursors (
        path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER, size INTEGER, mtime INTEGER,
        prefix TEXT, state TEXT);
      CREATE TABLE IF NOT EXISTS observations (
        digest TEXT PRIMARY KEY, kind TEXT, stamp REAL, data TEXT);
    """)
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        # Rebuild metadata attribution after the ownership fix, preserving valid
        # response observations even when a rotated source is no longer present.
        conn.execute("DELETE FROM cursors")
        conn.execute("DELETE FROM observations WHERE kind='context'")
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    return conn


def save(conn, kind, timestamp, data):
    encoded = canonical(data)
    digest = hashlib.sha256(canonical([kind, timestamp, data]).encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?)",
                 (digest, kind, timestamp, encoded))


def extract(conn, event, state, cutoff):
    """Allowlist at the ingestion boundary; never persist raw payloads or exception text."""
    p = event.get("payload")
    if not isinstance(p, dict):
        return
    kind, timestamp = event.get("type"), stamp(event.get("timestamp"))
    if kind == "session_meta":
        thread = identifier(p.get("id"))
        if thread:
            source = p.get("source")
            sub = source.get("subagent", {}) if isinstance(source, dict) else {}
            spawn = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
            spawn = spawn if isinstance(spawn, dict) else {}
            parent = identifier(spawn.get("parent_thread_id"))
            agent_path = safe(p.get("agent_path") or spawn.get("agent_path"))
            root_proven = (isinstance(source, str) and source in {"cli", "vscode", "exec", "app-server"}) or agent_path == "/root"
            child_proven = bool(parent or (isinstance(source, dict) and "subagent" in source) or
                                (agent_path and agent_path.startswith("/root/")))
            metas = state.setdefault("meta_ids", [])
            if thread not in metas:
                metas.append(thread)
            # A physical file may contain inherited metadata in either order.
            # No metadata position is an ownership guarantee.
            state["owner"] = metas[0] if len(metas) == 1 else None
            save(conn, "thread", timestamp, {"thread": thread, "parent": parent,
                 "agent_path": agent_path, "role": safe(p.get("agent_role") or spawn.get("agent_role")),
                 "root_proven": bool(root_proven), "child_proven": child_proven,
                 "forked_from": identifier(p.get("forked_from_id"))})
        return
    owner = state.get("owner")
    if kind == "turn_context":
        turn = identifier(p.get("turn_id"))
        context = {"model": safe(p.get("model")), "effort": safe(p.get("effort")), "timestamp": timestamp}
        if turn:
            state.setdefault("turns", {})[turn] = context
        return
    if kind == "token_usage_record":
        thread, response = identifier(p.get("thread_id")), identifier(p.get("response_id"))
        if timestamp is None or timestamp < cutoff:
            return
        if not thread or not response:
            save(conn, "issue", timestamp, {"code": "missing_response_identity", "thread": thread or owner})
            return
        usage = p.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        turn = identifier(p.get("turn_id"))
        context = state.get("turns", {}).get(turn, {})
        if context:
            save(conn, "context", context.get("timestamp"), {"thread": thread, "turn": turn,
                 "model": context.get("model"), "effort": context.get("effort")})
        save(conn, "response", timestamp, {"thread": thread, "response": response,
             "turn": turn, "model": context.get("model"), "effort": context.get("effort"),
             "usage": {key: number(usage.get(key)) for key in USAGE_KEYS}})
        return
    if timestamp is None or timestamp < cutoff:
        return
    if kind == "event_msg":
        event_type = p.get("type")
        if event_type in ("task_started", "task_complete", "turn_aborted"):
            save(conn, "turn", timestamp, {"thread": owner, "turn": identifier(p.get("turn_id")),
                 "event": event_type, "started": stamp(p.get("started_at")),
                 "completed": stamp(p.get("completed_at")), "duration_ms": number(p.get("duration_ms"))})
        elif event_type == "token_count":
            save(conn, "legacy", timestamp, {"thread": owner})
        elif event_type == "thread_settings_applied":
            # This event can configure a CHILD while physically inside the parent rollout.
            settings = p.get("thread_settings") or {}
            if isinstance(settings, dict):
                save(conn, "settings", timestamp, {"thread": identifier(p.get("thread_id")),
                     "model": safe(settings.get("model")), "effort": safe(settings.get("reasoning_effort"))})
    elif kind == "response_item":
        call = identifier(p.get("call_id"))
        if p.get("type") == "function_call" and str(p.get("name", "")).endswith("spawn_agent"):
            try:
                args = json.loads(p.get("arguments", "{}"))
            except (ValueError, TypeError):
                return
            if isinstance(args, dict):
                save(conn, "spawn_call", timestamp, {"thread": owner, "call": call,
                     **{key: safe(args.get(key)) for key in ("task_name", "agent_type", "fork_turns", "model", "reasoning_effort")}})
        elif p.get("type") == "function_call_output":
            try:
                output = json.loads(p.get("output", "{}"))
            except (ValueError, TypeError):
                return
            if isinstance(output, dict) and safe(output.get("task_name")):
                save(conn, "spawn_result", timestamp, {"thread": owner, "call": call,
                     "task_name": safe(output.get("task_name"))})


def scan(conn, home, cutoff):
    result = {"files": 0, "bytes_read": 0, "partial_files": 0, "malformed_lines": 0,
              "unreadable_files": 0, "resets": 0}
    for folder in ("sessions", "archived_sessions"):
        for path in sorted((home / folder).rglob("*.jsonl")):
            result["files"] += 1
            try:
                stat = path.stat()
                row = conn.execute("SELECT inode,offset,size,mtime,prefix,state FROM cursors WHERE path=?", (str(path),)).fetchone()
                with path.open("rb") as src:
                    prefix = hashlib.sha256(src.read(min(256, stat.st_size))).hexdigest()
                    offset, state = 0, {}
                    if row:
                        inode, previous, size, mtime, old_prefix, encoded = row
                        # Same-size mutation and prefix changes are replacements too. A previous
                        # short prefix may grow on normal append, so compare only once >=256 bytes.
                        reset = (inode != stat.st_ino or stat.st_size < size or
                                 (stat.st_size == size and stat.st_mtime_ns != mtime) or
                                 (size >= 256 and old_prefix != prefix))
                        if not reset:
                            offset, state = previous, json.loads(encoded)
                        else:
                            result["resets"] += 1
                    src.seek(offset)
                    while True:
                        line = src.readline()
                        if not line:
                            break
                        result["bytes_read"] += len(line)
                        if not line.endswith(b"\n"):
                            result["partial_files"] += 1
                            break
                        offset = src.tell()
                        try:
                            event = json.loads(line)
                            if isinstance(event, dict):
                                extract(conn, event, state, cutoff)
                        except (ValueError, UnicodeDecodeError, TypeError):
                            result["malformed_lines"] += 1
                    conn.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?,?,?,?)",
                                 (str(path), stat.st_ino, offset, stat.st_size, stat.st_mtime_ns, prefix, canonical(state)))
            except OSError:
                result["unreadable_files"] += 1
    conn.commit()
    return result


def read_index(conn, home):
    """Optional Codex index, read-only. No titles, prompts, tokens_used or mutable model."""
    path = home / "state_5.sqlite"
    result = {"available": False, "error": None}
    if not path.exists():
        return result
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as index:
            for thread, child in index.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                if identifier(thread) and identifier(child):
                    save(conn, "edge", None, {"parent": thread, "child": child})
            columns = {r[1] for r in index.execute("PRAGMA table_info(threads)")}
            selected = [c for c in ("id", "agent_path", "agent_role") if c in columns]
            if "id" in selected:
                for row in index.execute("SELECT " + ",".join(selected) + " FROM threads"):
                    item = dict(zip(selected, row))
                    if identifier(item["id"]):
                        save(conn, "thread", None, {"thread": item["id"], "parent": None,
                             "agent_path": safe(item.get("agent_path")), "role": safe(item.get("agent_role")), "forked_from": None,
                             "root_proven": item.get("agent_path") == "/root",
                             "child_proven": bool(item.get("agent_path") and item["agent_path"].startswith("/root/"))})
        result["available"] = True
    except sqlite3.Error:
        result["error"] = "index_unavailable_or_schema_changed"
    conn.commit()
    return result


def valid_usage(u):
    if u["input_tokens"] is None or u["output_tokens"] is None:
        return False
    if u["cached_input_tokens"] is not None and u["cached_input_tokens"] > u["input_tokens"]:
        return False
    if u["reasoning_output_tokens"] is not None and u["reasoning_output_tokens"] > u["output_tokens"]:
        return False
    return u["total_tokens"] is None or u["total_tokens"] == u["input_tokens"] + u["output_tokens"]


def aggregate(responses):
    result = {"responses": len(responses), "actual_cost_usd": None, "auth_mode": "unknown"}
    for key in USAGE_KEYS:
        values = [r["usage"][key] for r in responses]
        result[key] = sum(v for v in values if v is not None) if any(v is not None for v in values) else None
        result[key + "_unknown_responses"] = sum(v is None for v in values)
    result["accounted_tokens"] = sum(r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in responses)
    known = [r for r in responses if r["usage"]["cached_input_tokens"] is not None]
    result["fresh_input_tokens"] = sum(r["usage"]["input_tokens"] - r["usage"]["cached_input_tokens"] for r in known) if known else None
    return result


def api_scenario(responses):
    eligible, total = [], 0.0
    for r in responses:
        u, rate = r["usage"], RATES.get(r.get("model"))
        if rate and u["input_tokens"] <= 128000 and u["cached_input_tokens"] is not None and u["cache_write_input_tokens"] == 0:
            total += ((u["input_tokens"] - u["cached_input_tokens"]) * rate[0] +
                      u["cached_input_tokens"] * rate[1] + u["output_tokens"] * rate[2]) / 1000000
            eligible.append(r)
    tokens = aggregate(eligible)["accounted_tokens"]
    return {"usd": total if eligible else None, "included_responses": len(eligible),
            "excluded_responses": len(responses) - len(eligible), "included_tokens": tokens,
            "all_valid_tokens": aggregate(responses)["accounted_tokens"], "source": PRICING_SOURCE,
            "verified_date": "2026-10-15", "assumptions": "Standard short-context scenario; input <=128000; zero cache-write; no tool fees; not a subscription bill"}


def report(conn, cutoff, now, root_filter=None, scenario=False):
    observations = [(kind, timestamp, json.loads(data)) for kind, timestamp, data in conn.execute("SELECT kind,stamp,data FROM observations ORDER BY stamp,digest")]
    threads, parents, variants, contexts, calls, results = {}, {}, {}, {}, {}, {}
    issues, legacy, observed_threads, last_seen = [], set(), set(), {}
    for kind, timestamp, p in observations:
        if timestamp is not None and cutoff <= timestamp <= now.timestamp() and p.get("thread"):
            observed_threads.add(p["thread"])
            last_seen[p["thread"]] = max(timestamp, last_seen.get(p["thread"], 0))
        if kind == "thread":
            target = threads.setdefault(p["thread"], {"thread": p["thread"], "agent_path": None, "role": None, "root_proven": False, "child_proven": False})
            target["root_proven"] = target["root_proven"] or p.get("root_proven", False)
            target["child_proven"] = target["child_proven"] or p.get("child_proven", False)
            for key in ("agent_path", "role"):
                if p.get(key):
                    target[key] = p[key]
            if p.get("parent"):
                parents.setdefault(p["thread"], set()).add(p["parent"])
        elif kind == "edge":
            parents.setdefault(p["child"], set()).add(p["parent"])
        elif kind == "context" and p.get("thread") and p.get("turn"):
            contexts.setdefault((p["thread"], p["turn"]), set()).add((timestamp, p.get("model"), p.get("effort")))
        elif timestamp is not None and cutoff <= timestamp <= now.timestamp():
            if kind == "response":
                variants.setdefault(p["response"], []).append({**p, "observed_at": timestamp})
            elif kind == "issue":
                issues.append(p)
            elif kind == "legacy":
                legacy.add(p.get("thread"))
            elif kind == "spawn_call":
                calls.setdefault((p.get("thread"), p.get("call")), []).append((timestamp, p))
            elif kind == "spawn_result":
                results.setdefault((p.get("thread"), p.get("call")), set()).add(p["task_name"])
    parent_map = {}
    for child, options in parents.items():
        if len(options) == 1:
            parent_map[child] = next(iter(options))
        else:
            issues.append({"code": "ambiguous_parent", "thread": child})
    def root(thread):
        seen = set()
        while thread in parent_map:
            if thread in seen:
                issues.append({"code": "parent_cycle", "thread": thread})
                break
            seen.add(thread)
            thread = parent_map[thread]
        return thread
    def classify(thread):
        meta = threads.get(thread, {})
        child = thread in parent_map or meta.get("child_proven", False)
        is_root = meta.get("root_proven", False)
        if child and is_root:
            return "unknown"
        return "delegated" if child else "root" if is_root else "unknown"
    responses, conflicted, invalid = [], 0, 0
    for response, items in variants.items():
        signatures = {canonical([p["thread"], p["usage"]]) for p in items}
        if len(signatures) != 1:
            conflicted += 1
            issues.append({"code": "conflicting_response", "response": response})
            continue
        p = dict(items[0])
        if not valid_usage(p["usage"]):
            invalid += 1
            continue
        runtime = {(i.get("model"), i.get("effort")) for i in items if i.get("model")}
        if not runtime:
            runtime = {(model, effort) for timestamp, model, effort in contexts.get((p["thread"], p.get("turn")), set())
                       if model and timestamp is not None and timestamp <= p["observed_at"]}
        p["model"] = next(iter(runtime))[0] if len(runtime) == 1 else None
        p["effort"] = next(iter(runtime))[1] if len(runtime) == 1 else None
        p["runtime_state"] = "known" if len(runtime) == 1 else "mixed_or_unknown"
        p["root"] = root(p["thread"])
        p["classification"] = classify(p["thread"])
        if root_filter is None or p["root"] == root_filter:
            responses.append(p)
    by_thread, by_model = {}, {}
    for r in responses:
        by_thread.setdefault(r["thread"], []).append(r)
        by_model.setdefault(r.get("model") or "unknown/mixed", []).append(r)
    # Expense remains unknown for observed threads with no reliable usage.
    usage_threads = set(by_thread)
    for thread in observed_threads:
        if root_filter is None or root(thread) == root_filter:
            by_thread.setdefault(thread, [])
    # Attempts are joined by canonical agent path + parent + attempt time boundary.
    for thread in set(by_thread) | set(parent_map):
        threads.setdefault(thread, {"thread": thread, "agent_path": None, "role": None, "root_proven": False, "child_proven": False})
    active_turns = {}
    usage_turn_owners = {}
    for response in responses:
        if response.get("turn"):
            usage_turn_owners.setdefault(response["turn"], set()).add(response["thread"])
    unattributed_turn_events = 0
    for kind, timestamp, p in observations:
        if kind == "turn" and timestamp is not None and cutoff <= timestamp <= now.timestamp():
            explicit_owners = usage_turn_owners.get(p.get("turn"), set())
            owner = next(iter(explicit_owners)) if len(explicit_owners) == 1 else p.get("thread") if not explicit_owners else None
            if not owner:
                unattributed_turn_events += 1
                continue
            key = (owner, p.get("turn"))
            active_turns.setdefault(key, []).append((timestamp, p))
    thread_reports = []
    for thread, records in by_thread.items():
        meta = threads[thread]
        turns = []
        for (owner, turn), items in active_turns.items():
            if owner != thread:
                continue
            completions = [p for _, p in items if p["event"] in ("task_complete", "turn_aborted")]
            finish = completions[-1] if completions else None
            starts = [p.get("started") for _, p in items if p.get("started") is not None]
            duration = finish.get("duration_ms") if finish else None
            if duration is None and finish and finish.get("completed") is not None and finish.get("started") is not None:
                duration = max(0, (finish["completed"] - finish["started"]) * 1000)
            turns.append({"turn": turn, "status": finish["event"] if finish else "no_observed_completion",
                          "duration_ms": duration, "started": min(starts) if starts else None,
                          "business_success": None})
        candidates = []
        for key, outputs in results.items():
            if parent_map.get(thread) and key[0] == parent_map[thread] and meta.get("agent_path") in outputs:
                for timestamp, p in calls.get(key, []):
                    starts = [t["started"] for t in turns if t["started"] is not None]
                    if not starts or timestamp <= min(starts) + 1:
                        candidates.append((timestamp, p))
        request = candidates[0][1] if len(candidates) == 1 else None
        if len(candidates) > 1:
            issues.append({"code": "ambiguous_spawn_attempt", "thread": thread})
        thread_usage = aggregate(records)
        if not records:
            thread_usage["accounted_tokens"] = None
        thread_reports.append({**meta, "root": root(thread), "parent": parent_map.get(thread), "classification": classify(thread),
             "requested_model": request.get("model") if request else None,
             "requested_effort": request.get("reasoning_effort") if request else None,
             "request_match": "matched" if request else "ambiguous_or_unknown",
             "runtime_models": sorted({r.get("model") or "unknown/mixed" for r in records}),
             "runtime_efforts": sorted({r.get("effort") or "unknown" for r in records}),
             "last_observed_at": last_seen.get(thread),
             "api_equivalent_scenario": api_scenario(records) if scenario else None,
             "usage": thread_usage, "usage_coverage": "response_records" if records else "unknown_no_response_records", "turns": turns,
             "summed_agent_time_ms": sum(t["duration_ms"] for t in turns if t["duration_ms"] is not None) if any(t["duration_ms"] is not None for t in turns) else None,
             "actual_cost_usd": None})
    own = [r for r in responses if r["classification"] == "root"]
    delegated = [r for r in responses if r["classification"] == "delegated"]
    unclassified = [r for r in responses if r["classification"] == "unknown"]
    totals = aggregate(responses)
    output = {"schema_version": 1, "generated_at": now.isoformat(), "timezone": "Europe/Samara",
       "window_start": dt.datetime.fromtimestamp(cutoff, UTC).isoformat(), "root_filter": root_filter,
       "totals": totals, "root_own": aggregate(own), "delegated": aggregate(delegated), "unclassified": aggregate(unclassified),
       "models": {m: {**aggregate(rs), "api_equivalent_scenario": api_scenario(rs) if scenario else None} for m, rs in sorted(by_model.items())},
       "threads": sorted(thread_reports, key=lambda p: p["thread"]),
       "coverage": {"unique_response_ids_seen": len(variants), "valid_responses_in_selection": len(responses),
         "conflicting_responses_excluded": conflicted, "invalid_responses_excluded": invalid,
         "unknown_model_responses": sum(r.get("model") is None for r in responses),
         "unknown_classification_responses": len(unclassified), "unattributed_turn_events": unattributed_turn_events,
         "delegated_threads_without_parent_link": sum(r["classification"] == "delegated" and not r["parent"] for r in thread_reports),
         "legacy_only_threads": len(legacy - usage_threads),
         "threads_without_response_records": sum(not records for records in by_thread.values()), "issues": issues,
         "duration_unknown_turns": sum(t["duration_ms"] is None for tr in thread_reports for t in tr["turns"])},
       "api_equivalent_scenario": api_scenario(responses) if scenario else None}
    return output


def fmt(value):
    return "неизвестно" if value is None else f"{value:,}".replace(",", " ")


def note_link(thread, label=None, table=False):
    separator = r"\|" if table else "|"
    return f"[[Телеметрия/Делегирование/Агенты/{thread}{separator}{label or thread[:12]}]]"


def write_dashboard(vault, data):
    target = vault / "Телеметрия" / "Делегирование"
    labels = {r["thread"]: (r.get("agent_path") or "").split("/")[-1].replace("_", " ") or r["thread"][:12]
              for r in data["threads"]}
    def link(thread, table=False):
        return note_link(thread, labels.get(thread), table)
    statuses = {"task_complete": "ход завершён", "turn_aborted": "ход прерван",
                "no_observed_completion": "завершение не наблюдалось"}
    matches = {"matched": "запрос сопоставлен", "ambiguous_or_unknown": "запрос не сопоставлен однозначно"}
    def runtime_names(models):
        return ", ".join("неизвестна/смешанная" if m == "unknown/mixed" else m for m in models) or "неизвестна"
    # Only files bearing this collector marker are replaceable/deletable.
    marker = "<!-- codex-delegation-stats:v1 -->"
    def write_owned(path, body):
        if path.exists() and not path.read_text(encoding="utf-8").startswith(marker):
            raise ValueError("Refusing to replace an unmanaged telemetry note")
        atomic(path, marker + "\n" + body)
    totals, coverage = data["totals"], data["coverage"]
    generated = dt.datetime.fromisoformat(data["generated_at"]).astimezone(ZONE)
    start = dt.datetime.fromisoformat(data["window_start"]).astimezone(ZONE)
    lines = ["# Делегирование Codex", "", f"Обновлено: {generated:%Y-%m-%d %H:%M:%S} Europe/Samara.",
        f"Окно событий: {start:%Y-%m-%d %H:%M} — {generated:%Y-%m-%d %H:%M}.", "",
        "Считаются уникальные response-level записи. Cached входит в input, reasoning входит в output.",
        "Показанные суммы — известная часть; пропуски перечислены ниже. Длительности агентов могут перекрываться.", "",
        "| Группа | Input | Cached ⊂ input | Fresh input | Output | Reasoning ⊂ output | Input + output |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for label, row in (("Главные агенты: собственные", data["root_own"]), ("Делегированные", data["delegated"]), ("Тип агента неизвестен", data["unclassified"]), ("Всего", totals)):
        lines.append("| " + label + " | " + " | ".join(fmt(row[k]) for k in ("input_tokens", "cached_input_tokens", "fresh_input_tokens", "output_tokens", "reasoning_output_tokens", "accounted_tokens")) + " |")
    lines += ["", "## Деньги", "", "Фактические списания: **недоступны**. Режим оплаты: неизвестен.",
              "Тарифы API отдельно от подписки: [пояснение](https://learn.chatgpt.com/docs/pricing)."]
    cost = data["api_equivalent_scenario"]
    if cost:
        usd = "недоступен" if cost["usd"] is None else f"${cost['usd']:.4f}"
        lines += ["", f"API-эквивалент, сценарий: **{usd}**. Это не счёт и не расход подписки.",
            f"Включено {cost['included_responses']} ответов / {fmt(cost['included_tokens'])} токенов; исключено {cost['excluded_responses']} ответов.",
            "Условия сценария: точное имя модели, Standard short-context, input ≤128 000, cache-write = 0, без стоимости инструментов.",
            f"[Источник тарифов]({PRICING_SOURCE}), проверен 2026-10-15. Лимит input — условие сценария, а не утверждение о тарифном пороге."]
    lines += ["", "## По runtime-моделям", "", "Runtime здесь — конфигурация turn_context, не квитанция backend.", "",
        "| Модель | Ответы | Input | Cached | Output | Всего | API-сценарий, USD |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for model, row in data["models"].items():
        price = row.get("api_equivalent_scenario")
        price_text = f"${price['usd']:.4f}" if price and price["usd"] is not None else "недоступен"
        lines.append(f"| {runtime_names([model])} | {row['responses']} | {fmt(row['input_tokens'])} | {fmt(row['cached_input_tokens'])} | {fmt(row['output_tokens'])} | {fmt(row['accounted_tokens'])} | {price_text} |")
    lines += ["", "## Покрытие", "", f"Ответов принято: {totals['responses']}; конфликтов исключено: {coverage['conflicting_responses_excluded']}; неверных usage исключено: {coverage['invalid_responses_excluded']}.",
        f"Runtime-модель неизвестна: {coverage['unknown_model_responses']}; legacy-only потоков без надёжного расхода: {coverage['legacy_only_threads']}; всех потоков без response-level usage: {coverage['threads_without_response_records']}.",
        f"Не удалось определить root/child для {coverage['unknown_classification_responses']} ответов; события ходов без владельца: {coverage['unattributed_turn_events']}; делегированные потоки без связи с родителем: {coverage['delegated_threads_without_parent_link']}.",
        f"Неизвестная длительность: {coverage['duration_unknown_turns']} ходов. Завершение хода не подтверждает успех задачи."]
    for key in USAGE_KEYS:
        if totals[key + "_unknown_responses"]:
            lines.append(f"- {key}: неизвестно в {totals[key + '_unknown_responses']} ответах.")
    scan_info = data.get("scan", {})
    lines.append(f"- Чтение: незавершённых файлов {scan_info.get('partial_files', 0)}, неверных JSON-строк {scan_info.get('malformed_lines', 0)}, недоступных файлов {scan_info.get('unreadable_files', 0)}.")
    latest_delegates = sorted((r for r in data["threads"] if r["parent"]), key=lambda r: (r.get("last_observed_at") or 0, r["thread"]), reverse=True)[:20]
    lines += ["", "## Последние делегирования", "", "| Агент | Запрошена | Runtime | Собственные токены | Сумма ходов, с | API-сценарий, USD |", "| --- | --- | --- | ---: | ---: | ---: |"]
    for row in latest_delegates:
        price = row.get("api_equivalent_scenario")
        money = f"${price['usd']:.4f}" if price and price["usd"] is not None else "недоступен"
        duration = round(row["summed_agent_time_ms"] / 1000, 2) if row["summed_agent_time_ms"] is not None else None
        count = fmt(row["usage"]["accounted_tokens"]) if row["usage_coverage"] == "response_records" else "неизвестны"
        lines.append(f"| {link(row['thread'], table=True)} | {row['requested_model'] or 'неизвестна'} / {row['requested_effort'] or 'неизвестен'} | {runtime_names(row['runtime_models'])} | {count} | {fmt(duration)} | {money} |")
    groups = {}
    for row in data["threads"]:
        groups.setdefault(row["root"], []).append(row)
    def latest(rows):
        return max([r.get("last_observed_at") or 0 for r in rows] + [t.get("started") or 0 for r in rows for t in r["turns"]], default=0)
    visible = sorted(groups, key=lambda r: (latest(groups[r]), r), reverse=True)[:50]
    lines += ["", "## Последние группы делегирования", "", f"Показано {len(visible)} из {len(groups)} групп; агрегаты выше включают все группы окна.", "",
        "| Корневой узел группы | Агентов | Известных токенов | Сумма времени агентов, с |", "| --- | ---: | ---: | ---: |"]
    keep = set()
    for group in visible:
        rows = groups[group]
        durations = [r["summed_agent_time_ms"] for r in rows if r["summed_agent_time_ms"] is not None]
        seconds = round(sum(durations) / 1000, 2) if durations else None
        lines.append(f"| {link(group, table=True)} | {len(rows)} | {fmt(sum(r['usage']['accounted_tokens'] or 0 for r in rows))} | {fmt(seconds)} |")
        for row in rows:
            name = row["thread"]
            keep.add(name + ".md")
            parent = link(row["parent"]) if row["parent"] else "главный агент (подтверждён)" if row["classification"] == "root" else "связь с родителем неизвестна"
            children = [r for r in rows if r["parent"] == name]
            body = [f"# {labels.get(name, name)}", "", f"Идентификатор: {name}.", f"Родитель: {parent}.", f"Корень: {link(group)}.",
                    f"Роль: {row['role'] or 'неизвестна'}; путь: {row['agent_path'] or 'неизвестен'}.",
                    f"Запрошенная модель / effort: {row['requested_model'] or 'неизвестна'} / {row['requested_effort'] or 'неизвестен'} ({matches[row['request_match']]}).",
                    f"Runtime-модели: {runtime_names(row['runtime_models'])}; effort: {', '.join('неизвестен' if e == 'unknown' else e for e in row['runtime_efforts']) or 'неизвестен'}.",
                    f"Собственные известные токены: {fmt(row['usage']['accounted_tokens']) if row['usage_coverage'] == 'response_records' else 'неизвестны: нет response-level записей'}.",
                    f"Сумма длительностей ходов, мс: {fmt(row['summed_agent_time_ms'])}. Это не elapsed всей группы.",
                    "Фактическая стоимость: недоступна. Успех задачи: неизвестен.",
                    "API-сценарий: " + (f"${row['api_equivalent_scenario']['usd']:.6f}; включено {row['api_equivalent_scenario']['included_responses']}, исключено {row['api_equivalent_scenario']['excluded_responses']} ответов" if row.get("api_equivalent_scenario") and row["api_equivalent_scenario"]["usd"] is not None else "недоступен"),
                    "", "Дочерние агенты: " + (", ".join(link(c['thread']) for c in children) or "не наблюдались"), "",
                    "| Ход | Наблюдение | Длительность, мс |", "| --- | --- | ---: |"]
            for turn in row["turns"]:
                body.append(f"| {turn['turn'] or 'неизвестен'} | {statuses[turn['status']]} | {fmt(turn['duration_ms'])} |")
            write_owned(target / "Агенты" / (name + ".md"), "\n".join(body) + "\n")
        if group not in {r["thread"] for r in rows}:
            keep.add(group + ".md")
            write_owned(target / "Агенты" / (group + ".md"), f"# {group}\n\nСобственные usage-записи корня в этом окне не наблюдались.\n\nДети: " + ", ".join(link(r["thread"]) for r in rows) + "\n")
    for old in (target / "Агенты").glob("*.md"):
        if old.name not in keep and old.read_text(encoding="utf-8").startswith(marker):
            old.unlink()
    write_owned(target / "Дашборд.md", "\n".join(lines) + "\n")
    return target / "Дашборд.md"


def collect(home, state_dir, vault=None, days=7, root_thread=None, scenario=False, now=None):
    now = now or dt.datetime.now(UTC)
    cutoff = now.timestamp() - days * 86400
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    # One writer; sources are never locked or modified.
    import fcntl
    with (state_dir / "collector.lock").open("w") as lock:
        os.chmod(state_dir / "collector.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        conn = database(state_dir / "telemetry.sqlite3")
        try:
            # More history needs an explicit rewind; keep normalized prior observations.
            config_path = state_dir / "window.json"
            config = json.loads(config_path.read_text()) if config_path.exists() else {}
            if cutoff < config.get("earliest_cutoff", cutoff):
                conn.execute("DELETE FROM cursors")
            scan_info = scan(conn, home, cutoff)
            index_info = read_index(conn, home)
            data = report(conn, cutoff, now, root_thread, scenario)
            data["scan"], data["index"] = scan_info, index_info
            atomic(state_dir / "summary.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            atomic(config_path, canonical({"earliest_cutoff": min(cutoff, config.get("earliest_cutoff", cutoff))}))
            if vault:
                data["dashboard"] = str(write_dashboard(vault, data))
            return data
        finally:
            conn.close()


def default_codex_home():
    configured_home = os.environ.get("CODEX_HOME")
    return Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "codex-dialogue-kit" / "telemetry")
    parser.add_argument("--vault", type=Path, help="Existing Obsidian vault; only Телеметрия/Делегирование is managed")
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--root-thread", type=identifier)
    parser.add_argument("--api-equivalent", action="store_true", help="Optional API price scenario, never actual subscription cost")
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    data = collect(args.codex_home.expanduser().resolve(), args.state_dir.expanduser().resolve(),
                   args.vault.expanduser().resolve() if args.vault else None, args.days, args.root_thread, args.api_equivalent)
    print(json.dumps({"responses": data["totals"]["responses"], "tokens": data["totals"]["accounted_tokens"],
          "threads": len(data["threads"]), "coverage": data["coverage"], "scan": data["scan"],
          "dashboard": data.get("dashboard")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
