# Codex Voice for Herdr

Open a Herdr pane on your work computer, type `codex`, and use native `/voice`
with the microphone and speakers of the computer you are sitting at. Project
files, commands and Codex's backend stay on the work computer. Normal launches
print no plugin banner and open no helper pane.

**Agents:** follow [AGENT-INSTALL.md](AGENT-INSTALL.md). Setup, pairing,
diagnostics and removal take `--json`, never prompt, and return stable error
codes.

## Supported directions

| Microphone computer | Work computer: macOS or Linux (incl. WSL) | Native Windows work computer |
|---|---|---|
| macOS | Supported | Experimental preview (voice helper only) |
| Linux desktop | Supported | Not supported |
| Windows | Experimental (voice helper only) | Not supported |
| Same computer (`--local`) | Supported | Experimental (plain Codex) |

This is an early release. Phones and tablets are not supported. Tailscale
provides reachability, not audio forwarding or SSH authorization.

## Install once per work computer

Requirements: Herdr 0.9.3+, Python 3.9+, OpenSSH and Codex CLI on the work
computer (tested with Codex 0.160). On the microphone computer: Codex CLI,
Python 3, an SSH server, and native voice working there. SSH from the work
computer must work without prompts (`ssh -o BatchMode=yes HOST true`); put
users, ports and keys in `~/.ssh/config` and use the alias.

```sh
herdr plugin install --yes --ref windows-multihost ricardofelixb/herdr-codex-voice
$HOME/.local/bin/codex-voice setup you@your-microphone-computer
$HOME/.local/bin/codex-voice doctor
```

Until this release is merged, keep `--ref windows-multihost`; the default branch
holds an older version.

Setup discovers paths, checks the backend, saves the pairing and adds a marked
alias block to `.bashrc` or `.zshrc` (keeping a backup of the original). Open a
new Herdr pane, type `codex`, then `/voice`. A pane that was already open still
has its old shell: quit Codex there and run `source ~/.bashrc` (or
`source ~/.zshrc`) once, or open a new pane. New panes and reboots need nothing.
Bash and Zsh get the alias; other shells can run `codex-voice run`. Herdr's
plugin actions also offer **Set up Codex voice**.

`doctor` checks the installation, SSH, socket forwarding and, where the OS
exposes it, microphone permission. It cannot hear: test speech recognition and
playback once on each newly paired computer.

On a microphone Mac, log into its desktop as the SSH user. Apple's Command Line
Tools are needed once to build a small background app (`xcode-select --install`).
Allow **Codex Voice** microphone access when macOS asks during the first
`/voice`. Use `codex-voice setup --local` when Herdr runs on the computer whose
microphone you use.

## Several microphone computers

```sh
codex-voice pair Laptop you@your-laptop
codex-voice pair PC you@your-windows-pc
codex-voice unpair PC
```

With released Herdr, `codex` asks which microphone to use when several are
paired; one pairing never asks. A Herdr build reporting the `pane_last_input`
capability instead selects the computer that last typed into that pane, using
Tailscale node identities saved at pairing. Released Herdr 0.9.3 lacks it; this
repository packages it as a separately licensed source canary in
[canary/](canary/README.md), which the plugin does not install. Unknown, disconnected or ambiguous input never
silently selects a microphone. The route stays with that Codex session; restart
or resume Codex after switching computers.

## Windows microphone (experimental)

Only Codex's voice helper runs on the PC; the Codex terminal and backend stay on
the work computer. The PC needs an OpenSSH server whose sessions start in the
user's home, Python 3 as `py -3` or `python`, and Codex with **exactly the same
build** as the work computer. Setup selects a matching installed helper and
otherwise stops; it never upgrades Codex or downloads executables.

Each launch runs native `codex` from a private, content-addressed copy of the
work computer's Codex package (standalone or official npm), whose
`codex-voice-host` is replaced by a shim that runs one `ssh -T` to a small
launcher on the PC. Installed Codex files are never modified. SSH carries the
helper's binary stdin/stdout unchanged; audio goes directly between the PC and
OpenAI. This depends on Codex's package layout and is unsupported by Codex.

## Windows work computer with a Mac microphone (preview)

