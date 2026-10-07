"""Native Unix startup with a Mac microphone, without opening audio during launch."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import doctor
import voice
import windows_host

COMMIT = "c0ffee" * 6


class UnixMacAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "pkg"
        (self.package / "bin").mkdir(parents=True)
        (self.package / "bin/codex").write_bytes(b"original executable")
        self.helper = self.package / "codex-resources/voice/bin/codex-voice-host"
        self.helper.parent.mkdir(parents=True)
        self.helper.write_bytes(b"original helper")
        self.mic = {"host": "mac", "platform": "darwin", "hostname": "remote-mac",
                    "python": "/usr/bin/python3", "label": "My Mac", "tailscale_node_id": "node1",
                    "mac_audio": {"controller": "/Users/me/with spaces/macos_audio.py",
                                  "helper": "/Applications/Codex/voice/bin/codex-voice-host",
                                  "build_commit": COMMIT,
                                  "revision": hashlib.sha256((ROOT / "macos_audio.py").read_bytes()).hexdigest()}}
        self.path = self.root / "config.json"
        self.path.write_text(json.dumps(self.mic))

    def test_launch_keeps_arguments_local_and_defers_ssh_to_the_helper(self):
        codex = str(self.package / "bin/codex")
        listing = json.dumps({"result": {"plugins": [{"enabled": True, "plugin_root": str(ROOT)}]}})
        args = ["resume", "--last", "-C", "work dir", "--image", "local image.png"]
        with patch.object(voice.sys, "platform", "linux"), \
                patch.dict(os.environ, {"HERDR_SOCKET_PATH": ""}), \
                patch.object(voice, "config_dir", return_value=self.root) as config_dir, \
                patch.object(voice, "run", return_value=listing) as run, \
                patch.object(voice, "backend") as backend, \
                patch.object(voice, "load_module", return_value=windows_host), \
                patch.object(voice.shutil, "which", side_effect=lambda name: codex if name == "codex" else "/bin/ssh"), \
                patch.object(windows_host.audio, "work_package", return_value=(self.package, COMMIT)) as package, \
                patch.object(os, "execv", side_effect=SystemExit) as execute, \
                patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stdout, "isatty", return_value=True):
            with self.assertRaises(SystemExit):
                voice.connect(args)
        backend.assert_not_called()
        config_dir.assert_called_once_with()
        package.assert_called_once_with(codex)
        self.assertEqual(run.call_count, 1, "only Herdr's enabled check runs; SSH waits for voice")
        launched, argv = execute.call_args.args
        self.assertEqual(argv, [launched, *args])
        self.assertNotEqual(launched, codex)
        shim = Path(launched).parent.parent / "codex-resources/voice/bin/codex-voice-host"
        self.assertIn(windows_host.remote_command(self.mic), shim.read_text())
        self.assertIn("ControlPath=none", shim.read_text())
        self.assertEqual(self.helper.read_bytes(), b"original helper")

    def test_refresh_keeps_identity_and_rejects_a_build_changed_during_pairing(self):
        old = {**self.mic, "mac_audio": {**self.mic["mac_audio"], "revision": "old"}}
        def prepare(mic, codex, root):
            mic["mac_audio"] = {**self.mic["mac_audio"], "build_commit": "different-build"}
        with patch.object(voice.sys, "platform", "linux"), \
                patch.object(voice, "load_module", return_value=windows_host), \
                patch.object(windows_host.audio, "work_package", return_value=(self.package, COMMIT)), \
                patch.object(voice, "probe", return_value={"host": "mac", "platform": "darwin"}), \
                patch.object(voice, "prepare_mac_audio", side_effect=prepare), \
                patch.object(windows_host.audio, "run_package") as launch:
            with self.assertRaises(voice.VoiceError) as raised:
                voice.connect_mac_audio(self.path, old, "codex", [], str(ROOT), self.root)
        self.assertEqual(raised.exception.code, "mac_helper_build_mismatch")
        self.assertNotIn("setup", str(raised.exception), "a retry must not overwrite a different default pairing")
        launch.assert_not_called()
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["label"], saved["tailscale_node_id"]), ("My Mac", "node1"))

    def test_json_pairing_prepares_audio_without_frontend_or_windows_build(self):
        def prepare(mic, codex):
            mic["mac_audio"] = self.mic["mac_audio"]
        with patch.object(voice.sys, "platform", "linux"), \
                patch.object(voice, "config_dir", return_value=self.root), \
                patch.object(voice, "probe", return_value={"host": "mac", "platform": "darwin", "hostname": "remote-mac"}), \
                patch.object(voice, "backend") as backend, \
                patch.object(voice, "prepare_desktop") as desktop, \
                patch.object(voice, "load_module") as load, \
                patch.object(voice, "prepare_mac_audio", side_effect=prepare) as audio, \
                patch.object(voice, "pair_identity", return_value="node1"), \
                patch.object(voice.shutil, "which", return_value="/bin/codex"), \
                patch.object(voice, "install", return_value=self.root / "launcher"), \
                patch.object(voice, "shell_files", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            status = voice.main(["pair", "My Mac", "mac", "--audio-only", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["result"]["pairing"]["route"], "mac-audio-helper")
        audio.assert_called_once()
        backend.assert_not_called()
        desktop.assert_not_called()
        load.assert_not_called()
        with patch.object(doctor.sys, "platform", "linux"):
            self.assertIn("--audio-only", doctor.refresh(self.path, self.mic))
            self.assertIn("--audio-only", doctor.refresh(self.root / "named.json", self.mic))

    def test_audio_only_rejects_other_routes_before_installing(self):
        for mic in ({"host": "local", "local": True, "platform": "darwin"},
                    {"host": "pc", "platform": "win32"}):
            with self.subTest(mic=mic), patch.object(voice, "config_dir", return_value=self.root), \
                    patch.object(voice, "probe", return_value=mic), patch.object(voice, "install") as install:
                with self.assertRaises(voice.InputError):
                    voice.setup(mic["host"], audio_only=True)
                install.assert_not_called()

    def test_unix_build_mismatch_remedy_keeps_the_original_pairing_mode(self):
        with patch.object(voice.sys, "platform", "linux"):
            remedy = voice.fix("mac_helper_build_mismatch", "mac")[0]
        self.assertNotIn("--codex", remedy["text"])
        self.assertIn("same name and options", remedy["text"])

    @unittest.skipIf(sys.platform == "win32", "Unix helper is executable via its shebang")
    def test_build_probe_never_connects_and_binary_audio_is_forwarded_unchanged(self):
        marker = self.root / "connected"
        remote = "import pathlib,sys;pathlib.Path(sys.argv[1]).touch();sys.stdout.buffer.write(sys.stdin.buffer.read())"
        executable = windows_host.audio.prepare_package(self.package, COMMIT,
            [sys.executable, "-c", remote, str(marker)], self.root / "copies", build_commit=COMMIT)
        shim = executable.parent.parent / "codex-resources/voice/bin/codex-voice-host"
        result = subprocess.run([str(shim), "--build-commit"], capture_output=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, COMMIT.encode()))
        self.assertFalse(marker.exists())
        result = subprocess.run([str(shim), "unexpected"], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(marker.exists())
        payload = b"\x00\xffbinary\n\x01protocol"
        result = subprocess.run([str(shim)], input=payload, capture_output=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout), (0, payload))
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
