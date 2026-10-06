import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import client_origin


class ClientOriginTests(unittest.TestCase):
    def setUp(self):
        self.choices = [(Path("laptop"), {"tailscale_node_id": "node-laptop"}),
                        (Path("pc"), {"tailscale_node_id": "node-pc"}),
                        (Path("local"), {"local": True})]
        self.environment = patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/private/herdr.sock",
                                                  "HERDR_PANE_ID": "w1:p1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def client(self, **changes):
        return {"source": "client", "client": {"connected": True, "via": "ssh_bridge",
                "ssh_connection": "127.0.0.1 54321 127.0.0.1 22", **changes}}

    def responses(self, context):
        return [{"capabilities": {"pane_last_input": True}}, {"last_input": context}]

    def test_each_launch_uses_this_panes_client_not_previous_choice(self):
        for node, expected in (("node-pc", "pc"), ("node-laptop", "laptop")):
            with patch.object(client_origin, "rpc", side_effect=self.responses(self.client())) as rpc, \
                 patch.object(client_origin, "tailnet_node", return_value=node):
                self.assertEqual(client_origin.select(self.choices)[0], Path(expected))
            self.assertEqual(rpc.call_args.args[1:], ("pane.last_input", {"pane_id": "w1:p1"}))

    def test_local_client_selects_only_the_explicit_local_pairing(self):
        with patch.object(client_origin, "rpc", side_effect=self.responses(self.client(via="local", ssh_connection=None))), \
             patch.object(client_origin, "tailnet_node") as resolve:
            self.assertEqual(client_origin.select(self.choices)[0], Path("local"))
            resolve.assert_not_called()

    def test_api_disconnected_or_unknown_origin_never_uses_a_saved_default(self):
        for context in (None, {"source": "api"}, self.client(connected=False),
                        self.client(via="unknown"), self.client(ssh_connection=None)):
            with patch.object(client_origin, "rpc", side_effect=self.responses(context)):
                with self.assertRaises(RuntimeError):
                    client_origin.select(self.choices[:1])

    def test_no_match_or_duplicate_device_never_guesses(self):
        for choices in (self.choices[:1], [self.choices[1], self.choices[1]]):
            with patch.object(client_origin, "rpc", side_effect=self.responses(self.client())), \
                 patch.object(client_origin, "tailnet_node", return_value="node-pc"):
                with self.assertRaisesRegex(RuntimeError, "exactly one"):
                    client_origin.select(choices)

    def test_released_herdr_can_keep_explicit_pairing(self):
        with patch.object(client_origin, "rpc", return_value={"capabilities": {}}) as rpc:
            self.assertIsNone(client_origin.select(self.choices))
            self.assertEqual(rpc.call_count, 1)

    def test_unreachable_server_does_not_choose_a_different_microphone(self):
        with patch.object(client_origin, "rpc", side_effect=OSError("unreachable")):
            with self.assertRaisesRegex(RuntimeError, "identify"):
                client_origin.select(self.choices[:1])

    def test_userspace_tailscale_keeps_source_port(self):
        for connection, endpoint in (("127.0.0.1 54321 127.0.0.1 22", "127.0.0.1:54321"),
                                     ("::1 54321 ::1 22", "[::1]:54321"),
                                     ("100.64.0.2 54321 100.64.0.3 22", "100.64.0.2")):
            result = subprocess.CompletedProcess([], 0, json.dumps({"Node": {"StableID": "node-pc"}}))
            with patch.object(client_origin.shutil, "which", return_value="/bin/tailscale"), \
                 patch.object(client_origin.subprocess, "run", return_value=result) as run:
                self.assertEqual(client_origin.tailnet_node(connection), "node-pc")
                self.assertEqual(run.call_args.args[0], ["/bin/tailscale", "whois", "--json", endpoint])

    def test_invalid_ssh_connection_is_rejected_before_execution(self):
        with patch.object(client_origin.subprocess, "run") as run:
            for connection in ("not an address", "127.0.0.1 0 127.0.0.1 22",
                               "host;command 100 127.0.0.1 22", "::1 70000 ::1 22"):
                with self.assertRaises(ValueError):
                    client_origin.tailnet_node(connection)
            run.assert_not_called()

    def test_remote_identity_probe_ignores_noise_and_uses_python_on_windows(self):
        from unittest.mock import Mock
        run = Mock(return_value='banner\nHCV_NODE="node-pc"\n')
        mic = {"host": "pc-alias", "platform": "win32", "python": "py -3"}
        self.assertEqual(client_origin.pair_identity(mic, ["ssh"], run), "node-pc")
        self.assertEqual(run.call_args.args[0], ["ssh", "-T", "pc-alias", "py -3 -"])
        self.assertIn("input", run.call_args.kwargs)


