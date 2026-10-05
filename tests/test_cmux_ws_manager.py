"""Compatibility checks using disposable history and a fake cmux CLI.

Run with: python3 -m unittest discover -s tests -v
"""
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import pty
import select
import shlex
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest import mock


CLI = Path(__file__).resolve().parents[1] / "cmux-ws-manager"
GROUP_A = "11111111-1111-4111-8111-111111111111"
GROUP_B = "22222222-2222-4222-8222-222222222222"
GROUP_C = "33333333-3333-4333-8333-333333333333"
WINDOW_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WINDOW_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GROUP_METHODS = ["workspace.group.list", "workspace.group.add", "workspace.group.create"]


def controlling_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
FAKE_CMUX = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ["HOME"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps(args) + "\\n")
config = json.loads((root / "fake.json").read_text())
command = args[0]
if command == "--version":
    print(config.get("version", "cmux 0.64.25 (106) [b685a275c]"))
    sys.exit(config.get("version_exit", 0))
elif command == "capabilities":
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
    print(config.get("create_output", "OK workspace:99\\n"), end="")
    if config.get("create_error"):
        print(config["create_error"], file=sys.stderr)
    sys.exit(config.get("create_exit", 0))
elif command == "workspace-action":
    print("OK color-set")
    sys.exit(config.get("color_exit", 0))
elif command == "workspace-group":
    window = args[args.index("--window") + 1] if "--window" in args else config.get("current_window")
    if args[1] == "list":
        if window in config.get("group_fail_windows", []):
            sys.exit(1)
        if "group_list_output" in config:
            print(config["group_list_output"])
        else:
            print(json.dumps({"groups": config.get("groups", {}).get(window, []), "window_id": window}))
    elif args[1] == "add":
        print("OK added")
        sys.exit(config.get("group_add_exit", 0))
    elif args[1] == "create":
        if config.get("group_create_exit", 0):
            sys.exit(config["group_create_exit"])
        group = {"id": config.get("created_group_id", "33333333-3333-4333-8333-333333333333"),
                 "name": args[args.index("--name") + 1]}
        config.setdefault("groups", {}).setdefault(window, []).append(group)
        (root / "fake.json").write_text(json.dumps(config))
        print(config.get("group_create_output", json.dumps({"group": group, "window_id": window})))
    else:
        sys.exit(2)
else:
    sys.exit(2)
'''

FAKE_DEFAULTS = '''
import json, os, plistlib, sys
from pathlib import Path
root = Path(os.environ["HOME"])
with (root / "defaults-calls.jsonl").open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
config = json.loads((root / "fake.json").read_text())
if config.get("preferences_exit"):
    print("Could not export preferences", file=sys.stderr)
    sys.exit(1)
if "preferences_output" in config:
    sys.stdout.write(config["preferences_output"])
