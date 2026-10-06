# Codex Voice for Herdr

Open a remote machine's pane, type `codex`, and use native `/voice`.
Codex's interface and audio run on your microphone computer. Its backend,
project files, and commands stay on the work computer you selected in Herdr.
No helper pane appears on the microphone computer. Normal launches print no
plugin banner or setup messages.

This is an early release. macOS and Linux are supported; use WSL for a Windows
work computer. Native Windows and mobile microphone hosts are not supported.
Tailscale provides reachability, not audio forwarding or SSH authorization.

The Linux-to-macOS canary has verified native Codex launch through a hidden
desktop app, remote command execution, resume, resizing, and cleanup after a
disconnect without a helper pane. The original direct SSH launch was rejected
by macOS's microphone permission system. Version 0.2 uses the desktop app to give macOS a normal
application permission context. A voice conversation still awaits a microphone
on the Mac mini canary; do not treat audio as fully validated yet.

## Install once per work computer

Requirements:

- Herdr 0.9.3+, Python 3.9+, OpenSSH, and Codex CLI with `--remote` and
  `app-server daemon start` on the work computer. Tested with Codex 0.160.0.
- Codex CLI, Python 3.9+, and an SSH server on the microphone computer.
  Native voice must work there with a microphone and speakers or headset.
- On a microphone Mac, log into its desktop as the SSH user. Apple's Command
  Line Tools are required for the one-time app build. If missing, install them
  with `xcode-select --install`. Allow **Codex Voice** microphone access when
  macOS asks during the first voice session.
- Working SSH from the work computer to the microphone computer, including
  remote Unix socket forwarding. Use a Tailscale name or an existing SSH alias.

On the work computer:

```sh
herdr plugin install ricardofelixb/herdr-codex-voice
$HOME/.local/bin/codex-voice setup you@your-microphone-computer
```

Setup discovers executable paths, checks the backend, saves the pairing, and
adds a marked alias block to `.bashrc` or `.zshrc`. It preserves a backup of the
original file. Open a new Herdr terminal, then:

```sh
codex
```

Use `/voice` in Codex. Setup persists across terminal sessions and reboots;
nothing needs to be installed in the microphone computer's Herdr. Mac setup
installs a small background app under `~/.local/share/herdr-codex-voice`.
You can also choose **Set up Codex
voice** from Herdr's plugin actions. To change the microphone computer, run setup
again with its SSH alias. Bash and Zsh receive automatic integration; other
shells can call `codex-voice run` directly.

`codex resume --last`, prompts, model options, and `-C` are forwarded.
`codex exec`, `codex login`, help, version queries, and other utility commands
keep using the work computer's binary. Outside Herdr, `codex` is unchanged.
`command codex` bypasses the alias. A local pane on the paired microphone
computer uses Codex directly. Disabling or uninstalling the plugin restores
ordinary Codex behavior, even before you remove the shell alias.

## How it works

```text
Work computer's Herdr pane
  └─ one SSH connection to microphone computer
       ├─ native Codex terminal + microphone + speakers
       │    macOS: launched by a background desktop app on the same terminal
       └─ private forwarded Unix socket → work computer's Codex backend
```

The backend's normal managed daemon is started only if needed. The SSH process
owns the terminal and forwarding. Each run has a unique socket, removed when
Codex exits. The plugin creates no Herdr workspaces, private servers, or audio
relay processes. On macOS a small app launches Codex with the existing SSH
terminal as its input and output; it opens no window and exits with Codex.
This gives microphone requests a desktop application identity instead of the
SSH server's identity. The app has a microphone usage description and audio
input entitlement and uses the normal macOS consent flow.

The plugin adds no prompts, model overrides, or approval settings. Shell
profiles are loaded only during setup or when refreshing an older helper.
The app is compiled once per app revision. Helper updates keep the pairing,
and existing sessions continue using their original app executable.
An update that changes the app binary may require microphone approval again
on the Mac. Python-only helper updates reuse the existing app binary.

Install on any supported work device with the same two commands; usernames,
hostnames, paths, and Python installations are discovered, not built into the
plugin. No reverse SSH login or extra inbound TCP port is needed on the work
computer. SSH keys and Codex credentials stay in their existing locations.

## Troubleshooting

- **SSH connection error:** first make `ssh -o BatchMode=yes YOUR-HOST true`
  work from the work computer. Put a custom user, port, key, or proxy in
  `~/.ssh/config`; setup accepts the alias. Trust its host key with a normal
  `ssh YOUR-HOST` connection first.
- **Forwarding denied:** the microphone computer's SSH server must permit
  remote Unix socket forwarding. Managed Tailscale SSH servers may differ
  from OpenSSH; use an OpenSSH server reached over the tailnet.
- **Microphone unavailable:** connect an input device, allow microphone access
  in the OS, and test native Codex there. Voice is provided by Codex itself.
  On macOS, allow **Codex Voice** in Privacy & Security → Microphone and ensure
  the SSH user is logged into the desktop. If permission is stuck after an app
  update, run `tccutil reset Microphone dev.herdr.codex-voice` on that Mac, then
  restart the voice session and approve its new request.
- **Executable or helper moved after an update:** rerun setup to refresh the
  discovered Python, Codex, and Node paths or reinstall a removed helper.
  Stable Codex installer links are kept intact; changing Node installations
  may still need setup again.
- **Slow Codex startup:** plugin launch is quiet, but Codex's own updates, MCP
  startup, and login prompts still appear. The plugin does not disable them.
- **Different microphone computer:** rerun setup. Herdr's plugin context does
  not identify the physical client reliably, so the pairing is explicit.
- **Native remote limitations:** files read by the frontend, including image
  attachments and frontend profiles, must exist on the microphone computer.
  The project directory and agent tools run on the work computer.

## Update or remove

```sh
herdr plugin install ricardofelixb/herdr-codex-voice
```

Herdr 0.9.3 updates plugins by reinstalling them. Its build step refreshes the
standalone launcher; pairing and shell setup survive updates. On the next
launch, any changed Mac helper is updated automatically over SSH. To remove it:

```sh
$HOME/.local/bin/codex-voice unsetup
herdr plugin uninstall herdr-codex-voice
```

Open a new terminal afterward. Herdr retains plugin configuration; the small
launcher at `~/.local/bin/codex-voice` can also be removed. Unsetup removes only
the marked block, preserving later edits to your shell file.
The Mac helper can be shared by several work computers. After closing their
voice sessions, remove `~/.local/share/herdr-codex-voice` on the microphone Mac
when no work computer needs it anymore. It installs no login item or daemon.

## Development and publishing

```sh
python3 -m unittest discover -s tests -v
herdr plugin link "$PWD"
python3 voice.py install
```

The root manifest is ready for direct GitHub installation. Adding the GitHub
topic `herdr-plugin` opts the public repository into the Herdr Marketplace
index. This is a separate publication step.

See [Herdr plugins](https://herdr.dev/docs/plugins/),
[marketplace publishing](https://herdr.dev/docs/marketplace/), and
[Codex app-server](https://learn.chatgpt.com/docs/app-server).
