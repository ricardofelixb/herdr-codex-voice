"""Resolve a pane's keyboard client to a paired microphone, when Herdr supports it."""

import ipaddress
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import uuid


class OriginError(RuntimeError):
    """A failure with a stable `code` for diagnostics."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# Only the stable node identifier is returned; no keys or login data cross SSH.
NODE_PROBE = r'''import json, os, shutil, subprocess
candidates = [shutil.which("tailscale"),
 "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
 os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Tailscale", "tailscale.exe")]
node = None
for executable in candidates:
 if not executable or not os.path.isfile(executable): continue
 try:
  result = subprocess.run([executable,"status","--json"], capture_output=True, text=True, timeout=5)
  value = json.loads(result.stdout).get("Self",{}).get("ID") if result.returncode == 0 else None
  if isinstance(value,str) and value:
   node=value
   break
 except (OSError,ValueError,subprocess.SubprocessError): pass
print("HCV_NODE=" + json.dumps(node))
'''


def pair_identity(mic, ssh, run):
    try:
        if mic.get("local"):
            output = run([sys.executable, "-c", NODE_PROBE])
        elif mic.get("platform") == "win32":
            output = run([*ssh, "-T", mic["host"], mic["python"] + " -"], input=NODE_PROBE)
        else:
            output = run([*ssh, "-T", mic["host"], shlex.join([mic["python"], "-c", NODE_PROBE])])
        for line in reversed(output.splitlines()):
            if line.startswith("HCV_NODE="):
                node = json.loads(line[len("HCV_NODE="):])
                return node if isinstance(node, str) and node else None
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        pass
    return None


def rpc(path, method, params):
    request_id = "codex-voice-" + uuid.uuid4().hex
    request = {"id": request_id, "method": method, "params": params}
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(path)
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as stream:
            line = stream.readline(1024 * 1024 + 1)
    if len(line) > 1024 * 1024 or not line.endswith(b"\n"):
        raise OriginError("origin_lookup_failed", "Herdr returned an invalid microphone routing response")
    response = json.loads(line)
    if response.get("id") != request_id or "error" in response:
        raise OriginError("origin_lookup_failed", "Herdr could not identify this pane's keyboard client")
    return response["result"]


def herdr_cli(*args):
    """Herdr's own CLI, which knows its Windows named-pipe transport (canary/WINDOWS-API.md)."""
    executable = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not executable:
        raise OriginError("origin_lookup_failed", "Herdr's CLI was not found")
    result = subprocess.run([executable, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise OriginError("origin_lookup_failed", "Herdr could not identify this pane's keyboard client")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise OriginError("origin_lookup_failed", "Herdr returned an invalid microphone routing response")
    return data


def capabilities(path):
    if sys.platform == "win32":
        status = herdr_cli("status", "server", "--json")
        if status.get("running") is not True:
            raise OriginError("origin_lookup_failed", "The current Herdr server is unavailable")
        found = status.get("capabilities") or {}
    else:
        found = rpc(path, "ping", {}).get("capabilities", {})
    if not isinstance(found, dict):
        raise OriginError("origin_lookup_failed", "Herdr returned an invalid microphone routing response")
    return found


def last_input(path, pane):
    if sys.platform == "win32":
        context = herdr_cli("pane", "last-input", pane).get("result")
        if not isinstance(context, dict) or context.get("type") != "pane_last_input" \
                or context.get("pane_id") != pane:
            raise OriginError("origin_lookup_failed", "Herdr returned an unexpected keyboard-origin response")
        return context.get("last_input")
    return rpc(path, "pane.last_input", {"pane_id": pane}).get("last_input")


def capable(path):
    """Whether this Herdr server reports keyboard origins: True, False, or None if unreachable."""
    try:
        return capabilities(path).get("pane_last_input") is True
    except (OSError, ValueError, KeyError, AttributeError, RuntimeError, subprocess.SubprocessError):
        return None


def tailnet_node(ssh_connection):
    fields = ssh_connection.split()
    if len(fields) != 4:
        raise ValueError("invalid SSH connection")
    source, port, destination, server_port = fields
    source = ipaddress.ip_address(source)
    ipaddress.ip_address(destination)
    if not 0 < int(port) < 65536 or not 0 < int(server_port) < 65536:
        raise ValueError("invalid SSH port")
    # Userspace Tailscale exposes incoming SSH as a loopback TCP proxy. Its
    # source port identifies the peer; bare 127.0.0.1 would lose that identity.
    endpoint = str(source)
    if source.is_loopback:
        endpoint = f"[{source}]:{port}" if source.version == 6 else f"{source}:{port}"
    executable = shutil.which("tailscale")
    for installed in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                      os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe")):
        if not executable and os.path.isfile(installed):
            executable = installed
    if not executable:
        raise OriginError("tailscale_cli_missing", "Tailscale CLI is required for automatic microphone selection")
    result = subprocess.run([executable, "whois", "--json", endpoint],
                            capture_output=True, text=True, timeout=5, check=True)
    node = json.loads(result.stdout).get("Node", {}).get("StableID")
    if not isinstance(node, str) or not node:
        raise OriginError("tailscale_unidentified", "Tailscale did not identify the typing computer")
    return node


def select(choices):
    path = os.environ.get("HERDR_SOCKET_PATH")
    pane = os.environ.get("HERDR_PANE_ID")
    if not path or not pane:
        return None
    try:
        if not capabilities(path).get("pane_last_input"):
            return None  # Released Herdr still supports explicit pairing.
        context = last_input(path, pane)
        if not isinstance(context, dict) or context.get("source") != "client":
            raise OriginError("origin_not_typed", "Type codex directly in this Herdr pane so its microphone can be identified")
        client = context.get("client") or {}
        if client.get("connected") is not True:
            raise OriginError("origin_disconnected",
                              "The computer that typed into this pane disconnected; type codex again after reconnecting")
        connection = client.get("ssh_connection")
        if client.get("via") == "local" and not connection:
            matches = [choice for choice in choices if choice[1].get("local")]
        elif client.get("via") in {"local", "ssh_bridge"} and isinstance(connection, str) and connection:
            node = tailnet_node(connection)
            matches = [choice for choice in choices if choice[1].get("tailscale_node_id") == node]
        else:
            raise OriginError("origin_unavailable",
                              "This Herdr connection has no keyboard origin; reconnect it to the voice canary")
        if len(matches) != 1:
            raise OriginError("origin_no_match",
                              "Pair exactly one microphone for the typing computer with codex-voice pair NAME SSH-HOST")
        return matches[0]
    except (OSError, ValueError, KeyError, AttributeError, subprocess.SubprocessError) as error:
        raise OriginError("origin_lookup_failed",
                          "Could not identify this pane's microphone computer: " + str(error)) from None
