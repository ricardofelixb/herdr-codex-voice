#!/usr/bin/env python3
"""Native Codex frontend on a microphone host; backend stays in the current pane."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import uuid

PLUGIN = "herdr-codex-voice"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
       "-o", "LogLevel=ERROR"]
START = "# >>> herdr-codex-voice >>>"
END = "# <<< herdr-codex-voice <<<"
LOCAL_COMMANDS = set("agents exec e review login logout mcp plugin app-server "
                     "remote-control completion update doctor sandbox debug apply a "
                     "queue archive delete migrate-rollouts unarchive cloud exec-server "
                     "features help".split())
VALUE_OPTIONS = {"-c", "--config", "--enable", "--disable", "--remote",
                 "--remote-auth-token-env", "-i", "--image", "-m", "--model",
                 "--local-provider", "-p", "--profile", "-s", "--sandbox",
                 "-C", "--cd", "--add-dir", "-a", "--ask-for-approval"}


def run(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=30, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or f"{argv[0]} exited {result.returncode}")
    return result.stdout


def config_dir():
    override = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(override or run([os.environ.get("HERDR_BIN_PATH", "herdr"),
                                "plugin", "config-dir", PLUGIN]).strip())


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(text)
    temporary.replace(path)


def install():
    # Herdr runs this build step on install/reinstall. One standalone copy also
    # keeps unsetup available after uninstall, with no extra interpreter at launch.
    path = Path.home() / ".local/bin/codex-voice"
    if path.exists() and "herdr-codex-voice" not in path.read_text():
        raise RuntimeError(f"{path} belongs to another program; leaving it unchanged")
    write_private(path, Path(__file__).read_text())
    path.chmod(0o755)
    return path


def shell_file():
    shell = Path(os.environ.get("SHELL", "/bin/bash")).name
    if shell == "zsh":
        return Path(os.environ.get("ZDOTDIR", str(Path.home()))) / ".zshrc"
    if shell == "bash":
        return Path.home() / ".bashrc"
    return None


def integrate(path, launcher=None):
    original = path.read_text() if path.exists() else ""
    pattern = r"\n" + re.escape(START) + r"\n.*?" + re.escape(END) + r"\n?"
    text = re.sub(pattern, "", original, flags=re.S)
    if launcher:
        alias = shlex.quote(shlex.quote(str(launcher)) + " run")
        text += "\n" + START + "\n" + (
            'if [ "${HERDR_ENV:-}" = 1 ]; then\n'
            f"  alias codex={alias}\nfi\n" + END + "\n")
    if text != original:
        if path.exists() and not path.with_name(path.name + ".before-codex-voice").exists():
            shutil.copy2(path, path.with_name(path.name + ".before-codex-voice"))
        # Follow an existing dotfile symlink; don't replace the user's link.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def probe(host):
    if not re.fullmatch(r"[\w.@:\[\]-]+", host) or host.startswith("-"):
        raise ValueError("Use an SSH alias or user@Tailscale-name; put ports and keys in ~/.ssh/config")
    code = '''import json, os, shutil, socket
codex = shutil.which("codex")
if not codex: raise SystemExit("Install Codex CLI on the microphone computer first")
node = shutil.which("node")
print("CODEX_VOICE=" + json.dumps({"codex": os.path.join(os.path.realpath(os.path.dirname(codex)), os.path.basename(codex)),
 "node_dir": os.path.dirname(os.path.realpath(node)) if node else "",
 "hostname": socket.gethostname(), "codex_home": os.environ.get("CODEX_HOME", "")}))
'''
    # NO_HERDR is an opt-out for shell snippets that auto-attach over SSH; Herdr
    # itself does not define it. Only setup loads these interactive profiles.
    command = 'NO_HERDR=1 "${SHELL:-/bin/sh}" -lic ' + shlex.quote(
        "python3 -c " + shlex.quote(code))
    output = run([*SSH, "-nT", host, command])
    lines = [line.removeprefix("CODEX_VOICE=") for line in output.splitlines()
             if line.startswith("CODEX_VOICE=")]
    if not lines:
        raise RuntimeError("Could not find Codex and Python 3 in the microphone computer's login shell")
    return {"host": host, **json.loads(lines[-1])}


def backend(codex):
    data = json.loads(run([codex, "app-server", "daemon", "start"]))
    path = data.get("socketPath", "")
    if not path.startswith("/") or not Path(path).is_socket():
        raise RuntimeError("Codex must provide a Unix socket. Use macOS, Linux, or WSL on this device.")
    return path


def setup(host=None):
    host = host or input("Microphone computer's SSH alias or user@Tailscale-name: ").strip()
    mic = probe(host)
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("Install Codex CLI on this work computer first")
    backend(codex)
    launcher = install()
    write_private(config_dir() / "config.json", json.dumps(mic, indent=2) + "\n")
    print(f"Saved microphone computer: {host}")
    path = shell_file()
    if path:
        integrate(path, launcher)
        print("Open a new Herdr terminal and type codex.\n"
              f"Existing terminal: source {shlex.quote(str(path))}")
    else:
        print(f"Bash/Zsh aliases only. In this shell, run: {shlex.quote(str(launcher))} run")
    print("Run /voice in Codex to check microphone permission and audio devices.")


def local_command(args):
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return False
        if arg in {"--remote", "--no-daemon", "--help", "-h", "--version", "-V"} or arg.startswith("--remote="):
            return True
        if not arg.startswith("-"):
            return arg in LOCAL_COMMANDS
        i += 2 if arg in VALUE_OPTIONS else 1
    return False


def frontend_args(args, cwd):
    args = list(args)
    has_cd = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break
        if arg in ("-C", "--cd", "--add-dir") and i + 1 < len(args):
            args[i + 1] = os.path.abspath(args[i + 1])
            has_cd |= arg != "--add-dir"
        elif arg.startswith(("--cd=", "--add-dir=")) or (arg.startswith("-C") and len(arg) > 2):
            prefix = arg.split("=")[0] + "=" if arg.startswith("--") else "-C"
            args[i] = prefix + os.path.abspath(arg[len(prefix):])
            has_cd |= prefix != "--add-dir="
        i += 2 if arg in VALUE_OPTIONS else 1
    return args if has_cd else ["--cd", cwd, *args]


def ssh_command(mic, backend_socket, args):
    remote_socket = "/tmp/hcv-" + uuid.uuid4().hex + ".sock"
    cleanup = shlex.quote("rm -f -- " + shlex.quote(remote_socket))
    env = ["env"]
    if mic["node_dir"]:
        env.append("PATH=" + mic["node_dir"] + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    if mic["codex_home"]:
        env.append("CODEX_HOME=" + mic["codex_home"])
    missing = "Codex moved; rerun codex-voice setup " + mic["host"]
    frontend = shlex.join([*env, mic["codex"], "--remote", "unix://" + remote_socket, *args])
    command = (f"trap {cleanup} EXIT; trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM; "
               f"test -x {shlex.quote(mic['codex'])} || {{ printf '%s\\n' {shlex.quote(missing)} >&2; exit 127; }}; "
               + frontend)
    # OpenSSH's default StreamLocalBindMask 0177 restricts this socket to its owner.
    return [*SSH, "-o", "ExitOnForwardFailure=yes", "-o", "EscapeChar=none",
            "-o", "ControlPath=none",
            "-R", remote_socket + ":" + backend_socket, "-tt", mic["host"], command]


def connect(args):
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex CLI is not on PATH")
    if local_command(args) or not (sys.stdin.isatty() and sys.stdout.isatty()):
        os.execv(codex, [codex, *args])
    try:
        plugins = json.loads(run([os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "list",
                                  "--plugin", PLUGIN, "--json"]))["result"]["plugins"]
        enabled = any(plugin.get("enabled") for plugin in plugins)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        enabled = False
    if not enabled:
        os.execv(codex, [codex, *args])
    path = config_dir() / "config.json"
    if not path.exists():
        raise RuntimeError("Run codex-voice setup once to choose your microphone computer")
    mic = json.loads(path.read_text())
    if mic["hostname"] == socket.gethostname():
        os.execv(codex, [codex, *args])
    command = ssh_command(mic, backend(codex), frontend_args(args, os.getcwd()))
    os.execvp(command[0], command)


def main():
    action, *args = sys.argv[1:] or ["help"]
    if action == "install":
        print(f"Installed {install()}. Next: codex-voice setup YOUR-MICROPHONE-HOST")
    elif action == "setup":
        setup(args[0] if args else None)
    elif action == "run":
        connect(args)
    elif action == "open-setup":
        subprocess.run([os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open",
                        "--plugin", PLUGIN, "--entrypoint", "setup"], check=True)
    elif action == "unsetup":
        path = shell_file()
        if path:
            integrate(path)
        print("Removed shell integration. Open a new terminal to restore your previous codex command.")
    else:
        print("codex-voice setup [SSH-HOST]   Pair once and enable codex in Herdr\n"
              "codex-voice run [CODEX-ARGS]  Run with the saved microphone computer\n"
              "codex-voice unsetup          Remove the shell integration")


if __name__ == "__main__":
    status = 0
    try:
        main()
    except KeyboardInterrupt:
        status = 130
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print("Codex Voice: " + str(error), file=sys.stderr)
        status = 1
    finally:
        if os.environ.get("HERDR_PLUGIN_ENTRYPOINT_ID") == "setup":
            try:
                input("\nPress Enter to close.")
            except (EOFError, KeyboardInterrupt):
                pass
    sys.exit(status)
