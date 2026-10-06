# Installing Codex Voice: guide for agents

This guide is for an AI agent that installs, checks, updates or removes Codex
Voice for a person. Every step is a non-interactive command. A person is only
needed for things an agent cannot or must not do: approving the OS microphone
prompt, trusting an unverified SSH host key, security-policy decisions, and
confirming that speech and playback actually work.

Commands below use `codex-voice`, installed at `$HOME/.local/bin/codex-voice`.
Use that full path if `~/.local/bin` is not on `PATH`. On Windows it is a Python
file: in PowerShell run `py -3 "$env:USERPROFILE\.local\bin\codex-voice" ...`.
Commands in `next_steps` are already written to run as is on that computer; on
Windows they are PowerShell commands that start Python and the launcher with
the call operator (`& 'C:\Program Files\...\python.exe' '...\codex-voice' ...`),
so paths with spaces work.
Replace `HOST` with the SSH alias or `user@host` of the computer the person sits
at, and `NAME` with a short label such as `Laptop`.

## Two computers

- **Work computer:** runs Herdr, the project files, the commands and Codex's
  backend. Install the plugin and run every command in this guide here.
- **Microphone computer:** where the person sits, with a microphone and speakers
  or a headset. The work computer reaches it over SSH. It can be the work
  computer itself (`--local`). Nothing is installed in its Herdr.

## Supported directions

| Microphone computer | Work computer: macOS or Linux (WSL counts as Linux) | Work computer: native Windows |
|---|---|---|
| macOS | Supported. Codex's terminal runs on the Mac through a background **Codex Voice** app. | Experimental. Codex's terminal stays on Windows; only its voice helper runs on the Mac, through a separate **Codex Voice Audio** app. |
| Linux desktop | Supported. Codex's terminal runs there. | Not supported |
| Windows | Experimental. Only Codex's voice helper runs on the PC. | Not supported |
| Same computer (`--local`) | Supported | Experimental (plain Codex) |
| Phone or tablet | Not supported | Not supported |

Both "voice helper only" routes need exactly the same Codex build on the two
computers; setup picks an installed matching helper and never upgrades Codex.
A Codex terminal on macOS or Linux cannot open threads on a Windows Codex
backend, which is why a Windows work computer keeps its terminal. The native
Windows work computer route passed a physical speech and playback test with a
Mac microphone on 2026-10-06 using matching Codex 0.160 builds. It remains a
preview because it uses Codex's private helper protocol. Validate each user's
own installation using the final physical checks below.

## Requirements

Work computer: Herdr 0.9.3 or later, Python 3.9+, OpenSSH client, and Codex CLI
with `--remote` and `codex app-server daemon start` (tested with Codex 0.160).
Bash or Zsh for the automatic `codex` alias.

Microphone computer:

- macOS or Linux: an SSH server that allows remote Unix socket forwarding,
  Python 3 and Codex CLI on the login shell's `PATH`, and native Codex voice
  working there. On a Mac, the SSH user must be logged into the desktop, and
  Apple's Command Line Tools must be installed (`xcode-select --install`, done
  by the person).
- Windows: an OpenSSH server whose sessions start in the user's home directory,
  Python 3 as `py -3` or `python`, and Codex with exactly the same build as the
  work computer's Codex. Setup never upgrades Codex.

SSH from the work computer must work without prompts:
`ssh -o BatchMode=yes HOST true`. Put users, ports, keys and proxies in
`~/.ssh/config` and pass the alias. Tailscale names work; Tailscale provides
reachability, not SSH authorization.

### Native Windows work computer (experimental)

- Herdr for Windows, run as the normal (non-elevated) desktop user. Run setup,
  `build-relay` and `codex` in a normal Herdr pane, not an administrator's SSH
  session: Codex's daemon refuses elevated launchers and files created there may
  be unreadable to the desktop user. Setup and `doctor` report `windows_elevated`.
