import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import doctor

voice = doctor.voice
COMMIT = "c0ffee" * 6

FAKE_SSH = '''import json, socket, subprocess, sys
args = sys.argv[1:]
open(LOG, "a").write(json.dumps(args) + "\\n")
index = next(i for i, arg in enumerate(args) if arg in ("-nT", "-T"))
host, command = args[index + 1], args[index + 2]
if host == "denied":
    sys.stderr.write("you@denied: Permission denied (publickey).\\n")
    sys.exit(255)
listener = None
if "-R" in args:
    if host == "noforward":
        sys.stderr.write("Error: remote port forwarding failed for listen path /tmp/x\\n")
        sys.exit(255)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(args[args.index("-R") + 1].split(":")[0])
    listener.listen()
# The "remote" computer is this one: run the command the way sshd would.
sys.exit(subprocess.call(["sh", "-c", command], stdin=subprocess.DEVNULL if "-nT" in args else None))
'''


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return path


@unittest.skipUnless(shutil.which("bash"), "needs bash for the shell check")
class DoctorEndToEndTests(unittest.TestCase):
    """Runs the real doctor against fake herdr, codex and ssh executables."""

    def setUp(self):
        # Short paths: Unix socket paths are limited to about 100 bytes.
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="hcvd-")
        self.addCleanup(self.tmp.cleanup)
        root = self.root = Path(self.tmp.name)
        self.home, self.config, self.bin = root / "home", root / "config", root / "bin"
        self.ssh_log, self.codex_log = root / "ssh.log", root / "codex.log"
        self.backend = socket.socket(socket.AF_UNIX)
        self.backend.bind(str(root / "b.sock"))
        self.backend.listen()
        self.addCleanup(self.backend.close)
        script(self.bin / "herdr", f'''import json, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("herdr 0.9.3")
elif args[:2] == ["plugin", "list"]:
    print(json.dumps({{"result": {{"plugins": [{{"enabled": True, "plugin_root": {str(ROOT)!r}}}]}}}}))
elif args[:2] == ["plugin", "config-dir"]:
    print({str(self.config)!r})
else:
    sys.exit(2)
''')
        package = root / "pkg"
        script(package / "bin/codex", f'''import json, sys
open({str(self.codex_log)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.160.0")
elif sys.argv[1:4] == ["app-server", "daemon", "start"]:
    print(json.dumps({{"socketPath": {str(root / "b.sock")!r}}}))
else:
    sys.exit(2)
''')
        script(package / "codex-resources/voice/bin/codex-voice-host", f"print({COMMIT!r})\n")
        (self.bin / "codex").symlink_to(package / "bin/codex")
        script(self.bin / "ssh", f"LOG = {str(self.ssh_log)!r}\n" + FAKE_SSH)
        self.mic_codex = script(root / "mic/codex", "print('codex-cli 0.159.0')\n")
        launcher = self.home / ".local/bin/codex-voice"
        launcher.parent.mkdir(parents=True)
        launcher.write_text((ROOT / "voice.py").read_text())
        voice.integrate(self.home / ".bashrc", launcher)

    def pair(self, name, **mic):
        voice.write_private(voice.named_profile(self.config, name), json.dumps({"label": name, **mic}))

    def unix_mic(self, name, host, **extra):
        mic = dict(host=host, platform="linux", hostname=name.lower(), codex=str(self.mic_codex), node_dir="",
                   codex_home="", python=sys.executable, tailscale_node_id="nSECRET" + name)
        self.pair(name, **{**mic, **extra})

    def windows_mic(self, name, helper_commit=COMMIT):
        helper = script(self.root / f"win/{name}/codex-voice-host.exe", f"print({helper_commit!r})\n")
        launcher = self.home / ".herdr-codex-voice" / f"host-{name}.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(f"HELPER = {str(helper)!r}\nHOST = 'good'\n")
        self.pair(name, host="good", platform="win32", hostname="pc", python=sys.executable,
                  audio={"build_commit": COMMIT, "command": f"{sys.executable} .herdr-codex-voice/host-{name}.py"})

    def doctor(self, *args, **env):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("HERDR_") and key != voice.SHELL_MARKER}
        environment.update(HOME=str(self.home), SHELL=shutil.which("bash"), HERDR_PLUGIN_CONFIG_DIR=str(self.config),
                           PATH=str(self.bin) + os.pathsep + os.environ["PATH"], **env)
        result = subprocess.run([sys.executable, str(ROOT / "doctor.py"), "--json", *args],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, env=environment,
                                timeout=180)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            self.fail(f"doctor printed no JSON (exit {result.returncode}):\n{result.stderr}")
        checks = {(check["scope"], check["check"]): check for check in report["checks"]}
        return result.returncode, report, checks, result.stdout

    def ssh_calls(self):
        return [json.loads(line) for line in self.ssh_log.read_text().splitlines()] if self.ssh_log.exists() else []

    def test_each_microphone_route_is_checked_and_failures_are_specific(self):
        self.unix_mic("Laptop", "good")
        self.unix_mic("Broken", "denied")
        self.windows_mic("PC")
        status, report, checks, text = self.doctor()
        self.assertEqual(status, 1)
        self.assertFalse(report["ok"])
        self.assertIs(report["voice_verified"], False)
        self.assertEqual(report["physical_verification"], voice.PHYSICAL_CHECKS)
        self.assertEqual(report["error"]["code"], "ssh_auth_failed")
        expected = {("work", "launcher"): "ok", ("work", "shell"): "ok", ("work", "backend"): "ok",
                    ("work", "plugin"): "ok", ("microphone:Laptop", "ssh"): "ok",
                    ("microphone:Laptop", "codex"): "warn", ("microphone:Laptop", "forwarding"): "ok",
                    ("microphone:Broken", "ssh"): "fail", ("microphone:PC", "helper"): "ok",
                    ("microphone:PC", "microphone_permission"): "unknown"}
        self.assertEqual({key: checks[key]["status"] for key in expected}, expected)
        self.assertEqual(checks["microphone:Laptop", "codex"]["code"], "codex_version_mismatch")
        self.assertNotIn(("microphone:Broken", "forwarding"), checks)
        self.assertEqual(report["work_host"]["codex"]["build_commit"], COMMIT)
        self.assertEqual(report["work_host"]["herdr"]["version"], "0.9.3")
        self.assertEqual(report["route_selection"]["mode"], "unknown")
        forwarded = [call for call in self.ssh_calls() if "-R" in call]
        self.assertEqual([call[call.index("-nT") + 1] for call in forwarded], ["good"])
        remote_socket = Path(forwarded[0][forwarded[0].index("-R") + 1].split(":")[0])
        self.assertFalse(remote_socket.exists(), "the forwarding check left its socket behind")
        # Pairings are described, never dumped.
        for private in ("nSECRET", str(self.mic_codex), "host-PC.py"):
            self.assertNotIn(private, text)

    def test_offline_checks_make_no_connections(self):
        self.unix_mic("Laptop", "good")
        status, report, checks, _ = self.doctor("--offline")
        self.assertEqual(status, 0, [c for c in report["checks"] if c["status"] == "fail"])
        self.assertTrue(report["ok"])
        self.assertIs(report["voice_verified"], False)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(checks["work", "backend"]["status"], "skip")
        self.assertNotIn("app-server", self.codex_log.read_text())
        self.assertEqual(report["route_selection"]["mode"], "single")

    def test_unfinished_update_is_reported_with_the_repair_command(self):
        (self.home / ".local/bin/codex-voice").write_text("#!/bin/sh\n# herdr-codex-voice 0.1\n")
        status, report, checks, _ = self.doctor("--offline")
        self.assertEqual(status, 1)
        self.assertEqual(checks["work", "launcher"]["code"], "launcher_stale")
        self.assertIn("voice.py", checks["work", "launcher"]["next_step"]["command"])

    def test_no_pairing_is_a_failure_with_an_agent_step(self):
        status, report, checks, _ = self.doctor("--offline")
        self.assertEqual((status, report["error"]["code"]), (1, "no_pairing"))
        self.assertEqual(report["next_steps"][0]["actor"], "agent")

    def test_old_and_reloaded_pane_shells_are_distinguished(self):
        self.unix_mic("Laptop", "good")
        _, _, checks, _ = self.doctor("--offline")
        self.assertEqual(checks["work", "this_shell"]["status"], "unknown")
        _, _, checks, _ = self.doctor("--offline", HERDR_ENV="1")
        self.assertEqual(checks["work", "this_shell"]["code"], "shell_reload_needed")
        self.assertIn("source", checks["work", "this_shell"]["next_step"]["command"])
        _, _, checks, _ = self.doctor("--offline", HERDR_ENV="1", **{voice.SHELL_MARKER: voice.SHELL_REVISION})
        self.assertEqual(checks["work", "this_shell"]["status"], "ok")

    def test_missing_alias_in_new_shells_fails(self):
        self.unix_mic("Laptop", "good")
        voice.integrate(self.home / ".bashrc")
        _, _, checks, _ = self.doctor("--offline")
        self.assertEqual(checks["work", "shell"]["code"], "shell_integration_missing")

    def test_refused_socket_forwarding_is_identified(self):
        self.unix_mic("Locked", "noforward")
        _, report, checks, _ = self.doctor()
        self.assertEqual(checks["microphone:Locked", "ssh"]["status"], "ok")
        self.assertEqual(checks["microphone:Locked", "forwarding"]["code"], "ssh_forwarding_denied")
        self.assertEqual(checks["microphone:Locked", "forwarding"]["next_step"]["actor"], "human")

    def test_windows_helper_from_another_codex_build_fails(self):
        self.windows_mic("PC", helper_commit="f" * 40)
        status, _, checks, _ = self.doctor()
        self.assertEqual(status, 1)
        self.assertEqual(checks["microphone:PC", "helper"]["code"], "windows_helper_build_mismatch")

    def test_mac_permission_comes_from_the_current_desktop_helper(self):
        ran = self.root / "helper-ran"
        helper = script(self.root / "mac/macos.py", f'''import json, sys
open({str(ran)!r}, "w").write(" ".join(sys.argv[1:]))
print("CODEX_VOICE_STATUS=" + json.dumps({{"desktop_session": True, "app_installed": True, "microphone": "denied"}}))
''')
        self.unix_mic("Mac", "good", platform="darwin", desktop_helper=str(helper),
                      desktop_revision=voice.desktop_revision(ROOT))
        _, _, checks, _ = self.doctor()
        self.assertEqual(ran.read_text(), "status")
        permission = checks["microphone:Mac", "microphone_permission"]
        self.assertEqual((permission["status"], permission["code"]), ("fail", "mac_microphone_denied"))
        self.assertEqual(permission["next_step"]["actor"], "human")

    def test_stale_mac_helper_is_not_asked_and_refreshes_at_launch(self):
        ran = self.root / "helper-ran"
        helper = script(self.root / "mac/macos.py", f"open({str(ran)!r}, 'w').write('ran')\n")
        self.unix_mic("Mac", "good", platform="darwin", desktop_helper=str(helper), desktop_revision="old")
        _, _, checks, _ = self.doctor()
        self.assertEqual(checks["microphone:Mac", "desktop_helper"]["code"], "desktop_helper_stale")
        self.assertNotIn(("microphone:Mac", "microphone_permission"), checks)
        self.assertFalse(ran.exists())

    def test_one_microphone_can_be_checked_alone(self):
        self.unix_mic("Laptop", "good")
        self.unix_mic("Broken", "denied")
        status, report, _, _ = self.doctor("--microphone", "Laptop")
        self.assertEqual([m["name"] for m in report["microphones"]], ["Laptop"])
        self.assertNotIn("denied", json.dumps(self.ssh_calls()))

    def test_unknown_option_is_a_usage_error(self):
        result = subprocess.run([sys.executable, str(ROOT / "doctor.py"), "--json", "--bogus"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "usage")


class SelectionTests(unittest.TestCase):
    def choices(self, *mics):
        return [(Path(f"{index}.json"), mic) for index, mic in enumerate(mics)]

    def run_selection(self, choices, capability, select=None, in_pane=False):
        report = doctor.Report()
        with patch.object(doctor.origin, "select", side_effect=select or (lambda c: None)):
            info = doctor.selection(report, choices, capability, in_pane)
        return info, {check["code"]: check for check in report.checks if "code" in check}

    def test_released_herdr_with_several_microphones_asks_at_launch(self):
        info, codes = self.run_selection(self.choices({"host": "a"}, {"host": "b"}), False)
        self.assertEqual(info["mode"], "prompt")
        self.assertIn("pane_last_input", codes["automatic_selection_unavailable"]["message"])

    def test_automatic_selection_flags_unidentifiable_and_duplicate_pairings(self):
        choices = self.choices({"host": "a", "label": "A", "tailscale_node_id": "n1"},
                               {"host": "a2", "label": "A again", "tailscale_node_id": "n1"},
                               {"host": "c", "label": "C"})
        info, codes = self.run_selection(choices, True, lambda c: c[0])
        self.assertEqual(info["mode"], "automatic")
        self.assertEqual(codes["duplicate_identity"]["status"], "fail")
        self.assertEqual(codes["pairing_without_identity"]["status"], "warn")
        self.assertTrue(codes["pairing_without_identity"]["next_step"]["command"].endswith(
            "codex-voice pair C c --json"))

    def test_every_origin_error_is_reported_as_a_stopped_launch(self):
        for code in ("origin_unavailable", "origin_not_typed", "origin_disconnected", "origin_lookup_failed"):
            def stop(choices):
                raise doctor.origin.OriginError(code, "this pane cannot choose")
            info, codes = self.run_selection(self.choices({"local": True}), True, stop, in_pane=True)
            self.assertIsNone(info["selected"])
            self.assertEqual(codes[code]["status"], "fail", code)
            self.assertIn("would stop", codes[code]["message"])

    def test_unreachable_herdr_in_a_pane_fails_even_with_one_pairing(self):
        single = self.choices({"host": "a", "tailscale_node_id": "n1"})
        info, codes = self.run_selection(single, None, in_pane=True)
        self.assertEqual(info["mode"], "error")
        self.assertEqual(codes["origin_lookup_failed"]["status"], "fail")
        # Outside a pane nothing was asked, so one pairing simply starts.
        info, codes = self.run_selection(single, None, in_pane=False)
        self.assertEqual((info["mode"], codes), ("single", {}))


class BrokenCodexTests(unittest.TestCase):
    def test_codex_that_cannot_report_its_version_is_not_green(self):
        report = doctor.Report()
        with patch.object(doctor.shutil, "which", return_value="/bin/false"):
            info = doctor.codex(report, [(Path("local.json"), {"local": True})], True)
        self.assertIsNone(info["version"])
        failed = [c for c in report.checks if c["check"] == "codex"]
        self.assertEqual((failed[0]["status"], failed[0]["code"]), ("fail", "codex_broken"))

    def test_microphone_codex_that_cannot_report_its_version_is_not_green(self):
        mic = {"host": "mic", "platform": "linux", "codex": "/bin/false", "python": sys.executable,
               "node_dir": "", "codex_home": ""}
        output = subprocess.run([sys.executable, "-c", doctor.UNIX_DIAGNOSE, "/bin/false", "", "", ""],
                                capture_output=True, text=True, check=True).stdout
        report = doctor.Report()
        with patch.object(doctor, "remote", return_value=output), patch.object(doctor, "forwarding"):
            doctor.unix_microphone(report, "microphone:Laptop", Path("laptop.json"), mic, "codex-cli 0.160.1")
        codex = [c for c in report.checks if c["check"] == "codex"][0]
        self.assertEqual((codex["status"], codex["code"]), ("fail", "microphone_codex_broken"))


class PermissionTests(unittest.TestCase):
    def test_windows_privacy_summary(self):
        allow = {"device": "Allow", "user": "Allow", "desktop_apps": "Allow", "policy": None}
        self.assertEqual(doctor.audio.privacy_state(allow), "allowed")
        self.assertEqual(doctor.audio.privacy_state({**allow, "desktop_apps": "Deny"}), "denied")
        self.assertEqual(doctor.audio.privacy_state({**allow, "policy": 2}), "denied")
        self.assertEqual(doctor.audio.privacy_state({**allow, "user": None}), "unknown")
        self.assertEqual(doctor.audio.privacy_state(None), "unknown")


if __name__ == "__main__":
    unittest.main()
