import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/session-retrospective/scripts/retrospective.py"
spec = importlib.util.spec_from_file_location("retrospective", SCRIPT)
retro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retro)


class RetrospectiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skills = self.root / "skills"
        self.target_name = "dialogue-explore/SKILL.md"
        self.original = b'---\nname: dialogue-explore\ndescription: Test dialogue skill\nmetadata:\n  version: "0.1.0"\n---\n\nConsider the current question.\n'
        for skill in retro.SKILLS:
            folder = self.skills / skill
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_bytes(self.original)
        self.target = self.skills / self.target_name
        self.store = retro.Store(self.root / "state", self.skills)
        self.analysis = {"analyst_id": "analyst-1", "reason": "Two independently observed missed constraints.",
                         "expected_effect": "Restore the active constraint before selecting a question.",
                         "comparison_criteria": "Observed selection follows the current constraint.",
                         "evidence": [self.evidence("session-1", "turn-1"), self.evidence("session-2", "turn-2")]}
        self.candidate = self.root / "candidate.md"
        self.candidate.write_bytes(self.original.replace(b'0.1.0', b'0.1.1') + b'Restore the active constraint before the next question.\n')
        self.analysis_path = self.root / "analysis.json"

    def evidence(self, session, turn, kind="episode"):
        return {"session_id": session, "turn_id": turn, "events": [100, 250],
                "summary": "A constraint was omitted from the next question.", "kind": kind}

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def prepare(self):
        self.analysis_path.write_text(json.dumps(self.analysis))
        return self.store.prepare(self.target_name, self.candidate, self.analysis_path)

    def artifacts(self, manifest):
        validation = {"candidate_sha256": manifest["candidate_sha256"], "baseline_sha256": manifest["baseline"]["sha256"],
                      "runner_id": "runner-1", "syntax_passed": True, "cases": []}
        for control in retro.CONTROLS:
            validation["cases"].append({"control": control, "expected": "Observe the behavior required by this scenario.",
                                        "baseline_observed": "Baseline action recorded in an isolated scenario.",
                                        "candidate_observed": "Candidate action recorded in an isolated scenario.",
                                        "baseline_passed": control != "relevant-improvement", "candidate_passed": True})
        review = {"reviewer_id": "reviewer-2", "analyst_id": "analyst-1", "candidate_sha256": manifest["candidate_sha256"],
                  "verdict": "accept", "reason": "Independent observations support this limited improvement.",
                  "boundaries_preserved": {key: True for key in retro.BOUNDARIES}, "independent": True}
        return validation, review

    def apply(self, manifest, validation=None, review=None):
        default_validation, default_review = self.artifacts(manifest)
        return self.store.apply(manifest["id"], self.write("validation.json", validation if validation is not None else default_validation),
                                self.write("review.json", review if review is not None else default_review), day="2026-10-15")

    def test_canonical_actor_paths_apply_and_confirm(self):
        self.analysis["analyst_id"] = "/root/analyst"
        manifest = self.prepare()
        validation, review = self.artifacts(manifest)
        validation["runner_id"] = "/root/behavior_runner"
        review["analyst_id"] = "/root/analyst"
        review["reviewer_id"] = "/root/independent_reviewer"
        self.assertEqual(self.apply(manifest, validation, review)["status"], "applied-pending-verification")
        post = {"candidate_sha256": manifest["candidate_sha256"], "runner_id": "/root/installed_smoke", "passed": True,
                "reason": "Installed smoke preserved observed behavior and control boundaries."}
        self.assertEqual(self.store.verify_applied(manifest["id"], self.write("post.json", post))["status"], "applied")
        for actor in ["/root", "/root/task/nested_task", "01a14069-2960-7381-b396-fe46175555aa"]:
            self.assertTrue(retro.valid_actor_id(actor))

    def test_canonical_actor_paths_do_not_relax_filesystem_ids(self):
        for actor in ["/root/../other", "/root//task", "/root/task/", "../../other", "/other/task", None, {"actor": "task"}]:
            self.assertFalse(retro.valid_actor_id(actor))
        for candidate in ["/root/analyst", "../analyst", "abc/../../outside"]:
            with self.assertRaises(retro.Rejected):
                self.store.candidate_dir(candidate)
        with self.assertRaises(retro.Rejected):
            self.store.target("/root/analyst")

    def test_canonical_reviewer_cannot_be_analyst_or_behavior_runner(self):
        self.analysis["analyst_id"] = "/root/analyst"
        manifest = self.prepare()
        validation, review = self.artifacts(manifest)
        validation["runner_id"] = "/root/behavior_runner"
        review["analyst_id"] = "/root/analyst"
        for reviewer in ["/root/analyst", "/root/behavior_runner"]:
            review["reviewer_id"] = reviewer
            with self.assertRaises(retro.Rejected):
                self.apply(manifest, validation, review)
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_apply_snapshot_and_rollback_roundtrip_preserves_unrelated_bytes(self):
        unrelated = self.skills / "dialogue-validate/SKILL.md"
        before = unrelated.read_bytes()
        manifest = self.prepare()
        self.assertEqual(self.apply(manifest)["status"], "applied-pending-verification")
        self.assertEqual(self.target.read_bytes(), self.candidate.read_bytes())
        self.assertEqual(self.store.rollback(manifest["id"])["status"], "rolled-back")
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(unrelated.read_bytes(), before)
        self.assertEqual((self.store.state.stat().st_mode & 0o777), 0o700)

    def test_forbidden_targets_and_traversal(self):
        for target in ["session-retrospective/SKILL.md", "model-router/scripts/run.py", "../AGENTS.md", "/tmp/a.md", "dialogue-explore/references/../../AGENTS.md", "model-router/agents/profile.md", "dialogue-explore//SKILL.md"]:
            with self.subTest(target=target), self.assertRaises(retro.Rejected):
                self.store.target(target)
        with self.assertRaises(retro.Rejected):
            self.store.internal("versions/../../outside")

    def test_target_and_parent_symlinks_rejected(self):
        self.target.unlink()
        self.target.symlink_to(self.candidate)
        with self.assertRaises(retro.Rejected):
            self.store.snapshot(self.target_name)
        references = self.skills / "dialogue-validate/references"
        references.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(retro.Rejected):
            self.store.target("dialogue-validate/references/candidate.md")

    def test_state_symlink_rejected(self):
        self.store.internal("candidates").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(retro.Rejected):
            self.prepare()

    def test_current_correction_can_replace_repeated_evidence(self):
        self.analysis["evidence"] = [self.evidence("current", "current-turn", "current-user-correction")]
        self.assertEqual(self.prepare()["status"], "prepared")

    def test_duplicate_or_single_evidence_rejected(self):
        for evidence in [[self.evidence("same", "same")], [self.evidence("same", "same")] * 2]:
            self.analysis["evidence"] = evidence
            with self.assertRaises(retro.Rejected):
                self.prepare()

    def test_secret_and_unexpected_raw_fields_rejected(self):
        self.analysis["raw_prompt"] = "password=do-not-copy-this"
        with self.assertRaises(retro.Rejected):
            self.prepare()
        self.analysis.pop("raw_prompt")
        self.analysis["reason"] = "password=do-not-copy-this"
        with self.assertRaises(retro.Rejected):
            self.prepare()
        for path in self.store.state.rglob("*.json"):
            self.assertNotIn("do-not-copy-this", path.read_text())

    def test_declared_version_must_increment(self):
        self.candidate.write_bytes(self.original + b'Useful small addition.\n')
        with self.assertRaises(retro.Rejected):
            self.prepare()

    def test_no_change_or_large_change_rejected(self):
        for content in [self.original, self.original.replace(b'0.1.0', b'0.1.1') + b'line\n' * 100]:
            self.candidate.write_bytes(content)
            with self.assertRaises(retro.Rejected):
                self.prepare()

    def test_missing_review_evidence_and_failed_controls_rejected(self):
        manifest = self.prepare()
        valid, review = self.artifacts(manifest)
        variants = []
        broken = dict(valid, syntax_passed=False)
        variants.append((broken, review))
        variants.append((dict(valid, cases=[]), review))
        variants.append((valid, dict(review, verdict="reject")))
        variants.append((valid, dict(review, reviewer_id="analyst-1")))
        variants.append((valid, dict(review, reviewer_id="runner-1")))
        variants.append((valid, dict(review, independent=False)))
        variants.append((dict(valid, candidate_sha256="mismatch"), review))
        for validation, report in variants:
            with self.subTest(validation=validation, report=report), self.assertRaises(retro.Rejected):
                self.apply(manifest, validation, report)
        valid["cases"][0]["candidate_passed"] = False
        with self.assertRaises(retro.Rejected):
            self.apply(manifest, valid, review)
        with self.assertRaises(retro.Rejected):
            self.store.apply(manifest["id"], self.root / "absent.json", self.root / "missing.json")
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_syntax_only_without_observed_gain_rejected(self):
        manifest = self.prepare()
        validation, review = self.artifacts(manifest)
        for case in validation["cases"]:
            case["baseline_passed"] = True
        with self.assertRaises(retro.Rejected):
            self.apply(manifest, validation, review)

    def test_concurrent_edit_blocks_apply(self):
        manifest = self.prepare()
        self.target.write_bytes(b'User changed the file.\n')
        with self.assertRaises(retro.Rejected):
            self.apply(manifest)
        self.assertEqual(self.target.read_bytes(), b'User changed the file.\n')

    def test_rollback_preserves_later_edit(self):
        manifest = self.prepare()
        self.apply(manifest)
        self.target.write_bytes(b'New user edit.\n')
        with self.assertRaises(retro.Rejected):
            self.store.rollback(manifest["id"])
        self.assertEqual(self.target.read_bytes(), b'New user edit.\n')

    def test_post_apply_failure_auto_restores_original(self):
        manifest = self.prepare()
        with patch.object(self.store, "post_apply", side_effect=RuntimeError("simulated validation failure")):
            with self.assertRaises(RuntimeError):
                self.apply(manifest)
        self.assertEqual(self.target.read_bytes(), self.original)
        saved = retro.read_json(self.store.candidate_dir(manifest["id"]) / "manifest.json")
        self.assertEqual(saved["status"], "rolled-back")

    def test_failed_replacement_recovers_without_touching_file(self):
        manifest = self.prepare()
        original_atomic = retro.atomic
        def failing(path, data, mode=0o600):
            if path.resolve() == self.target.resolve():
                raise OSError("simulated disk failure")
            return original_atomic(path, data, mode)
        with patch.object(retro, "atomic", side_effect=failing):
            with self.assertRaises(OSError):
                self.apply(manifest)
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_successful_post_apply_confirmation(self):
        manifest = self.prepare()
        self.apply(manifest)
        result = {"candidate_sha256": manifest["candidate_sha256"], "runner_id": "post-runner", "passed": True,
                  "reason": "Live installed instruction smoke preserved controls and target behavior."}
        saved = self.store.verify_applied(manifest["id"], self.write("post.json", result))
        self.assertEqual(saved["status"], "applied")
        self.assertEqual(self.store.recover(), [])

    def test_failed_or_missing_post_apply_confirmation_rolls_back(self):
        manifest = self.prepare()
        self.apply(manifest)
        result = {"candidate_sha256": manifest["candidate_sha256"], "runner_id": "post-runner", "passed": False,
                  "reason": "Installed instruction smoke regressed one required control."}
        with self.assertRaises(retro.Rejected):
            self.store.verify_applied(manifest["id"], self.write("post.json", result))
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_unconfirmed_apply_recovers_on_next_invocation(self):
        manifest = self.prepare()
        self.apply(manifest)
        self.store.recover()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_interrupt_journal_recovers_on_next_run(self):
        manifest = self.prepare()
        manifest["status"] = "applying"
        retro.save(self.store.candidate_dir(manifest["id"]) / "manifest.json", manifest)
        self.target.write_bytes(self.candidate.read_bytes())
        self.assertEqual(self.store.recover(), [manifest["id"]])
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_one_attempt_per_day_even_after_rollback(self):
        first = self.prepare()
        self.apply(first)
        self.store.rollback(first["id"])
        second = self.prepare()
        with self.assertRaises(retro.Rejected):
            self.apply(second)

    def source(self, session="11111111-1111-1111-1111-111111111111", own=False, complete=True):
        path = self.root / "sessions/2026/10/15" / ("rollout-" + session + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = [
            {"timestamp": "2026-10-15T00:00:00Z", "type": "session_meta", "payload": {"id": session}},
            {"type": "turn_context", "payload": {"turn_id": "turn-1"}},
            {"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": ("[session-retrospective-run] " if own else "") + "Обсудим план навыка. password=do-not-store-me"}]}},
        ]
        content.append({"type": "token_usage_record", "payload": {"thread_id": session, "turn_id": "turn-1", "response_id": "response-1"}})
        if complete:
            content.append({"type": "event_msg", "payload": {"type": "task_complete"}})
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in content))
        return path

    def scan(self, **kwargs):
        return self.store.scan(self.root / "sessions", {"current-automation-thread"}, now=dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc), **kwargs)

    def test_metadata_baseline_and_incremental_scan_never_copy_source_text(self):
        path = self.source()
        first = self.scan(baseline=True)
        self.assertEqual(first["episodes"], [])
        self.assertEqual(first["bytes_scanned"], 0)
        self.store.acknowledge(first["id"], "no-change")
        with path.open("a") as stream:
            for event in [{"type": "turn_context", "payload": {"turn_id": "turn-2"}}, {"type": "response_item", "payload": {"role": "user", "content": [{"text": "Обсудим план password=do-not-store-me"}]}}, {"type": "token_usage_record", "payload": {"thread_id": "11111111-1111-1111-1111-111111111111", "turn_id": "turn-2", "response_id": "response-2"}}, {"type": "event_msg", "payload": {"type": "task_complete"}}]:
                stream.write(json.dumps(event) + "\n")
        second = self.scan()
        self.assertEqual(len(second["episodes"]), 1)
        self.assertEqual(second["episodes"][0]["turn_id"], "turn-2")
        for output in self.store.state.rglob("*.json"):
            self.assertNotIn("do-not-store-me", output.read_text())

    def test_duplicate_scan_reuses_pending_then_ack_prevents_repeat(self):
        self.source()
        scan = self.scan()
        self.assertEqual(len(scan["episodes"]), 1)
        self.assertEqual(self.scan()["id"], scan["id"])
        self.store.acknowledge(scan["id"], "no-change")
        self.assertEqual(self.scan()["episodes"], [])

    def test_own_workflow_and_explicit_threads_are_excluded(self):
        self.source(own=True)
        self.assertEqual(self.scan()["episodes"], [])

    def test_active_incomplete_turn_is_deferred(self):
        path = self.source(complete=False)
        scan = self.scan()
        self.assertEqual(scan["episodes"], [])
        self.store.acknowledge(scan["id"], "no-change")
        with path.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
        self.assertEqual(len(self.scan()["episodes"]), 1)

    def test_continuation_filename_uses_first_metadata_and_ignores_inherited_meta(self):
        logical = "11111111-1111-1111-1111-111111111111"
        inherited = "22222222-2222-2222-2222-222222222222"
        continuation = "33333333-3333-3333-3333-333333333333"
        path = self.source(session=logical)
        lines = path.read_text().splitlines(keepends=True)
        lines.insert(1, json.dumps({"type": "session_meta", "payload": {"id": inherited}}) + "\n")
        path.write_text("".join(lines))
        path.rename(path.with_name("rollout-" + logical + "_" + continuation + ".jsonl"))
        manifest = self.scan()
        self.assertEqual(manifest["episodes"][0]["session_id"], logical)
        self.assertEqual(len(manifest["episodes"]), 1)

    def test_continuation_own_session_cannot_evade_exclusion(self):
        logical = "11111111-1111-1111-1111-111111111111"
        path = self.source(session=logical)
        path.rename(path.with_name("rollout-" + logical + "_33333333-3333-3333-3333-333333333333.jsonl"))
        result = self.store.scan(self.root / "sessions", {logical}, now=dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc))
        self.assertEqual(result["episodes"], [])
        self.assertEqual(result["bytes_scanned"], 0)

    def test_per_turn_token_identity_overrides_physical_file_owner(self):
        path = self.source()
        lines = path.read_text().splitlines(keepends=True)
        lines = [line for line in lines if json.loads(line)["type"] != "token_usage_record"]
        lines.insert(-1, json.dumps({"type": "token_usage_record", "payload": {"thread_id": "logical-thread", "turn_id": "turn-1", "response_id": "response-1"}}) + "\n")
        path.write_text("".join(lines))
        manifest = self.scan()
        self.assertEqual(manifest["episodes"][0]["session_id"], "logical-thread")

    def test_per_turn_own_thread_exclusion(self):
        path = self.source()
        lines = path.read_text().splitlines(keepends=True)
        lines.insert(-1, json.dumps({"type": "token_usage_record", "payload": {"thread_id": "current-automation-thread", "turn_id": "turn-1"}}) + "\n")
        path.write_text("".join(lines))
        self.assertEqual(self.scan()["episodes"], [])

    def test_child_metadata_parent_exclusion(self):
        path = self.source()
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events[0]["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "current-automation-thread"}}}
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(self.scan()["episodes"], [])

    def test_missing_or_ambiguous_runtime_owner_is_unknown(self):
        path = self.source()
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events = [event for event in events if event["type"] != "token_usage_record"]
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        result = self.scan()
        self.assertEqual(result["episodes"], [])
        self.assertEqual(result["unknown_identity"], 1)

    def test_inverted_metadata_order_uses_runtime_turn_identity(self):
        path = self.source()
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events[0]["payload"]["id"] = "inherited-parent"
        events.insert(1, {"type": "session_meta", "payload": {"id": "actual-child"}})
        for event in events:
            if event["type"] == "token_usage_record":
                event["payload"]["thread_id"] = "actual-child"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(self.scan()["episodes"][0]["session_id"], "actual-child")

    def test_any_inherited_workflow_metadata_causes_conservative_exclusion(self):
        path = self.source()
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events.insert(1, {"type": "session_meta", "payload": {"id": "current-automation-thread"}})
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(self.scan()["episodes"], [])

    def test_continuation_copies_deduplicate_same_logical_turn(self):
        path = self.source()
        copy = path.with_name(path.stem + "_33333333-3333-3333-3333-333333333333.jsonl")
        copy.write_bytes(path.read_bytes())
        self.assertEqual(len(self.scan()["episodes"]), 1)

    def test_acknowledge_cannot_claim_unverified_application(self):
        self.source()
        scan = self.scan()
        with self.assertRaises(retro.Rejected):
            self.store.acknowledge(scan["id"], "applied")
        manifest = self.prepare()
        with self.assertRaises(retro.Rejected):
            self.store.acknowledge(scan["id"], "applied", manifest["id"])

    def test_scan_requires_exclusions(self):
        with self.assertRaises(retro.Rejected):
            self.store.scan(self.root / "sessions", set())


if __name__ == "__main__":
    unittest.main()
