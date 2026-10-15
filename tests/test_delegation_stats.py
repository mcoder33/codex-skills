import datetime as dt
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "skills/delegation-stats/scripts/collect.py"
spec = importlib.util.spec_from_file_location("delegation_stats", MODULE)
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)
NOW = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
TIME = "2026-10-15T11:00:00Z"


def event(kind, payload, timestamp=TIME):
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def meta(thread, parent=None, path=None):
    return event("session_meta", {"id": thread, "session_id": "wrong-session-owner",
        "agent_path": path, "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "cli"})


def context(turn="turn-a", model="gpt-6-luna", effort="high"):
    return event("turn_context", {"turn_id": turn, "model": model, "effort": effort})


def response(thread="root", response="resp-a", turn="turn-a", total=120, **kw):
    usage = {"input_tokens": 100, "cached_input_tokens": 40, "cache_write_input_tokens": 0,
             "output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": total}
    usage.update(kw)
    return event("token_usage_record", {"thread_id": thread, "response_id": response,
        "turn_id": turn, "usage": usage,
        "thread_token_usage": {"total_tokens": 999999999}, "turn_token_usage": {"total_tokens": 88888888}})


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home, self.state, self.vault = (self.base / p for p in ("codex", "state", "vault"))
        (self.home / "sessions").mkdir(parents=True)
        self.vault.mkdir()
        self.source = self.home / "sessions" / "root_continuation.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, events, path=None, append=False):
        path = path or self.source
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w") as out:
            for item in events:
                out.write(json.dumps(item) + "\n")

    def collect(self, **kw):
        return stats.collect(self.home, self.state, self.vault, now=NOW, scenario=True, **kw)

    def test_response_sum_ignores_cumulative_compaction_and_fork_baseline(self):
        self.write([meta("root"), context(), response(),
            event("compacted", {"latest_token_usage_record": response()["payload"]}),
            event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 77777777}}})])
        self.write([meta("child", "root", "/root/work"), context(),
                    response("child", "child-own")], self.home / "sessions/child.jsonl")
        result = self.collect()
        self.assertEqual(result["totals"]["accounted_tokens"], 240)
        self.assertEqual(result["delegated"]["accounted_tokens"], 120)
        self.assertEqual(result["root_own"]["accounted_tokens"], 120)
        self.assertEqual(result["totals"]["fresh_input_tokens"], 120)
        self.assertEqual(result["totals"]["reasoning_output_tokens"], 10)

    def test_idempotence_continuation_archive_and_inherited_duplicate(self):
        records = [meta("root"), context(), response()]
        self.write(records)
        self.write(records, self.home / "archived_sessions/archive.jsonl")
        self.write([meta("root"), context(), response(response="resp-b")], self.home / "sessions/root_otheruuid.jsonl")
        self.write([meta("child", "root"), response()], self.home / "sessions/child.jsonl")
        first, second = self.collect(), self.collect()
        self.assertEqual(first["totals"], second["totals"])
        self.assertEqual(second["totals"]["responses"], 2)
        self.assertEqual(second["scan"]["bytes_read"], 0)
        self.assertEqual(second["coverage"]["conflicting_responses_excluded"], 0)

    def test_partial_line_is_deferred_until_append(self):
        self.write([meta("root"), context()])
        encoded = json.dumps(response()) + "\n"
        with self.source.open("a") as out:
            out.write(encoded[:40])
        first = self.collect()
        self.assertEqual(first["totals"]["responses"], 0)
        self.assertEqual(first["scan"]["partial_files"], 1)
        with self.source.open("a") as out:
            out.write(encoded[40:])
        second = self.collect()
        self.assertEqual(second["totals"]["responses"], 1)

    def test_truncate_and_replace_preserve_valid_records(self):
        self.write([meta("root"), context(), response()])
        self.collect()
        self.write([meta("root"), response(response="resp-b")])
        truncated = self.collect()
        self.assertEqual(truncated["totals"]["responses"], 2)
        replacement = self.source.with_suffix(".new")
        self.write([meta("root"), response(response="resp-c")], replacement)
        replacement.replace(self.source)
        replaced = self.collect()
        self.assertEqual(replaced["totals"]["responses"], 3)
        self.assertEqual(replaced["scan"]["resets"], 1)

    def test_conflicts_excluded_globally_including_different_owner(self):
        self.write([meta("root"), context(), response(), response(input_tokens=101, total=121),
                    response(response="resp-owner"), response("other", "resp-owner")])
        result = self.collect()
        self.assertEqual(result["totals"]["responses"], 0)
        self.assertEqual(result["coverage"]["conflicting_responses_excluded"], 2)

    def test_context_switch_per_response_and_child_settings_do_not_taint_parent(self):
        self.write([meta("root"), context(), response(),
            event("event_msg", {"type": "thread_settings_applied", "thread_id": "child",
                                "thread_settings": {"model": "gpt-6-astra"}}),
            response(response="resp-b"), context(model="gpt-6-astra"), response(response="resp-c")])
        result = self.collect()
        self.assertEqual(result["models"]["gpt-6-luna"]["responses"], 2)
        self.assertEqual(result["models"]["gpt-6-astra"]["responses"], 1)

    def test_ambiguous_turn_model_is_unknown_when_no_segment_context(self):
        self.write([meta("root"), response(), context(model="gpt-6-luna"), context(model="gpt-6-astra")])
        result = self.collect()
        self.assertEqual(result["coverage"]["unknown_model_responses"], 1)
        self.assertIsNone(result["threads"][0]["requested_model"])
        self.assertIsNone(result["totals"]["actual_cost_usd"])

    def test_unknown_metric_is_null_not_zero_and_money_exclusion(self):
        self.write([meta("root"), context(), response(cached_input_tokens=None, cache_write_input_tokens=None,
                                                    reasoning_output_tokens=None)])
        result = self.collect()
        self.assertIsNone(result["totals"]["cached_input_tokens"])
        self.assertIsNone(result["totals"]["reasoning_output_tokens"])
        self.assertEqual(result["api_equivalent_scenario"]["excluded_responses"], 1)
        self.assertIsNone(result["api_equivalent_scenario"]["usd"])

    def test_cost_known_zero_cache_write_and_no_reasoning_double_count(self):
        self.write([meta("root"), context(), response()])
        result = self.collect()
        self.assertAlmostEqual(result["api_equivalent_scenario"]["usd"], (60 * .1 + 40 * .01 + 20 * .5) / 1e6)
        self.assertEqual(result["totals"]["accounted_tokens"], 120)

    def test_cost_excludes_large_context_unknown_alias_and_cache_write(self):
        self.write([meta("root"), context(), response(cache_write_input_tokens=1),
                    response(response="large", input_tokens=200000, total=200020),
                    context(model="luna"), response(response="alias")])
        result = self.collect()
        self.assertEqual(result["api_equivalent_scenario"]["included_responses"], 0)

    def test_completion_is_not_business_success_and_unfinished_is_not_running(self):
        self.write([meta("root"), response(),
            event("event_msg", {"type": "task_started", "turn_id": "turn-a", "started_at": NOW.timestamp()-100}),
            event("event_msg", {"type": "task_complete", "turn_id": "turn-a", "duration_ms": 1250}),
            event("event_msg", {"type": "task_started", "turn_id": "unfinished"})])
        turns = self.collect()["threads"][0]["turns"]
        self.assertEqual(turns[0]["status"], "task_complete")
        self.assertIsNone(turns[0]["business_success"])
        self.assertEqual(turns[1]["status"], "no_observed_completion")

    def test_event_timestamp_not_mtime_and_expanding_window_rewinds(self):
        old = [meta("old"), response("old", "old-response")]
        for item in old:
            item["timestamp"] = "2026-10-01T10:00:00Z"
        self.write(old)
        fresh = self.home / "sessions/ancient-mtime.jsonl"
        self.write([meta("root"), response()], fresh)
        os.utime(fresh, (1, 1))
        first = self.collect()
        self.assertEqual(first["totals"]["responses"], 1)
        second = self.collect(days=30)
        self.assertEqual(second["totals"]["responses"], 2)

    def test_privacy_disallowed_fields_absent_from_all_derived_outputs(self):
        secret = "DO-NOT-COPY-PRIVATE-PROMPT-OR-CREDENTIAL"
        items = [meta("root"), context(), response(),
                 event("event_msg", {"type": "task_complete", "turn_id": "turn-a", "last_agent_message": secret}),
                 event("response_item", {"type": "message", "content": secret}),
                 event("response_item", {"type": "function_call", "name": "spawn_agent", "call_id": "c1",
                     "arguments": json.dumps({"message": secret, "task_name": "child", "model": "gpt-6-luna"})})]
        items[0]["payload"]["instructions"] = secret
        self.write(items)
        self.collect()
        for folder in (self.state, self.vault):
            for path in folder.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "telemetry.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_readonly_index_edges_and_mutable_model_not_used(self):
        with sqlite3.connect(self.home / "state_5.sqlite") as db:
            db.executescript("CREATE TABLE thread_spawn_edges(parent_thread_id,child_thread_id,status);"
                            "CREATE TABLE threads(id,agent_path,agent_role,model,title);")
            db.execute("INSERT INTO thread_spawn_edges VALUES ('root','child','open')")
            db.execute("INSERT INTO threads VALUES ('child','/root/child','worker','mutable-wrong-model','secret-title')")
        self.write([event("session_meta", {"id": "child"}), context(), response("child")])
        before = (self.home / "state_5.sqlite").read_bytes()
        result = self.collect(root_thread="root")
        self.assertEqual(result["delegated"]["responses"], 1)
        self.assertEqual(result["threads"][0]["runtime_models"], ["gpt-6-luna"])
        self.assertEqual(before, (self.home / "state_5.sqlite").read_bytes())

    def test_spawn_requested_model_join_and_ambiguity(self):
        self.write([meta("root"), response(),
            event("response_item", {"type": "function_call", "name": "spawn_agent", "call_id": "c1",
                "arguments": json.dumps({"task_name": "child", "model": "gpt-6-luna", "reasoning_effort": "high", "message": "private"})}),
            event("response_item", {"type": "function_call_output", "call_id": "c1", "output": json.dumps({"task_name": "/root/child"})})])
        self.write([meta("child", "root", "/root/child"), context(model="gpt-6.1-sol"), response("child", "c1response")], self.home / "sessions/child.jsonl")
        child = next(r for r in self.collect()["threads"] if r["thread"] == "child")
        self.assertEqual(child["requested_model"], "gpt-6-luna")
        self.assertEqual(child["runtime_models"], ["gpt-6.1-sol"])
        self.write([event("response_item", {"type": "function_call", "name": "spawn_agent", "call_id": "c1",
                "arguments": json.dumps({"task_name": "child", "model": "gpt-6-astra"})})], append=True)
        child = next(r for r in self.collect()["threads"] if r["thread"] == "child")
        self.assertIsNone(child["requested_model"])

    def test_only_managed_notes_are_modified(self):
        unrelated = self.vault / "Телеметрия/Личное.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("my note")
        self.write([meta("root"), response()])
        self.collect()
        self.assertEqual(unrelated.read_text(), "my note")
        dashboard = self.vault / "Телеметрия/Делегирование/Дашборд.md"
        dashboard.write_text("user-authored existing note")
        with self.assertRaises(ValueError):
            self.collect()
        self.assertEqual(dashboard.read_text(), "user-authored existing note")

    def test_observed_thread_without_response_records_has_unknown_expense(self):
        self.write([meta("root"), event("event_msg", {"type": "token_count", "info": None})])
        result = self.collect()
        self.assertEqual(result["coverage"]["legacy_only_threads"], 1)
        self.assertEqual(result["coverage"]["threads_without_response_records"], 1)
        self.assertEqual(result["threads"][0]["usage_coverage"], "unknown_no_response_records")
        self.assertIsNone(result["threads"][0]["usage"]["input_tokens"])
        self.assertIsNone(result["threads"][0]["usage"]["accounted_tokens"])

    def test_future_events_do_not_enter_current_window(self):
        item = response()
        item["timestamp"] = "2026-10-16T10:00:00Z"
        self.write([meta("root"), context(), item])
        self.assertEqual(self.collect()["totals"]["responses"], 0)

    def test_future_model_context_does_not_relabel_an_earlier_unknown_response(self):
        later = context(model="gpt-6-astra")
        later["timestamp"] = "2026-10-15T11:30:00Z"
        self.write([meta("root"), response(), later])
        self.assertEqual(self.collect()["coverage"]["unknown_model_responses"], 1)

    def test_native_markdown_links_resolve_inside_vault_and_tables_have_no_alias_pipes(self):
        self.write([meta("root"), context(), response()])
        self.write([meta("child", "root", "/root/child"), context(), response("child", "child-r")], self.home / "sessions/child.jsonl")
        self.collect()
        notes = list((self.vault / "Телеметрия/Делегирование").rglob("*.md"))
        for note in notes:
            text = note.read_text()
            for target in re.findall(r"\[\[([^]\n]+)\]\]", text):
                destination = target.replace(r"\|", "|").split("|", 1)[0]
                self.assertTrue((self.vault / (destination + ".md")).exists(), destination)
            for line in text.splitlines():
                if line.startswith("|") and "[[" in line:
                    for target in re.findall(r"\[\[([^]\n]+)\]\]", line):
                        self.assertNotRegex(target, r"(?<!\\)\|")
        child_note = self.vault / "Телеметрия/Делегирование/Агенты/child.md"
        self.assertIn("# child", child_note.read_text())
        self.assertNotIn("ambiguous_or_unknown", child_note.read_text())

    def test_inherited_metadata_before_own_metadata_uses_explicit_usage_owner(self):
        self.write([meta("root"), meta("child", "root", "/root/child"), context(), response("child")])
        result = self.collect()
        self.assertEqual(result["root_own"]["accounted_tokens"], 0)
        self.assertEqual(result["delegated"]["accounted_tokens"], 120)
        child = next(r for r in result["threads"] if r["thread"] == "child")
        self.assertEqual(child["root"], "root")
        self.assertEqual(child["runtime_models"], ["gpt-6-luna"])

    def test_missing_parent_provenance_is_not_silently_classified_as_root(self):
        self.write([event("session_meta", {"id": "unknown"}), context(), response("unknown")])
        result = self.collect()
        self.assertEqual(result["root_own"]["accounted_tokens"], 0)
        self.assertEqual(result["delegated"]["accounted_tokens"], 0)
        self.assertEqual(result["unclassified"]["accounted_tokens"], 120)
        self.assertEqual(result["coverage"]["unknown_classification_responses"], 1)

    def test_cli_default_env_and_explicit_codex_home_without_real_configuration(self):
        synthetic_home = self.base / "synthetic-user"
        configured_home = self.base / "configured-codex"
        explicit_home = self.base / "explicit-codex"
        original_get = stats.os.environ.get
        original_home_env = stats.os.environ.get("HOME")
        empty_result = {"totals": {"responses": 0, "accounted_tokens": 0}, "threads": [], "coverage": {}, "scan": {}}
        cases = [(None, [], synthetic_home / ".codex"),
                 (str(configured_home), [], configured_home),
                 (str(configured_home), ["--codex-home", str(explicit_home)], explicit_home)]
        for configured, arguments, expected in cases:
            with self.subTest(configured=configured, arguments=arguments):
                def environment_get(key, default=None):
                    return configured if key == "CODEX_HOME" else original_get(key, default)
                with mock.patch.object(stats.os.environ, "get", side_effect=environment_get), \
                     mock.patch.object(stats.Path, "home", return_value=synthetic_home), \
                     mock.patch.object(stats, "collect", return_value=empty_result) as collector, \
                     mock.patch("sys.argv", ["collect.py", *arguments]), \
                     mock.patch("sys.stdout", new_callable=io.StringIO):
                    stats.main()
                self.assertEqual(collector.call_args.args[0], expected.resolve())
        self.assertEqual(stats.os.environ.get("HOME"), original_home_env)
        self.assertFalse(synthetic_home.exists())
        self.assertFalse(configured_home.exists())
        self.assertFalse(explicit_home.exists())

    def test_invalid_accounting_excluded(self):
        self.write([meta("root"), response(total=999), response(response="cache", cached_input_tokens=101),
                    response(response="reason", reasoning_output_tokens=21)])
        result = self.collect()
        self.assertEqual(result["totals"]["responses"], 0)
        self.assertEqual(result["coverage"]["invalid_responses_excluded"], 3)


if __name__ == "__main__":
    unittest.main()
