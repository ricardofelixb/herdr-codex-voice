# Herdr input-origin community canary

This package builds an independent Herdr v0.9.3 canary for automatic microphone
selection by the Herdr Codex Voice plugin. It exposes the last accepted input
origin for each terminal. It is not an official Herdr release.

The exact upstream commit is `7b116c05bfda646af39d2524c54e70c751f57ee8`.
The resulting version is `0.9.3-voice-canary.20261006.1`. Protocol 22 and the frozen
client endpoint generation 1 stay unchanged. Stock generation-1 clients can
connect; the work host needs the patched server and SSH bridge.

## Contents and licensing

`herdr-v0.9.3-input-origin.patch` contains the source changes and focused tests.
`manifest.json` pins the base commit, annotated tag, source tree, toolchain,
patch, complete source manifests, schema, and license hashes. `base-files.json`
and `source-files.json` record SHA-256 for every source entry before and after
the patch, including the two input-origin modules. `SHA256SUMS` covers the
package files, including its build helpers and verification record.
The included `.gitattributes` preserves those exact bytes on Windows checkouts.

Herdr declares Apache-2.0. `LICENSE.herdr` is its unmodified license. The
packaging scripts and community changes use the same license. Modified source files carry notices pointing to `HERDR_VOICE_CANARY_CHANGES.md`, which is
also added to the patched tree. Preserve the original source notices and
vendored component licenses when redistributing source or binaries. This
package does not claim upstream endorsement or permission to use its trademarks.

## Prepare a separate source directory

Requirements: Git and Python 3.8 or newer. Use a new directory, not a checkout
containing other work. The following commands fetch only the public upstream
repository and apply the included local patch:

```sh
git -c core.autocrlf=false clone --branch v0.9.3 --single-branch \
  https://github.com/herdrdev/herdr.git herdr-source
git -C herdr-source rev-parse HEAD
python3 prepare.py --source herdr-source --apply
```

On Windows, use `py -3` in place of `python3`, and enter the clone command on one
line. HEAD must be the exact commit above. `prepare.py` verifies all base-file
hashes before `git apply --check`, applies the patch, then verifies every patched
file. It never resets or cleans a checkout. Without `--apply`, it only verifies
an already patched tree. No credentials or machine configuration are included.

## Build

Install Rust **1.96.1** and Zig **0.16.0** from their official distributions.
Verify official download checksums before executing tool installers. Prefer a
task-specific `CARGO_HOME` and `RUSTUP_HOME`; rustup's `--no-modify-path` keeps
shell startup files unchanged. Set `ZIG` to the pinned Zig executable if it is
not on the build process's PATH. Use process-local environment variables rather
than changing global PATH or the user's default toolchain.

Linux needs a C toolchain and the usual native linker prerequisites. macOS needs
Xcode Command Line Tools. Windows x64 needs Visual Studio 2022 C++ Build Tools
and its Windows SDK; run the build in its x64 Native Tools environment.

```sh
python3 build.py --source herdr-source --output canary-output --jobs 2 --tests
```

The build uses `cargo --locked`, release optimization level 1, LTO disabled, and
64 codegen units. It builds only the Herdr binary. `--tests` runs the input-origin,
frozen endpoint, dedicated handshake, and generated-schema tests. No full suite
or full workspace check runs. Omit `--tests` to build without executing test
binaries. Use `--offline` only after the pinned dependencies are cached, and
`--target-dir` to reuse a separate task build cache.

For CI, `--tests-only` runs the same four filters and requires exactly 12, 10,
1, and 1 passing tests. It skips the separate release binary build and writes
`focused-tests.json`. The dedicated `canary-check.yml` workflow exercises this
on a normal Windows runner; local Windows policy can allow the release binary
while blocking the separate unit-test executable.

On Windows, build without `--tests` until execution of locally built binaries is
permitted by the machine's policy:

```powershell
py -3 build.py --source herdr-source --output canary-output --jobs 4 --target x86_64-pc-windows-msvc
```

`build.py` verifies the source and toolchain versions, writes a versioned binary,
and records its SHA-256 and provenance in adjacent `.sha256` and `.json` files.
It does not run or install the produced binary. Source and build inputs are
pinned; identical binary bytes across different compilers, SDK installations,
or build paths are not claimed.

For a Windows runtime bundle, follow upstream's
`scripts/package_windows_conpty.ps1` with its pinned Microsoft ConPTY package.
Keep the Microsoft signatures and required license files. A successful build
alone is not Windows runtime verification.

## Install without losing sessions

Keep a versioned copy of the artifact, its hash, and the previous binary. Use the
plugin installer's checks to determine whether the local server advertises
`pane_last_input`. A feature already present on a newer upstream server does not
require replacing it with this older canary.

Before changing an existing installation, inspect that exact server and record
its pane shell PIDs. On Unix, an authorized migration can preserve running panes
with the existing CLI:

```sh
herdr server live-handoff --import-exe /absolute/path/to/canary \
  --expected-protocol 22 --expected-version 0.9.3-voice-canary.20261006.1
```

Retain the existing session/socket environment for that command. Afterwards,
verify the server version, capability, original pane PIDs, and session state.
Then update the installation's normal executable path using its existing
atomic replacement/rollback workflow. Do not use a stop/start as a substitute
for handoff. Failed handoff must leave the existing server available.

Windows does not support live handoff in this upstream version. First validate
an explicitly isolated new session. Do not stop a user's server or overwrite a
running executable as part of automatic installation. If Windows policy blocks
a locally built artifact, report that boundary and leave the current install
and policy unchanged. Never disable Smart App Control, remove quarantine marks,
or change signing/security policy to make the canary run.

See [Windows activation](WINDOWS-ACTIVATION.md) for selecting a future client
without replacing its running server, using a separate named session, and the
explicit approval boundary for a default-session restart.

A newly started or handed-off server has no last-input history. The next real
keyboard, text, or paste event establishes it. API-driven pane input remains
marked as API input and must not choose a human microphone.

## Query from agents and plugins

Read [WINDOWS-API.md](WINDOWS-API.md) for the Windows transport. On every
platform, the canary provides:

```sh
herdr status server --json
herdr pane last-input PANE_ID
```

The first returns a top-level `capabilities.pane_last_input` boolean. The second
returns the normal `{ "id": ..., "result": ... }` JSON wrapper; no extra
`--json` flag is needed. Use the current pane's `HERDR_PANE_ID`, and inherit
`HERDR_SOCKET_PATH` and `HERDR_SESSION` to address the correct server.

Require `source == "client"` and `client.connected == true`. Unknown origin,
API input, a disconnected client, missing/ambiguous pairing, and transport
errors must stop automatic microphone activation. Treat origin as metadata
from the local user session, not as an authentication or authorization claim.

## Verification scope

The original canary's input-origin behavior was exercised on Linux and both
macOS architectures, including real SSH input attribution, isolated live
handoff, and native voice routing. This package adds CLI capability propagation
and source modification notices. `verification.json` records the checks run on
this exact patch and whether any platform remains unexecuted. Do not describe a
build-only Windows artifact as a tested Windows release.
