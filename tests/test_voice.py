import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_existing_alias_does_not_break_shell_and_setup_is_repeatable(self):
        for shell in ("bash", "zsh"):
            binary = shutil.which(shell)
            if not binary:
                continue
            rc = self.root / (shell + "rc")
            original = "# user settings\nalias codex='printf original'\n"
            rc.write_text(original)
            launcher = self.root / "launcher ' with spaces"
            launcher.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            launcher.chmod(0o755)
            voice.integrate(rc, launcher)
            first = rc.read_text()
            voice.integrate(rc, launcher)
            self.assertEqual(first, rc.read_text())
            commands = ("shopt -s expand_aliases\n" if shell == "bash" else "") + (
                f"alias codex='printf prior'\n. {shlex.quote(str(rc))}\n"
                f". {shlex.quote(str(rc))}\ncodex resume --last\n")
            result = subprocess.run([binary, "-f"], input=commands, text=True,
                                    capture_output=True, env={**os.environ, "HERDR_ENV": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "run\nresume\n--last\n")
            voice.integrate(rc)
            self.assertEqual(rc.read_text(), original)
            self.assertEqual(rc.with_name(rc.name + ".before-codex-voice").read_text(), original)

    def test_dotfile_symlink_is_preserved_and_outside_herdr_alias_is_unchanged(self):
        target = self.root / "dotfile"
        target.write_text("alias codex='printf original'\n")
        rc = self.root / "rc"
        rc.symlink_to(target)
        voice.integrate(rc, Path("/not/run"))
        self.assertTrue(rc.is_symlink())
        script = "shopt -s expand_aliases\n. " + shlex.quote(str(rc)) + "\ncodex\n"
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                env={k: v for k, v in os.environ.items() if k != "HERDR_ENV"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "original")

    def test_paths_and_prompts_are_data_and_exit_status_and_cleanup_survive(self):
        fake = self.root / "codex ' fake"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n")
        fake.chmod(0o755)
        sentinel = self.root / "injected"
        args = ["--cd", f"/tmp/$(touch {sentinel}) ' directory",
                f"hello; touch {sentinel}", "resume", "--last"]
        mic = dict(host="mic", codex=str(fake), node_dir="", codex_home="")
        command = voice.ssh_command(mic, "/tmp/backend.sock", args)
        remote_socket = command[command.index("-R") + 1].split(":")[0]
        Path(remote_socket).touch()
        for shell in ("sh", "bash", "zsh"):
            binary = shutil.which(shell)
            if not binary:
                continue
            Path(remote_socket).touch()
            result = subprocess.run([binary, "-c", command[-1]], text=True, capture_output=True)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(json.loads(result.stdout)[2:], args)
            self.assertFalse(sentinel.exists())
            self.assertFalse(Path(remote_socket).exists())

    def test_native_subcommands_and_explicit_endpoints_bypass_voice(self):
        for args in (["exec", "--json", "hello"], ["-m", "some-model", "login"],
                     ["--version"], ["--no-daemon"], ["--remote=unix:///tmp/other.sock"], ["app-server", "daemon", "start"]):
            self.assertTrue(voice.local_command(args), args)
        for args in ([], ["resume", "--last"], ["fork"], ["--", "login"],
                     ["-m", "review", "hello"], ["hello --help"]):
            self.assertFalse(voice.local_command(args), args)

    def test_cwd_is_on_work_machine_and_not_duplicated(self):
        self.assertEqual(voice.frontend_args(["resume", "--last"], "/work/repo"),
                         ["--cd", "/work/repo", "resume", "--last"])
        for flags in (["-C", "project"], ["--cd=project"], ["-Cproject"]):
            result = voice.frontend_args(flags, os.getcwd())
            self.assertIn(os.path.abspath("project"), " ".join(result))
            self.assertEqual(len(result), len(flags))
        self.assertEqual(voice.frontend_args(["--", "-Cfake"], "/work"),
                         ["--cd", "/work", "--", "-Cfake"])
        self.assertEqual(voice.frontend_args(["-m", "-Cfake"], "/work"),
                         ["--cd", "/work", "-m", "-Cfake"])
        self.assertEqual(voice.frontend_args(["--add-dir", "lib", "--add-dir=other"], "/work"),
                         ["--cd", "/work", "--add-dir", os.path.abspath("lib"),
                          "--add-dir=" + os.path.abspath("other")])

    def test_bad_host_is_rejected_before_ssh_and_login_noise_is_ignored(self):
        with patch.object(voice, "run") as run:
            for host in ("-oProxyCommand=evil", "host; evil", "name\ncommand", "host with space", ""):
                with self.assertRaises(ValueError):
                    voice.probe(host)
            run.assert_not_called()
            run.return_value = 'login banner\nCODEX_VOICE={"codex":"/bin/codex"}\n'
            self.assertEqual(voice.probe("you@my-tailnet-device")["codex"], "/bin/codex")

    def test_transport_failure_is_not_replaced_by_a_json_error(self):
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            voice.run(["sh", "-c", "echo 'permission denied' >&2; exit 255"])

    def test_local_setup_does_not_require_ssh_to_itself(self):
        with patch.object(voice, 'run', return_value='CODEX_VOICE={"codex":"/bin/codex"}\n') as run:
            mic = voice.probe(None)
            self.assertTrue(mic['local'])
            self.assertEqual(run.call_args.args[0][0], sys.executable)
        mic.update(platform='darwin')
        with patch.object(voice.subprocess, 'run') as execute:
            voice.prepare_desktop(mic)
            execute.assert_not_called()

    def test_local_pairing_survives_hostname_changes_without_connecting_elsewhere(self):
        (self.root / 'config.json').write_text(json.dumps({'local': True, 'hostname': 'old-name'}))
        with patch.object(voice, 'config_dir', return_value=self.root), \
             patch.object(voice, 'run', return_value='{"result":{"plugins":[{"enabled":true}]}}'), \
             patch.object(voice.shutil, 'which', return_value='/bin/codex'), \
             patch.object(sys.stdin, 'isatty', return_value=True), \
             patch.object(sys.stdout, 'isatty', return_value=True), \
             patch.object(os, 'execv', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                voice.connect([])
            execute.assert_called_once_with('/bin/codex', ['/bin/codex'])

    def test_reinstall_refreshes_standalone_launcher(self):
        source = self.root / "source.py"
        for version in ("old", "new"):
            source.write_text(f"#!/usr/bin/env python3\n# herdr-codex-voice\nprint({version!r})\n")
            with patch.object(Path, "home", return_value=self.root), patch.object(voice, "__file__", str(source)):
                launcher = voice.install()
            result = subprocess.run([str(launcher), "run"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), version)

    def test_missing_pairing_does_not_fall_back_to_wrong_microphone(self):
        with patch.object(voice, "config_dir", return_value=self.root), \
             patch.object(voice, "run", return_value='{"result":{"plugins":[{"enabled":true}]}}'), \
             patch.object(voice.shutil, "which", return_value="/bin/codex"), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdout, "isatty", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "setup once"):
                voice.connect([])

    def test_disabled_or_removed_plugin_keeps_native_codex_usable(self):
        for plugins in ([], [{"enabled": False}]):
            with patch.object(voice, "run", return_value=json.dumps({"result": {"plugins": plugins}})), \
                 patch.object(voice.shutil, "which", return_value="/bin/codex"), \
                 patch.object(sys.stdin, "isatty", return_value=True), \
                 patch.object(sys.stdout, "isatty", return_value=True), \
                 patch.object(os, "execv", side_effect=SystemExit) as execute:
                with self.assertRaises(SystemExit):
                    voice.connect(["resume", "--last"])
                execute.assert_called_once_with("/bin/codex", ["/bin/codex", "resume", "--last"])

    def test_other_shells_can_pair_without_an_alias(self):
        mic = {"host": "mic"}
        with patch.object(voice, "probe", return_value=mic), \
             patch.object(voice, "pair_identity", return_value=None), \
             patch.object(voice, "backend"), patch.object(voice, "shell_file", return_value=None), \
             patch.object(voice.shutil, "which", return_value="/bin/codex"), \
             patch.object(voice, "install", return_value=self.root / "codex-voice"), \
             patch.object(voice, "config_dir", return_value=self.root), contextlib.redirect_stdout(io.StringIO()):
            voice.setup("mic")
        self.assertEqual(json.loads((self.root / "config.json").read_text()), mic)

    def test_unsetup_preserves_trailing_newlines_and_later_edits(self):
        for original in ("export A=1", "export A=1\n", "export A=1\n\n\n", ""):
            path = self.root / "rc"
            path.write_text(original)
            voice.integrate(path, Path("/bin/codex-voice"))
            voice.integrate(path)
            self.assertEqual(path.read_text(), original)
        path.write_text("export A=1\n")
        voice.integrate(path, Path("/bin/codex-voice"))
        with path.open("a") as file:
            file.write("export B=2\n")
        voice.integrate(path)
        self.assertEqual(path.read_text(), "export A=1\nexport B=2\n")

    def test_setup_popup_keeps_errors_visible_until_enter(self):
        env = {**os.environ, "HERDR_PLUGIN_ENTRYPOINT_ID": "setup"}
        with subprocess.Popen([sys.executable, voice.__file__, "setup", "-bad"], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) as process:
            line = process.stdout.readline()
            self.assertIn("Codex Voice:", line)
            self.assertIsNone(process.poll())
            process.stdin.write("\n")
            process.stdin.close()
            process.wait(timeout=5)
            output = process.stdout.read()
            self.assertEqual(process.returncode, 1)
            self.assertIn("Press Enter to close.", output)

    def test_ctrl_c_before_frontend_start_removes_socket(self):
        fake = self.root / "codex"
        fake.write_text("#!/usr/bin/env python3\nimport time\nprint('ready',flush=True)\ntime.sleep(10)\n")
        fake.chmod(0o755)
        for shell in ("sh", "bash", "zsh"):
            binary = shutil.which(shell)
            if not binary:
                continue
            mic = dict(host="mic", codex=str(fake), node_dir="", codex_home="")
            command = voice.ssh_command(mic, "/tmp/backend.sock", [])
            remote_socket = Path(command[command.index("-R") + 1].split(":")[0])
            remote_socket.touch()
            with subprocess.Popen([binary, "-c", command[-1]], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
                try:
                    self.assertEqual(process.stdout.readline().strip(), "ready")
                    os.killpg(process.pid, signal.SIGINT)
                    _, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 130, stderr)
                    self.assertFalse(remote_socket.exists())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()


if __name__ == "__main__":
    unittest.main()
import contextlib
import io