The same idea in reverse: Codex's terminal and backend stay on Windows, and only
the voice helper of the identical Codex build runs on the Mac, started by a
separate background app, **Codex Voice Audio**, so it has its own microphone
permission. In the private package copy, the helper is a small relay built once
from this repository's Rust source (`windows/relay`, no dependencies, needs
Rust's MSVC toolchain); it runs one fixed `ssh -T` and passes the helper's
binary stream through. Setup adds a `codex` function, for Herdr panes only, at
the end of each PowerShell edition's `$PROFILE.CurrentUserCurrentHost`. That
needs an execution policy that already runs your profile (`doctor` checks);
setup never changes execution policy or other security settings. This
route was verified with speech recognition and playback from a MacBook to a
native Windows work computer on 2026-10-06, using matching Codex 0.160 builds.
It remains a preview because it depends on Codex's private helper protocol. See
[AGENT-INSTALL.md](AGENT-INSTALL.md#native-windows-work-computer-experimental).

## Using codex

Prompts, `resume`, model options and `-C` are forwarded. Utility commands such
as `codex exec`, `login`, help and version queries run on the work computer.
Outside Herdr, `codex` is unchanged, and `command codex` bypasses the alias.
Disabling or uninstalling the plugin restores plain Codex. If Herdr cannot say
whether the plugin is enabled, `codex` stops with an error instead of silently
using the work computer's microphone.

Files read by the Codex terminal, such as image attachments and frontend
profiles, must exist on the microphone computer (Mac/Linux routes).

## How it works

For Mac or Linux microphones with a Unix work computer:

```text
Work computer's Herdr pane
  └─ one SSH connection to the microphone computer
       ├─ native Codex terminal + microphone + speakers
       │    macOS: started by a background desktop app on the same terminal
       └─ private forwarded Unix socket → work computer's Codex backend
```

Codex's managed daemon is started only if needed and keeps its default
behavior. Each run uses a unique socket, removed when Codex exits. No reverse
SSH login, extra inbound port or Herdr workspace is created. Windows routes
use the helper relay described above. On
macOS the app gives microphone requests a desktop application identity with a
usage description and audio-input entitlement; it opens no window and exits with
Codex. It is compiled once per app revision; an update that changes it may ask
for microphone approval again. SSH keys and Codex credentials stay where they are.

## Troubleshooting

Run `codex-voice doctor` first; each failure names a code and a fix (see
[AGENT-INSTALL.md](AGENT-INSTALL.md#codes)).

- **SSH errors:** make `ssh -o BatchMode=yes HOST true` work. Trust new host
  keys with a normal `ssh HOST` first.
- **Forwarding denied:** the microphone computer's OpenSSH server must allow
  remote Unix socket forwarding. Managed Tailscale SSH servers may differ.
- **Microphone unavailable:** connect a device and test native Codex there. On
  macOS allow **Codex Voice** in Privacy & Security → Microphone; if permission
  is stuck after an update, run `tccutil reset Microphone dev.herdr.codex-voice`
  on that Mac and approve again. On Windows, turn on Microphone access and Let
  desktop apps access your microphone.
- **Codex moved or was updated:** pair again; setup is idempotent.
- **Slow startup:** Codex's own updates, MCP startup and login prompts still
  appear; the plugin does not disable them.

## Update or remove

```sh
herdr plugin install --yes --ref windows-multihost ricardofelixb/herdr-codex-voice   # update
codex-voice unsetup --purge                         # remove alias, pairings, launcher
herdr plugin uninstall herdr-codex-voice
```

Updates keep pairings and shell setup; a changed Mac helper is updated over SSH
at the next launch. Plain `unsetup` removes only the marked shell block. Helper
files on microphone computers may be shared by several work computers; remove
`~/.local/share/herdr-codex-voice` (Mac) or `$env:USERPROFILE\.herdr-codex-voice`
(Windows) when none uses them. Nothing installs login items or daemons.

## Development

```sh
python3 -m unittest discover -s tests -v
herdr plugin link "$PWD"
python3 voice.py install
```

The root manifest supports direct GitHub installation. The GitHub topic
`herdr-plugin` opts the repository into the Herdr Marketplace index. See
[Herdr plugins](https://herdr.dev/docs/plugins/),
[marketplace publishing](https://herdr.dev/docs/marketplace/) and
[Codex app-server](https://learn.chatgpt.com/docs/app-server).
