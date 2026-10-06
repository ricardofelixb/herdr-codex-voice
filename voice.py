#!/usr/bin/env python3
"""Native Codex frontend on a microphone host; backend stays in the current pane."""

import hashlib
import importlib.util
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


def probe(host, root=None):
    if host is not None and (not re.fullmatch(r"[\w.@:\[\]-]+", host) or host.startswith("-")):
        raise ValueError("Use an SSH alias or user@Tailscale-name; put ports and keys in ~/.ssh/config")
    code = '''import json, os, shutil, socket, sys
codex = shutil.which("codex")
if not codex: raise SystemExit("Install Codex CLI on the microphone computer first")
node = shutil.which("node")
print("CODEX_VOICE=" + json.dumps({"codex": os.path.join(os.path.realpath(os.path.dirname(codex)), os.path.basename(codex)),
 "node_dir": os.path.dirname(os.path.realpath(node)) if node else "",
 "hostname": socket.gethostname(), "codex_home": os.environ.get("CODEX_HOME", ""),
 "platform": sys.platform, "python": sys.executable,
 "desktop_root": os.path.expanduser("~/.local/share/herdr-codex-voice")}))
'''
    # NO_HERDR is an opt-out for shell snippets that auto-attach over SSH; Herdr
    # itself does not define it. Only setup loads these interactive profiles.
    command = 'NO_HERDR=1 "${SHELL:-/bin/sh}" -lic ' + shlex.quote(
        "python3 -c " + shlex.quote(code))
    try:
        output = run([sys.executable, "-c", code] if host is None else [*SSH, "-nT", host, command])
    except RuntimeError as unix_error:
        if host is None:
            raise
        try:
            mic = load_audio(root).probe(host, SSH, run)
        except RuntimeError as error:
            raise RuntimeError(f"{unix_error}\nWindows check: {error}") from None
        return {"host": host, "local": False, **mic}
    lines = [line.removeprefix("CODEX_VOICE=") for line in output.splitlines()
             if line.startswith("CODEX_VOICE=")]
    if not lines:
        raise RuntimeError("Could not find Codex and Python 3 in the microphone computer's login shell")
    return {"host": host or "local", "local": host is None, **json.loads(lines[-1])}


def backend(codex):
    data = json.loads(run([codex, "app-server", "daemon", "start"]))
    path = data.get("socketPath", "")
    if not path.startswith("/") or not Path(path).is_socket():
        raise RuntimeError("Codex must provide a Unix socket. Use macOS, Linux, or WSL on this device.")
    return path


def desktop_revision(root):
    return hashlib.sha256((Path(root) / "macos.py").read_bytes()).hexdigest()


def plugin_root(root=None):
    root = root or os.environ.get("HERDR_PLUGIN_ROOT")
    if not root:
        plugins = json.loads(run([os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "list",
                                  "--plugin", PLUGIN, "--json"]))["result"]["plugins"]
        if not plugins:
            raise RuntimeError("Install or link the Herdr plugin before setup")
        root = plugins[0]["plugin_root"]
    return root


