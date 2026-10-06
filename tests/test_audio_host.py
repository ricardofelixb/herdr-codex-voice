import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audio_host
import voice

COMMIT = "c0ffee" * 6


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return path


class AudioHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.package = self.root / "pkg"
        script(self.package / "bin/codex", "print('codex')\n")
        (self.package / "codex").symlink_to("bin/codex")
        (self.package / "codex-package.json").write_text("{}")
        self.helper = script(self.package / audio_host.HELPER, f"print({COMMIT!r})\n")
        self.cache = self.root / "cache"

    def snapshot(self):
        return {str(p.relative_to(self.package)): (p.stat().st_ino, p.stat().st_mode, p.read_bytes())
                for p in self.package.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_package_copy_never_touches_installed_files(self):
        before = self.snapshot()
        exe = audio_host.prepare_package(self.package, COMMIT, ["/usr/bin/ssh", "host"], self.cache)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(exe.is_file() and not exe.is_symlink())
        shim = exe.parent.parent / audio_host.HELPER
        self.assertNotEqual(shim.stat().st_ino, self.helper.stat().st_ino)
        self.assertIn("/usr/bin/ssh", shim.read_text())
        self.assertEqual((exe.parent.parent / "codex-package.json").stat().st_ino,
                         (self.package / "codex-package.json").stat().st_ino)

    def test_failed_prepare_publishes_nothing_and_keeps_original(self):
        (self.package / "bin/codex").unlink()
        (self.package / "bin/codex").symlink_to("../codex-package.json")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "layout"):
            audio_host.prepare_package(self.package, COMMIT, ["ssh"], self.cache)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_symlinked_helper_directory_is_rejected_before_any_write(self):
        shared = self.root / "shared"
        (self.package / "codex-resources").rename(shared)
        (self.package / "codex-resources").symlink_to(shared)
        original = (shared / "voice/bin/codex-voice-host").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "layout"):
            audio_host.prepare_package(self.package, COMMIT, ["ssh"], self.cache)
        self.assertEqual((shared / "voice/bin/codex-voice-host").read_bytes(), original)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_any_changed_bundled_resource_refreshes_cache(self):
        rg = script(self.package / "codex-path/rg", "print(1)\n")
        first = audio_host.prepare_package(self.package, COMMIT, ["ssh"], self.cache)
        rg.unlink()
        script(rg, "print(2)\n")
        second = audio_host.prepare_package(self.package, COMMIT, ["ssh"], self.cache)
        self.assertNotEqual(first, second)
        self.assertIn("2", (second.parent.parent / "codex-path/rg").read_text())

    def test_content_addressed_cache_is_idempotent_and_refreshes(self):
        first = audio_host.prepare_package(self.package, COMMIT, ["ssh", "host"], self.cache)
        self.assertEqual(first, audio_host.prepare_package(self.package, COMMIT, ["ssh", "host"], self.cache))
        self.assertNotEqual(first, audio_host.prepare_package(self.package, COMMIT, ["ssh", "other"], self.cache))
        self.helper.write_text(self.helper.read_text() + "# newer build\n")
        self.assertNotEqual(first, audio_host.prepare_package(self.package, COMMIT, ["ssh", "host"], self.cache))

    def test_user_args_are_forwarded_unchanged(self):
        codex = self.root / "bin" / "codex"
        codex.parent.mkdir()
        codex.symlink_to(self.package / "bin/codex")
        mic = {"host": "pc", "audio": {"build_commit": COMMIT, "command": "py -3 .herdr-codex-voice/host-x.py"}}
        args = ["resume", "--last", "-C", "/work/a b", "--", "-weird"]
        with patch.object(os, "execv") as execute:
            audio_host.run_codex(mic, str(codex), args, ["/usr/bin/ssh"], self.cache)
        exe = execute.call_args.args[0]
        self.assertEqual(execute.call_args.args[1], [exe, *args])
        self.assertTrue(exe.startswith(str(self.cache)))
        shim = Path(exe).parent.parent / audio_host.HELPER
        self.assertIn("'py -3 .herdr-codex-voice/host-x.py'", shim.read_text())
        mic["audio"]["build_commit"] = "other"
        with self.assertRaisesRegex(RuntimeError, "rerun codex-voice setup pc"):
            audio_host.run_codex(mic, str(codex), args, ["/usr/bin/ssh"], self.cache)

    def test_unsupported_layout_fails_clearly(self):
        npm = self.root / "npm/bin/codex.js"
        script(npm, "")
        with self.assertRaisesRegex(RuntimeError, "not supported"):
            audio_host.work_package(str(npm))

    def node_target(self):
        info = subprocess.run(["node", "-p", "JSON.stringify([process.platform, process.arch])"],
                              capture_output=True, text=True, check=True).stdout
        return audio_host.TRIPLES[tuple(json.loads(info))], "codex-" + "-".join(json.loads(info))

    def npm_install(self, official=True):
        root = self.root / "lib/node_modules/@openai/codex"
        script(root / "bin/codex.js", "")
        (root / "package.json").write_text(json.dumps({"name": "@openai/codex" if official else "other"}))
        (self.root / "bin").mkdir(exist_ok=True)
        link = self.root / "bin/codex"
        link.symlink_to(root / "bin/codex.js")
        return link, root

    def native(self, directory, with_executable=True):
        triple, _ = self.node_target()
        (directory).mkdir(parents=True)
        (directory / "package.json").write_text("{}")
        vendor = directory / "vendor" / triple
        if with_executable:
            shutil.copytree(self.package, vendor, symlinks=True)
        return vendor

    @unittest.skipUnless(shutil.which("node"), "needs Node like the npm wrapper")
    def test_npm_wrapper_resolves_native_package_nested_or_hoisted(self):
        _, name = self.node_target()
        for layout in ("nested", "hoisted"):
            shutil.rmtree(self.root / "lib", ignore_errors=True)
            shutil.rmtree(self.root / "bin", ignore_errors=True)
            link, root = self.npm_install()
            directory = root / "node_modules/@openai" / name if layout == "nested" else root.parent / name
            vendor = self.native(directory)
            with self.subTest(layout):
                package, commit = audio_host.work_package(str(link))
                self.assertEqual((package, commit), (vendor.resolve(), COMMIT))
                exe = audio_host.prepare_package(package, commit, ["ssh"], self.cache)
                self.assertTrue((exe.parent.parent / audio_host.HELPER).read_text().startswith("#!"))

    @unittest.skipUnless(shutil.which("node"), "needs Node like the npm wrapper")
    def test_npm_wrapper_fallback_vendor_directory(self):
        link, root = self.npm_install()
        triple, _ = self.node_target()
        shutil.copytree(self.package, root / "vendor" / triple, symlinks=True)
        self.assertEqual(audio_host.work_package(str(link))[0], (root / "vendor" / triple).resolve())

    @unittest.skipUnless(shutil.which("node"), "needs Node like the npm wrapper")
    def test_npm_wrapper_never_falls_through_to_another_package(self):
        _, name = self.node_target()
        link, root = self.npm_install()
        self.native(root / "node_modules/@openai" / name, with_executable=False)
        self.native(root.parent / name)  # an older hoisted build Codex itself would not use
        with self.assertRaisesRegex(RuntimeError, "native executable .* is missing"):
            audio_host.work_package(str(link))

    @unittest.skipUnless(shutil.which("node"), "needs Node like the npm wrapper")
    def test_unofficial_wrapper_is_rejected(self):
        link, root = self.npm_install(official=False)
        self.native(root.parent / self.node_target()[1])
        with self.assertRaisesRegex(RuntimeError, "not supported"):
            audio_host.work_package(str(link))

    def mic(self, helpers):
        return {"host": "pc", "python": "py -3", "home_is_cwd": True, "helpers": helpers}

    def test_incompatible_helper_is_rejected_without_remote_changes(self):
        run = unittest.mock.Mock()
        mic = self.mic([{"path": "C:/a.exe", "build_commit": "f" * 40}])
        with self.assertRaisesRegex(RuntimeError, "no Codex voice helper matching"):
            audio_host.prepare(mic, str(self.package / "bin/codex"), ["ssh"], run)
        run.assert_not_called()

    def test_matching_build_is_selected_over_newer_version(self):
        run = unittest.mock.Mock(return_value="")
        mic = self.mic([{"path": "C:/new.exe", "build_commit": "f" * 40},
                        {"path": "C:/Users/some user/old.exe", "build_commit": COMMIT}])
        audio_host.prepare(mic, str(self.package / "bin/codex"), ["ssh"], run)
        command = mic["audio"]["command"]
        self.assertRegex(command, r"^py -3 \.herdr-codex-voice/host-[0-9a-f]{16}\.py$")
        self.assertIn("some user/old.exe", run.call_args.kwargs["input"])
        self.assertNotIn("new.exe", run.call_args.kwargs["input"])
        self.assertNotIn("helpers", mic)

    def test_probe_ignores_login_noise_and_tries_next_python(self):
        data = {"platform": "win32", "hostname": "pc", "home_is_cwd": True, "helpers": []}
        noise = 'Welcome {"platform": "win32"}\nCODEX_VOICE=garbage\nCODEX_VOICE=' + json.dumps(data) + "\nbye\n"
        run = unittest.mock.Mock(side_effect=[RuntimeError("'py' is not recognized"), noise])
        mic = audio_host.probe("pc", ["ssh"], run)
        self.assertEqual(mic["python"], "python")
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], ["py -3 -", "python -"])
        self.assertEqual(run.call_args.kwargs["input"], audio_host.PROBE)

    def test_probe_script_finds_only_working_helpers(self):
        home = self.root / "home"
        for release, body in (("a", "print('abc123')"), ("b", "raise SystemExit(1)")):
            script(home / f".codex/packages/standalone/releases/{release}/codex-resources/voice/bin/codex-voice-host.exe",
                   body + "\n")
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
               "CODEX_HOME": str(home / ".codex")}
        result = subprocess.run([sys.executable, "-c", audio_host.PROBE], capture_output=True, text=True, env=env,
                                cwd=home, check=True)
        data = json.loads(result.stdout.removeprefix("CODEX_VOICE="))
        self.assertEqual([h["build_commit"] for h in data["helpers"]], ["abc123"])
        self.assertTrue(data["home_is_cwd"])

    def test_probe_honors_codex_home_and_launcher_package(self):
        custom, other = self.root / "custom", self.root / "other"
        tail = "codex-resources/voice/bin/codex-voice-host.exe"
        script(custom / "packages/standalone/releases/v1" / tail, "print('aaa')\n")
        script(other / tail, "print('bbb')\n")
        script(other / "bin/codex", "")
        env = {**os.environ, "HOME": str(self.root), "USERPROFILE": str(self.root),
               "CODEX_HOME": str(custom), "PATH": str(other / "bin") + os.pathsep + os.environ["PATH"]}
        result = subprocess.run([sys.executable, "-c", audio_host.PROBE], capture_output=True, text=True,
                                env=env, check=True)
        data = json.loads(result.stdout.removeprefix("CODEX_VOICE="))
        self.assertEqual(sorted(h["build_commit"] for h in data["helpers"]), ["aaa", "bbb"])

    def test_end_to_end_binary_stream_environment_and_exit_status(self):
        # shim -> fake ssh -> Windows launcher -> fake helper, all on this machine.
        spaced = self.root / "dir with space"
        helper = script(spaced / "codex-voice-host", '''import json, os, sys
sys.stderr.write("ENV=" + json.dumps(dict(os.environ)) + "\\n")
sys.stderr.flush()
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(data[::-1])
sys.stdout.buffer.flush()
sys.exit(7)
''')
        mic_helpers = [{"path": str(helper), "build_commit": COMMIT}]
        mic = {"host": "pc", "python": "py -3", "home_is_cwd": True, "helpers": mic_helpers}
        home = self.root / "winhome"
        home.mkdir()
        installed = []

        def install(argv, input):
            installed.append(input)
            subprocess.run([sys.executable, "-"], input=input, text=True, check=True,
                           env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)})
            return ""

        audio_host.prepare(mic, str(self.package / "bin/codex"), ["ssh"], install)
        launcher = home / audio_host.REMOTE_DIR / mic["audio"]["command"].split("/")[-1]
        audio_host.prepare({**mic, "helpers": mic_helpers, "home_is_cwd": True}, str(self.package / "bin/codex"), ["ssh"], install)  # idempotent
        self.assertTrue(launcher.is_file())
        fake_ssh = script(self.root / "ssh", f'''import subprocess, sys, os
sys.stderr.write("AUTH=" + os.environ.get("SSH_AUTH_SOCK", "") + "\\n")
sys.stderr.flush()
raise SystemExit(subprocess.call([{sys.executable!r}, {str(launcher)!r}]))
''')
        exe = audio_host.prepare_package(self.package, COMMIT, [str(fake_ssh), "-T", "pc", "cmd"],
                                         self.cache, "/agent sock")
        shim = exe.parent.parent / audio_host.HELPER
        payload = bytes(range(256)) * 4096 + b"\r\n\x1a\x00"
        env = {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}
        env["SECRET_TOKEN"] = "do-not-forward"
        result = subprocess.run([str(shim)], input=payload, capture_output=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, payload[::-1])
        lines = result.stderr.decode().splitlines()
        self.assertIn("AUTH=/agent sock", lines)
        forwarded = json.loads([l for l in lines if l.startswith("ENV=")][0][4:])
        self.assertEqual(forwarded["GST_REGISTRY"], "NUL")
        self.assertEqual(forwarded["GST_PLUGIN_PATH"], "")
        self.assertNotIn("SECRET_TOKEN", forwarded)
        self.assertNotIn("SSH_AUTH_SOCK", forwarded)
        # Disconnect: closing stdin (EOF) ends the helper without an extra byte.
        quiet = subprocess.run([str(shim)], input=b"", capture_output=True, env=env, timeout=60)
        self.assertEqual((quiet.returncode, quiet.stdout), (7, b""))

    def test_launcher_reports_moved_helper(self):
        source = f"HELPER = {str(self.root / 'gone')!r}\nHOST = 'pc'\n" + audio_host.LAUNCHER
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
        self.assertEqual(result.returncode, 127)
        self.assertIn("rerun codex-voice setup pc", result.stderr)


