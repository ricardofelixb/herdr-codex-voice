import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import voice


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return path


class AgentCliTests(unittest.TestCase):
    """The --json contract agents rely on: one JSON document, stable codes, no prompts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config"
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def cli(self, *args):
        env = {**os.environ, "HERDR_PLUGIN_CONFIG_DIR": str(self.config), "HOME": str(self.root / "home"),
               "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
        env.pop("HERDR_PLUGIN_ENTRYPOINT_ID", None)
        result = subprocess.run([sys.executable, str(ROOT / "voice.py"), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, env=env, timeout=60)
        try:
            return result.returncode, json.loads(result.stdout)
        except ValueError:
            self.fail(f"no JSON document on stdout (exit {result.returncode}):\n{result.stderr}")

    def fake_ssh(self, stderr, status=255):
        log = self.root / "ssh.log"
        script(self.bin / "ssh", f"import sys\nopen({str(log)!r}, 'a').write(repr(sys.argv[1:]) + '\\n')\n"
                                 f"sys.stderr.write({stderr!r})\nsys.exit({status})\n")
        return log

    def test_missing_values_are_usage_errors_without_prompting(self):
        for args in (["setup", "--json"], ["pair", "OnlyName", "--json"], ["unpair", "--json"],
                     ["unsetup", "--surprise", "--json"]):
            status, report = self.cli(*args)
            self.assertEqual(status, 2, args)
            self.assertEqual(report["schema"], voice.SCHEMA)
            self.assertFalse(report["ok"])
            self.assertEqual(report["error"]["code"], "usage")

    def test_invalid_input_is_reported_before_any_connection(self):
        log = self.fake_ssh("unexpected")
        for args, code in ((["setup", "-oProxyCommand=x", "--json"], "invalid_host"),
                           (["pair", "../escape", "host", "--json"], "invalid_name")):
            status, report = self.cli(*args)
            self.assertEqual((status, report["error"]["code"]), (1, code))
        self.assertFalse(log.exists())

    def test_ssh_failures_have_distinct_codes_and_skip_windows_retries(self):
        # SSH repairs are agent work; trusting an unverified host key is the person's decision.
        cases = (("you@mic: Permission denied (publickey).\n", "ssh_auth_failed",
                  "ssh -o BatchMode=yes mic true", "agent"),
                 ("Host key verification failed.\n", "ssh_host_key_unknown", "ssh mic true", "human"),
                 ("ssh: Could not resolve hostname mic: Name or service not known\n", "ssh_host_unresolved",
                  None, "agent"),
                 ("ssh: connect to host mic port 22: Connection refused\n", "ssh_unreachable", None, "agent"))
        for stderr, code, command, actor in cases:
            log = self.fake_ssh(stderr)
            if log.exists():
                log.unlink()
            status, report = self.cli("setup", "mic", "--json")
            self.assertEqual((status, report["error"]["code"]), (1, code), stderr)
            self.assertEqual(len(log.read_text().splitlines()), 1, "a Windows probe cannot fix SSH")
            self.assertEqual(report["next_steps"][0]["actor"], actor)
            if command:
                self.assertEqual(report["next_steps"][0]["command"], command)
        self.assertFalse(self.config.exists() and any(self.config.rglob("*.json")))

    def test_unix_host_without_codex_is_identified_without_windows_retries(self):
        log = self.fake_ssh("Install Codex CLI on the microphone computer first\n", 1)
        status, report = self.cli("setup", "mic", "--json")
        self.assertEqual((status, report["error"]["code"]), (1, "microphone_codex_missing"))
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def setup_patches(self, mic, rc):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(voice, "probe", side_effect=lambda host: dict(mic)))
        stack.enter_context(patch.object(voice, "pair_identity", return_value="nSECRETNODE"))
        stack.enter_context(patch.object(voice, "backend"))
        stack.enter_context(patch.object(voice, "shell_file", return_value=rc))
        stack.enter_context(patch.object(voice.shutil, "which", return_value="/bin/codex"))
        stack.enter_context(patch.object(voice, "install", return_value=self.root / "codex-voice"))
        stack.enter_context(patch.object(voice, "config_dir", return_value=self.config))
        return stack

    def json_main(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = voice.main([*args, "--json"])
        return status, json.loads(output.getvalue()), output.getvalue()

    def test_setup_reports_route_and_human_checks_without_private_details(self):
        mic = {"host": "mic", "platform": "darwin", "hostname": "laptop", "codex": "/secret/bin/codex",
               "codex_home": "/secret/codex-home", "python": "/secret/python3", "node_dir": "/secret/node"}
        rc = self.root / "rc"
        with self.setup_patches(mic, rc), patch.object(voice, "prepare_desktop"):
            status, report, text = self.json_main("pair", "My Mac", "mic")
            again = self.json_main("pair", "My Mac", "mic")[1]
        self.assertEqual(status, 0)
        self.assertTrue(report["ok"])
        pairing = report["result"]["pairing"]
        self.assertEqual((pairing["name"], pairing["route"], pairing["support"]),
                         ("My Mac", "mac-desktop-frontend", "supported"))
        self.assertTrue(pairing["automatic_identity"])
        self.assertNotIn("/secret", text)
        self.assertNotIn("nSECRETNODE", text)
        self.assertTrue(report["result"]["shell"]["changed"])
        self.assertFalse(again["result"]["shell"]["changed"], "setup must be idempotent")
        actors = [step["actor"] for step in report["next_steps"]]
        # Written with the launcher's full path, so an agent can run it as is.
        self.assertEqual(shlex.split(report["next_steps"][0]["command"]),
                         [str(voice.launcher_path()), "doctor", "--json"])
        self.assertGreaterEqual(actors.count("human"), len(voice.PHYSICAL_CHECKS))

    def test_local_windows_pairing_never_prepares_a_remote_helper(self):
        mic = {"host": "local", "local": True, "platform": "win32", "hostname": "pc"}
        with self.setup_patches(mic, None), patch.object(voice, "prepare_windows") as prepare, \
                contextlib.redirect_stdout(io.StringIO()):
            voice.setup("--local")
        prepare.assert_not_called()

    def test_unpair_is_idempotent(self):
        voice.write_private(voice.named_profile(self.config, "PC"), json.dumps({"host": "pc", "label": "PC"}))
        with patch.object(voice, "config_dir", return_value=self.config):
            first = self.json_main("unpair", "PC")[1]
            second = self.json_main("unpair", "PC")[1]
        self.assertEqual((first["ok"], first["result"]["removed"]), (True, True))
        self.assertEqual((second["ok"], second["result"]["removed"]), (True, False))
        self.assertEqual(list((self.config / "microphones").glob("*.json")), [])

    def test_purge_removes_only_this_plugins_files(self):
        home = self.root / "home"
        rc = home / ".bashrc"
        rc.parent.mkdir(parents=True)
        rc.write_text("export KEEP=1\n")
        launcher = home / ".local/bin/codex-voice"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("# herdr-codex-voice\n")
        voice.integrate(rc, launcher)
        voice.write_private(self.config / "config.json", "{}")
        voice.write_private(voice.named_profile(self.config, "PC"), "{}")
        packages = self.config / "windows-voice-packages" / "abc"
        packages.mkdir(parents=True)
        (packages / "codex").write_text("copy")
        (self.config / "unrelated.txt").write_text("keep")
        with patch.object(Path, "home", return_value=home), patch.object(voice, "shell_file", return_value=rc), \
                patch.object(voice, "config_dir", return_value=self.config):
            status, report, _ = self.json_main("unsetup", "--purge")
        self.assertEqual(status, 0)
        self.assertEqual(rc.read_text(), "export KEEP=1\n")
        self.assertFalse(launcher.exists())
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), ["unrelated.txt"])
        self.assertIn("herdr plugin uninstall herdr-codex-voice", [s.get("command") for s in report["next_steps"]])

    def test_purge_keeps_a_launcher_owned_by_another_program(self):
        home = self.root / "home"
        launcher = home / ".local/bin/codex-voice"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\necho someone else\n")
        with patch.object(Path, "home", return_value=home), patch.object(voice, "shell_file", return_value=None), \
                patch.object(voice, "config_dir", return_value=self.config):
            self.json_main("unsetup", "--purge")
        self.assertTrue(launcher.exists())

    def test_unreadable_plugin_state_never_falls_back_to_this_computers_microphone(self):
        for response in (RuntimeError("Herdr server is not running"), '{"result": {}}', "not json"):
            answer = response if isinstance(response, Exception) else (lambda *args, reply=response, **kw: reply)
            with patch.object(voice, "run", side_effect=answer), \
                    patch.object(voice.shutil, "which", return_value="/bin/codex"), \
                    patch.object(sys.stdin, "isatty", return_value=True), \
                    patch.object(sys.stdout, "isatty", return_value=True), \
                    patch.object(os, "execv", side_effect=SystemExit) as execute:
                with self.assertRaises(voice.VoiceError) as raised:
                    voice.connect(["resume", "--last"])
            self.assertEqual(raised.exception.code, "herdr_unavailable")
            self.assertIn("command codex", str(raised.exception))
            execute.assert_not_called()

    def test_shell_block_marks_only_herdr_shells_that_loaded_it(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("needs bash")
        rc = self.root / "rc"
        voice.integrate(rc, Path("/opt/codex-voice"))
        probe = f'. "{rc}"; printf %s "${{{voice.SHELL_MARKER}-unset}}"'
        for herdr, expected in (("1", voice.SHELL_REVISION), (None, "unset")):
            env = {k: v for k, v in os.environ.items() if k not in ("HERDR_ENV", voice.SHELL_MARKER)}
            if herdr:
                env["HERDR_ENV"] = herdr
            result = subprocess.run([bash, "-c", probe], capture_output=True, text=True, env=env)
            self.assertEqual(result.stdout, expected)

    def test_launcher_and_manifest_versions_match(self):
        manifest = (ROOT / "herdr-plugin.toml").read_text()
        self.assertEqual(re.search(r'^version = "([^"]+)"', manifest, re.M).group(1), voice.VERSION)


if __name__ == "__main__":
    unittest.main()
