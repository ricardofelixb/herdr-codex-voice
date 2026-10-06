# Windows input-origin API

Herdr v0.9.3 uses `interprocess` 2.4.2 for local IPC. On Unix this is a Unix-domain
socket. On Windows it is a named pipe. `HERDR_SOCKET_PATH` is a filesystem marker
used for server discovery; opening it as a Unix socket is incorrect.

The Windows implementation in `src/ipc.rs` converts that path with
`GenericNamespaced`. The pinned library prefixes its UTF-16 representation with
`\\.\pipe\`; it does not hash the string. API messages still use one JSON object
per line. Do not send Python multiprocessing pipe framing or bincode framing to
the JSON API.

## Recommended plugin path

Use Herdr's own CLI for the two attribution queries on Windows. This reuses its
pipe connection, namespacing, session selection, error handling, and evolving
transport contract. Invoke the executable selected by `HERDR_BIN_PATH`, or an
explicitly verified Herdr executable on PATH, with an argument array and a
subprocess timeout. Do not run a shell command assembled from a pane ID.

```python
import json
import os
import subprocess

herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
pane_id = os.environ["HERDR_PANE_ID"]

def cli(*args):
    result = subprocess.run(
        [herdr, *args], check=True, capture_output=True, text=True, timeout=5
    )
    return json.loads(result.stdout)

status = cli("status", "server", "--json")
if status.get("running") is not True:
    raise RuntimeError("The current Herdr server is unavailable")
if (status.get("capabilities") or {}).get("pane_last_input") is not True:
    raise RuntimeError("The current Herdr server does not support input origin")

response = cli("pane", "last-input", pane_id)
context = response["result"]
if context.get("type") != "pane_last_input" or context.get("pane_id") != pane_id:
    raise RuntimeError("Unexpected Herdr input-origin response")
last_input = context.get("last_input")
if not isinstance(last_input, dict) or last_input.get("source") != "client":
    raise RuntimeError("Type directly in this pane before selecting its microphone")
client = last_input.get("client") or {}
if client.get("connected") is not True:
    raise RuntimeError("The typing client has disconnected")
```

Production callers must also validate the origin fields and require exactly one
matching paired microphone. `via == "ssh_bridge"` supplies the bridge process's
`SSH_CONNECTION`. The bridge overwrites a client's supplied origin before
forwarding the optional JSON endpoint hello. A local client can report `local`;
absence of SSH metadata is valid for local input and must not be replaced with a
process-tree or globally focused-client guess.

This package adds `pane_last_input` to `ServerCapabilitiesJson` in CLI status,
with a focused test for both `true` and `false`. The earlier private canary only
advertised it in raw API ping. Therefore a CLI capability check against that
earlier binary cannot identify support reliably; the package version ends in
`.20261006.1` to distinguish the fix.

## Build and runtime limits

Native compilation targets `x86_64-pc-windows-msvc` with Rust 1.96.1, Zig 0.16.0,
and Visual Studio 2022 C++ Build Tools/Windows SDK. No Unix-only API was added to
the shared input-origin code or SSH hello stamping. The existing Windows bridge
pump uses the same process-global stdin handle, so the prefix-read buffering
regression test is relevant on Windows too.

Windows live handoff is unsupported by the upstream server. Locally built
unsigned binaries may be blocked by Windows policy. Compilation and PE-format
inspection can complete without running the canary; runtime/voice claims must
wait for a permitted execution path. This package does not change that policy.
