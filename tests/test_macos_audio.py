import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import macos
import macos_audio

COMMIT = "c0ffee" * 6
REAL_RUN = subprocess.run


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return path


def fake_app(argv):
    """What the Swift runner does for `open APP --args JOB`, in Python: connect, start, wait."""
    job = Path(argv[-1])
    request = json.loads(job.read_text())

    def app():
        try:
            with socket.socket(socket.AF_UNIX) as stream:
                stream.connect(request["socket"])
                child = subprocess.Popen([request["program"]], stdin=stream, stdout=stream,
                                         stderr=subprocess.DEVNULL, env=request["env"])
            (job.parent / "pids").write_text(f"{os.getpid()} {child.pid} 0")
            (job.parent / "exit").write_text(str(child.wait()))
        except OSError as error:
            (job.parent / "error").write_text(str(error))
            (job.parent / "exit").write_text("1")

    threading.Thread(target=app, daemon=True).start()
    return subprocess.CompletedProcess(argv, 0)


class MacAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = self.root / "environment.json"
        self.helper = script(self.root / "Codex/codex-resources/voice/bin/codex-voice-host", f'''import json, os, sys
if sys.argv[1:] == ["--build-commit"]:
    print({COMMIT!r})
    sys.exit()
open({str(self.environment)!r}, "w").write(json.dumps(dict(os.environ)))
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(data[::-1])
sys.stdout.buffer.flush()
sys.exit(7)
''')
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self.opened = []

    def opener(self, app=fake_app):
        def run(argv, **kwargs):
            if argv[0] != "/usr/bin/open":
                return REAL_RUN(argv, **kwargs)
            self.opened.append(argv)
            return app(argv)
        return run

    def session(self, payload, commit=COMMIT, app=fake_app):
        source_read, source_write = os.pipe()
        sink_read, sink_write = os.pipe()
        received = []

        def feed():
            view = memoryview(payload)
            while view:
                view = view[os.write(source_write, view):]
            os.close(source_write)

        def collect():
            while chunk := os.read(sink_read, 65536):
                received.append(chunk)

        threads = [threading.Thread(target=feed), threading.Thread(target=collect)]
        for thread in threads:
            thread.start()
        try:
            with patch.object(macos_audio, "check_desktop"), patch.object(macos_audio, "BINARY", self.helper), \
                    patch.object(macos_audio.subprocess, "run", side_effect=self.opener(app)), \
                    patch.dict(os.environ, {"SECRET_TOKEN": "do-not-forward"}):
                return macos_audio.run(str(self.helper), commit, source_read, sink_write)
        finally:
            os.close(sink_write)
            for thread in threads:
                thread.join(timeout=30)
            os.close(source_read)
            os.close(sink_read)
            self.received = b"".join(received)

    def test_helper_frames_pass_through_unchanged_with_a_minimal_environment(self):
        payload = bytes(range(256)) * 1024 + b"\r\n\x1a\x00"
        self.assertEqual(self.session(payload), 7)
        self.assertEqual(self.received, payload[::-1])
        environment = json.loads(self.environment.read_text())
        self.assertEqual(environment["GST_REGISTRY"], "/dev/null")
        self.assertEqual(environment["GST_PLUGIN_PATH"], "")
        self.assertNotIn("SECRET_TOKEN", environment)
        self.assertEqual(self.opened[0][:5], ["/usr/bin/open", "-g", "-j", "-n", str(macos_audio.APP)])

    def test_closing_stdin_ends_the_session_without_extra_bytes(self):
        self.assertEqual(self.session(b""), 7)
        self.assertEqual(self.received, b"")

    def test_other_builds_and_other_programs_are_refused_before_launch(self):
        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
            self.session(b"x", commit="f" * 40)
        other = script(self.root / "elsewhere/codex-voice-host", "print('x')\n")
        with patch.object(macos_audio, "check_desktop"):
            with self.assertRaisesRegex(RuntimeError, "not a Codex voice helper"):
                macos_audio.run(str(other), COMMIT)
        self.assertEqual(self.opened, [])

    def test_app_that_never_connects_is_reported(self):
        def broken(argv):
            (Path(argv[-1]).parent / "error").write_text("no microphone session")
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(macos_audio, "START_TIMEOUT", .5):
            with self.assertRaisesRegex(RuntimeError, "did not start.*no microphone session"):
                self.session(b"x", app=broken)

    def test_status_reports_this_apps_own_permission(self):
        for answer, expected in (("3", "authorized"), ("0", "not_determined"), ("2", "denied"), ("?", "unknown")):
            def answering(argv, **kwargs):
                Path(argv[-1]).write_text(answer)
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(macos_audio, "check_desktop"), patch.object(macos_audio, "BINARY", self.helper), \
                    patch.object(macos_audio.subprocess, "run", side_effect=answering) as run:
                self.assertEqual(macos_audio.status()["microphone"], expected)
            self.assertIn("--microphone-status", run.call_args.args[0])
        with patch.object(macos_audio, "check_desktop", side_effect=RuntimeError("log in")), \
                patch.object(macos_audio, "BINARY", self.helper), patch.object(macos_audio.subprocess, "run") as run:
            self.assertEqual(macos_audio.status()["desktop_session"], False)
            run.assert_not_called()

    def test_helpers_lists_every_installed_build(self):
        home = self.root / "codex-home"
        script(home / "packages/standalone/releases/1.0/codex-resources/voice/bin/codex-voice-host", "print('aaa')\n")
        script(home / "packages/standalone/releases/0.9/codex-resources/voice/bin/codex-voice-host",
               "raise SystemExit(1)\n")
        npm = self.root / "lib/node_modules/@openai/codex"
        script(npm / "bin/codex.js", "")
        script(npm / "node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/codex-resources/voice/bin/"
                     "codex-voice-host", "print('ccc')\n")
        (self.root / "bin").mkdir()
        (self.root / "bin/codex").symlink_to(npm / "bin/codex.js")
        found = macos_audio.helpers(str(self.root / "bin/codex"), str(home))
        self.assertEqual(sorted(h["build_commit"] for h in found), ["aaa", "ccc"])
        standalone = macos_audio.helpers(str(self.helper.parents[3] / "bin/codex"), str(home))
        self.assertIn(COMMIT, [h["build_commit"] for h in standalone])

    def test_audio_app_is_separate_from_the_frontend_app(self):
        self.assertNotEqual(macos_audio.INFO["CFBundleIdentifier"], macos.INFO["CFBundleIdentifier"])
        self.assertNotEqual(macos_audio.APP, macos.APP)
        self.assertIn("NSMicrophoneUsageDescription", macos_audio.INFO)


if __name__ == "__main__":
    unittest.main()
