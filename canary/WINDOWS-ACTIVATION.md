# Windows activation without interrupting existing panes

Installing a canary and replacing a running server are separate operations.
Herdr v0.9.3 has no Windows live handoff. A compatible client connects to the
existing server, so selecting a new executable does not add `pane.last_input`
to an old server.

## Select the executable for future launches

The least invasive choice is to keep the canary in its own versioned directory
and launch that absolute path. This changes no PATH, shortcut, or existing
installation. Starting it against the default session still attaches to the
existing default server.

For an authorized normal installation, upstream's `distribution/install.ps1`
supports a local package with an exact SHA-256. It stages a versioned release,
updates its managed `current` and compatibility `bin` junctions, and selects
the release in the user's PATH. It does not stop running servers. Preserve the
old release and record both junction targets and the previous user PATH first.
Use `-Retain 0` to disable old-release pruning.

Example after a complete Windows runtime ZIP has been prepared and verified:

```powershell
& $Installer -LocalPackagePath $CanaryZip -LocalPackageFormat zip `
  -LocalPackageIdentity '0.9.3-voice-canary.20261006.1' `
  -LocalPackageSha256 $ExpectedZipSha256 -Retain 0
```

`$Installer` is the pinned upstream installer from the verified source tree.
This example does not override PowerShell execution policy. If existing policy
blocks a script or binary, stop and report it. An installer success must be
followed by checking the resolved executable, `--version`, server capability,
artifact SHA-256, and the original pane PIDs. A server still reporting the old version is expected
until its own lifecycle ends.

Windows SSH sessions can inherit an older PATH. Check `Get-Command herdr.exe`
in a fresh SSH connection as well as the desktop terminal. Remote discovery
tries PATH before running-process paths and the managed `current` junction.
The selected SSH bridge must be the canary too; an old bridge cannot stamp the
origin metadata needed for automatic microphone routing. Saved connections
should reconnect after selection changes.

## Use a separate session now

From a new terminal belonging to the normal desktop user, outside Herdr:

```powershell
& $Canary --session voice-canary
```

Choose an unused session name. The canary starts its own server using its own
executable and gives that named session separate saved state and named pipes.
The default server and its panes remain running. Verify the new session before
opening work there:

```powershell
& $Canary --session voice-canary status server --json
```

Require the expected version and `capabilities.pane_last_input == true`.
After verifying that SSH selects the canary bridge, a Mac can attach with:

```sh
herdr --remote YOUR_SSH_ALIAS --session voice-canary
```

This is a separate workspace session. It does not transfer existing panes or
their shell state. Automatic microphone routing applies only where the server
advertises the capability and the pane has recent genuine client input.

## Pause saved PC connections before a Windows cutover

A reconnecting SSH bridge can start a missing server with the SSH account's
process token. Selecting the canary executable does not guarantee that the new
server has the normal desktop user's token. Keep saved connections to the PC
paused until the normal desktop server has started and its owner is verified.

On every client machine that has a saved connection to the PC, record the
matching profile IDs, target, session, and enabled state:

```sh
herdr machine list --json
herdr machine disable PROFILE_ID
```

Disable only previously enabled profiles for the PC session being replaced.
These supported commands keep the saved profile and the remote session.
Open clients reload profiles and disconnect disabled endpoints, usually within
a second. If a client was viewing that endpoint, its view returns to Local.
Let in-flight connection attempts finish and verify that no bridge for the
replaced session remains before stopping its server. Do not start a fresh
manual attachment during this interval. A client that is offline must have
its profile paused when it returns, before the cutover proceeds.

After the new server passes the ownership and capability checks, restore only
the profiles that were enabled before maintenance:

```sh
herdr machine enable PROFILE_ID
```

Clients reconnect automatically. Enabling a profile does not force the user's
view back to it. Leave unrelated profiles and previously disabled profiles
unchanged. Stock v0.9.3 has no JSON client-list or pause-all-clients method, so
an agent must inventory the participating clients before claiming quiescence.

## Start with the normal desktop user's token

A cutover controller started inside a Herdr pane must survive that pane's
closure. Upstream's Windows daemon launcher uses local WMI when its launcher
belongs to a job that kills children on close. A task controller can use the
same local `Win32_Process.Create` mechanism with `DETACHED_PROCESS`, launched
from the normal desktop user. Verify its user SID, non-elevated token, Windows
session, lack of an attached console, and lack of a kill-on-close job before
stopping anything. A plain `Start-Process` call alone is not proof of survival.
Do not create the controller through an elevated SSH session.

Once the approved graceful stop has completed, wait for the original server
process to exit as well as its pipes to disappear. Preserve the final saved
session, then the detached normal-user controller can launch the exact canary
with `--session default remote-client-bridge` and empty stdin. The bridge
starts its daemon before accepting input and exits successfully at clean EOF.
A TUI is not required for this startup path.

Require the new server's exact version, `capabilities.pane_last_input == true`,
`capabilities.detached_server_daemon == true`, expected executable checksum,
normal desktop SID, non-elevated token, and expected Windows session. If an
unexpected server appears or ownership differs, stop the procedure and report
it. Do not kill that process or change Windows policy to make the check pass.

A stock local frontend without saved machines exits when its server stops.
A frontend with saved machines only retries the Local socket; it does not
start a replacement Local server. Saved SSH connections can start one, which
is why their temporary pause is required.

## Migrate the default session later

A default-session stop requires explicit permission to end its pane processes.
An aggregate workspace state of `idle` or `unknown` is not proof that its panes
contain only disposable shells. Even idle shells can hold unsaved environment
variables, jobs, or application state.

Before the agreed restart window, prepare the normal-user controller and pause
the matching saved connections as described above:

1. Inventory the exact default session with `api snapshot` and each pane's
   `pane process-info --pane ID`. Record IDs, process state, and the work the
   user wants preserved without collecting pane transcripts.
2. Let active agents and jobs finish, or get explicit permission for their
   termination. Confirm which conversations have valid native resume references.
3. Locate state with `session list --json`. Keep a private backup of that
   session directory and the old executable. Avoid closing panes individually
   just to empty the server, because closures update the saved layout.
4. With the permission above, run the old executable's `--session default
   server stop` outside the session. Wait for successful exit and disconnected
   server sockets. Do not force-kill a timeout.
5. Wait for the old process to exit. Back up the final `session.json` written
   by shutdown, then start the canary default daemon as the normal desktop
   user. Keep the same config location. Check ownership, layout, cwd, agent
   resume results, version, and capability before re-enabling saved connections.

A graceful stop saves the layout, but it still ends pane processes. Restore
creates new shells, with new PIDs. Supported agents can resume conversations
only when valid integration-reported references exist. Arbitrary processes,
in-memory shell state, and unrecorded conversations do not survive. Screen
history returns only if that feature was already enabled; do not enable it
silently because it persists terminal output.

Rollback before server cutover only changes future executable selection back
to the retained release. Rollback after cutover requires another approved stop
and restore. Neither operation can recreate terminated original processes.

These instructions are based on the exact upstream source pinned in
`manifest.json`: `distribution/install.ps1`, `src/server/autodetect.rs`,
`src/remote/attach.rs`, `src/session.rs`, `src/persist/restore.rs`, and
`src/client/endpoint/supervisor.rs`, `src/client/catalog_reload.rs`,
`src/cli/machine.rs`, `src/platform/windows.rs`, and
`docs/next/website/src/content/docs/session-state.mdx`.