- Python 3.9+ as `py -3` or `python`, and Windows' OpenSSH Client (`ssh.exe`).
- Codex installed as the official npm package or a standalone package, so a
  native `codex.exe` sits beside `codex-resources\voice`. npm's `codex.cmd` is
  resolved to its `codex.js` and native package; it is never run through a shell.
- Rust with the MSVC toolchain (`cargo`), once, to build the plugin's small
  voice relay from source (`windows/relay`, no dependencies, built offline).
  Setup builds it; `codex-voice build-relay --json` rebuilds it after an update.
  Visual Studio Build Tools' installer may ask the person to approve it.
- PowerShell for the automatic `codex` function, whose existing effective
  execution policy lets it run the person's profile scripts; `doctor` checks
  this in each installed edition (`shell_alias_not_loaded`). The plugin never
  changes execution policy, Smart App Control or other security settings.
- A Mac microphone computer meeting the macOS requirements above, with Codex of
  exactly the same build as the Windows Codex. If `codex` on PATH has another
  build than the Mac but a matching Codex package is installed, name its
  `codex.exe` explicitly: `codex-voice pair NAME HOST --codex PATH --json`.
  Setup never upgrades either side.

Automatic microphone selection works on Windows with the same Herdr canary as
elsewhere; the plugin asks Herdr's CLI (`herdr status server --json`,
`herdr pane last-input PANE`) instead of its socket.

If Herdr's build step (`py -3 voice.py install`) could not install the launcher
on Windows, run setup from the plugin directory reported by
`herdr plugin list --plugin herdr-codex-voice --json` (`plugin_root`):
`py -3 "PLUGIN_ROOT\voice.py" setup HOST --json` in PowerShell. Setup installs
the launcher at `$env:USERPROFILE\.local\bin\codex-voice`; afterwards run it in
PowerShell as `py -3 "$env:USERPROFILE\.local\bin\codex-voice" ...`.

## 1. Discover

```sh
uname -s
python3 --version
herdr --version
codex --version
ssh -o BatchMode=yes HOST true && echo ssh-ok
"$HOME/.local/bin/codex-voice" doctor --json --offline   # only if already installed
```

Ask the person which computer they sit at and how it is reached by SSH. Do not
guess hostnames, and do not read or print SSH keys, `~/.codex/auth.json` or other
credentials. If `ssh` asks to trust a host key, stop and let the person verify the
fingerprint; never disable host key checking.

## 2. Install the plugin

```sh
herdr plugin install --yes --ref windows-multihost ricardofelixb/herdr-codex-voice
"$HOME/.local/bin/codex-voice" doctor --json --offline
```

`--yes` makes the install non-interactive. Until this release is merged, the
`--ref` is required: the default branch still holds an older version. Herdr's
build step installs the `codex-voice` launcher (`python3` on macOS and Linux,
`py -3` on Windows). Before pairing, the offline doctor reports `no_pairing`;
anything else that fails must be fixed first.

## 3. Pair

```sh
codex-voice setup HOST --json          # one microphone computer
codex-voice pair NAME HOST --json      # each of several microphone computers
codex-voice setup --local --json       # this computer's own microphone
```

Pairing is idempotent; repeat it to refresh after Codex or the microphone
computer changes. `setup HOST` also refreshes named pairings with that exact
alias. `codex-voice unpair NAME --json` forgets one (`--default` forgets the
one made by `setup`). On a Windows work computer, `--codex PATH` pairs with an
explicitly chosen installed `codex.exe` instead of the one on PATH.

Pairing writes only:

- `~/.local/bin/codex-voice` (refreshed launcher);
- a marked block in `~/.bashrc` or `~/.zshrc` defining `codex` only inside Herdr
  panes (the original is kept once as `.before-codex-voice`); on Windows, the
  same block at the end of each PowerShell edition's console profile
  (`$PROFILE.CurrentUserCurrentHost`, which loads after the other profiles),
  keeping the profile's encoding, byte order mark and line endings. In Herdr
  panes only, it also removes a `codex` alias, which would otherwise outrank the
  function;