def load_module(name, root=None):
    spec = importlib.util.spec_from_file_location(name, Path(plugin_root(root)) / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_audio(root=None):
    return load_module("audio_host", root)


def pair_identity(mic):
    return load_module("client_origin").pair_identity(mic, SSH, run)


def prepare_windows(mic, root=None):
    load_audio(root).prepare(mic, shutil.which("codex"), SSH, run)


def prepare_desktop(mic, root=None):
    if mic.get("platform") != "darwin" or mic.get("local"):
        return
    root = plugin_root(root)
    source = (Path(root) / "macos.py").read_bytes()
    revision = hashlib.sha256(source).hexdigest()
    mic["desktop_helper"] = str(Path(mic["desktop_root"]) / "helpers" / revision / "macos.py")
    bootstrap = '''import fcntl, pathlib, subprocess, sys
path=pathlib.Path(sys.argv[1])
path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
with (path.parents[2]/"setup.lock").open("a") as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 temporary=path.with_suffix(".tmp")
 temporary.write_bytes(sys.stdin.buffer.read())
 temporary.chmod(0o600)
 temporary.replace(path)
 sys.exit(subprocess.run([sys.executable,str(path),"install"]).returncode)
'''
    command = shlex.join([mic["python"], "-c", bootstrap, mic["desktop_helper"]])
    # Compilation happens once per helper revision, never during normal launch.
    result = subprocess.run([*SSH, "-T", mic["host"], command], input=source,
                            capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip()
                           or "Could not install the microphone Mac's desktop helper")
    mic["desktop_revision"] = revision


def named_profile(directory, name):
    if not re.fullmatch(r"[\w -]{1,40}", name, flags=re.ASCII) or not name.strip():
        raise ValueError("Use a microphone name of 1–40 letters, numbers, spaces, underscores or hyphens")
    return directory / "microphones" / (hashlib.sha256(name.encode()).hexdigest() + ".json")


def microphone_identity(mic, path):
    if not isinstance(mic, dict) or not isinstance(mic.get("local", False), bool):
        raise RuntimeError(f"Invalid microphone configuration in {path}: expected a microphone object")
    for key in ("host", "label", "hostname"):
        if key in mic and not isinstance(mic[key], str):
            raise RuntimeError(f"Invalid microphone configuration in {path}: {key} must be text")
    if mic.get("local"):
        return ("local",)
    if not mic.get("host"):
        raise RuntimeError(f"Invalid microphone configuration in {path}: missing SSH host")
    return ("ssh", mic["host"])


def choose_microphone(directory, select=None):
    # Named pairings take precedence over the older single-host configuration.
    # The old file stays intact for rollback and for installs with no pairings.
    choices = []
    hosts = set()
    paths = sorted((directory / "microphones").glob("*.json"))
    legacy = directory / "config.json"
    if legacy.exists():
        paths.append(legacy)
    for path in paths:
        mic = json.loads(path.read_text())
        host = microphone_identity(mic, path)
        if path != legacy or host not in hosts:
            hosts.add(host)
            choices.append((path, mic))
    if not choices:
        raise RuntimeError("Run codex-voice setup once to choose your microphone computer")
    if select is not None:
        selected = select(choices)
        if selected is not None:
            return selected
    if len(choices) == 1:
        return choices[0]

    def label(choice):
        mic = choice[1]
        value = mic.get("label") or mic.get("hostname") or mic["host"]
        return "".join(c for c in value if c.isprintable())

    choices.sort(key=lambda choice: label(choice).casefold())
    print("Which computer's microphone are you using?", file=sys.stderr)
    for index, choice in enumerate(choices, 1):
        print(f"  {index}. {label(choice)}", file=sys.stderr)
    while True:
        print(f"Choose 1–{len(choices)}, or q to cancel: ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline()
        if not answer or answer.strip().lower() == "q":
            raise KeyboardInterrupt
        answer = answer.strip()
        if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]


def setup(host=None, name=None):
    directory = config_dir()
    path = named_profile(directory, name) if name is not None else directory / "config.json"
    paired_profiles = []
    if name is None:
        for paired in (directory / "microphones").glob("*.json"):
            old = json.loads(paired.read_text())
            microphone_identity(old, paired)
            paired_profiles.append((paired, old))
    host = host or input("Microphone computer's SSH alias or user@Tailscale-name: ").strip()
    mic = probe(None if host == "--local" else host)
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("Install Codex CLI on this work computer first")
    if not mic.get("local") and mic.get("platform") != "win32":
        backend(codex)
    prepare_desktop(mic)
    if mic.get("platform") == "win32":
        prepare_windows(mic)
    node = pair_identity(mic)
    if node:
        mic["tailscale_node_id"] = node
    if name is not None:
        mic["label"] = name
    launcher = install()
    write_private(path, json.dumps(mic, indent=2) + "\n")
    if name is None:
        # Recovery instructions use `setup HOST`. Refresh every named pairing
        # for that route too, so an older named copy cannot hide the repair.
        identity = microphone_identity(mic, path)
        for paired, old in paired_profiles:
            if microphone_identity(old, paired) == identity:
                refreshed = {**mic, **({"label": old["label"]} if "label" in old else {})}
                write_private(paired, json.dumps(refreshed, indent=2) + "\n")
    print("Enabled this computer's microphone" if mic.get("local") else f"Saved microphone computer: {host}")
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
    missing = "Codex or its voice helper moved; rerun codex-voice setup " + mic["host"]
    executable = [mic["codex"]]
    available = "test -x " + shlex.quote(mic["codex"])
    if mic.get("platform") == "darwin":
        executable = [mic["python"], mic["desktop_helper"], *executable]
        available += f" && test -x {shlex.quote(mic['python'])} && test -f {shlex.quote(mic['desktop_helper'])}"
    frontend = shlex.join([*env, *executable, "--remote", "unix://" + remote_socket, *args])
    command = (f"trap {cleanup} EXIT; trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM; "
               f"{available} || {{ printf '%s\\n' {shlex.quote(missing)} >&2; exit 127; }}; "
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
    root = plugins[0].get("plugin_root")
    select = None
    if os.environ.get("HERDR_SOCKET_PATH") and os.environ.get("HERDR_PANE_ID"):
        select = load_module("client_origin", root).select
    path, mic = choose_microphone(config_dir(), select)
    if mic.get("local") or (mic.get("platform") != "win32" and mic["hostname"] == socket.gethostname()):
        os.execv(codex, [codex, *args])
    root = root or plugin_root()
    if mic.get("platform") == "win32":
        audio = load_audio(root)
        if audio.stale(mic, codex):
            mic = {**probe(mic["host"], root), **{key: mic[key] for key in ("label", "tailscale_node_id") if key in mic}}
            prepare_windows(mic, root)
            write_private(path, json.dumps(mic, indent=2) + "\n")
        audio.run_codex(mic, codex, args, [shutil.which("ssh"), *SSH[1:]],
                        config_dir() / "windows-voice-packages")
    if "platform" not in mic or (mic["platform"] == "darwin" and mic.get("desktop_revision") != desktop_revision(root)):
        mic = {**probe(mic["host"]), **{key: mic[key] for key in ("label", "tailscale_node_id") if key in mic}}
        prepare_desktop(mic, root)
        write_private(path, json.dumps(mic, indent=2) + "\n")
    command = ssh_command(mic, backend(codex), frontend_args(args, os.getcwd()))
    os.execvp(command[0], command)


def main():
    action, *args = sys.argv[1:] or ["help"]
    if action == "install":
        print(f"Installed {install()}. Next: codex-voice setup YOUR-MICROPHONE-HOST")
    elif action == "setup":
        setup(args[0] if args else None)
    elif action == "pair":
        if len(args) != 2:
            raise ValueError("Usage: codex-voice pair NAME SSH-HOST (or --local)")
        setup(args[1], args[0])
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
              "codex-voice setup --local     Use this computer's microphone without SSH\n"
              "codex-voice pair NAME HOST    Save a microphone for selection at Codex launch\n"
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
