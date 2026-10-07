# Startup performance

Version 0.6 adds an opt-in `--audio-only` pairing for a Mac microphone with a
Unix work computer. It runs Codex's terminal on the work computer and connects
the Mac helper when Codex needs audio. Existing pairings keep their old route.
The Mac helper and its desktop application identity are unchanged.

## Measured on 2026-10-07

Three launches per route, using genuine Mac Mini input over Tailscale SSH into
an isolated Herdr server on the Linux work computer. Both used Codex 0.160.1,
helper build `d27764b82f7118f674371e6d6e76271d9d606edb`, the same trusted project
folder and the same account configuration. These are warm launches after setup;
no daemon was stopped to simulate a reboot.

| Route | Median first prompt | Median `/voice` menu availability |
|---|---:|---:|
| Installed 0.5, terminal on the Mac | 2.698 s | 6.449 s |
| 0.6 `--audio-only`, terminal on Linux | 0.590 s | 4.141 s |

`/voice` became available 2.308 seconds sooner, a 35.8% reduction in this
comparison. The first prompt appeared 78.1% sooner. `/voice` samples were
7.098 / 5.545 / 6.449 seconds before and 4.089 / 4.141 / 4.141 seconds after.
This is a small sample on this installation, not a latency guarantee.

The remaining delay includes Codex's own session initialization. Plain native
Codex on the same Linux host took 4.266 and 4.565 seconds to show `/voice` in a
separate comparison. On the Windows PC, plain native Codex showed its composer
in a median 0.236 seconds and `/voice` in 2.000 seconds. Early and already-ready
probes, including individual keystrokes, confirmed that the Windows gap was
not just paste handling. The installed Codex source [enables the voice command
after the session is configured](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/tui/src/chatwidget/session_flow.rs#L84-L88).

The Windows routes also reuse the package/build and Herdr configuration results
within each launch. The removed second calls cost about 48 ms and 8 ms on the
test PC. They are still read fresh on the next launch. Input origin, plugin
state and exact-build validation are never cached across launches.

## Method and validation limits

Time starts when Enter is sent after `codex` is typed. Record the first usable
composer separately. Type `/voi` without pressing Enter and wait for the
actual `/voice` completion description; this measures availability without
opening a microphone. The Linux measurements poll the owned pane every 75 ms.
Quit each frontend, verify its exit status, and close only test-owned panes or
servers. Every frontend and private server in the reported comparison exited 0.
Normal user panes were not restarted.

An instrumented candidate confirmed zero microphone-helper invocations before
`/voice`. A subsequent local `--build-commit` probe verified that the instrument
worked and returned the expected build without SSH. Tests also verify binary
protocol forwarding, rejection of unsupported probe arguments, preservation of
installed packages, exact-build repair checks, native argument forwarding and
pairing repair instructions.

The MacBook was unreachable during these measurements. The Mini has speakers
but no input device, so the new Unix-to-Mac route has **not** received a physical
speech/playback test. Startup success does not establish that result. Existing
voice installations were not migrated to this mode. The already verified
Windows-to-Mac route uses the same Mac helper, but that is not a substitute for
testing the new direction.

Before adopting the preview on a user's setup, verify speech and playback,
resume an existing thread, check image workflows, and test exit/interrupt
cleanup. Measure `/voice` connection time too: SSH and the Mac app now start
when the helper is needed, so startup savings must not be mistaken for a
measurement of first-word latency. See [the agent installation guide](AGENT-INSTALL.md#faster-mac-to-unix-startup)
for the one-time pairing command and rollback.