- pairing files in Herdr's configuration directory for this plugin;
- on a Mac microphone computer, `~/.local/share/herdr-codex-voice` (small
  background apps, compiled once);
- on a Windows microphone computer, one launcher in `$env:USERPROFILE\.herdr-codex-voice`;
- on a Windows work computer, the relay built from source and, at launch,
  private copies of the Codex package in the plugin's configuration directory.

It never modifies Codex installations, copies credentials, accepts host keys,
changes OS privacy or security settings, or creates login items or daemons.
Codex's own app-server daemon starts as it would for any `codex` launch.
On Windows, `codex-voice build-relay --json` builds the relay without pairing.

## 4. Diagnose

```sh
codex-voice doctor --json                       # everything, including SSH
codex-voice doctor --json --offline             # this computer only
codex-voice doctor --json --microphone NAME     # one pairing
```

`doctor` is read-only. It checks the platform, Herdr and the plugin, whether the
launcher matches the installed plugin, Codex's version and build, the backend
socket, the shell integration (both new shells and the current one), how the
microphone is chosen at launch, and for each pairing: SSH, Codex on the
microphone computer, remote socket forwarding, and, where the OS exposes it,
microphone permission (macOS: the Codex Voice or Codex Voice Audio app's
permission; Windows: the privacy settings for desktop apps). On a Windows work
computer it also checks the relay, that a plain `codex` in a new Herdr session
of each PowerShell edition runs exactly this launcher (not a later alias or
function, and not blocked by execution policy), and that the Mac's
helper matches the Windows Codex build. The Codex Voice app's permission may
read `unknown`; that is not a failure.

Run it in the Herdr pane being checked when you need the `this_shell` result.
Outside a pane, or from a process started before setup, it reports `unknown`.

`"ok": true` only means no automated check failed. `"voice_verified"` is always
`false`: nothing here opens a microphone or plays audio.

## 5. Hand off to the person

Give the person the `human` steps from `next_steps`, then these checks from
`physical_verification`, and wait for their answer before saying voice works:

1. Herdr panes opened before setup: quit Codex there and run `source ~/.bashrc`
   (or `source ~/.zshrc`; PowerShell: `. $PROFILE.CurrentUserCurrentHost`) once, or
   open a new pane. New panes and later reboots need nothing.
2. In a new pane on the work computer, run `codex`, then `/voice`.
3. On a Mac, click **Allow** when macOS asks about **Codex Voice** (or **Codex
   Voice Audio** for a Windows work computer); first time only, and possibly
   again after an update that changes the app.
4. Speak; confirm the words are recognized. Confirm Codex's spoken reply plays
   on the microphone computer.

## Choosing among several microphones

