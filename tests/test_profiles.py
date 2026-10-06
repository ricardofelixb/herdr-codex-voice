import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def pair(self, name, host):
        voice.write_private(voice.named_profile(self.root, name),
                            json.dumps({"label": name, "host": host}))

    def test_existing_single_pairing_starts_without_a_question(self):
        mic = {"host": "first", "hostname": "laptop"}
        path = self.root / "config.json"
        path.write_text(json.dumps(mic))
        with patch.object(sys.stdin, "readline", side_effect=AssertionError("unexpected question")):
            self.assertEqual(voice.choose_microphone(self.root), (path, mic))

    def test_named_microphones_keep_legacy_pairing_and_choose_each_launch(self):
        legacy = self.root / "config.json"
        legacy.write_text(json.dumps({"host": "laptop"}))
        before = legacy.read_bytes()
        self.pair("Laptop", "laptop")
        self.pair("PC", "desktop")
        # Reusing a pane from another client must not silently reuse its mic.
        with patch.object(sys, "stdin", io.StringIO("2\n1\n")), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(voice.choose_microphone(self.root)[1]["host"], "desktop")
            self.assertEqual(voice.choose_microphone(self.root)[1]["host"], "laptop")
        self.assertEqual(output.getvalue().count("Which computer"), 2)
        self.assertNotIn("3.", output.getvalue())
        self.assertEqual(legacy.read_bytes(), before)

    def test_choice_never_defaults_on_cancel_or_invalid_input(self):
        self.pair("A", "a")
        self.pair("B", "b")
        for answer in ("", "q\n", "0\n3\n\ninvalid\nq\n"):
            with patch.object(sys, "stdin", io.StringIO(answer)), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    voice.choose_microphone(self.root)
        with patch.object(sys, "stdin", io.StringIO("0\n3\n1\n")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(voice.choose_microphone(self.root)[1]["host"], "a")

    def test_local_and_ssh_alias_named_local_remain_distinct(self):
        voice.write_private(voice.named_profile(self.root, "A this computer"),
                            json.dumps({"label": "A this computer", "host": "local", "local": True}))
        self.pair("B remote", "local")
        with patch.object(sys, "stdin", io.StringIO("2\n")), contextlib.redirect_stderr(io.StringIO()):
            mic = voice.choose_microphone(self.root)[1]
        self.assertEqual(mic["label"], "B remote")
        self.assertFalse(mic.get("local", False))

    def test_explicit_names_for_one_host_do_not_hide_each_other(self):
        self.pair("A", "same-host")
        self.pair("B", "same-host")
        with patch.object(sys, "stdin", io.StringIO("2\n")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(voice.choose_microphone(self.root)[1]["label"], "B")

    def test_malformed_profile_reports_its_path_without_a_traceback(self):
        path = voice.named_profile(self.root, "Broken")
        for data in ([], None, {"host": []}, {"host": "a", "label": 12},
                     {"host": "a", "hostname": []}, {"local": "yes"}):
            voice.write_private(path, json.dumps(data))
            with self.assertRaisesRegex(RuntimeError, str(path)):
                voice.choose_microphone(self.root)

    def test_named_setup_preserves_default_and_updates_one_profile(self):
        legacy = self.root / "config.json"
        legacy.write_text('{"host":"old"}')
        with patch.object(voice, "probe", side_effect=lambda _: {"host": "new", "platform": "linux"}), \
             patch.object(voice, "pair_identity", return_value=None), \
             patch.object(voice, "backend"), patch.object(voice, "shell_file", return_value=None), \
             patch.object(voice.shutil, "which", return_value="/bin/codex"), \
             patch.object(voice, "install", return_value=self.root / "codex-voice"), \
             patch.object(voice, "config_dir", return_value=self.root), contextlib.redirect_stdout(io.StringIO()):
            voice.setup("new", "My laptop")
            voice.setup("new", "My laptop")
        paths = list((self.root / "microphones").glob("*.json"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text())["label"], "My laptop")
        self.assertEqual(legacy.read_text(), '{"host":"old"}')

    def test_setup_recovery_refreshes_named_routes_and_preserves_their_names(self):
        self.pair("Laptop", "laptop")
        self.pair("PC", "pc")
        with patch.object(voice, "probe", return_value={"host": "laptop", "codex": "/new/codex"}), \
             patch.object(voice, "pair_identity", return_value=None), \
             patch.object(voice, "backend"), patch.object(voice, "shell_file", return_value=None), \
             patch.object(voice.shutil, "which", return_value="/bin/codex"), \
             patch.object(voice, "install", return_value=self.root / "codex-voice"), \
             patch.object(voice, "config_dir", return_value=self.root), contextlib.redirect_stdout(io.StringIO()):
            voice.setup("laptop")
        updated = json.loads(voice.named_profile(self.root, "Laptop").read_text())
        self.assertEqual(updated["codex"], "/new/codex")
        self.assertEqual(updated["label"], "Laptop")
        self.assertEqual(json.loads(voice.named_profile(self.root, "PC").read_text()),
                         {"label": "PC", "host": "pc"})

    def test_invalid_name_is_rejected_before_remote_setup(self):
        with patch.object(voice, "config_dir", return_value=self.root), patch.object(voice, "probe") as probe:
            for name in ("../escape", "bad\nname", "", " ", "a" * 41):
                with self.assertRaises(ValueError):
                    voice.setup("desktop", name)
            probe.assert_not_called()

    def test_failed_recovery_validation_preserves_all_existing_profiles(self):
        legacy = self.root / "config.json"
        legacy.write_text('{"host":"laptop","codex":"old"}')
        self.pair("Laptop", "laptop")
        broken = voice.named_profile(self.root, "Broken")
        voice.write_private(broken, "[]")
        before = {p: p.read_bytes() for p in self.root.rglob("*.json")}
        with patch.object(voice, "config_dir", return_value=self.root), patch.object(voice, "probe") as probe:
            with self.assertRaises(RuntimeError):
                voice.setup("laptop")
            probe.assert_not_called()
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*.json")}, before)

    def test_utilities_do_not_select_a_microphone(self):
        with patch.object(voice.shutil, "which", return_value="/bin/codex"), \
             patch.object(voice, "choose_microphone", side_effect=AssertionError("unexpected question")), \
             patch.object(os, "execv", side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                voice.connect(["--version"])
            execute.assert_called_once_with("/bin/codex", ["/bin/codex", "--version"])


if __name__ == "__main__":
    unittest.main()
