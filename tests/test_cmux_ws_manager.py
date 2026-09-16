"""Compatibility checks using disposable history and a fake cmux CLI.

Run with: python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


CLI = Path(__file__).resolve().parents[1] / "cmux-ws-manager"
FAKE_CMUX = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ["HOME"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps(args) + "\\n")
config = json.loads((root / "fake.json").read_text())
command = args[0]
if command == "capabilities":
    print(json.dumps({"methods": config.get("methods", [])}))
elif command == "list-windows":
    print(json.dumps(config.get("windows", [])))
elif command in ("workspace", "list-workspaces"):
    window = args[args.index("--window") + 1] if "--window" in args else "current"
    if window in config.get("fail_windows", []):
        sys.exit(1)
    print(json.dumps({"workspaces": config.get("workspaces", {}).get(window, [])}))
elif command == "surface":
    sys.exit(config.get("binding_exit", 0))
elif command == "restore":
    sys.exit(config.get("restore_exit", 0))
elif command == "new-workspace":
    print("OK workspace:99")
else:
    sys.exit(2)
'''


def snapshot(wsid="closed", cwd="/tmp/project", title="Project", agent=None):
    terminal = {"workingDirectory": cwd}
    if agent:
        terminal["agent"] = {"kind": agent, "sessionId": "session-123"}
    return {"workspaceId": wsid, "currentDirectory": cwd, "customTitle": title,
            "panels": [{"id": "terminal", "customTitle": "Agent pane",
                        "terminal": terminal}],
            "layout": {"type": "pane", "pane": {"panelIds": ["terminal"]}}}


def closed(snap, at=800000000):
    return {"closedAt": at, "entry": {"workspace": {"_0": {
        "workspaceId": snap.get("workspaceId", ""), "snapshot": snap}}}}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cmux-ws-manager-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        fake = self.bin / "cmux"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_CMUX)
        fake.chmod(0o755)
        self.environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "CMUX_WORKSPACE_ID": "test-workspace", "CMUX_SURFACE_ID": "test-surface"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.configure()
        loader = importlib.machinery.SourceFileLoader("manager_test", str(CLI))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.manager = importlib.util.module_from_spec(spec)
        loader.exec_module(self.manager)

    def configure(self, **values):
        defaults = {"methods": ["surface.resume.set", "surface.resume.get"]}
        (self.home / "fake.json").write_text(json.dumps(dict(defaults, **values)))

    def native(self, records, legacy=False):
        path = Path(self.manager.NATIVE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records if legacy else {"version": 1, "records": records}))
        return path

    def events(self, records):
        path = Path(self.manager.ROTATING[-1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return path

    def calls(self):
        path = self.home / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CLI)] + list(args),
                              capture_output=True, text=True, timeout=10)

    def test_help_never_reads_history_or_invokes_cmux(self):
        self.events([{"category": "workspace", "id": "event", "workspace_id": "ws",
                      "name": "workspace.closed"}])
        for flag in ("-h", "--help"):
            result = self.run_cli(flag)
            self.assertEqual(result.returncode, 0)
            self.assertIn("usage:", result.stdout)
            self.assertEqual(result.stderr, "")
        self.assertEqual(self.calls(), [])
        self.assertFalse(Path(self.manager.HISTORY).exists())

    def test_unknown_option_is_a_usage_error(self):
        result = self.run_cli("--unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--help", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_spaced_events_are_harvested_once_and_created_removes_close(self):
        event = {"category": "workspace", "workspace_id": "event-only",
                 "payload": {"cwd": "/tmp/event-only", "custom_title": "From event"}}
        records = [dict(event, id="close", name="workspace.closed", occurred_at="2026-09-01T10:00:00Z")]
        source = self.events(records)
        original = source.read_bytes()
        self.assertEqual(self.manager.entries()[0]["title"], "From event")
        self.manager.entries()
        self.assertEqual(len(Path(self.manager.HISTORY).read_text().splitlines()), 1)
        self.assertEqual(source.read_bytes(), original)
        self.events(records + [dict(event, id="create", name="workspace.created",
                                   occurred_at="2026-09-01T11:00:00Z")])
        self.assertEqual(self.manager.entries(), [])

    def test_native_preferred_and_malformed_records_are_skipped(self):
        snap = snapshot()
        path = self.native([None, [], {"entry": []}, {"closedAt": "bad"},
                            {"closedAt": 10 ** 400}, {"entry": {"workspace": []}},
                            closed(snap)])
        original = path.read_bytes()
        self.events([None, [], {"category": "workspace", "id": [], "workspace_id": "x"},
                     {"category": "workspace", "id": "event", "workspace_id": "closed",
                      "name": "workspace.closed", "payload": [], "seq": {}, "occurred_at": {}}])
        items = self.manager.entries()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["snapshot"], snap)
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_array_and_closed_window_workspaces(self):
        snaps = [snapshot("first"), snapshot("second", "/tmp/second")]
        record = {"closedAt": 800000000, "entry": {"window": {"_0": {
            "workspaceIds": ["outdated-first", "outdated-second"],
            "snapshot": {"tabManager": {"workspaces": snaps + [None]}}}}}}
        self.native([record], legacy=True)
        self.assertEqual([it["id"] for it in self.manager.native_closed()], ["first", "second"])

    def test_open_windows_are_independent_and_titles_distinguish_workspaces(self):
        self.configure(windows=[{"id": "broken"}, {"id": "working"}],
                       fail_windows=["broken"], workspaces={"working": [None, {
                           "id": "open", "current_directory": "/tmp/project/.",
                           "custom_title": "Project", "title": "Project"}]})
        self.native([closed(snapshot()), closed(snapshot("other", title="Other")),
                     closed(snapshot("untitled", title=""))])
        items = self.manager.entries()
        self.assertEqual([it["title"] for it in items], ["Other"])
        self.assertTrue(items[0]["dir_open"])
        self.assertEqual(len(self.manager.entries(show_all=True)), 3)

    def test_json_window_envelope_refs_and_current_window_fallback(self):
        for windows in ({"windows": [{"ref": "window:1"}]}, []):
            with self.subTest(windows=windows):
                ws = {"id": "open", "current_directory": "/tmp/project", "title": "Project"}
                self.configure(windows=windows, workspaces={"window:1": [ws], "current": [ws]})
                ids, paths, titles = self.manager.open_workspaces()
                self.assertEqual(ids, {"open"})
                self.assertEqual(paths, {"/tmp/project"})
                self.assertEqual(titles["/tmp/project"], {"project"})

    def test_duplicate_closes_and_agent_graft_stay_with_same_title(self):
        donor = snapshot(agent="claude")
        self.native([closed(donor), closed(snapshot("new-id"), 800000100),
                     closed(snapshot("neighbor", title="Other"), 800000101)])
        items = self.manager.entries()
        self.assertEqual(len(items), 2)
        self.assertNotIn("grafted", items[0])
        self.assertTrue(items[1]["grafted"])
        self.assertEqual(self.manager.restore_counts(items[1]["snapshot"]), (0, 1, 0))
        self.assertTrue(all("grafted" not in it for it in self.manager.entries(True)))

    def test_split_layout_browser_names_and_cwd(self):
        snap = snapshot(cwd="/tmp/path with 'quotes'", agent="claude")
        snap["panels"].append({"id": "web", "customTitle": "Docs", "browser": {"urlString": "https://example.com"}})
        snap["layout"] = {"type": "split", "split": {"orientation": "vertical", "dividerPosition": 0.3,
            "first": snap["layout"], "second": {"type": "pane", "pane": {"panelIds": ["web"]}}}}
        layout = self.manager.build_layout(snap)
        terminal = layout["children"][0]["pane"]["surfaces"][0]
        browser = layout["children"][1]["pane"]["surfaces"][0]
        self.assertEqual(layout["split"], 0.3)
        self.assertEqual(terminal["cwd"], snap["currentDirectory"])
        self.assertEqual(terminal["name"], "Agent pane")
        self.assertEqual(browser, {"type": "browser", "name": "Docs", "url": "https://example.com"})
        self.assertIn("cd " + shlex.quote(snap["currentDirectory"]), terminal["command"])

    def test_agent_binding_is_registered_and_never_retried_after_exit(self):
        unexpected = self.home / "unexpected-provider-launch"
        for provider in ("claude", "codex", "opencode"):
            executable = self.bin / provider
            executable.write_text("#!/bin/sh\nprintf called >> " + shlex.quote(str(unexpected)) + "\nexit 99\n")
            executable.chmod(0o755)
            for bind_exit, restore_exit in ((0, 0), (0, 7), (1, 0)):
                with self.subTest(provider=provider, binding=bind_exit, restore=restore_exit):
                    self.configure(binding_exit=bind_exit, restore_exit=restore_exit)
                    command = self.manager.resume_command_for({"resumeBinding": {
                        "kind": provider, "checkpointId": "session ' quoted; $(false)",
                        "command": "touch /should-never-run"}})
                    before = len(self.calls())
                    result = subprocess.run(["/bin/sh", "-c", command + "\nprintf shell-survived"],
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.stdout, "shell-survived")
                    calls = self.calls()[before:]
                    self.assertEqual(calls[0][:3], ["surface", "resume", "set"])
                    self.assertEqual(len(calls), 2 if bind_exit == 0 else 1)
                    self.assertIn("session ' quoted; $(false)", calls[0])
                    self.assertNotIn("should-never-run", command)
                    self.assertFalse(unexpected.exists(), "provider was retried outside cmux restore")

    def test_missing_provider_in_legacy_mode_leaves_shell_usable(self):
        command = self.manager.resume_command_for({"agent": {
            "kind": "opencode", "sessionId": "missing"}}, bind_sessions=False)
        result = subprocess.run(["/bin/sh", "-c", command + "\nprintf shell-survived"],
                                env=dict(os.environ, PATH=str(self.bin)),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stdout, "shell-survived")

    def test_remote_binding_without_workspace_metadata_is_not_run_locally(self):
        snap = snapshot()
        term = {"resumeBinding": {"kind": "codex", "checkpointId": "remote-session",
                                   "launchFlavor": {"kind": "persistentSSH"}}}
        snap["panels"][0]["terminal"] = term
        self.assertIsNone(self.manager.resume_command_for(term))
        self.assertTrue(self.manager.remote_snapshot(snap))

    def test_supported_binding_wins_over_stale_agent_and_managed_binding_works(self):
        term = {"resumeBinding": {"kind": "codex", "checkpointId": "current"},
                "agent": {"kind": "claude", "sessionId": "stale"}}
        command = self.manager.resume_command_for(term)
        self.assertIn("current", command)
        self.assertNotIn("stale", command)
        term = {"managedAgentResumeBinding": {"kind": "opencode", "checkpointId": "managed"}}
        self.assertIn("managed", self.manager.resume_command_for(term))
        self.assertIsNone(self.manager.resume_command_for({"agent": {"kind": "codex", "sessionId": "--help"}}))

    def test_cli_reopen_and_plain_use_default_numbering(self):
        self.native([closed(snapshot("older", "/tmp/old", agent="claude")),
                     closed(snapshot("newer", "/tmp/new", agent="codex"), 800000100)])
        for flags in ([], ["--plain"]):
            with self.subTest(flags=flags):
                result = self.run_cli("--all", "reopen", "1", *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                cmd = [c for c in self.calls() if c[0] == "new-workspace"][-1]
                self.assertEqual(cmd[cmd.index("--cwd") + 1], "/tmp/new")
                self.assertEqual("--layout" in cmd, not flags)
                if not flags:
                    self.assertIn("surface resume set", cmd[cmd.index("--layout") + 1])

    def test_older_cmux_uses_provider_fallback(self):
        self.configure(methods=[])
        self.native([closed(snapshot(agent="codex"))])
        self.assertEqual(self.run_cli("reopen", "1").returncode, 0)
        layout = [c for c in self.calls() if c[0] == "new-workspace"][-1]
        self.assertIn("codex resume session-123", layout[-1])
        self.assertNotIn("surface resume set", layout[-1])

    def test_missing_or_malformed_snapshot_opens_empty(self):
        for layout in (None, [], {"type": "split", "split": None}):
            with self.subTest(layout=layout):
                snap = snapshot()
                snap["layout"] = layout
                snap["panels"] = [None, {"id": [], "terminal": []}]
                self.native([closed(snap)])
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("No usable snapshot", result.stdout)
                self.assertNotIn("--layout", self.calls()[-1])
        self.native([])
        self.events([{"category": "workspace", "id": "event", "workspace_id": "event-only",
                      "name": "workspace.closed", "payload": {"cwd": "/tmp"}}])
        self.assertIn("No usable snapshot", self.run_cli("reopen", "1").stdout)

    def test_invalid_index_never_creates_workspace(self):
        self.native([closed(snapshot())])
        for index in ("0", "2", "bad"):
            self.assertNotEqual(self.run_cli("reopen", index).returncode, 0)
        self.assertFalse(any(c[0] == "new-workspace" for c in self.calls()))

    def test_remote_snapshot_requires_native_restore_but_plain_is_local(self):
        for field in ("remote", "cloudVM"):
            with self.subTest(field=field):
                snap = snapshot(agent="codex")
                snap[field] = {"destination": "remote"}
                self.native([closed(snap)])
                before = len(self.calls())
                result = self.run_cli("reopen", "1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("native History", result.stderr)
                self.assertFalse(any(c[0] == "new-workspace" for c in self.calls()[before:]))
                self.assertEqual(self.run_cli("reopen", "1", "--plain").returncode, 0)

    def test_unavailable_cmux_or_timeout_gives_concise_error(self):
        item = {"title": "Project", "cwd": "/tmp"}
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("cmux", 30)):
            with self.subTest(error=type(error).__name__), mock.patch.object(
                    self.manager.subprocess, "run", side_effect=error), contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(self.manager.do_reopen(item, plain=True), 1)
                self.assertTrue(output.getvalue())


if __name__ == "__main__":
    unittest.main()
