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

## Migrate the default session later

A default-session stop requires explicit permission to end its pane processes.
An aggregate workspace state of `idle` or `unknown` is not proof that its panes
contain only disposable shells. Even idle shells can hold unsaved environment
variables, jobs, or application state.

Before the agreed restart window:

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
5. Back up the final `session.json` written by shutdown, then start the canary
   with `--session default` as the normal desktop user. Keep the same config
   location. Check layout, cwd, agent resume results, version, and capability.

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
`docs/next/website/src/content/docs/session-state.mdx`.