class VoiceWindowsTests(unittest.TestCase):
    def test_same_hostname_does_not_bypass_windows_pairing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        mic = {"host": "pc", "hostname": voice.socket.gethostname(), "platform": "win32",
               "audio": {"build_commit": COMMIT, "command": "py -3 x"}}
        (root / "config.json").write_text(json.dumps(mic))
        listing = json.dumps({"result": {"plugins": [{"enabled": True, "plugin_root": str(Path(voice.__file__).parent)}]}})
        with patch.object(voice, "config_dir", return_value=root), \
             patch.object(voice, "run", return_value=listing), \
             patch.object(voice.shutil, "which", return_value="/bin/codex"), \
             patch.object(voice, "load_audio") as load, \
             patch.object(os, "execv") as execute, \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdout, "isatty", return_value=True):
            load.return_value.stale.return_value = False
            load.return_value.run_codex.side_effect = SystemExit
            with self.assertRaises(SystemExit):
                voice.connect([])
        execute.assert_not_called()

    def test_unix_probe_failure_falls_back_to_windows_probe(self):
        windows = {"platform": "win32", "python": "py -3", "helpers": []}
        with patch.object(voice, "run", side_effect=RuntimeError("not recognized")), \
             patch.object(voice, "load_audio") as load:
            load.return_value.probe.return_value = windows
            mic = voice.probe("pc", "/plugin")
        self.assertEqual(mic, {"host": "pc", "local": False, **windows})
        with patch.object(voice, "run", side_effect=RuntimeError("not recognized")), \
             patch.object(voice, "load_audio") as load:
            load.return_value.probe.side_effect = RuntimeError("no Python 3 found")
            with self.assertRaisesRegex(RuntimeError, "not recognized\nWindows check: no Python"):
                voice.probe("pc", "/plugin")

    def test_connect_uses_audio_only_route_without_backend_or_remote_socket(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        mic = {"host": "pc", "hostname": "pc", "platform": "win32",
               "audio": {"build_commit": COMMIT, "command": "py -3 x"}}
        (root / "config.json").write_text(json.dumps(mic))
        listing = json.dumps({"result": {"plugins": [{"enabled": True, "plugin_root": str(Path(voice.__file__).parent)}]}})
        with patch.object(voice, "config_dir", return_value=root), \
             patch.object(voice, "run", return_value=listing), \
             patch.object(voice.shutil, "which", side_effect=lambda n: "/bin/" + n), \
             patch.object(voice, "backend") as backend, \
             patch.object(voice, "load_audio", return_value=audio_host), \
             patch.object(audio_host, "work_package", return_value=(root, COMMIT)), \
             patch.object(audio_host, "run_codex") as run_codex, \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdout, "isatty", return_value=True):
            run_codex.side_effect = SystemExit
            with self.assertRaises(SystemExit):
                voice.connect(["resume", "--last"])
        backend.assert_not_called()
        self.assertEqual(run_codex.call_args.args[2], ["resume", "--last"])
        self.assertEqual(run_codex.call_args.args[3][0], "/bin/ssh")


if __name__ == "__main__":
    unittest.main()