class WindowsCliTests(unittest.TestCase):
    """On Windows HERDR_SOCKET_PATH names a pipe, so the plugin asks Herdr's own CLI."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.log, self.answers = root / "herdr.log", root / "answers.json"
        herdr = root / "herdr"
        herdr.write_text(f'''#!{sys.executable}
import json, sys
open({str(self.log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
answers = json.load(open({str(self.answers)!r}))
key = " ".join(sys.argv[1:3])
if key not in answers:
    sys.exit(2)
print(json.dumps(answers[key]))
''')
        herdr.chmod(0o755)
        self.context = {"type": "pane_last_input", "pane_id": "w1:p1", "last_input": {
            "source": "client", "client": {"connected": True, "via": "ssh_bridge",
                                           "ssh_connection": "100.64.0.2 51000 100.64.0.3 22"}}}
        self.answer({"running": True, "capabilities": {"pane_last_input": True}}, self.context)
        for change in (patch.dict(os.environ, {"HERDR_BIN_PATH": str(herdr), "HERDR_PANE_ID": "w1:p1",
                                               "HERDR_SOCKET_PATH": r"C:\Users\me\AppData\herdr\herdr.sock"}),
                       patch.object(client_origin.sys, "platform", "win32"),
                       patch.object(client_origin, "rpc", side_effect=AssertionError("no Unix socket on Windows"))):
            change.start()
            self.addCleanup(change.stop)
        self.choices = [(Path("mac"), {"tailscale_node_id": "node-mac"}), (Path("pc"), {"local": True})]

    def answer(self, status, context):
        self.answers.write_text(json.dumps({"status server": status,
                                            "pane last-input": {"id": "1", "result": context}}))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_typing_computer_is_found_through_the_cli(self):
        with patch.object(client_origin, "tailnet_node", return_value="node-mac"):
            self.assertEqual(client_origin.select(self.choices)[0], Path("mac"))
        self.assertEqual(self.calls(), [["status", "server", "--json"], ["pane", "last-input", "w1:p1"]])
        self.assertIs(client_origin.capable(None), True)

    def test_released_herdr_keeps_the_explicit_choice(self):
        self.answer({"running": True, "capabilities": {"pane_last_input": False}}, None)
        self.assertIsNone(client_origin.select(self.choices))
        self.assertIs(client_origin.capable(None), False)

    def test_unavailable_or_unexpected_answers_never_choose(self):
        for status, context in (({"running": False, "capabilities": {"pane_last_input": True}}, self.context),
                                ({"running": True, "capabilities": {"pane_last_input": True}},
                                 {**self.context, "pane_id": "w1:p2"}),
                                ({"running": True, "capabilities": {"pane_last_input": True}}, None)):
            self.answer(status, context)
            with patch.object(client_origin, "tailnet_node", return_value="node-mac"):
                with self.assertRaises(client_origin.OriginError) as raised:
                    client_origin.select(self.choices)
            self.assertEqual(raised.exception.code, "origin_lookup_failed")
        self.answers.write_text("{}")
        self.assertIsNone(client_origin.capable(None))


if __name__ == "__main__":
    unittest.main()