With several pairings, released Herdr asks which microphone to use each time
`codex` starts; a single pairing never asks. Automatic selection uses the
computer that last typed into the pane. It requires a Herdr server and SSH
bridge that report the `pane_last_input` capability, which released Herdr
0.9.3 lacks. This repository packages that change as a source canary with its
own build and installation instructions: [canary/README.md](canary/README.md)
(Apache-2.0, separate from the plugin's MIT license). Installing the plugin does
not install the canary. Automatic selection also needs the Tailscale CLI on each
microphone computer at pairing time and on the work computer.

`doctor` shows `route_selection.mode`: `single`, `prompt` (released Herdr),
`automatic`, `unknown` (not run inside a pane) or `none`. Fix
`duplicate_identity` with `unpair`, and `pairing_without_identity` by installing
Tailscale on that computer and pairing it again.

## Update

```sh
herdr plugin install --yes --ref windows-multihost ricardofelixb/herdr-codex-voice
codex-voice doctor --json
```

Pairings and shell setup survive updates. A changed Mac helper is updated over
SSH at the next launch (`desktop_helper_stale` until then; pairing again updates
it now). For a Windows microphone, a changed work computer Codex build is
re-paired at the next launch; if the PC has no matching build, `doctor` reports
`windows_helper_build_mismatch` and the person decides whether to install it.
A Windows work computer likewise re-pairs its Mac at the next launch after Codex
or the plugin changes (`mac_helper_build_mismatch` if the Mac lacks the build),
and an update that changes the relay source needs `codex-voice build-relay --json`
(`windows_relay_missing` until then).

## Uninstall

```sh
codex-voice unsetup --purge --json
herdr plugin uninstall herdr-codex-voice
```

`unsetup` removes only the marked shell block; `--purge` also removes the
launcher and this plugin's pairing, relay and package-copy files. Open a new pane
afterwards. Files on microphone computers can be shared by several work
computers, so removing them is the person's decision:
`~/.local/share/herdr-codex-voice` on a Mac, `$env:USERPROFILE\.herdr-codex-voice`
on Windows.

## Output reference

Every `--json` command prints one JSON document on stdout:

```json
{
  "schema": "herdr-codex-voice/1",
  "command": "setup",
  "version": "0.5.0",
  "ok": false,
  "result": null,
  "error": {"code": "ssh_auth_failed", "message": "..."},
  "next_steps": [{"actor": "human", "text": "...", "command": "ssh -o BatchMode=yes HOST true"}]
}
```

`doctor` adds `work_host`, `microphones`, `route_selection`, `checks`,
`voice_verified` and `physical_verification`. Each check has `scope`
(`work`, `routes` or `microphone:NAME`), `check`, `status` (`ok`, `warn`,
`fail`, `unknown`, `skip`), `message`, and when relevant `code` and
`next_step`. `actor` is `agent` for steps an agent may run and `human` for steps
that need the person. Pairings are summarized without executable paths, Codex
home directories or network identifiers.

Exit status: `0` success, `1` failed (see `error.code`), `2` usage error,
`130` interrupted.

### Codes

| Code | Meaning | Who fixes it |
|---|---|---|
| `usage` | Missing or unknown argument | agent |
| `invalid_host`, `invalid_name` | Host must be an SSH alias or `user@host`; names are 1–40 letters, digits, spaces, `_` or `-` | agent |
| `unsupported_platform` | Work computer OS other than macOS, Linux or Windows | not supported |
| `experimental_route` (warning) | Native Windows work computer: preview route, Mac microphone only | none |
| `unsupported_route` | A Windows work computer paired with a non-Mac microphone computer | agent |
| `herdr_unavailable`, `herdr_too_old` | Herdr missing, not answering, or older than 0.9.3 | agent |
| `plugin_not_installed`, `plugin_disabled` | Plugin missing or disabled in Herdr | agent / person |
| `launcher_missing`, `launcher_stale` | Launcher missing or older than the plugin; run the given `voice.py install` | agent |
| `launcher_conflict` | `~/.local/bin/codex-voice` belongs to another program | person |
| `codex_missing`, `codex_broken`, `backend_unavailable` | Codex missing, `codex --version` fails, or its daemon gave no Unix socket | agent |
| `windows_elevated` | Run in a normal Herdr pane, not an administrator session | agent |
| `no_pairing`, `pairing_invalid` | Nothing paired, or a pairing file is damaged | agent |
| `shell_integration_missing`, `shell_integration_stale`, `shell_alias_not_loaded` | New shells will not get `codex`: no current block, a later alias or function redefines it, or (PowerShell) execution policy refuses the profile; rerun setup, or see `next_step` | agent / person |
| `shell_reload_needed` | This pane predates setup; reload once | person |
| `shell_unsupported` | Not Bash, Zsh or PowerShell; use `codex-voice run` | person |
| `ssh_host_key_unknown`, `ssh_host_key_changed` | Host key not trusted, or changed; the person verifies the fingerprint | person |
| `ssh_auth_failed` | SSH asks for a password or rejects the key; set up key-based login | agent |
| `ssh_host_unresolved`, `ssh_unreachable` | Name does not resolve, or computer is off or offline | agent |
| `ssh_forwarding_denied` | SSH server refuses remote Unix socket forwarding (a server security setting) | person |
| `microphone_codex_missing`, `microphone_codex_moved`, `microphone_codex_broken`, `microphone_probe_failed` | Codex or Python missing or failing on the microphone computer | agent |
| `codex_version_mismatch` (warning) | Different Codex versions on the two computers | person decides |
| `mac_command_line_tools_missing` | Install Apple's Command Line Tools on the Mac | person |
| `mac_desktop_not_logged_in` | The SSH user is not the active desktop user | person |
| `mac_desktop_helper_failed`, `mac_desktop_helper_missing`, `mac_desktop_app_missing` | Mac helper could not be built or is gone; pair again | agent |
| `desktop_helper_stale` (warning) | Updated automatically at next launch | none |
| `mac_microphone_not_determined` (warning) | macOS will ask at the first `/voice` | person |
| `mac_microphone_denied`, `mac_microphone_restricted` | Allow Codex Voice in Privacy & Security, or ask the administrator | person |
| `windows_layout_unsupported` | The work computer's Codex install cannot be copied for the Windows route | agent |
| `windows_home_not_cwd` | Windows SSH sessions must start in the home directory | agent |
| `windows_helper_build_mismatch` | The PC lacks this exact Codex build; install it (ask before replacing one in use) | agent |
| `windows_launcher_missing`, `windows_helper_missing` | Pair the PC again | agent |
| `windows_microphone_denied` | Turn on Microphone access and Let desktop apps access your microphone | person |
| `windows_relay_missing` | Build the relay: `codex-voice build-relay --json` | agent |
| `windows_relay_toolchain_missing` | Install Rust with the MSVC toolchain | agent |
| `windows_relay_build_failed`, `windows_relay_config` | The relay did not build, or a setting cannot be written safely | agent |
| `ssh_missing` | Add Windows' OpenSSH Client | agent |
| `mac_helper_build_mismatch` | The Mac lacks the Windows Codex build; install it there or pass `--codex` (ask before replacing one in use) | agent |
| `mac_helper_missing` | The Mac's Codex helper moved; pair again | agent |
| `mac_audio_controller_stale` (warning) | Updated automatically at next launch | none |
| `automatic_selection_unavailable` (warning) | Several pairings and no `pane_last_input`: Codex asks at launch | person chooses |
| `pairing_without_identity` (warning), `duplicate_identity` | Automatic selection cannot tell computers apart | agent |
| `origin_not_typed`, `origin_disconnected`, `origin_unavailable`, `origin_no_match`, `origin_lookup_failed`, `tailscale_cli_missing`, `tailscale_unidentified` | A launch in this pane would stop instead of guessing a microphone; see `next_step` | agent / person |
| `timeout`, `interrupted`, `failed` | Retry; `failed` carries the underlying message | agent |

## Rules

- Always pass `--json` and every value on the command line. Never feed answers
  into prompts.
- Never print, copy or move SSH keys, Codex credentials or tokens.
- Routine prerequisites (Python, Codex, Rust, Tailscale CLI, key-based SSH
  login, the Herdr plugin) are agent work once the person has asked for this
  setup. Ask before replacing a Codex version the person relies on.
- Do not trust unverified host keys or change security policy on the person's
  behalf: Smart App Control, execution policy, Gatekeeper, quarantine,
  microphone permissions, SSH forwarding policy, accounts or keys. Tell them
  exactly what to change.
- Do not edit, rename or patch Codex installations.
- Do not report voice as working until the person confirms speech recognition
  and playback on the microphone computer.
