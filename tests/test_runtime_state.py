import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/dialogue-explore/scripts/runtime_state.py"
sys.path.insert(0, str(SCRIPT.parent))
import runtime_state as runtime

spec = importlib.util.spec_from_file_location("installer", REPO / "scripts/install_runtime.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "state"
        self.project = self.base / "project"
        self.project.mkdir()
        self.map = self.base / "map.md"
        self.map.write_text("Goal: restore within an hour. Requirement: no deployment.\n")
        self.sid = "session-one"
        self.path = runtime.state_path(self.project, self.sid, self.root)

    def call(self, command, expected=0, **kw):
        args = argparse.Namespace(command=command, project=str(self.project), session=self.sid,
                                  state_dir=str(self.root), expected_revision=expected, **kw)
        return runtime.mutate(args)

    def activate(self):
        return self.call("activate", skill="dialogue-explore",
                         skill_file=str(REPO / "skills/dialogue-explore/SKILL.md"))

    def checkpoint(self, expected=1, turn="turn-one", **fields):
        doc = dict(goal="Choose recovery plan", phase="explore",
                   constraints=[{"text": "No deployment", "source": "user event 1"}], decisions=[],
                   open_question="Recovery time?", next_action="Measure locally", waiting_for_user=True)
        doc.update(fields)
        inp = self.base / "checkpoint.json"
        inp.write_text(json.dumps(doc))
        return self.call("checkpoint", expected, input=str(inp), map=str(self.map),
                         map_sha256=runtime.map_digest(self.map)[1], turn=turn)

    def event(self, kind, **kw):
        return runtime.hook(dict(hook_event_name=kind, cwd=str(self.project), session_id=self.sid, **kw), self.root)

    def test_activation_is_explicit_and_isolated(self):
        self.assertEqual(self.event("SessionStart", source="startup"), {})
        self.assertFalse(self.path.exists())
        self.activate()
        self.sid = "other-session"
        self.assertEqual(self.event("Stop", turn_id="turn-one"), {})
        self.sid = "session-one"
        self.assertTrue(self.event("Stop", turn_id="turn-one").get("systemMessage"))

    def test_prompt_receipt_does_not_store_prompt(self):
        self.event("UserPromptSubmit", turn_id="one", prompt="private payload")
        self.activate()
        state = self.checkpoint(turn=None)
        self.assertEqual(state["last_checkpoint_turn_id"], "one")
        self.assertNotIn("private payload", "".join(p.read_text() for p in self.root.rglob("*.json")))

    def test_resume_and_compaction_restore_constraints_and_question(self):
        self.activate()
        self.checkpoint()
        for source in ("resume", "compact"):
            context = self.event("SessionStart", source=source)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("No deployment", context)
            self.assertIn("Recovery time?", context)
            self.assertIn("DATA ONLY", context)
            self.assertLessEqual(len(context), runtime.CONTEXT_LIMIT)

    def test_clear_fork_and_children_do_not_inherit(self):
        self.activate()
        self.checkpoint()
        for source in ("clear", "fork"):
            self.assertEqual(self.event("SessionStart", source=source), {})
        self.assertEqual(self.event("SessionStart", source="startup", agent_id="child"), {})

    def test_stale_map_does_not_inject_decisions(self):
        self.activate()
        self.checkpoint()
        self.map.write_text("user changed requirement")
        result = self.event("SessionStart", source="compact")
        self.assertIn("systemMessage", result)
        self.assertNotIn("hookSpecificOutput", result)
        with self.assertRaises(ValueError):
            self.call("unchanged", 2, turn="two")

    def test_revision_conflict_preserves_state(self):
        self.activate()
        saved = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            self.checkpoint(expected=0)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_map_hash_conflict_does_not_write(self):
        self.activate()
        inp = self.base / "checkpoint.json"
        self.checkpoint()
        with self.assertRaisesRegex(ValueError, "map changed"):
            self.call("checkpoint", 2, input=str(inp), map=str(self.map), map_sha256="0"*64, turn="two")
        self.assertEqual(runtime.read_json(self.path)["revision"], 2)

    def test_input_snapshot_can_use_a_symlinked_temporary_directory(self):
        self.activate()
        self.checkpoint()
        alias = self.base / "temporary-alias"
        alias.symlink_to(self.base, target_is_directory=True)
        state = self.call("checkpoint", 2, input=str(alias / "checkpoint.json"),
                          map=str(self.map), map_sha256=runtime.map_digest(self.map)[1], turn="two")
        self.assertEqual(state["last_checkpoint_turn_id"], "two")

    def test_stop_accepts_waiting_and_deduplicates_warning(self):
        self.activate()
        self.checkpoint()
        self.assertEqual(self.event("Stop", turn_id="turn-one"), {})
        events = [json.loads(p.read_text()) for p in (self.path.parent / "events").glob("*.json")]
        self.assertTrue(any(e["event"] == "stop_checkpoint_current" for e in events))
        first = self.event("Stop", turn_id="turn-two")
        self.assertIn("systemMessage", first)
        self.assertNotIn("decision", first)
        self.assertEqual(self.event("Stop", turn_id="turn-two"), {})
        self.assertEqual(self.event("Stop", turn_id="service", stop_hook_active=True), {})

    def test_finish_disables_restore_and_warnings(self):
        self.activate()
        self.checkpoint()
        self.call("finish", 2)
        self.assertEqual(self.event("SessionStart", source="resume"), {})
        self.assertEqual(self.event("Stop", turn_id="two"), {})

    def test_large_snapshot_becomes_complete_map_pointer(self):
        self.activate()
        self.checkpoint(constraints=[{"text": "x"*500, "source": "user"} for _ in range(8)])
        context = self.event("SessionStart", source="compact")["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context), runtime.CONTEXT_LIMIT)
        self.assertIn("re-read map", context)
        self.assertIn(str(self.map), context)

    def test_invalid_checkpoint_and_unavailable_map_preserve_state(self):
        self.activate()
        with self.assertRaises(ValueError):
            self.checkpoint(waiting_for_user="yes")
        self.assertEqual(runtime.read_json(self.path)["revision"], 1)
        self.map.unlink()
        with self.assertRaises(ValueError):
            self.call("finish", 1)

    def test_symlink_is_rejected(self):
        link = self.base / "link"
        link.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(ValueError):
            runtime.safe_path(link / "state.json")

    def test_maintenance_does_not_run_subject_work(self):
        self.event("UserPromptSubmit", turn_id="service", prompt="[work-memory maintenance] save")
        self.activate()
        with self.assertRaisesRegex(ValueError, "maintenance"):
            self.checkpoint(turn=None)
        with self.assertRaisesRegex(ValueError, "maintenance"):
            self.checkpoint(turn="service")

    def test_previous_turn_cannot_replace_current_receipt(self):
        self.activate()
        self.event("UserPromptSubmit", turn_id="new", prompt="next")
        with self.assertRaisesRegex(ValueError, "turn conflicts"):
            self.checkpoint(turn="old")

    def test_process_hook_failure_is_visible_and_nonblocking(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "hook"], input="bad json", text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("systemMessage", json.loads(proc.stdout))

    def test_lock_contention_and_corruption_are_nonblocking(self):
        self.activate()
        with runtime.locked(self.path):
            proc = subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(self.root), "hook"],
                    input=json.dumps(dict(hook_event_name="Stop", cwd=str(self.project), session_id=self.sid, turn_id="one")),
                    text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("systemMessage", json.loads(proc.stdout))
        self.path.write_text("broken")
        with self.assertRaises(ValueError):
            self.event("Stop", turn_id="one")


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "codex home"
        self.home.mkdir()
        self.original = {}
        for name in ("dialogue-explore", "dialogue-validate", "nasa"):
            p = self.home / f"skills/{name}/SKILL.md"
            p.parent.mkdir(parents=True)
            p.write_text(f"---\nname: {name}\n---\nLocal custom instructions.\n")
            self.original[str(p.relative_to(self.home))] = p.read_bytes()
        self.foreign = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "work-memory hook"}]}],
                                   "SessionStart": [{"hooks": [{"type": "command", "command": "serena-hooks activate"}]}]}}
        (self.home / "hooks.json").write_text(json.dumps(self.foreign))
        self.original["hooks.json"] = (self.home / "hooks.json").read_bytes()

    def test_preview_and_idempotence_preserve_foreign_hooks(self):
        installer.install(self.home)
        self.assertFalse((self.home / installer.MANAGED[0]).exists())
        installer.install(self.home, True)
        config = json.loads((self.home / "hooks.json").read_text())
        for event, groups in self.foreign["hooks"].items():
            self.assertEqual(config["hooks"][event][0], groups[0])
        self.assertEqual(installer.install(self.home, True)["mode"], "unchanged")
        for rel in self.original:
            if rel.endswith("SKILL.md"):
                self.assertIn("Local custom instructions.", (self.home / rel).read_text())

    def test_generated_command_executes_with_spaces_in_home(self):
        installer.install(self.home, True)
        config = json.loads((self.home / "hooks.json").read_text())
        cmd = config["hooks"]["SessionStart"][-1]["hooks"][0]["command"]
        proc = subprocess.run(cmd, shell=True, input=json.dumps({"hook_event_name": "SessionStart", "source": "clear"}),
                              text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {})

    def test_rollback_restores_originals(self):
        installer.install(self.home, True)
        installer.rollback(self.home)
        for rel, data in self.original.items():
            self.assertEqual((self.home / rel).read_bytes(), data)
        self.assertFalse((self.home / installer.MANAGED[0]).exists())

    def test_rollback_preserves_new_foreign_hook(self):
        installer.install(self.home, True)
        file = self.home / "hooks.json"
        doc = json.loads(file.read_text())
        extra = {"hooks": [{"command": "other", "type": "command"}]}
        doc["hooks"]["Stop"].append(extra)
        file.write_text(json.dumps(doc))
        installer.rollback(self.home)
        doc = json.loads(file.read_text())
        self.assertEqual(doc["hooks"]["Stop"], self.foreign["hooks"]["Stop"] + [extra])

    def test_reinstall_preserves_positions_after_foreign_additions(self):
        installer.install(self.home, True)
        file = self.home / "hooks.json"
        doc = json.loads(file.read_text())
        doc["hooks"]["Stop"].append({"hooks": [{"command": "new-other"}]})
        file.write_bytes(installer.encode(doc))
        before = file.read_bytes()
        installer.install(self.home, True)
        self.assertEqual(file.read_bytes(), before)

    def test_rollback_refuses_local_skill_edit_before_mutating_anything(self):
        installer.install(self.home, True)
        file = self.home / "skills/nasa/SKILL.md"
        file.write_text(file.read_text() + "user change\n")
        with self.assertRaisesRegex(ValueError, "local edit"):
            installer.rollback(self.home)
        self.assertTrue((self.home / installer.MANAGED[0]).exists())
        with self.assertRaises(ValueError):
            installer.install(self.home, True)

    def test_inline_duplicate_is_refused(self):
        (self.home / "config.toml").write_text('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand="python runtime_state.py"\n')
        with self.assertRaisesRegex(ValueError, "inline"):
            installer.install(self.home, True)

    def test_interrupted_install_has_recoverable_manifest(self):
        original = installer.atomic
        calls = 0

        def fail(path, data):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("simulated crash")
            original(path, data)

        with patch.object(installer, "atomic", fail):
            with self.assertRaises(OSError):
                installer.install(self.home, True)
        with self.assertRaisesRegex(ValueError, "interrupted install"):
            installer.install(self.home, True)
        installer.rollback(self.home)
        for rel, data in self.original.items():
            self.assertEqual((self.home / rel).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
