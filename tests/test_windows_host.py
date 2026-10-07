import base64
import codecs
import contextlib
import io
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
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import audio_host
import doctor
import voice
import windows_host

COMMIT = "c0ffee" * 6
# Windows runs both editions; elsewhere PowerShell 7 if installed.
POWERSHELLS = [shell for shell in ("pwsh", "powershell") if shutil.which(shell)]


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return path


class WindowsPackageTests(unittest.TestCase):
    """A Windows Codex package: bin/codex.exe beside codex-resources/voice/bin/codex-voice-host.exe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "pkg"
        # The fake codex.exe starts the helper beside its own physical path, as Codex does.
        script(self.package / "bin/codex.exe", '''import pathlib, subprocess, sys
helper = pathlib.Path(__file__).resolve().parents[1] / "codex-resources/voice/bin/codex-voice-host.exe"
answer = subprocess.run([str(helper), "--build-commit"], capture_output=True, text=True).stdout
pathlib.Path(sys.argv[2]).write_text(answer + "|" + (helper.parent / "codex-voice-host.relay").read_text())
sys.exit(5)
''')
        self.helper = script(self.package / "codex-resources/voice/bin/codex-voice-host.exe", f"print({COMMIT!r})\n")
        (self.package / "codex-resources/voice/bin/runtime.dll").write_text("library")
        self.state = self.root / "state"
        self.ssh = script(self.root / "OpenSSH/ssh.exe", "")
        self.mic = {"host": "mac", "python": "/usr/bin/python3",
                    "mac_audio": {"controller": "/Users/me/.local/share/herdr-codex-voice/helpers/r/macos_audio.py",
                                  "helper": "/Applications/Codex/codex-resources/voice/bin/codex-voice-host",
                                  "build_commit": COMMIT, "revision": "r"}}

    def snapshot(self):
        return {str(p.relative_to(self.package)): (p.stat().st_ino, p.read_bytes())
                for p in self.package.rglob("*") if p.is_file()}

    def relay(self, body):
        return script(windows_host.relay_path(self.state), body)

    def test_exe_layout_is_recognized_with_its_exact_build(self):
        self.assertEqual(audio_host.work_package(str(self.package / "bin/codex.exe")), (self.package.resolve(), COMMIT))

    def test_launch_uses_a_private_copy_whose_helper_is_the_relay(self):
        self.relay("print('relayed build')\n")
        before = self.snapshot()
        out = self.root / "out"
        previous = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        status = windows_host.run_codex(self.mic, str(self.package / "bin/codex.exe"), ["--out", str(out)],
                                        self.state, str(self.ssh), ["-o", "BatchMode=yes"])
        self.assertEqual(status, 5)
        answer, config = out.read_text().split("|", 1)
        self.assertEqual(answer.strip(), "relayed build")
        lines = config.splitlines()
        self.assertEqual(lines[:3], [windows_host.RELAY_MAGIC, "build_commit=" + COMMIT, "ssh=" + str(self.ssh)])
        args = [line[4:] for line in lines if line.startswith("arg=")]
        self.assertEqual(args[:4], ["-o", "BatchMode=yes", "-T", "mac"])
        self.assertEqual(shlex.split(args[4]), [self.mic["python"], self.mic["mac_audio"]["controller"], "run",
                                                self.mic["mac_audio"]["helper"], COMMIT])
        self.assertEqual(self.snapshot(), before, "installed Codex files must not change")

    def test_changed_relay_or_route_gets_a_new_copy_and_old_ones_stay(self):
        self.relay("print(1)\n")
        package = self.package
        first = audio_host.private_copy(package, COMMIT, self.state / "copies",
                                        {"codex-voice-host.exe": b"one", "codex-voice-host.relay": b"a"})
        again = audio_host.private_copy(package, COMMIT, self.state / "copies",
                                        {"codex-voice-host.exe": b"one", "codex-voice-host.relay": b"a"})
        other = audio_host.private_copy(package, COMMIT, self.state / "copies",
                                        {"codex-voice-host.exe": b"one", "codex-voice-host.relay": b"b"})
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertTrue(first.is_file() and other.is_file())
        self.assertEqual(first.name, "codex.exe")
        library = first.parent.parent / "codex-resources/voice/bin/runtime.dll"
        self.assertEqual(library.stat().st_ino,
                         (package / "codex-resources/voice/bin/runtime.dll").stat().st_ino)

    def test_copy_must_replace_the_helper_and_only_plain_names(self):
        for replacements in ({"codex-voice-host.relay": b"x"},
                             {"codex-voice-host.exe": b"x", "../escape": b"x"}):
            with self.assertRaises(ValueError):
                audio_host.private_copy(self.package, COMMIT, self.state / "copies", replacements)

    def test_missing_relay_or_changed_codex_stop_before_launch(self):
        with self.assertRaises(windows_host.WindowsHostError) as raised:
            windows_host.run_codex(self.mic, str(self.package / "bin/codex.exe"), [], self.state, str(self.ssh), [])
        self.assertEqual(raised.exception.code, "windows_relay_missing")
        self.relay("")
        changed = {**self.mic, "mac_audio": {**self.mic["mac_audio"], "build_commit": "f" * 40}}
        with self.assertRaises(windows_host.WindowsHostError) as raised:
            windows_host.run_codex(changed, str(self.package / "bin/codex.exe"), [], self.state, str(self.ssh), [])
        self.assertEqual(raised.exception.code, "mac_helper_build_mismatch")
        self.assertFalse((self.state / "windows-voice-packages").exists())

    def test_launch_checks_the_package_and_configuration_once(self):
        self.relay("print('relay')\n")
        mic = {**self.mic, "platform": "darwin", "hostname": "remote-mac",
               "mac_audio": {**self.mic["mac_audio"], "revision": voice.hashlib.sha256(
                   (ROOT / "macos_audio.py").read_bytes()).hexdigest()}}
        (self.root / "config.json").write_text(json.dumps(mic))
        codex = str(self.package / "bin/codex.exe")
        listing = json.dumps({"result": {"plugins": [{"enabled": True, "plugin_root": str(ROOT)}]}})
        previous = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        with patch.object(voice.sys, "platform", "win32"), \
                patch.object(voice, "config_dir", return_value=self.root) as config_dir, \
                patch.object(voice, "run", return_value=listing), \
                patch.object(voice, "load_module", return_value=windows_host), \
                patch.object(voice.shutil, "which", side_effect=lambda name: codex if name == "codex" else str(self.ssh)), \
                patch.object(windows_host.audio, "work_package", wraps=audio_host.work_package) as work_package, \
                patch.object(windows_host, "relay_path", return_value=windows_host.relay_path(self.state)), \
                patch.object(windows_host.subprocess, "call", return_value=7) as launch, \
                patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stdout, "isatty", return_value=True):
            with self.assertRaises(SystemExit) as raised:
                voice.connect(["resume", "--last"])
        self.assertEqual(raised.exception.code, 7)
        config_dir.assert_called_once_with()
        work_package.assert_called_once_with(codex)
        self.assertEqual(launch.call_args.args[0][1:], ["resume", "--last"])


class RelayConfigTests(unittest.TestCase):
    ssh = str(Path(tempfile.gettempdir()) / "OpenSSH" / "ssh.exe")  # absolute on every OS

    def test_only_allowlisted_environment_is_passed(self):
        text = windows_host.relay_config(COMMIT, self.ssh, ["-T", "mac", "cmd"],
                                         {"USERPROFILE": "C:\\Users\\a b", "OPENAI_API_KEY": "secret", "PATH": "x"})
        self.assertIn("env=USERPROFILE=C:\\Users\\a b", text.splitlines())
        self.assertNotIn("secret", text)
        self.assertNotIn("env=PATH", text)

    def test_values_that_could_break_the_line_format_are_refused(self):
        for commit, ssh, args in ((COMMIT, self.ssh, ["a\nssh=C:\\evil.exe"]), (COMMIT, self.ssh, ["a\r"]),
                                  (COMMIT, str(Path(self.ssh).with_name("cmd.exe")), ["a"]), (COMMIT, "ssh.exe", ["a"]),
                                  ("abc def", self.ssh, ["a"]), (COMMIT, self.ssh, ["a"] * 65)):
            with self.assertRaises(windows_host.WindowsHostError):
                windows_host.relay_config(commit, ssh, args, {})

    def test_helper_choice_requires_the_exact_build(self):
        helpers = [{"path": "/new/codex-voice-host", "build_commit": "f" * 40},
                   {"path": "/old/codex-voice-host", "build_commit": COMMIT}]
        self.assertEqual(windows_host.select_helper("mac", COMMIT, helpers), "/old/codex-voice-host")
        with self.assertRaises(windows_host.WindowsHostError) as raised:
            windows_host.select_helper("mac", "a" * 40, helpers)
        self.assertEqual(raised.exception.code, "mac_helper_build_mismatch")
        self.assertIn("ffffffffffff", str(raised.exception))

    def test_pairing_asks_the_mac_for_its_helpers_and_records_the_route(self):
        mic = {"host": "mac", "python": "/usr/bin/python3", "codex": "/opt/homebrew/bin/codex", "codex_home": ""}
        run = Mock(return_value='motd\nCODEX_VOICE_HELPERS=' + json.dumps(
            [{"path": "/a b/codex-voice-host", "build_commit": COMMIT}]) + "\n")
        with patch.object(windows_host.audio, "work_package", return_value=(Path("pkg"), COMMIT)):
            windows_host.pair(mic, "codex.exe", "/ctl/macos_audio.py", "rev", ["ssh"], run)
        self.assertEqual(mic["mac_audio"], {"controller": "/ctl/macos_audio.py", "revision": "rev",
                                            "build_commit": COMMIT, "helper": "/a b/codex-voice-host"})
        self.assertEqual(shlex.split(run.call_args.args[0][-1]),
                         ["/usr/bin/python3", "/ctl/macos_audio.py", "helpers", "/opt/homebrew/bin/codex", ""])


@unittest.skipUnless(shutil.which("node"), "needs Node like the npm wrapper")
class NpmShimTests(unittest.TestCase):
    def test_cmd_shim_resolves_to_the_native_package_without_running_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        npm = Path(tmp.name) / "npm"
        (npm / "codex.cmd").parent.mkdir(parents=True)
        (npm / "codex.cmd").write_text("@echo off\r\nexit /b 99\r\n")
        package = npm / "node_modules/@openai/codex"
        script(package / "bin/codex.js", "")
        (package / "package.json").write_text(json.dumps({"name": "@openai/codex"}))
        info = json.loads(subprocess.run(["node", "-p", "JSON.stringify([process.platform, process.arch])"],
                                         capture_output=True, text=True, check=True).stdout)
        triple = audio_host.TRIPLES[tuple(info)]
        name = "codex.exe" if info[0] == "win32" else "codex"
        vendor = package / "vendor" / triple
        script(vendor / "bin" / name, "")
        script(vendor / audio_host.helper_of(name), f"print({COMMIT!r})\n")
        self.assertEqual(audio_host.work_package(str(npm / "codex.cmd")), (vendor.resolve(), COMMIT))


class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_npm_shims_run_their_script_with_node_never_through_a_shell(self):
        npm = self.root / "npm"
        npm.mkdir()
        (npm / "codex.cmd").write_text("")
        (npm / "node.exe").write_text("")
        with patch.object(voice.sys, "platform", "win32"):
            with self.assertRaises(voice.VoiceError) as raised:
                voice.native(str(npm / "codex.cmd"))
            self.assertEqual(raised.exception.code, "codex_missing")
            script(npm / "node_modules/@openai/codex/bin/codex.js", "")
            self.assertEqual(voice.native(str(npm / "codex.cmd")),
                             [str(npm / "node.exe"), str(npm / "node_modules/@openai/codex/bin/codex.js")])
            self.assertEqual(voice.native(str(npm / "codex.exe")), [str(npm / "codex.exe")])

    def test_windows_waits_for_codex_and_passes_on_its_status(self):
        previous = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        with patch.object(voice.sys, "platform", "win32"), \
                patch.object(voice.subprocess, "call", return_value=3) as call, \
                patch.object(voice.os, "execv") as execv:
            with self.assertRaises(SystemExit) as raised:
                voice.become(["codex.exe", "resume"])
        self.assertEqual(raised.exception.code, 3)
        call.assert_called_once_with(["codex.exe", "resume"])
        execv.assert_not_called()
        self.assertIs(signal.getsignal(signal.SIGINT), signal.SIG_IGN)

    def test_elevated_sessions_are_refused_with_a_clear_code(self):
        with patch.object(windows_host, "elevated", return_value=True):
            with self.assertRaises(windows_host.WindowsHostError) as raised:
                windows_host.require_desktop_user()
        self.assertEqual(raised.exception.code, "windows_elevated")
        with patch.object(windows_host, "elevated", return_value=False):
            windows_host.require_desktop_user()

    def test_explicit_codex_is_kept_for_the_windows_route_only(self):
        chosen = script(self.root / "Codex 0.160.1/bin/codex.exe", "")
        _, prepare, _ = self.setup_on_windows({"host": "mac", "platform": "darwin", "hostname": "mac"},
                                              codex=str(chosen))
        self.assertEqual(prepare.call_args.args[1], str(chosen))
        saved = json.loads(voice.named_profile(self.root, "Mac").read_text())
        self.assertEqual(saved["work_codex"], str(chosen))
        if sys.platform != "win32":  # Elsewhere the option has no meaning and is refused.
            with patch.object(voice, "config_dir", return_value=self.root), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                status = voice.main(["pair", "Mac", "mac", "--codex", str(chosen), "--json"])
            self.assertEqual((status, json.loads(output.getvalue())["error"]["code"]), (2, "usage"))

    def setup_on_windows(self, mic, codex=None):
        windows = Mock()
        with patch.object(voice.sys, "platform", "win32"), \
                patch.object(voice, "probe", return_value=dict(mic)), \
                patch.object(voice, "config_dir", return_value=self.root), \
                patch.object(voice.shutil, "which", return_value="C:/npm/codex.cmd"), \
                patch.object(voice, "load_module", return_value=windows), \
                patch.object(voice, "prepare_mac_audio") as prepare, \
                patch.object(voice, "backend") as backend, \
                patch.object(voice, "prepare_desktop") as desktop, \
                patch.object(voice, "pair_identity", return_value=None), \
                patch.object(voice, "shell_files", return_value=[]), \
                patch.object(voice, "install", return_value=self.root / "codex-voice"):
            result = voice.setup("mac", "Mac", codex)
        backend.assert_not_called()
        desktop.assert_not_called()
        return result, prepare, windows

    def test_windows_setup_pairs_only_a_mac_without_the_unix_backend(self):
        result, prepare, windows = self.setup_on_windows({"host": "mac", "platform": "darwin", "hostname": "mac"})
        windows.build_relay.assert_called_once_with(self.root)
        prepare.assert_called_once()
        with self.assertRaises(voice.VoiceError) as raised:
            self.setup_on_windows({"host": "box", "platform": "linux", "hostname": "box"})
        self.assertEqual(raised.exception.code, "unsupported_route")

    def test_windows_launch_refreshes_the_mac_side_when_codex_or_plugin_changed(self):
        path = self.root / "pairing.json"
        mic = {"host": "mac", "label": "Mac", "tailscale_node_id": "n1", "platform": "darwin",
               "mac_audio": {"build_commit": COMMIT, "revision": "old"}}
        windows = Mock()
        windows.audio.work_package.return_value = (self.root, COMMIT)
        windows.run_codex.return_value = 4
        with patch.object(voice.sys, "platform", "win32"), \
                patch.object(voice, "load_module", return_value=windows), \
                patch.object(voice.shutil, "which", return_value="/bin/ssh"), \
                patch.object(voice, "probe", return_value={"host": "mac", "platform": "darwin"}) as probe, \
                patch.object(voice, "prepare_mac_audio") as prepare:
            with self.assertRaises(SystemExit) as raised:
                voice.connect_mac_audio(path, mic, "codex.cmd", ["resume"], str(ROOT), self.root)
        self.assertEqual(raised.exception.code, 4)
        probe.assert_called_once()
        prepare.assert_called_once()
        windows.audio.work_package.assert_called_once_with("codex.cmd")
        self.assertEqual(windows.run_codex.call_args.kwargs["package"], (self.root, COMMIT))
        saved = json.loads(path.read_text())
        self.assertEqual((saved["label"], saved["tailscale_node_id"]), ("Mac", "n1"))
        with self.assertRaises(voice.VoiceError) as raised:
            voice.connect_mac_audio(path, {"host": "box", "platform": "linux"}, "codex.cmd", [], str(ROOT), self.root)
        self.assertEqual(raised.exception.code, "unsupported_route")

    @unittest.skipUnless(shutil.which("pwsh"), "needs PowerShell")
    def test_windows_next_steps_run_as_written_in_powershell(self):
        launcher = script(self.root / "it's here/codex-voice", "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        with patch.object(voice.sys, "platform", "win32"), patch.object(voice, "launcher_path", return_value=launcher):
            command = voice.step("agent", "Pair it.", "codex-voice pair 'My Mac' mac --json")["command"]
        result = subprocess.run(["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(result.stdout), ["pair", "My Mac", "mac", "--json"], result.stderr)

    def powershell(self, shell, profile, command, herdr=True):
        """Dot-source `profile` in a fresh session, as PowerShell does at startup, then run `command`."""
        env = {k: v for k, v in os.environ.items() if k not in ("HERDR_ENV", voice.SHELL_MARKER)}
        if herdr:
            env["HERDR_ENV"] = "1"
        return subprocess.run([shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                               f". {voice.ps_quote(profile)}; {command}"],
                              capture_output=True, text=True, errors="replace", env=env, timeout=60)

    @unittest.skipUnless(POWERSHELLS, "needs PowerShell")
    def test_powershell_profile_defines_codex_only_in_herdr_panes(self):
        launcher = script(self.root / "it's here/codex-voice", "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        profile = self.root / "profile.ps1"
        # A user's own codex alias: Herdr panes get the launcher, other sessions keep the alias.
        original = "$env:KEEP = 'yes'\nSet-Alias codex Write-Output\n"
        profile.write_text(original)
        voice.integrate(profile, launcher)
        voice.integrate(profile, launcher)
        for shell in POWERSHELLS:
            result = self.powershell(shell, profile, "codex resume --last 'two words'")
            self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]),
                             ["run", "resume", "--last", "two words"], result.stderr)
            result = self.powershell(shell, profile, "codex outside", herdr=False)
            self.assertEqual(result.stdout.strip(), "outside", result.stderr)
            result = self.powershell(shell, profile, doctor.RESOLVE_CODEX)
            self.assertEqual(doctor.resolve_codex(result.stdout, str(launcher)), (True, None), result.stderr)
        voice.integrate(profile)
        self.assertEqual(profile.read_text(), original)

    @unittest.skipUnless(POWERSHELLS, "needs PowerShell")
    def test_doctor_resolves_what_plain_codex_runs_after_the_profile(self):
        launcher = script(self.root / "codex-voice", "print('launcher')\n")
        profile = self.root / "profile.ps1"
        voice.integrate(profile, launcher)
        block = profile.read_bytes()
        for later, kind in (("Set-Alias codex Write-Output", "Alias"),
                            ("function global:codex { 'other' }", "Function")):
            profile.write_bytes(block + later.encode() + b"\n")
            for shell in POWERSHELLS:
                result = self.powershell(shell, profile, doctor.RESOLVE_CODEX)
                self.assertEqual(doctor.resolve_codex(result.stdout, str(launcher)), (True, kind), result.stderr)
            # Rerunning setup moves the block after the later definition.
            voice.integrate(profile, launcher)
            for shell in POWERSHELLS:
                result = self.powershell(shell, profile, doctor.RESOLVE_CODEX)
                self.assertEqual(doctor.resolve_codex(result.stdout, str(launcher)), (True, None), result.stderr)
        # As when an execution policy refuses the profile: the block never runs.
        profile.write_bytes(b"")
        for shell in POWERSHELLS:
            result = self.powershell(shell, profile, doctor.RESOLVE_CODEX)
            self.assertIs(doctor.resolve_codex(result.stdout, str(launcher))[0], False, result.stderr)
        self.assertEqual(doctor.resolve_codex("profile noise\n", str(launcher)), (None, None))

    @unittest.skipUnless(POWERSHELLS, "needs PowerShell")
    def test_utf16_profile_keeps_bom_encoding_newlines_and_runs_unicode_paths(self):
        launcher = script(self.root / "Usuário é €/codex-voice", "import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
        profile = self.root / "Microsoft.PowerShell_profile.ps1"
        # What Windows PowerShell 5.1's `Out-File` and `>` write: UTF-16 LE with a BOM, CRLF.
        original = codecs.BOM_UTF16_LE + "$env:KEEP = 'café'\r\n".encode("utf-16-le")
        profile.write_bytes(original)
        self.assertTrue(voice.integrate(profile, launcher))
        installed = profile.read_bytes()
        self.assertFalse(voice.integrate(profile, launcher))
        self.assertEqual(profile.read_bytes(), installed)
        self.assertTrue(installed.startswith(original))
        text = installed[2:].decode("utf-16-le")
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertEqual(text.count(voice.START), 1)
        self.assertTrue(voice.integration_block(profile).isascii())
        for shell in POWERSHELLS:
            result = self.powershell(shell, profile, "$env:KEEP -eq ('caf' + [char]0xe9); codex x")
            self.assertEqual(result.stdout.split(), ["True", '["run",', '"x"]'], result.stderr)
            result = self.powershell(shell, profile, doctor.RESOLVE_CODEX)
            self.assertEqual(doctor.resolve_codex(result.stdout, str(launcher)), (True, None), result.stderr)
        backup = profile.with_name(profile.name + ".before-codex-voice")
        voice.integrate(profile)
        self.assertEqual(profile.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), original)
        voice.integrate(profile, launcher)
        voice.integrate(profile)
        self.assertEqual((profile.read_bytes(), backup.read_bytes()), (original, original))

    def test_profile_encodings_round_trip_without_powershell(self):
        launcher = self.root / "ü/codex-voice"
        cases = {"utf8-bom.ps1": codecs.BOM_UTF8 + "# ü\r\n".encode(),
                 "utf16-be.ps1": codecs.BOM_UTF16_BE + "# ü\n".encode("utf-16-be"),
                 "utf32.ps1": codecs.BOM_UTF32_LE + "# ü\n".encode("utf-32-le"),
                 # BOM-less ANSI (cp1252), as Windows PowerShell 5.1 reads it: bytes stay as they are.
                 "ansi.ps1": "# ü\r\n".encode("cp1252"),
                 "rc": "export A='ü'\n".encode()}
        for name, original in cases.items():
            path = self.root / name
            path.write_bytes(original)
            voice.integrate(path, launcher)
            self.assertTrue(path.read_bytes().startswith(original), name)
            self.assertIn(str(launcher) if name == "rc" else voice.powershell_function(launcher),
                          voice.integration_block(path), name)
            voice.integrate(path)
            self.assertEqual(path.read_bytes(), original, name)
        new = self.root / "new/profile.ps1"
        voice.integrate(new, launcher)
        data = new.read_bytes()
        self.assertTrue(data.startswith(codecs.BOM_UTF8 + b"\n" + voice.START.encode()))
        self.assertTrue(data[len(codecs.BOM_UTF8):].isascii())

    def test_powershell_profile_path_survives_a_non_ascii_user_folder(self):
        folder = "C:\\Users\\José Ünïcode\\Documents\\PowerShell\\Microsoft.PowerShell_profile.ps1"
        answer = subprocess.CompletedProcess([], 0, base64.b64encode(folder.encode()).decode() + "\r\n", "")
        with patch.object(voice.shutil, "which", side_effect=lambda name: "C:/ps/" + name + ".exe"), \
                patch.object(voice.subprocess, "run", return_value=answer) as run:
            profiles = voice.powershell_profiles()
        self.assertEqual([str(path) for _, path in profiles], [folder, folder])
        self.assertIn("CurrentUserCurrentHost", run.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