else:
    path = root / "preferences.plist"
    sys.stdout.buffer.write(path.read_bytes() if path.exists() else plistlib.dumps({}))
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
        defaults = self.bin / "defaults"
        defaults.write_text("#!" + sys.executable + "\n" + FAKE_DEFAULTS)
        defaults.chmod(0o755)
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

    def session(self, groups, at=1778307400, previous=False):
        path = Path(self.manager.SESSIONS[0 if previous else 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"createdAt": at, "windows": [
            {"tabManager": {"workspaceGroups": groups}}]}))
        return path

    def configure_groups(self, **values):
        defaults = {"methods": GROUP_METHODS + ["surface.resume.set", "surface.resume.get"],
                    "windows": [{"id": WINDOW_A}], "current_window": WINDOW_A}
        self.configure(**dict(defaults, **values))

    def group_mutations(self):
        return [c for c in self.calls() if c[0] == "workspace-group" and c[1] != "list"]

    def calls(self):
        path = self.home / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CLI)] + list(args),
                              capture_output=True, text=True, timeout=10)

    @contextlib.contextmanager
    def terminal_cli(self, *args):
        master, slave = pty.openpty()
        process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 90, 0, 0))
            process = subprocess.Popen([sys.executable, str(CLI)] + list(args),
                                       stdin=slave, stdout=slave, stderr=slave,
                                       env={**os.environ, "TERM": "xterm-256color", "NO_COLOR": "1"},
                                       preexec_fn=controlling_terminal)
            os.close(slave)
            slave = None
            yield process, master
        finally:
            try:
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            finally:
                os.close(master)
                if slave is not None:
                    os.close(slave)

    def terminal_read(self, master, needle):
        output, deadline = b"", time.monotonic() + 10
        while needle not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                break
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        self.assertIn(needle, output)
        return output

    def terminal_wait(self, process, master):
        # Drain curses' final repaint and terminal restoration before waiting;
        # a PTY has a small output buffer and a blocked write cannot exit.
        output, deadline = b"", time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
        return process.wait(timeout=1)

    def close_event(self, wsid="closed", at=800000000, title="Project", cwd="/tmp/project"):
        return {"category": "workspace", "workspace_id": wsid,
                "id": f"close-{wsid}-{at}", "name": "workspace.closed",
                "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime(at + self.manager.APPLE_EPOCH)),
                "payload": {"cwd": cwd, "custom_title": title}}

    def deletion_tui_state(self):
        st = self.settings_tui_state()
        st.update(items=self.manager.entries(), q="", home=str(self.home),
                  up="^", down="v", enter="enter", dot="-", dash="-",
                  attr=dict.fromkeys(("hdr", "warn", "time", "meta", "title", "bar", "sel",
                                      "num", "path", "badge"), 0))
        return st

    def settings(self, data, legacy=False, support=False):
        path = self.home / ("Library/Application Support/com.cmuxterm.app/settings.json" if support else
                            ".config/cmux/settings.json" if legacy else ".config/cmux/cmux.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def preferences(self, data):
        path = self.home / "preferences.plist"
        path.write_bytes(plistlib.dumps(data))
        return path

    def settings_items(self):
        report = self.manager.settings_report()
        return report, {it["setting"]: it for it in report["items"]}

    def settings_tui_state(self):
        return {"items": [{"title": "repo", "cwd": "/tmp/repo", "group_name": ""}],
                "sel": 0, "off": 0, "q": "repo", "mode": "list", "rows": 2,
                "status": "", "pick": None, "show_all": False, "ell": "...", "ascii": False,
                "mark": ">", "attr": {key: 0 for key in
                ("hdr", "warn", "time", "meta", "title", "bar", "sel")}}

    def test_settings_factory_values_and_explicit_defaults_are_omitted(self):
        self.settings({"app": {"appearance": "system", "focusPaneOnFirstClick": False,
                               "keepWorkspaceOpenWhenClosingLastSurface": False, "minimalMode": False},
                       "sidebarAppearance": {"tintOpacity": 0.18},
                       "shortcuts": {"bindings": {"newTab": "command+n", "sendFeedback": None}}})
        self.preferences({"appearanceMode": "system", "paneFirstClickFocus.enabled": False,
                          "closeWorkspaceOnLastSurfaceShortcut": True,
                          "workspacePresentationMode": "standard", "sidebarTintOpacity": 0.18})
        report, items = self.settings_items()
        self.assertEqual(items, {})
        self.assertTrue(report["complete"])
        self.assertGreater(report["checked"], 250)
        result = self.run_cli("settings", "diff")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No differences among", result.stdout)

    def test_settings_preferences_override_imported_file_values(self):
        self.settings({"app": {"appearance": "dark"}, "sidebar": {"showPorts": False}})
        self.preferences({"appearanceMode": "light", "sidebarShowPorts": True,
                          "closeWorkspaceOnLastSurfaceShortcut": False,
                          "workspacePresentationMode": "minimal", "sidebarBranchVerticalLayout": False,
                          "fileExplorerDoubleClickAction": "defaultEditor", "agentConversationForkDefaultDestination": "left"})
        report, items = self.settings_items()
        self.assertTrue(report["complete"])
        self.assertEqual(items["app.appearance"]["current"], "light")
        self.assertEqual(items["app.appearance"]["source"], "macOS preferences")
        self.assertNotIn("sidebar.showPorts", items)
        self.assertTrue(items["app.keepWorkspaceOpenWhenClosingLastSurface"]["current"])
        self.assertTrue(items["app.minimalMode"]["current"])
        self.assertEqual(items["sidebar.branchLayout"]["current"], "inline")
        self.assertEqual(items["fileExplorer.doubleClickAction"]["current"], "defaultEditor")
        self.assertEqual(items["app.forkConversationDefaultDestination"]["current"], "left")

    def test_settings_legacy_fallbacks_fill_only_missing_valid_keys(self):
        self.settings({"app": {"appearance": "dark"}, "sidebar": {"showPorts": "wrong"}})
        self.settings({"app": {"appearance": "light", "menuBarOnly": True},
                       "sidebar": {"showPorts": False}}, legacy=True)
        self.settings({"app": {"menuBarOnly": False, "minimalMode": True}}, support=True)
        report, items = self.settings_items()
        self.assertEqual(items["app.appearance"]["current"], "dark")
        self.assertEqual(items["app.menuBarOnly"]["source"], "settings.json")
        self.assertEqual(items["app.minimalMode"]["source"], "Application Support settings.json")
        self.assertFalse(items["sidebar.showPorts"]["current"])
        self.assertTrue(any("Invalid sidebar.showPorts" in n for n in report["notices"]))

    def test_settings_jsonc_preserves_urls_escaped_quotes_and_comment_tokens(self):
        command = 'editor "//not a comment" /*literal*/ ,] \\ path'
        data = {"app": {"preferredEditor": command}, "browser": {"customSearchEngineURLTemplate":
                "https://example.test/search?q={query}"}}
        body = json.dumps(data)
        text = "\ufeff/* settings */\n" + body[:-1] + ", // trailing comment\n}"
        self.settings(text)
        report, items = self.settings_items()
        self.assertTrue(report["complete"], report["notices"])
        self.assertEqual(items["app.preferredEditor"]["current"], command)
        self.assertEqual(items["browser.customSearchEngineURLTemplate"]["current"],
                         data["browser"]["customSearchEngineURLTemplate"])

    def test_settings_invalid_primary_does_not_import_legacy_and_keeps_preferences(self):
        self.settings('{"app": /* broken')
        self.settings({"app": {"appearance": "dark"}}, legacy=True)
        self.preferences({"notificationDockBadgeEnabled": False})
        report, items = self.settings_items()
        self.assertFalse(report["complete"])
        self.assertNotIn("app.appearance", items)
        self.assertIn("notifications.dockBadge", items)
        self.assertTrue(any("Could not read cmux.json" in n for n in report["notices"]))

    def test_settings_invalid_values_and_unrecognized_keys_are_not_differences(self):
        self.settings({"app": {"appearance": "unknown", "menuBarOnly": 1, "unknown": "private-value"},
                       "browser": {"defaultZoomLevel": True}, "sidebarAppearance": {"tintOpacity": 20}})
        report, items = self.settings_items()
        self.assertEqual(items, {})
        self.assertFalse(report["complete"])
        result = self.run_cli("settings", "diff")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("private-value", result.stdout + result.stderr)
        self.assertIn("Unsupported settings", result.stderr)

    def test_settings_nonfinite_json_and_malformed_preferences_degrade(self):
        self.settings('{"sidebarAppearance":{"tintOpacity":NaN}}')
        self.configure(preferences_output="<?xml version='1.0'?><plist><dict>")
        result = self.run_cli("settings", "diff")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("Could not read", result.stderr)
        self.assertNotIn("No differences among", result.stdout)

    def test_settings_unsupported_version_and_build_never_claim_verified_defaults(self):
        self.settings({"app": {"appearance": "dark"}})
        for version in ("cmux 9.0.0", "cmux 0.64.25 (106) [abcdef012]"):
            self.configure(version=version)
            report, items = self.settings_items()
            self.assertFalse(report["complete"])
            self.assertEqual(report["checked"], 0)
            self.assertFalse(items["app.appearance"]["verified"])
            self.assertEqual(self.manager.setting_value(items["app.appearance"], True), "(unverified)")
            result = self.run_cli("settings", "diff")
            self.assertEqual(result.returncode, 1)
            self.assertIn("(unverified)", result.stdout)

    def test_settings_missing_cmux_marks_default_comparisons_unverified(self):
        (self.bin / "cmux").unlink()
        self.settings({"app": {"appearance": "dark"}})
        with mock.patch.object(self.manager.shutil, "which", return_value=None), \
                mock.patch.object(self.manager.subprocess, "run", side_effect=FileNotFoundError):
            report = self.manager.settings_report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["changed"], 0)
        self.assertFalse(report["items"][0]["verified"])

    def test_settings_export_fallback_is_not_treated_as_live_preferences(self):
        self.configure(preferences_exit=1)
        path = self.home / "Library/Preferences/com.cmuxterm.app.plist"
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({"appearanceMode": "dark"}))
        report, items = self.settings_items()
        self.assertFalse(report["complete"])
        self.assertFalse(items["app.appearance"]["verified"])
        self.assertEqual(items["app.appearance"]["current"], "dark")

    def test_settings_null_colors_and_cleared_imported_preferences(self):
        self.settings({"notifications": {"paneFlashColor": "#abcdef"},
                       "workspaceColors": {"selectionColor": None}})
        self.preferences({"cmux.settingsFile.importedManagedDefaults.v1": json.dumps({
            "notificationPaneFlashColorHex": {"nullableString": {"_0": "#ABCDEF"}}}).encode(),
            "sidebarSelectionColorHex": ""})
        report, items = self.settings_items()
        self.assertTrue(report["complete"])
        self.assertNotIn("notifications.paneFlashColor", items)
        self.assertNotIn("workspaceColors.selectionColor", items)

    def test_settings_pane_colors_and_supported_sidebar_paths(self):
        self.settings({"paneBorderColor": "abcdef", "activePaneBorderColor": None,
                       "sidebar": {"stackBranchDirectory": True, "pathLastSegmentOnly": True,
                                   "beta": {"workspaceTodos": {"controls": {"enabled": True}}}}})
        report, items = self.settings_items()
        self.assertTrue(report["complete"], report["notices"])
        self.assertEqual(items["paneBorderColor"]["current"], "#ABCDEF")
        self.assertNotIn("activePaneBorderColor", items)
        self.assertTrue(items["sidebar.stackBranchDirectory"]["current"])
        self.assertTrue(items["sidebar.pathLastSegmentOnly"]["current"])
        self.assertTrue(items["sidebar.beta.workspaceTodos.controls.enabled"]["current"])

    def test_settings_widths_use_their_distinct_cmux_normalization(self):
        self.preferences({"rightSidebarMaxWidth": 513.4, "terminal.sessionContentMaxWidth": 513.4})
        report, items = self.settings_items()
        self.assertTrue(report["complete"], report["notices"])
        self.assertEqual(items["sidebar.rightMaxWidth"]["current"], 513)
        self.assertEqual(items["terminal.sessionContentMaxWidth"]["current"], 520)

    def test_settings_invalid_saved_preference_is_skipped_instead_of_using_file(self):
        self.settings({"app": {"appearance": "dark"}})
        self.preferences({"appearanceMode": {"invalid": "private-data"}})
        report, items = self.settings_items()
        self.assertFalse(report["complete"])
        self.assertNotIn("app.appearance", items)
        self.assertTrue(any("Invalid app.appearance in macOS preferences" in n for n in report["notices"]))

    def test_settings_reset_palette_and_explicit_default_shortcut_aliases(self):
        self.settings({"workspaceColors": {"colors": {"Red": "#112233"}},
                       "shortcuts": {"bindings": {"browserBack": "cmd+leftbracket",
                                                  "sendFeedback": ["none"]}}})
        self.preferences({"cmux.settingsFile.importedManagedDefaults.v1": json.dumps({
            "workspaceTabColor.colors": {"stringDictionary": {"_0": {"Red": "#112233"}}}}).encode()})
        report, items = self.settings_items()
        self.assertTrue(report["complete"], report["notices"])
        self.assertEqual(items, {})

    def test_settings_shortcuts_normalize_chords_and_explicit_unbinding(self):
        self.settings({"shortcuts": {"bindings": {"newTab": ["control+b", "c"],
            "openSettings": None, "sendFeedback": "none", "toggleSidebar": "command+b"}}})
        self.preferences({"shortcut.newTab": json.dumps({"key": "x", "command": True,
                          "shift": False, "option": False, "control": False}).encode()})
        report, items = self.settings_items()
        self.assertTrue(report["complete"], report["notices"])
        self.assertEqual(items["shortcuts.bindings.newTab"]["current"], ["ctrl+b", "c"])
        self.assertEqual(items["shortcuts.bindings.newTab"]["source"], "cmux.json")
        self.assertIsNone(items["shortcuts.bindings.openSettings"]["current"])
        self.assertEqual(self.manager.setting_value(items["shortcuts.bindings.openSettings"]), "(unbound)")
        self.assertNotIn("shortcuts.bindings.sendFeedback", items)
        self.assertNotIn("shortcuts.bindings.toggleSidebar", items)

    def test_settings_saved_shortcuts_and_malformed_file_bindings(self):
        self.preferences({"shortcut.newTab": json.dumps({"key": "b", "command": False,
            "shift": False, "option": False, "control": True, "chordKey": "c"}).encode(),
            "shortcut.simulatorRotateLeft": json.dumps({"key": "←", "command": True,
                "shift": False, "option": False, "control": False}).encode()})
        report, items = self.settings_items()
        self.assertEqual(items["shortcuts.bindings.newTab"]["current"], ["ctrl+b", "c"])
        self.assertNotIn("shortcuts.bindings.simulatorRotateLeft", items)
        self.settings({"shortcuts": {"bindings": {"newTab": ["", "c"], "notAnAction": "cmd+x"}}})
        report, items = self.settings_items()
        self.assertFalse(report["complete"])
        self.assertNotIn("shortcuts.bindings.newTab", items)
        self.assertTrue(any("Invalid shortcut binding" in n for n in report["notices"]))

    def test_settings_context_dependent_defaults_are_explicitly_unverified(self):
        self.settings({"shortcuts": {"bindings": {"switchRightSidebarToFiles": "ctrl+5"},
                                    "when": {"newTab": "terminalFocus"}}})
        report, items = self.settings_items()
        self.assertEqual(report["unverified"], 2)
        self.assertFalse(items["shortcuts.bindings.switchRightSidebarToFiles"]["verified"])
        self.assertFalse(items["shortcuts.when.newTab"]["verified"])

    def test_settings_command_is_read_only_and_does_not_harvest_history(self):
        config = self.settings({"app": {"appearance": "dark"},
                                "automation": {"socketPassword": "secret-password"},
                                "commands": {"private": "secret-command"}})
        prefs = self.preferences({"cmux.auth.token": "secret-token", "NSWindow Frame": "private-state"})
        native = self.native([closed(snapshot())])
        events = self.events([{"category": "workspace", "id": "closed", "workspace_id": "ws",
                              "name": "workspace.closed", "payload": {"private": "secret-event"}}])
        before = {path: path.read_bytes() for path in (config, prefs, native, events)}
        result = self.run_cli("settings", "diff")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Setting", result.stdout)
        self.assertEqual(self.calls(), [["--version"]])
        self.assertFalse(Path(self.manager.HISTORY).exists())
        self.assertFalse(Path(self.manager.GROUPS).exists())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        for value in ("secret-password", "secret-command", "secret-token", "private-state", "secret-event"):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_settings_help_and_invalid_arguments_never_load_settings_or_history(self):
        for args in (("settings", "diff", "--help"), ("settings", "-h")):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 0)
            self.assertIn("settings diff", result.stdout)
        for args in (("settings",), ("settings", "other"), ("settings", "diff", "--all"),
                     ("--no-interactive", "settings", "diff"), ("settings", "diff", "--unknown")):
            result = self.run_cli(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--help", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.home / "defaults-calls.jsonl").exists())

    def test_settings_external_reads_have_timeouts(self):
        with mock.patch.object(self.manager.subprocess, "run", side_effect=subprocess.TimeoutExpired("read", 3)) as run:
            report = self.manager.settings_report()
        self.assertFalse(report["complete"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 3)
            self.assertIsInstance(call.args[0], list)

    def test_settings_tui_preserves_workspaces_and_never_reopens_from_settings(self):
        st = self.settings_tui_state()
        report = {"items": [{"setting": "app.appearance", "current": "dark", "default": "system",
                            "source": "cmux.json", "verified": True}], "complete": True,
                  "version": "0.64.25", "changed": 1, "unverified": 0, "checked": 319, "notices": []}
        original = {key: st[key] for key in ("items", "sel", "off", "q", "mode", "pick")}
        with mock.patch.object(self.manager, "settings_report", return_value=report) as load, \
                mock.patch.object(self.manager, "tui_settings_draw"), \
                mock.patch.object(self.manager.curses, "flushinp"), \
                mock.patch.object(self.manager, "entries", side_effect=AssertionError("history read")), \
                mock.patch.object(self.manager, "do_reopen", side_effect=AssertionError("reopen")):
            self.assertFalse(self.manager.tui_key(None, st, "s"))
            self.assertEqual(st["screen"], "settings")
            self.assertFalse(self.manager.tui_key(None, st, 10))
            self.assertEqual(st["settings"]["detail"]["setting"], "app.appearance")
            self.assertFalse(self.manager.tui_key(None, st, 27))
            self.assertIsNone(st["settings"]["detail"])
            self.assertFalse(self.manager.tui_key(None, st, "/"))
            for key in "DARK": self.manager.tui_key(None, st, key)
            self.manager.tui_key(None, st, 10)
            self.assertEqual(len(self.manager.settings_visible(st["settings"])), 1)
            self.manager.tui_key(None, st, "r")
            self.assertEqual(load.call_count, 2)
            self.assertEqual(st["settings"]["q"], "DARK")
            self.manager.tui_key(None, st, 27)
            self.assertEqual(st["screen"], "settings")
            self.manager.tui_key(None, st, 27)
            self.assertEqual(st["screen"], "workspaces")
        self.assertEqual({key: st[key] for key in original}, original)

    def test_settings_tui_loading_drawing_details_and_small_terminals(self):
        st = self.settings_tui_state()
        st["screen"] = "settings"
        self.settings({"app": {"preferredEditor": "editor " + "界" * 70 + "\n\x1b\u202e"}})
        window = mock.Mock()
        window.getmaxyx.return_value = (14, 90)
        with mock.patch.object(self.manager, "put") as put, \
                mock.patch.object(self.manager.curses, "flushinp"), \
                mock.patch.object(self.manager.curses, "ACS_HLINE", ord("-"), create=True):
            self.manager.tui_settings_load(window, st)
            self.manager.tui_settings_draw(window, st)
            self.manager.tui_key(window, st, 10)
            self.manager.tui_settings_draw(window, st)
            lines = self.manager.settings_detail_lines(st, 40)
            self.assertGreater(len(lines), 10)
            self.assertTrue(all(self.manager.dwidth(line) <= 40 for line in lines))
            self.manager.tui_key(window, st, "i")
            self.manager.tui_settings_draw(window, st)
            for text in (call.args[3] for call in put.call_args_list):
                for control in ("\n", "\x1b", "\u202e"):
                    self.assertNotIn(control, text)
            window.getmaxyx.return_value = (5, 20)
            self.manager.tui_settings_draw(window, st)
            self.assertIn("window too small", put.call_args_list[-1].args)

    def test_delete_merges_all_past_closes_and_preserves_sources_and_neighbor_titles(self):
        native = self.native([closed(snapshot("old", agent="claude")),
                              closed(snapshot("new"), 800000200),
                              closed(snapshot("neighbor", title="Other"), 800000300)])
        records = [self.close_event("event-only", 799999900),
                   self.close_event("old", title=""), self.close_event("new", 800000200, title="")]
        events = self.events(records)
        before = {path: path.read_bytes() for path in (native, events)}
        item = next(it for it in self.manager.entries() if it["title"] == "Project")
        self.assertIsNone(self.manager.delete_entry(item))
        for show_all in (False, True):
            self.assertEqual([it["id"] for it in self.manager.entries(show_all)], ["neighbor"])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        saved = json.loads(Path(self.manager.DELETIONS).read_text())["records"][0]
        self.assertEqual(saved["ids"], ["event-only", "new", "old"])
        self.assertNotIn("snapshot", saved)
        self.assertNotIn("session-123", Path(self.manager.DELETIONS).read_text())
        self.assertNotIn("Project", self.run_cli("--no-interactive").stdout)
        self.assertNotIn("Project", self.run_cli("--all", "--no-interactive").stdout)
        # Native retention must not reveal the title-less event fallback.
        self.native([])
        self.assertEqual(self.manager.entries(), [])

    def test_deleted_snapshots_never_graft_into_a_later_close(self):
        donor = closed(snapshot("old", agent="claude"))
        self.native([donor, closed(snapshot("new"), 800000100)])
        self.assertTrue(self.manager.entries()[0]["grafted"])
        self.assertIsNone(self.manager.delete_entry(self.manager.entries()[0]))
        self.native([donor, closed(snapshot("later"), 800000200)])
        item, = self.manager.entries()
        self.assertEqual(item["id"], "later")
        self.assertNotIn("grafted", item)
        self.assertEqual(self.manager.restore_counts(item["snapshot"]), (1, 0, 0))
        self.assertEqual([it["id"] for it in self.manager.entries(True)], ["later"])

    def test_delete_event_only_history_and_allow_same_id_to_close_later(self):
        old, newest = self.close_event(), self.close_event(at=800000200)
        created = dict(self.close_event(at=800000100), name="workspace.created", id="created")
        records = [old, created, newest, self.close_event("neighbor", title="Other")]
        source = self.events(records)
        before = source.read_bytes()
        item = next(it for it in self.manager.entries() if it["id"] == "closed")
        self.assertIsNone(self.manager.delete_entry(item))
        self.assertEqual([it["id"] for it in self.manager.entries(True)], ["neighbor"])
        self.assertEqual(source.read_bytes(), before)
        self.events(records + [self.close_event(at=800000300)])
        self.assertEqual([it["id"] for it in self.manager.entries()], ["closed", "neighbor"])

    def test_delete_from_all_view_removes_newer_and_older_closes(self):
        self.native([closed(snapshot("old")), closed(snapshot("new"), 800000300),
                     closed(snapshot("neighbor", title="Other"), 800000100)])
        raw = self.manager.entries(True)
        self.assertEqual(len(raw), 3)
        self.assertIsNone(self.manager.delete_entry(next(it for it in raw if it["id"] == "old")))
        self.assertEqual([it["id"] for it in self.manager.entries(True)], ["neighbor"])

    def test_delete_individual_workspace_from_closed_window(self):
        record = {"closedAt": 800000000, "entry": {"window": {"_0": {
            "workspaceIds": ["first", "second"], "snapshot": {"tabManager": {
                "workspaces": [snapshot("first"), snapshot("second", title="Other")]}}}}}}
        source = self.native([record], legacy=True)
        before = source.read_bytes()
        self.assertIsNone(self.manager.delete_entry(self.manager.entries()[0]))
        self.assertEqual([it["id"] for it in self.manager.entries()], ["second"])
        self.assertEqual(source.read_bytes(), before)

    def test_delete_untitled_and_directoryless_entries_use_existing_identity(self):
        self.native([closed(snapshot("untitled-old", title="")),
                     closed(snapshot("untitled-new", title=""), 800000100),
                     closed(snapshot("named", title="Other")),
                     closed(snapshot("no-dir", cwd="", title="")),
                     closed(snapshot("no-dir", cwd="", title=""), 800000300),
                     closed(snapshot("different-id", cwd="", title=""), 800000200)])
        items = self.manager.entries()
        self.assertIsNone(self.manager.delete_entry(next(it for it in items if it["id"] == "untitled-new")))
        self.assertIsNone(self.manager.delete_entry(next(it for it in items if it["id"] == "no-dir")))
        self.assertEqual({it["id"] for it in self.manager.entries(True)}, {"named", "different-id"})

    def test_malformed_removal_records_are_skipped_and_preserved_on_save(self):
        self.native([closed(snapshot()), closed(snapshot("neighbor", title="Other"), 800000100)])
        path = Path(self.manager.DELETIONS)
        path.parent.mkdir(parents=True, exist_ok=True)
        bad = [None, [], {"identity": ["/tmp/project"], "before": 800000000},
               {"identity": ["/tmp/project", "project"], "before": "invalid"},
               {"identity": [[], "project"], "before": 800000000}]
        path.write_text(json.dumps({"version": 1, "records": bad}))
        self.assertEqual(len(self.manager.entries()), 2)
        self.assertIsNone(self.manager.delete_entry(self.manager.entries()[0]))
        self.assertEqual(json.loads(path.read_text())["records"][:-1], bad)
        self.assertEqual([it["id"] for it in self.manager.entries()], ["closed"])
        for invalid in ('{"version": 2, "records": []}', '{"version": 1, "records": {}}', '{broken'):
            path.write_text(invalid)
            self.assertEqual(len(self.manager.entries()), 2)
            self.assertIn("Could not save removal", self.manager.delete_entry(self.manager.entries()[0]))
            self.assertEqual(path.read_text(), invalid)

    def test_failed_atomic_removal_write_keeps_existing_removals_and_sources(self):
        native = self.native([closed(snapshot()),
                              closed(snapshot("neighbor", title="Other"), 800000100)])
        events = self.events([self.close_event()])
        self.assertIsNone(self.manager.delete_entry(self.manager.entries()[0]))
        saved = Path(self.manager.DELETIONS)
        before = {path: path.read_bytes() for path in (native, events, saved)}
        with mock.patch.object(self.manager.os, "replace", side_effect=PermissionError):
            self.assertIn("PermissionError", self.manager.delete_entry(self.manager.entries()[0]))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual([it["id"] for it in self.manager.entries()], ["closed"])
        self.assertFalse(list(saved.parent.glob(".workspace-deletions-*")))

    def test_delete_tui_requires_exact_confirmation_and_preserves_filtered_position(self):
        self.native([closed(snapshot("one", title="Project one"), 800000300),
                     closed(snapshot("two", title="Project two"), 800000200),
                     closed(snapshot("three", title="Project three"), 800000100),
                     closed(snapshot("neighbor", title="Other"), 800000400)])
        st = self.deletion_tui_state()
        st.update(q="Project ", sel=1, off=1)
        window = mock.Mock()
        window.getmaxyx.return_value = (24, 90)
        original = list(st["items"])
        with mock.patch.object(self.manager, "tui_draw"), \
                mock.patch.object(self.manager.curses, "flushinp"), \
                mock.patch.object(self.manager, "delete_entry", wraps=self.manager.delete_entry) as delete:
            self.assertFalse(self.manager.tui_key(window, st, "d"))
            self.assertEqual(st["delete"]["item"]["id"], "two")
            for text in ("", "yes", "YES ", " YES", "YESx"):
                self.manager.tui_key(window, st, 21)
                for key in text:
                    self.manager.tui_key(window, st, key)
                self.manager.tui_key(window, st, 10)
                self.assertIn("exactly", st["delete"]["error"])
                delete.assert_not_called()
                self.assertEqual(st["items"], original)
            self.manager.tui_key(window, st, 21)
            for key in "qsr/":
                self.assertFalse(self.manager.tui_key(window, st, key))
            self.assertEqual(st["delete"]["text"], "qsr/")
            self.manager.tui_key(window, st, self.manager.curses.KEY_DOWN)
            self.assertEqual(st["sel"], 1)
            self.manager.tui_key(window, st, 21)
            for key in "YESx":
                self.manager.tui_key(window, st, key)
            self.manager.tui_key(window, st, 127)
            window.getmaxyx.return_value = (5, 20)
            self.manager.tui_key(window, st, 10)
            delete.assert_not_called()
            self.manager.tui_key(window, st, self.manager.curses.KEY_RESIZE)
            self.assertEqual(st["delete"]["text"], "YES")
            self.assertEqual(st["delete"]["item"]["id"], "two")
            window.getmaxyx.return_value = (24, 90)
            self.assertFalse(self.manager.tui_key(window, st, 10))
            delete.assert_called_once()
        self.assertIsNone(st["delete"])
        self.assertIsNone(st["pick"])
        self.assertEqual(st["q"], "Project ")
        self.assertEqual([it["id"] for it in self.manager.visible(st)], ["one", "three"])
        self.assertEqual((st["sel"], st["off"]), (1, 0))

    def test_delete_tui_cancellation_failure_search_and_empty_list(self):
        self.native([closed(snapshot())])
        window = mock.Mock()
        window.getmaxyx.return_value = (24, 90)
        for key in (27, 3):
            st = self.deletion_tui_state()
            original = {k: st[k] for k in ("items", "sel", "off", "q", "pick")}
            self.manager.tui_key(window, st, "d")
            for char in "YES":
                self.manager.tui_key(window, st, char)
            self.assertFalse(Path(self.manager.DELETIONS).exists())
            self.assertFalse(self.manager.tui_key(window, st, key))
            self.assertIsNone(st["delete"])
            self.assertEqual({k: st[k] for k in original}, original)
        st = self.deletion_tui_state()
        self.manager.tui_key(window, st, "d")
        st["delete"]["text"] = "YES"
        with mock.patch.object(self.manager, "delete_entry", return_value="Could not save removal."):
            self.manager.tui_key(window, st, 10)
        self.assertIn("Could not save", st["delete"]["error"])
        self.assertEqual(len(st["items"]), 1)
        self.manager.tui_key(window, st, 27)
        st["mode"] = "search"
        self.manager.tui_key(window, st, "d")
        self.assertEqual(st["q"], "d")
        self.assertIsNone(st["delete"])
        st.update(mode="list", q="", items=[])
        self.manager.tui_key(window, st, "d")
        self.assertIsNone(st["delete"])
        self.assertIn("No closed workspace", st["status"])

    def test_delete_dialog_sanitizes_labels_and_draws_in_small_and_ascii_terminals(self):
        st = self.settings_tui_state()
        item = {"title": "界\n\x1b\u202e" * 80, "cwd": "/tmp/\n\x1b\u202e" * 40}
        st["delete"] = {"item": item, "text": "YES", "error": ""}
        window = mock.Mock()
        window.getmaxyx.return_value = (24, 90)
        with mock.patch.object(self.manager.curses, "newwin", return_value=window), \
                mock.patch.object(self.manager.curses, "curs_set"), \
                mock.patch.object(self.manager, "put") as put:
            for ascii_only in (False, True):
                st["ascii"] = ascii_only
                self.manager.tui_delete_draw(window, st)
                for call in put.call_args_list:
                    for control in ("\n", "\x1b", "\u202e"):
                        self.assertNotIn(control, call.args[3])
                    self.assertLessEqual(self.manager.dwidth(call.args[3]), call.args[4])
                put.reset_mock()
            window.getmaxyx.return_value = (5, 20)
            self.manager.tui_delete_draw(window, st)
            self.assertIn("Resize terminal", put.call_args.args[3])

    def test_cli_delete_requires_a_terminal_and_rejects_invalid_arguments(self):
        self.native([closed(snapshot())])
        result = subprocess.run([sys.executable, str(CLI), "delete", "1"], input="YES\n",
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interactive terminal", result.stderr)
        for args in (("delete",), ("delete", "0"), ("delete", "x"),
                     ("delete", "1", "extra"), ("delete", "1", "--plain"),
                     ("delete", "1", "--no-interactive"), ("delete", "1", "--yes")):
            result = self.run_cli(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--help", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(Path(self.manager.DELETIONS).exists())
        with self.terminal_cli("delete", "9") as (process, master):
            self.terminal_read(master, b"--help")
            self.assertNotEqual(process.wait(timeout=10), 0)
        self.assertFalse(Path(self.manager.DELETIONS).exists())

    def test_cli_delete_uses_default_numbering_and_exact_typed_confirmation(self):
        self.native([closed(snapshot("new"), 800000300), closed(snapshot("old"), 800000200),
                     closed(snapshot("neighbor", title="Other"), 800000100)])
        with self.terminal_cli("--all", "delete", "2") as (process, master):
            output = self.terminal_read(master, b"Ctrl+C to cancel): ")
            self.assertIn(b"Workspace: Other", output)
            self.assertFalse(Path(self.manager.DELETIONS).exists())
            os.write(master, b"yes\nYES \n YES\n\nYES\n")
            output = self.terminal_read(master, b"Removed workspace history from this tool.")
            self.assertEqual(output.count(b"Type YES exactly"), 4)
            self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual([it["id"] for it in self.manager.entries()], ["new"])
        self.assertEqual({it["id"] for it in self.manager.entries(True)}, {"new", "old"})
        self.assertFalse(any(call[0] == "new-workspace" for call in self.calls()))

    def test_cli_delete_ctrl_c_and_eof_cancel(self):
        self.native([closed(snapshot())])
        for key in (b"\x03", b"\x04"):
            with self.terminal_cli("delete", "1") as (process, master):
                self.terminal_read(master, b"Ctrl+C to cancel): ")
                os.write(master, key)
                self.terminal_read(master, b"Deletion cancelled.")
                self.assertNotEqual(process.wait(timeout=10), 0)
            self.assertFalse(Path(self.manager.DELETIONS).exists())

    def test_real_curses_delete_dialog_cancel_confirm_and_continue(self):
        self.native([closed(snapshot())])
        with self.terminal_cli() as (process, master):
            self.terminal_read(master, b"Project")
            os.write(master, b"d")
            self.terminal_read(master, b"Are you sure?")
            os.write(master, b"\x03")
            self.terminal_read(master, b"Deletion cancelled.")
            self.assertIsNone(process.poll())
            self.assertFalse(Path(self.manager.DELETIONS).exists())
            os.write(master, b"d")
            self.terminal_read(master, b"Are you sure?")
            os.write(master, b"YES")
            self.assertFalse(Path(self.manager.DELETIONS).exists())
            os.write(master, b"\n")
            self.terminal_read(master, b"Removed workspace history from this tool.")
            self.assertIsNone(process.poll())
            os.write(master, b"q")
            self.assertEqual(self.terminal_wait(process, master), 0)
        self.assertEqual(self.manager.entries(), [])
        self.assertFalse(any(call[0] == "new-workspace" for call in self.calls()))

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
        self.assertFalse(Path(self.manager.GROUPS).exists())
        self.assertFalse(Path(self.manager.DELETIONS).exists())

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
        snaps[0]["customColor"] = "#EA8D3F"
        record = {"closedAt": 800000000, "entry": {"window": {"_0": {
            "workspaceIds": ["outdated-first", "outdated-second"],
            "snapshot": {"tabManager": {"workspaces": snaps + [None]}}}}}}
        self.native([record], legacy=True)
        self.assertEqual([it["id"] for it in self.manager.native_closed()], ["first", "second"])
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1], ["workspace-action", "--workspace", "workspace:99",
                                          "--action", "set-color", "--color", "#EA8D3F"])

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

    def test_saved_color_targets_created_workspace_and_preserves_output(self):
        for handle in ("workspace:99", "01234567-89ab-cdef-0123-456789abcdef"):
            with self.subTest(handle=handle):
                self.configure(create_output="OK " + handle + "\n", create_error="creation diagnostic")
                path = self.native([closed(dict(snapshot(), customColor="#aB12eF"))])
                original = path.read_bytes()
                before = len(self.calls())
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith("OK " + handle + "\nRestored "))
                self.assertNotIn("color-set", result.stdout)
                self.assertEqual(result.stderr, "creation diagnostic\n")
                calls = self.calls()[before:]
                self.assertEqual(calls[-1], ["workspace-action", "--workspace", handle,
                                             "--action", "set-color", "--color", "#aB12eF"])
                self.assertEqual(sum(c[0] == "new-workspace" for c in calls), 1)
                self.assertEqual(path.read_bytes(), original)

    def test_plain_skips_saved_color(self):
        self.native([closed(dict(snapshot(), customColor="#ABCDEF"))])
        result = self.run_cli("reopen", "1", "--plain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "OK workspace:99\n")
        self.assertEqual(self.calls()[-1], ["new-workspace", "--name", "Project",
                                          "--cwd", "/tmp/project"])
        self.assertFalse(any(c[0] == "workspace-action" for c in self.calls()))

    def test_missing_or_malformed_color_is_skipped(self):
        for color in (None, "", [], {}, 123, "Blue", "#ABC", "#ABCDEF00", "#ABCDEG",
                      "#ABCDEF\n", "#ABCDEF\0", "--help"):
            with self.subTest(color=color):
                self.native([closed(dict(snapshot(), customColor=color))])
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
        self.native([closed(snapshot())])
        self.assertEqual(self.run_cli("reopen", "1").returncode, 0)
        self.assertFalse(any(c[0] == "workspace-action" for c in self.calls()))

    def test_saved_color_survives_unusable_layout(self):
        self.native([closed(dict(snapshot(), customColor="#ABCDEF", layout=None))])
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No usable snapshot", result.stdout)
        creation = [c for c in self.calls() if c[0] == "new-workspace"]
        self.assertEqual(len(creation), 1)
        self.assertNotIn("--layout", creation[0])
        self.assertEqual(self.calls()[-1][0], "workspace-action")

    def test_failed_creation_never_sets_color(self):
        self.configure(create_exit=7)
        self.native([closed(dict(snapshot(), customColor="#ABCDEF"))])
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "OK workspace:99\n")
        self.assertNotIn("Restored", result.stdout)
        self.assertFalse(any(c[0] == "workspace-action" for c in self.calls()))

    def test_unexpected_creation_output_warns_without_targeting_another_workspace(self):
        self.native([closed(dict(snapshot(), customColor="#ABCDEF"))])
        for output in ("", "OK", "OK current\n", "OK surface:99\n", "OK --help\n",
                       "OK workspace:99\nOK workspace:100\n"):
            with self.subTest(output=output):
                self.configure(create_output=output)
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0)
                self.assertIn("Restored", result.stdout)
                self.assertIn("saved color could not be restored", result.stderr)
        self.assertFalse(any(c[0] == "workspace-action" for c in self.calls()))

    def test_color_command_failure_keeps_workspace_open(self):
        self.configure(color_exit=2)
        self.native([closed(dict(snapshot(), customColor="#ABCDEF"))])
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Restored", result.stdout)
        self.assertIn("saved color could not be restored", result.stderr)
        self.assertEqual(sum(c[0] == "new-workspace" for c in self.calls()), 1)
        self.assertFalse(any(c[0] == "close-workspace" for c in self.calls()))

    def test_color_command_timeout_or_unavailable_keeps_success(self):
        item = {"title": "Project", "cwd": "/tmp", "snapshot": {"customColor": "#ABCDEF"}}
        created = subprocess.CompletedProcess([], 0, stdout="OK workspace:99\n")
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("cmux", 5)):
            with self.subTest(error=type(error).__name__), mock.patch.object(
                    self.manager.subprocess, "run", side_effect=[created, error]) as run, \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(self.manager.do_reopen(item), 0)
                self.assertIn("saved color could not be restored", output.getvalue())
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_colored_creation_timeout_replays_output_without_setting_color(self):
        item = {"title": "Project", "cwd": "/tmp", "snapshot": {"customColor": "#ABCDEF"}}
        error = subprocess.TimeoutExpired("cmux", 30, output=b"OK workspace:99\n")
        with mock.patch.object(self.manager.subprocess, "run", side_effect=error) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            self.assertEqual(self.manager.do_reopen(item), 1)
            self.assertEqual(output.getvalue(), "OK workspace:99\n")
            self.assertIn("30 seconds", diagnostic.getvalue())
            self.assertEqual(run.call_count, 1)

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

    def test_session_group_names_are_cached_and_survive_deletion(self):
        native = self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        previous = self.session([{"id": GROUP_A, "name": "Old name"}], at=1778307300, previous=True)
        current = self.session([{"id": GROUP_A, "name": "Infra"}], at=1778307400)
        original = [p.read_bytes() for p in (native, previous, current)]
        self.assertEqual(self.manager.entries()[0]["group_name"], "Infra")
        cache = json.loads(Path(self.manager.GROUPS).read_text())
        self.assertEqual(cache["groups"][GROUP_A]["name"], "Infra")
        self.assertEqual(set(cache), {"version", "groups", "aliases"})
        self.assertEqual(set(cache["groups"][GROUP_A]), {"name", "ts"})
        self.assertEqual([p.read_bytes() for p in (native, previous, current)], original)
        previous.unlink()
        current.unlink()
        self.assertEqual(self.manager.entries()[0]["group_name"], "Infra")

    def test_window_group_name_stays_historical_and_live_name_resolves_individual_close(self):
        self.configure_groups(groups={WINDOW_A: [{"id": GROUP_A, "name": "Current name"}]})
        window = {"closedAt": 800000000, "entry": {"window": {"snapshot": {"tabManager": {
            "workspaces": [dict(snapshot("from-window", "/tmp/window"), groupId=GROUP_A)],
            "workspaceGroups": [None, {"id": GROUP_A, "name": "Saved name"}]}}}}}
        self.native([window, closed(dict(snapshot("individual"), groupId=GROUP_A), 800000100)], legacy=True)
        self.session([{"id": GROUP_A, "name": "Session name"}])
        items = {it["id"]: it for it in self.manager.entries()}
        self.assertEqual(items["from-window"]["group_name"], "Saved name")
        self.assertEqual(items["individual"]["group_name"], "Current name")

    def test_newest_closed_window_catalog_resolves_other_snapshots(self):
        records = [closed(dict(snapshot(), groupId=GROUP_A))]
        for name, at in (("Newer", 800000200), ("Older", 800000100)):
            records.append({"closedAt": at, "entry": {"window": {"_0": {"snapshot": {
                "tabManager": {"workspaceGroups": [{"id": GROUP_A, "name": name}]}}}}}})
        self.native(records)
        self.session([{"id": GROUP_A, "name": "Earlier session"}], at=1778307250)
        self.assertEqual(self.manager.entries()[0]["group_name"], "Newer")

    def test_group_labels_are_sanitized_in_listing_and_picker_and_searchable(self):
        name = "Infra\ttools\n\x1b[31m\u202e"
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": name}])
        items = self.manager.entries()
        self.assertEqual(self.manager.visible({"items": items, "q": "INFRA"}), items)
        result = self.run_cli("--no-interactive")
        self.assertIn("group: Infra", result.stdout)
        for control in ("\t", "\x1b", "\u202e"):
            self.assertNotIn(control, result.stdout)
        self.assertEqual(len([line for line in result.stdout.splitlines() if line.strip().startswith("1 ")]), 1)
        state = {"attr": {k: 0 for k in ("sel", "num", "time", "title", "path", "badge")},
                 "ascii": False, "ell": "…", "home": str(self.home), "mark": ">",
                 "dot": "·", "show_all": False}
        with mock.patch.object(self.manager, "put") as put:
            self.manager.draw_row(None, state, 0, 1, items[0], False, 180)
        metadata = put.call_args_list[-1].args[3]
        self.assertIn("group: Infra", metadata)
        for control in ("\t", "\n", "\x1b", "\u202e"):
            self.assertNotIn(control, metadata)

    def test_existing_group_targets_its_window_and_exact_created_handle(self):
        for handle in ("workspace:99", "01234567-89ab-cdef-0123-456789abcdef"):
            with self.subTest(handle=handle):
                self.configure_groups(windows=[{"id": WINDOW_A}, {"id": WINDOW_B}],
                                      groups={WINDOW_B: [{"id": GROUP_A, "name": "Infra"}]},
                                      create_output="OK " + handle + "\n")
                self.native([closed(dict(snapshot(), groupId=GROUP_A, customColor="#ABCDEF"))])
                before = len(self.calls())
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                calls = self.calls()[before:]
                creation = [c for c in calls if c[0] == "new-workspace"]
                self.assertEqual(len(creation), 1)
                self.assertEqual(creation[0][creation[0].index("--window") + 1], WINDOW_B)
                self.assertEqual(calls[-1], ["workspace-group", "add", "--group", GROUP_A,
                                             "--workspace", handle, "--window", WINDOW_B])
                self.assertIn(["workspace-action", "--workspace", handle, "--action", "set-color",
                               "--color", "#ABCDEF"], calls)

    def test_group_refresh_follows_moves_since_listing(self):
        self.configure_groups(groups={WINDOW_A: [{"id": GROUP_A, "name": "Infra"}]})
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        item = self.manager.entries()[0]
        self.configure_groups(windows={"windows": [{"ref": "window:2"}]},
                              groups={"window:2": [{"id": GROUP_A, "name": "Infra"}]})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.manager.do_reopen(item), 0)
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(self.group_mutations()[-1][-2:], ["--window", "window:2"])

    def test_missing_group_is_recreated_and_reused_on_subsequent_reopen(self):
        self.configure_groups()
        path = self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        original = path.read_bytes()
        self.session([{"id": GROUP_A, "name": "Infra"}])
        first = self.run_cli("reopen", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        creation = [c for c in self.calls() if c[0] == "new-workspace"]
        self.assertEqual(len(creation), 1)
        self.assertNotIn("--window", creation[0])
        self.assertEqual(self.group_mutations(), [["workspace-group", "create", "--name", "Infra",
            "--from", "workspace:99", "--cwd", "/tmp/project", "--json", "--id-format", "uuids"]])
        catalog = json.loads(Path(self.manager.GROUPS).read_text())
        self.assertEqual(catalog["aliases"][GROUP_A], GROUP_C)
        second = self.run_cli("reopen", "1")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stderr, "")
        self.assertEqual(self.group_mutations()[-1], ["workspace-group", "add", "--group", GROUP_C,
            "--workspace", "workspace:99", "--window", WINDOW_A])
        self.assertEqual(sum(c[1] == "create" for c in self.group_mutations()), 1)
        self.assertEqual(path.read_bytes(), original)

    def test_equal_group_names_are_not_identity_and_names_are_argv_values(self):
        marker = self.home / "unexpected"
        name = "Infra 'tools' $(touch " + shlex.quote(str(marker)) + ")"
        self.configure_groups(groups={WINDOW_A: [{"id": GROUP_B, "name": name}]})
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": name}])
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        mutation = self.group_mutations()[0]
        self.assertEqual(mutation[1], "create")
        self.assertEqual(mutation[mutation.index("--name") + 1], name)
        self.assertFalse(marker.exists())

    def test_unknown_group_name_warns_and_invalid_membership_is_skipped(self):
        self.configure_groups()
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.assertIn("group unknown", self.run_cli("--no-interactive").stdout)
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("saved group could not be restored", result.stderr)
        self.assertEqual(self.group_mutations(), [])
        for group_id in (None, "", [], {}, 123, "--help", "workspace_group:1", GROUP_A + "\0"):
            with self.subTest(group_id=group_id):
                self.native([closed(dict(snapshot(), groupId=group_id))])
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
        self.assertEqual(self.group_mutations(), [])

    def test_plain_skips_group_membership_and_window_routing(self):
        self.configure_groups(windows=[{"id": WINDOW_B}],
                              groups={WINDOW_B: [{"id": GROUP_A, "name": "Infra"}]})
        self.native([closed(dict(snapshot(), groupId=GROUP_A, customColor="#ABCDEF"))])
        result = self.run_cli("reopen", "1", "--plain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "OK workspace:99\n")
        self.assertEqual(self.calls()[-1], ["new-workspace", "--name", "Project", "--cwd", "/tmp/project"])
        self.assertEqual(self.group_mutations(), [])

    def test_grafted_agent_and_all_numbering_keep_selected_snapshot_group(self):
        self.configure_groups(groups={WINDOW_A: [
            {"id": GROUP_A, "name": "Old group"}, {"id": GROUP_B, "name": "New group"}]})
        self.native([closed(dict(snapshot("older", agent="claude"), groupId=GROUP_A)),
                     closed(dict(snapshot("newer"), groupId=GROUP_B), 800000100)])
        result = self.run_cli("--all", "reopen", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AI session came from an earlier close", result.stdout)
        self.assertEqual(self.group_mutations()[-1][3], GROUP_B)

    def test_unusable_layout_still_restores_group(self):
        self.configure_groups(groups={WINDOW_A: [{"id": GROUP_A, "name": "Infra"}]})
        self.native([closed(dict(snapshot(), groupId=GROUP_A, layout=None))])
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No usable snapshot", result.stdout)
        self.assertEqual(self.group_mutations()[-1][1], "add")

    def test_failed_creation_and_unexpected_output_never_mutate_groups(self):
        self.configure_groups(create_exit=7)
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": "Infra"}])
        self.assertEqual(self.run_cli("reopen", "1").returncode, 7)
        for output in ("", "OK", "OK current\n", "OK surface:99\n", "OK --help\n",
                       "OK workspace:99\nOK workspace:100\n"):
            with self.subTest(output=output):
                self.configure_groups(create_output=output)
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0)
                self.assertIn("saved group could not be restored", result.stderr)
        self.assertEqual(self.group_mutations(), [])

    def test_group_mutation_failures_keep_reopen_success(self):
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": "Infra"}])
        for subcommand in ("add", "create"):
            with self.subTest(subcommand=subcommand):
                groups = {WINDOW_A: [{"id": GROUP_A, "name": "Infra"}]} if subcommand == "add" else {}
                self.configure_groups(groups=groups, **{"group_" + subcommand + "_exit": 7})
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0)
                self.assertIn("Restored 1 terminal", result.stdout)
                self.assertIn("saved group could not be restored", result.stderr)
        self.assertEqual(sum(c[0] == "new-workspace" for c in self.calls()), 2)
        self.assertFalse(any(c[0].startswith("close-") for c in self.calls()))

    def test_incomplete_inventory_does_not_recreate_but_known_groups_remain_usable(self):
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": "Infra"}])
        self.configure_groups(windows=[{"id": WINDOW_A}, {"id": WINDOW_B}],
                              group_fail_windows=[WINDOW_A],
                              groups={WINDOW_B: [{"id": GROUP_A, "name": "Infra"}]})
        self.assertEqual(self.run_cli("reopen", "1").stderr, "")
        before = len(self.group_mutations())
        cases = ({"group_fail_windows": [WINDOW_A]}, {"windows": [None]},
                 {"group_list_output": "not JSON"}, {"group_list_output": '{"groups": {}}'},
                 {"groups": {WINDOW_A: [None, {"id": "--help"}]}},
                 {"groups": {WINDOW_A: [{"id": GROUP_A}, {"id": GROUP_A}]}})
        for values in cases:
            with self.subTest(values=values):
                self.configure_groups(**values)
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0)
                self.assertIn("saved group could not be restored", result.stderr)
        self.assertEqual(len(self.group_mutations()), before)

    def test_older_cmux_displays_saved_group_and_warns_on_restore(self):
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": "Infra"}])
        self.assertIn("group: Infra", self.run_cli("--no-interactive").stdout)
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("saved group could not be restored", result.stderr)
        self.assertFalse(any(c[0] == "workspace-group" for c in self.calls()))

    def test_group_calls_have_finite_timeouts_and_fail_without_rollback(self):
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": "Infra"}])
        original_run = self.manager.subprocess.run
        for subcommand in ("list", "add", "create"):
            for failure in (FileNotFoundError(), subprocess.TimeoutExpired("cmux", 5)):
                with self.subTest(subcommand=subcommand, failure=type(failure).__name__):
                    groups = {WINDOW_A: [{"id": GROUP_A, "name": "Infra"}]} if subcommand == "add" else {}
                    self.configure_groups(groups=groups)
                    observed = []
                    def run(args, **kwargs):
                        if args[:3] == ["cmux", "workspace-group", subcommand]:
                            observed.append(kwargs["timeout"])
                            raise failure
                        return original_run(args, **kwargs)
                    with mock.patch.object(self.manager.subprocess, "run", side_effect=run), \
                            contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()) as error:
                        self.assertEqual(self.manager.do_reopen(self.manager.entries()[0]), 0)
                    self.assertTrue(observed)
                    self.assertEqual(set(observed), {5})
                    self.assertIn("saved group could not be restored", error.getvalue())
        self.assertFalse(any(c[0].startswith("close-") for c in self.calls()))

    def test_group_creation_timeout_never_attempts_followup_mutation(self):
        self.configure_groups()
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": "Infra"}])
        item = self.manager.entries()[0]
        original_run = self.manager.subprocess.run
        def run(args, **kwargs):
            if args[:2] == ["cmux", "new-workspace"]:
                raise subprocess.TimeoutExpired("cmux", 30, output=b"OK workspace:99\n")
            return original_run(args, **kwargs)
        with mock.patch.object(self.manager.subprocess, "run", side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.manager.do_reopen(item), 1)
        self.assertEqual(output.getvalue(), "OK workspace:99\n")
        self.assertIn("30 seconds", error.getvalue())
        self.assertEqual(self.group_mutations(), [])

    def test_malformed_group_cache_and_sessions_do_not_break_listing(self):
        self.configure_groups()
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        cache = Path(self.manager.GROUPS)
        cache.parent.mkdir(parents=True, exist_ok=True)
        for data in ("bad JSON", "[]", '{"version":1,"groups":[],"aliases":[]}',
                     json.dumps({"version": 1, "groups": {GROUP_A: {"name": [], "ts": "bad"}},
                                 "aliases": {GROUP_A: GROUP_B, GROUP_B: GROUP_A, "bad": []}})):
            with self.subTest(data=data):
                cache.write_text(data)
                path = self.session([None, {"id": "--help", "name": "Invalid"},
                                     {"id": GROUP_A, "name": []}])
                self.assertEqual(self.manager.entries()[0]["group_name"], "")
                path.write_text('{"windows": [{"tabManager": null}, null], "createdAt": []}')
                self.assertEqual(self.manager.entries()[0]["group_name"], "")
        future = '{"version": 2, "groups": {}}'
        cache.write_text(future)
        self.session([{"id": GROUP_A, "name": "Infra"}])
        self.assertEqual(self.manager.entries()[0]["group_name"], "Infra")
        self.assertEqual(cache.read_text(), future)

    def test_catalog_write_failure_keeps_recreated_group_and_workspace(self):
        self.configure_groups()
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": "Infra"}])
        with mock.patch.object(self.manager.os, "replace", side_effect=OSError()), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
            item = self.manager.entries()[0]
            self.assertEqual(item["group_name"], "Infra")
            self.assertEqual(self.manager.do_reopen(item), 0)
        self.assertIn("local group mapping could not be saved", error.getvalue())
        self.assertNotIn("saved group could not be restored", error.getvalue())
        self.assertEqual(len(self.group_mutations()), 1)
        self.assertFalse(list(Path(self.manager.LOG_DIR).glob(".workspace-groups-*")))

    def test_malformed_group_creation_response_never_records_an_alias(self):
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        self.session([{"id": GROUP_A, "name": "Infra"}])
        for response in ("not JSON", "[]", '{"group": null}', '{"group": {"id": "--help"}}',
                         '{"group": {"id": []}}'):
            with self.subTest(response=response):
                self.configure_groups(group_create_output=response)
                result = self.run_cli("reopen", "1")
                self.assertEqual(result.returncode, 0)
                self.assertIn("saved group could not be restored", result.stderr)
                self.assertEqual(json.loads(Path(self.manager.GROUPS).read_text())["aliases"], {})
        self.assertFalse(any(c[0].startswith("close-") for c in self.calls()))

    def test_saved_original_group_wins_over_recreated_mapping(self):
        self.configure_groups(groups={WINDOW_A: [
            {"id": GROUP_A, "name": "Original"}, {"id": GROUP_C, "name": "Recreated"}]})
        self.native([closed(dict(snapshot(), groupId=GROUP_A))])
        cache = Path(self.manager.GROUPS)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"version": 1, "groups": {}, "aliases": {GROUP_A: GROUP_C}}))
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.group_mutations()[-1][3], GROUP_A)

    def test_event_only_close_does_not_infer_membership_from_name_or_directory(self):
        self.configure_groups(groups={WINDOW_A: [{"id": GROUP_A, "name": "Project"}]})
        self.events([{"category": "workspace", "id": "event", "workspace_id": "closed",
                      "name": "workspace.closed", "payload": {"cwd": "/tmp/project", "title": "Project"}}])
        self.assertNotIn("group:", self.run_cli("--no-interactive").stdout)
        result = self.run_cli("reopen", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.group_mutations(), [])


if __name__ == "__main__":
    unittest.main()
