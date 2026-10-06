"""Resolve a pane's keyboard client to a paired microphone, when Herdr supports it."""

import ipaddress
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import uuid


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
        raise RuntimeError("Herdr returned an invalid microphone routing response")
    response = json.loads(line)
    if response.get("id") != request_id or "error" in response:
        raise RuntimeError("Herdr could not identify this pane's keyboard client")
    return response["result"]


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
    if not executable and Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale").is_file():
        executable = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not executable:
        raise RuntimeError("Tailscale CLI is required for automatic microphone selection")
    result = subprocess.run([executable, "whois", "--json", endpoint],
                            capture_output=True, text=True, timeout=5, check=True)
    node = json.loads(result.stdout).get("Node", {}).get("StableID")
    if not isinstance(node, str) or not node:
        raise RuntimeError("Tailscale did not identify the typing computer")
    return node


def select(choices):
    path = os.environ.get("HERDR_SOCKET_PATH")
    pane = os.environ.get("HERDR_PANE_ID")
    if not path or not pane:
        return None
    try:
        capabilities = rpc(path, "ping", {}).get("capabilities", {})
        if not capabilities.get("pane_last_input"):
            return None  # Released Herdr still supports explicit pairing.
        context = rpc(path, "pane.last_input", {"pane_id": pane}).get("last_input")
        if not isinstance(context, dict) or context.get("source") != "client":
            raise RuntimeError("Type codex directly in this Herdr pane so its microphone can be identified")
        client = context.get("client") or {}
        if client.get("connected") is not True:
            raise RuntimeError("The computer that typed into this pane disconnected; type codex again after reconnecting")
        connection = client.get("ssh_connection")
        if client.get("via") == "local" and not connection:
            matches = [choice for choice in choices if choice[1].get("local")]
        elif client.get("via") in {"local", "ssh_bridge"} and isinstance(connection, str) and connection:
            node = tailnet_node(connection)
            matches = [choice for choice in choices if choice[1].get("tailscale_node_id") == node]
        else:
            raise RuntimeError("This Herdr connection has no keyboard origin; reconnect it to the voice canary")
        if len(matches) != 1:
            raise RuntimeError("Pair exactly one microphone for the typing computer with codex-voice pair NAME SSH-HOST")
        return matches[0]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise RuntimeError("Could not identify this pane's microphone computer: " + str(error)) from None
