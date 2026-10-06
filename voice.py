#!/usr/bin/env python3
"""Native Codex frontend on a microphone host; backend stays in the current pane."""

import base64
import codecs
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import uuid

PLUGIN = "herdr-codex-voice"
VERSION = "0.5.0"
INSTALL_REF = "windows-multihost"  # Git ref agents install until this release is merged
SCHEMA = "herdr-codex-voice/1"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
       "-o", "LogLevel=ERROR"]
START = "# >>> herdr-codex-voice >>>"
END = "# <<< herdr-codex-voice <<<"
# Exported by the shell block so diagnostics can tell a pane that loaded it
# from one opened before setup. Bump when the block changes meaningfully.
SHELL_MARKER = "HERDR_CODEX_VOICE_SHELL"
SHELL_REVISION = "1"
# OpenSSH's own diagnostics, most specific first. A match means SSH itself
# failed, not the command it ran.
SSH_FAILURES = ((r"REMOTE HOST IDENTIFICATION HAS CHANGED", "ssh_host_key_changed"),
                (r"Host key verification failed", "ssh_host_key_unknown"),
                (r"Permission denied \(", "ssh_auth_failed"),
                (r"Could not resolve hostname", "ssh_host_unresolved"),
                (r"remote port forwarding failed", "ssh_forwarding_denied"),
                (r"Connection refused|Connection timed out|Operation timed out|"
                 r"No route to host|Network is unreachable", "ssh_unreachable"))
PHYSICAL_CHECKS = [
    "On the microphone computer, allow microphone access if the OS asks (macOS asks for Codex Voice, "
    "or Codex Voice Audio when the work computer runs Windows, during the first /voice).",
    "In a new Herdr pane on the work computer, run codex, then /voice, and speak; "
    "confirm the words are recognized.",
    "Confirm Codex's spoken reply plays on the microphone computer's speakers or headset.",
]
LOCAL_COMMANDS = set("agents exec e review login logout mcp plugin app-server "
                     "remote-control completion update doctor sandbox debug apply a "
                     "queue archive delete migrate-rollouts unarchive cloud exec-server "
                     "features help".split())
VALUE_OPTIONS = {"-c", "--config", "--enable", "--disable", "--remote",
                 "--remote-auth-token-env", "-i", "--image", "-m", "--model",
                 "--local-provider", "-p", "--profile", "-s", "--sandbox",
                 "-C", "--cd", "--add-dir", "-a", "--ask-for-approval"}


class VoiceError(RuntimeError):
    """A failure with a stable `code` for --json callers; the message is for people."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class InputError(ValueError):
    """Invalid arguments. The `usage` code exits 2; other codes exit 1."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def ssh_failure(message):
    return next((code for pattern, code in SSH_FAILURES if re.search(pattern, message)), None)


def error_code(error):
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    if isinstance(error, subprocess.TimeoutExpired):
        return "timeout"
    return ssh_failure(str(error)) or "failed"


def run(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=30, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or f"{argv[0]} exited {result.returncode}")
    return result.stdout


def config_dir():
    override = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if override:
        return Path(override)
    try:
        return Path(run([os.environ.get("HERDR_BIN_PATH", "herdr"),
                         "plugin", "config-dir", PLUGIN]).strip())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise VoiceError("herdr_unavailable", f"Herdr could not report this plugin's configuration: {error}") from None


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(text)
    temporary.replace(path)


def install():
    # Herdr runs this build step on install/reinstall. One standalone copy also
    # keeps unsetup available after uninstall, with no extra interpreter at launch.
    path = launcher_path()
    if path.exists() and "herdr-codex-voice" not in path.read_text(encoding="utf-8", errors="replace"):
        raise VoiceError("launcher_conflict", f"{path} belongs to another program; leaving it unchanged")
    write_private(path, Path(__file__).read_text(encoding="utf-8"))
    path.chmod(0o755)
    return path


def launcher_path():
    return Path.home() / ".local/bin/codex-voice"


def shell_file():
    if sys.platform == "win32":
        return None
    shell = Path(os.environ.get("SHELL", "/bin/bash")).name
    if shell == "zsh":
        return Path(os.environ.get("ZDOTDIR", str(Path.home()))) / ".zshrc"
    if shell == "bash":
        return Path.home() / ".bashrc"
    return None


def powershell_profiles():
    """(executable, console profile) for each installed PowerShell edition. The current-user
    current-host profile loads last, so the block appended there follows other customizations."""
    found = []
    # Base64 keeps a non-ASCII user folder intact whatever the console code page.
    command = "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PROFILE.CurrentUserCurrentHost))"
    for name in ("pwsh", "powershell"):
        executable = shutil.which(name)
        if not executable:
            continue
        try:
            result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            path = base64.b64decode(result.stdout.strip(), validate=True).decode("utf-8")
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if result.returncode == 0 and path:
            found.append((executable, Path(path)))
    return found


def shell_files():
    """Startup files that get the codex integration on this computer."""
    if sys.platform == "win32":
        return [profile for _, profile in powershell_profiles()]
    path = shell_file()
    return [path] if path else []


def reload_command(path):
    return ". $PROFILE.CurrentUserCurrentHost" if path.suffix.lower() == ".ps1" else f"source {shlex.quote(str(path))}"


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def ps_literal(value):
    """A PowerShell string expression in plain ASCII, so a profile's encoding (ANSI,
    UTF-8 or UTF-16) cannot change a non-ASCII path."""
    value = str(value)
    if value.isascii():
        return ps_quote(value)
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')))"


def launcher_command(launcher=None):
    """The launcher as a command for this computer's shell. On Windows it is a Python
    file, started through PowerShell's call operator and this Python."""
    launcher = launcher or launcher_path()
    if sys.platform == "win32":
        return f"& {ps_quote(sys.executable)} {ps_quote(launcher)}"
    return shlex.quote(str(launcher))


def runnable(command):
    """Write a `codex-voice ...` step so it runs as written here. Arguments are
    single-quoted the POSIX way, which PowerShell reads the same for these values."""
    if command and command.startswith("codex-voice "):
        return launcher_command() + command[len("codex-voice"):]
    return command


def launch_command(launcher):
    """How to start Codex by hand in a shell without the integration."""
    return launcher_command(launcher) + " run"


BLOCK = r"\r?\n" + re.escape(START) + r"\r?\n.*?" + re.escape(END) + r"(?:\r?\n)?"
# Byte order marks a startup file may start with; UTF-32 LE's begins like UTF-16 LE's, so it comes first.
BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"), (b"", "utf-8"))


def read_startup(path):
    """(text, bom, codec) of a shell startup file. Newlines are kept as they are, and bytes
    of a file without a BOM (UTF-8, or ANSI for Windows PowerShell 5.1) round-trip unchanged."""
    data = path.read_bytes()
    bom, codec = next((bom, codec) for bom, codec in BOMS if data.startswith(bom))
    return data[len(bom):].decode(codec, "surrogateescape"), bom, codec


def integration_block(path):
    """Return the installed shell block, or None."""
    try:
        match = re.search(BLOCK, "\n" + read_startup(path)[0], flags=re.S)
    except (OSError, UnicodeError):
        return None
    return match.group(0) if match else None


def powershell_function(launcher):
    """Body of the PowerShell codex function; doctor checks that a plain codex runs exactly this."""
    return f"& {ps_literal(sys.executable)} {ps_literal(launcher)} run @args"


def shell_block(path, launcher):
    if path.suffix.lower() == ".ps1":
        # An alias outranks a function, which outranks codex.cmd/.exe. Both changes
        # happen only in Herdr panes; the block is ASCII whatever the paths.
        return (START + "\nif ($env:HERDR_ENV -eq '1') {\n"
                "  if (Test-Path -LiteralPath Alias:codex) { Remove-Item -LiteralPath Alias:codex -Force }\n"
                f"  function global:codex {{ {powershell_function(launcher)} }}\n"
                f"  $env:{SHELL_MARKER} = '{SHELL_REVISION}'\n}}\n" + END + "\n")
    alias = shlex.quote(shlex.quote(str(launcher)) + " run")
    return (START + "\n" + 'if [ "${HERDR_ENV:-}" = 1 ]; then\n'
            f"  alias codex={alias}\n  export {SHELL_MARKER}={SHELL_REVISION}\nfi\n" + END + "\n")


def integrate(path, launcher=None):
    """Add (with `launcher`) or remove the block, keeping the file's BOM, encoding and newlines."""
    if path.exists():
        original, bom, codec = read_startup(path)
    else:
        # Windows PowerShell 5.1 and PowerShell 7 both read a UTF-8 profile with a BOM as UTF-8.
        original, bom, codec = "", codecs.BOM_UTF8 if path.suffix.lower() == ".ps1" else b"", "utf-8"
    text = re.sub(BLOCK, "", original, flags=re.S)
    if launcher:
        newline = "\r\n" if "\r\n" in original else "\n"
        text += newline + shell_block(path, launcher).replace("\n", newline)
    if text != original:
        if path.exists() and not path.with_name(path.name + ".before-codex-voice").exists():
            shutil.copy2(path, path.with_name(path.name + ".before-codex-voice"))
        # Follow an existing dotfile symlink; don't replace the user's link.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bom + text.encode(codec, "surrogateescape"))
    return text != original


def probe(host, root=None):
    if host is not None and (not re.fullmatch(r"[\w.@:\[\]-]+", host) or host.startswith("-")):
        raise InputError("invalid_host", "Use an SSH alias or user@Tailscale-name; put ports and keys in ~/.ssh/config")
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
        # A Windows attempt cannot succeed where SSH itself failed, and a Unix
        # Python reporting missing Codex already identified the platform.
        code = ssh_failure(str(unix_error))
        if code or "Install Codex CLI on the microphone computer first" in str(unix_error):
            raise VoiceError(code or "microphone_codex_missing", str(unix_error)) from None
        try:
            mic = load_audio(root).probe(host, SSH, run)
        except RuntimeError as error:
            raise VoiceError("microphone_probe_failed", f"{unix_error}\nWindows check: {error}") from None
        return {"host": host, "local": False, **mic}
    lines = [line.removeprefix("CODEX_VOICE=") for line in output.splitlines()
             if line.startswith("CODEX_VOICE=")]
    if not lines:
        raise VoiceError("microphone_probe_failed",
                         "Could not find Codex and Python 3 in the microphone computer's login shell")
    return {"host": host or "local", "local": host is None, **json.loads(lines[-1])}


def backend(codex):
    try:
        data = json.loads(run([codex, "app-server", "daemon", "start"]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise VoiceError("backend_unavailable", f"Codex's app-server daemon did not start: {error}") from None
    path = data.get("socketPath", "") if isinstance(data, dict) else ""
    if not path.startswith("/") or not Path(path).is_socket():
        raise VoiceError("backend_unavailable",
                         "Codex must provide a Unix socket. Use macOS, Linux, or WSL on this device.")
    return path


def desktop_revision(root):
    return hashlib.sha256((Path(root) / "macos.py").read_bytes()).hexdigest()


def plugin_root(root=None):
    root = root or os.environ.get("HERDR_PLUGIN_ROOT")
    if not root:
        plugins = json.loads(run([os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "list",
                                  "--plugin", PLUGIN, "--json"]))["result"]["plugins"]
        if not plugins:
            raise VoiceError("plugin_not_installed", "Install or link the Herdr plugin before setup")
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
    mic["desktop_helper"], mic["desktop_revision"] = deploy(mic, root, "macos")


def prepare_mac_audio(mic, codex, root=None):
    """Windows work computer: install the Mac audio controller and pick its exact-build helper."""
    controller, revision = deploy(mic, root, "macos_audio")
    load_module("windows_host", root).pair(mic, codex, controller, revision, SSH, run)


def deploy(mic, root, name):
    """Copy a plugin script to a microphone Mac and run its install step; return (path there, revision)."""
    root = plugin_root(root)
    source = (Path(root) / (name + ".py")).read_bytes()
    revision = hashlib.sha256(source).hexdigest()
    # The Mac's paths are POSIX even when this work computer runs Windows.
    remote = str(PurePosixPath(mic["desktop_root"]) / "helpers" / revision / (name + ".py"))
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
    command = shlex.join([mic["python"], "-c", bootstrap, remote])
    # Compilation happens once per helper revision, never during normal launch.
    result = subprocess.run([*SSH, "-T", mic["host"], command], input=source,
                            capture_output=True, timeout=180)
    if result.returncode:
        message = (result.stderr.decode(errors="replace").strip()
                   or "Could not install the microphone Mac's desktop helper")
        code = ssh_failure(message) or ("mac_command_line_tools_missing" if "xcode-select --install" in message
                                        else "mac_desktop_helper_failed")
        raise VoiceError(code, message)
    return remote, revision


def named_profile(directory, name):
    if not re.fullmatch(r"[\w -]{1,40}", name, flags=re.ASCII) or not name.strip():
        raise InputError("invalid_name", "Use a microphone name of 1–40 letters, numbers, spaces, underscores or hyphens")
    return directory / "microphones" / (hashlib.sha256(name.encode()).hexdigest() + ".json")


def microphone_identity(mic, path):
    if not isinstance(mic, dict) or not isinstance(mic.get("local", False), bool):
        raise VoiceError("pairing_invalid", f"Invalid microphone configuration in {path}: expected a microphone object")
    for key in ("host", "label", "hostname"):
        if key in mic and not isinstance(mic[key], str):
            raise VoiceError("pairing_invalid", f"Invalid microphone configuration in {path}: {key} must be text")
    if mic.get("local"):
        return ("local",)
    if not mic.get("host"):
        raise VoiceError("pairing_invalid", f"Invalid microphone configuration in {path}: missing SSH host")
    return ("ssh", mic["host"])


def microphones(directory):
    """Return every saved (path, microphone) pairing without asking anything."""
    # Named pairings take precedence over the older single-host configuration.
    # The old file stays intact for rollback and for installs with no pairings.
    choices = []
    hosts = set()
    paths = sorted((directory / "microphones").glob("*.json"))
    legacy = directory / "config.json"
    if legacy.exists():
        paths.append(legacy)
    for path in paths:
        try:
            mic = json.loads(path.read_text())
        except ValueError:
            raise VoiceError("pairing_invalid", f"Invalid microphone configuration in {path}: not JSON") from None
        host = microphone_identity(mic, path)
        if path != legacy or host not in hosts:
            hosts.add(host)
            choices.append((path, mic))
    return choices


def route(mic):
    """Name the audio route a pairing uses and how well it is supported."""
    if mic.get("local"):
        return "local", "supported"
    if mic.get("platform") == "win32":
        return "windows-audio-helper", "experimental"
    if mic.get("mac_audio"):
        return "mac-audio-helper", "experimental"
    if mic.get("platform") == "darwin":
        return "mac-desktop-frontend", "supported"
    return "unix-frontend", "supported"


def describe(path, mic):
    """Summarize a pairing for people and agents. Paths, commands and identifiers stay private."""
    name, support = route(mic)
    return {"name": mic.get("label"), "default": path.name == "config.json",
            "host": None if mic.get("local") else mic.get("host"),
            "local": bool(mic.get("local")), "platform": mic.get("platform"),
            "hostname": mic.get("hostname"), "route": name, "support": support,
            "automatic_identity": bool(mic.get("local") or mic.get("tailscale_node_id"))}


def choose_microphone(directory, select=None):
    choices = microphones(directory)
    if not choices:
        raise VoiceError("no_pairing", "Run codex-voice setup once to choose your microphone computer")
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


def setup(host=None, name=None, codex=None):
    """Pair a microphone computer and install the shell integration; idempotent.

    `codex` names the Windows work computer's Codex explicitly, for example an
    installed package whose build matches the Mac when PATH has another version.
    """
    directory = config_dir()
    path = named_profile(directory, name) if name is not None else directory / "config.json"
    paired_profiles = []
    if name is None:
        for paired in (directory / "microphones").glob("*.json"):
            try:
                old = json.loads(paired.read_text())
            except ValueError:
                raise VoiceError("pairing_invalid", f"Invalid microphone configuration in {paired}: not JSON") from None
            microphone_identity(old, paired)
            paired_profiles.append((paired, old))
    if codex is not None and sys.platform != "win32":
        raise InputError("usage", "--codex applies only to native Windows work computers")
    if sys.platform == "win32":
        load_module("windows_host").require_desktop_user()
    if host is None:
        host = input("Microphone computer's SSH alias or user@Tailscale-name: ").strip()
    mic = probe(None if host == "--local" else host)
    if codex is not None and not Path(codex).is_file():
        raise VoiceError("codex_missing", f"{codex} does not exist")
    work_codex = codex
    codex = codex or shutil.which("codex")
    if not codex:
        raise VoiceError("codex_missing", "Install Codex CLI on this work computer first")
    if sys.platform == "win32" and not mic.get("local"):
        # Codex's terminal and backend stay on Windows; only its voice helper runs on the Mac.
        if mic.get("platform") != "darwin":
            raise VoiceError("unsupported_route", "A Windows work computer can use only a Mac microphone computer")
        load_module("windows_host").build_relay(directory)
        prepare_mac_audio(mic, codex)
        if work_codex:
            mic["work_codex"] = os.path.abspath(work_codex)
    else:
        if not mic.get("local") and mic.get("platform") != "win32":
            backend(codex)
        prepare_desktop(mic)
        if mic.get("platform") == "win32" and not mic.get("local"):
            prepare_windows(mic)
    node = pair_identity(mic)
    if node:
        mic["tailscale_node_id"] = node
    if name is not None:
        mic["label"] = name
    launcher = install()
    write_private(path, json.dumps(mic, indent=2) + "\n")
    refreshed_names = []
    if name is None:
        # Recovery instructions use `setup HOST`. Refresh every named pairing
        # for that route too, so an older named copy cannot hide the repair.
        identity = microphone_identity(mic, path)
        for paired, old in paired_profiles:
            if microphone_identity(old, paired) == identity:
                refreshed = {**mic, **({"label": old["label"]} if "label" in old else {})}
                write_private(paired, json.dumps(refreshed, indent=2) + "\n")
                refreshed_names.append(old.get("label"))
    files = shell_files()
    changed = [integrate(rc, launcher) for rc in files]
    return {"pairing": describe(path, mic), "refreshed": refreshed_names, "launcher": str(launcher),
            "run": launch_command(launcher),
            "shell": {"file": str(files[0]) if files else None, "files": [str(rc) for rc in files],
                      "changed": any(changed), "reload": reload_command(files[0]) if files else None}}


def setup_lines(result):
    pairing, shell = result["pairing"], result["shell"]
    lines = ["Enabled this computer's microphone" if pairing["local"]
             else f"Saved microphone computer: {pairing['host']}"]
    if shell["file"]:
        lines += ["Open a new Herdr terminal and type codex.", f"Existing terminal: {shell['reload']}"]
    else:
        shells = "PowerShell only" if sys.platform == "win32" else "Bash/Zsh aliases only"
        lines.append(f"{shells}. In this shell, run: {result['run']}")
    lines.append("Run /voice in Codex to check microphone permission and audio devices.")
    return lines


def setup_steps(result):
    """Next steps after pairing: what an agent can still check, then what only a person can."""
    shell = result["shell"]
    steps = [step("agent", "Check the pairing without audio.", "codex-voice doctor --json")]
    if shell["file"]:
        steps.append(step("human", "Herdr panes opened before this setup: quit Codex there and reload the "
                          "shell once, or open a new pane. New panes need nothing.", shell["reload"]))
    else:
        steps.append(step("human", "This shell has no automatic integration; start Codex with the launcher.",
                          result["run"]))
    return steps + [step("human", check) for check in PHYSICAL_CHECKS]


def step(actor, text, command=None):
    return {"actor": actor, "text": text, **({"command": runnable(command)} if command else {})}


def unpair(name):
    """Forget one pairing; `--default` forgets the single-host configuration. Idempotent."""
    directory = config_dir()
    path = directory / "config.json" if name == "--default" else named_profile(directory, name)
    removed = path.exists()
    if removed:
        path.unlink()
    return {"name": None if name == "--default" else name, "default": name == "--default", "removed": removed}


def unsetup(purge=False):
    """Remove the shell block; with purge, also this plugin's own local files."""
    files = shell_files()
    changed = [integrate(rc) for rc in files]
    result = {"shell": {"file": str(files[0]) if files else None, "files": [str(rc) for rc in files],
                        "changed": any(changed)},
              "removed": []}
    if not purge:
        return result
    launcher = launcher_path()
    if launcher.is_file() and "herdr-codex-voice" in launcher.read_text(encoding="utf-8", errors="replace"):
        launcher.unlink()
        result["removed"].append(str(launcher))
    directory = config_dir()
    for path in [directory / "config.json", *sorted((directory / "microphones").glob("*.json"))]:
        if path.is_file():
            path.unlink()
            result["removed"].append(str(path))
    for path in (directory / "microphones", directory / "windows-voice-packages",
                 directory / "relay", directory / "relay-build"):
        if path.is_dir() and not path.is_symlink():
            # Package copies are hardlinks or copies; removing them never touches Codex itself.
            shutil.rmtree(path)
            result["removed"].append(str(path))
    return result


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


def native(codex):
    """Command that starts Codex without a shell; npm's Windows shims run their codex.js with Node."""
    path = Path(codex)
    if sys.platform != "win32" or path.suffix.lower() not in (".cmd", ".ps1", ".bat"):
        return [codex]
    script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = str(path.parent / "node.exe") if (path.parent / "node.exe").is_file() else shutil.which("node")
    if not script.is_file() or not node:
        raise VoiceError("codex_missing", f"{codex} is not the official npm Codex wrapper; put codex.exe on PATH")
    return [node, str(script)]


def become(argv):
    """Replace this process with argv. Windows has no exec, so wait there and pass on the status."""
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # Codex handles Ctrl+C itself.
        raise SystemExit(subprocess.call(argv))
    os.execv(argv[0], argv)


def keep(old, new):
    """A refreshed pairing keeps the user's name, saved identity and chosen Codex."""
    return {**new, **{key: old[key] for key in ("label", "tailscale_node_id", "work_codex") if key in old}}


def connect(args):
    codex = shutil.which("codex")
    if not codex:
        raise VoiceError("codex_missing", "Codex CLI is not on PATH")
    plain = [*native(codex), *args]
    if local_command(args) or not (sys.stdin.isatty() and sys.stdout.isatty()):
        become(plain)
    try:
        plugins = json.loads(run([os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "list",
                                  "--plugin", PLUGIN, "--json"]))["result"]["plugins"]
        enabled = any(plugin.get("enabled") for plugin in plugins)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError,
            subprocess.SubprocessError) as error:
        # Only a disabled or removed plugin means plain Codex. If Herdr cannot
        # answer, plain Codex would silently use this computer's microphone.
        raise VoiceError("herdr_unavailable", f"Could not check whether Codex Voice is enabled in Herdr ({error}). "
                         "Run `command codex` to use Codex here without the voice route.") from None
    if not enabled:
        become(plain)
    root = plugins[0].get("plugin_root")
    select = None
    if os.environ.get("HERDR_SOCKET_PATH") and os.environ.get("HERDR_PANE_ID"):
        select = load_module("client_origin", root).select
    path, mic = choose_microphone(config_dir(), select)
    if mic.get("local") or (mic.get("platform") != "win32" and mic["hostname"] == socket.gethostname()):
        become(plain)
    root = root or plugin_root()
    if sys.platform == "win32":
        connect_windows(path, mic, codex, args, root)
    if mic.get("platform") == "win32":
        audio = load_audio(root)
        if audio.stale(mic, codex):
            mic = keep(mic, probe(mic["host"], root))
            prepare_windows(mic, root)
            write_private(path, json.dumps(mic, indent=2) + "\n")
        audio.run_codex(mic, codex, args, [shutil.which("ssh"), *SSH[1:]],
                        config_dir() / "windows-voice-packages")
    if "platform" not in mic or (mic["platform"] == "darwin" and mic.get("desktop_revision") != desktop_revision(root)):
        mic = keep(mic, probe(mic["host"]))
        prepare_desktop(mic, root)
        write_private(path, json.dumps(mic, indent=2) + "\n")
    command = ssh_command(mic, backend(codex), frontend_args(args, os.getcwd()))
    os.execvp(command[0], command)


def connect_windows(path, mic, codex, args, root):
    """Native Windows work computer: Codex runs here and only its voice helper runs on the Mac."""
    if not mic.get("mac_audio"):
        raise VoiceError("unsupported_route", "A Windows work computer can use only a Mac microphone computer; "
                                              "pair one with codex-voice pair NAME MAC-HOST")
    windows = load_module("windows_host", root)
    codex = mic.get("work_codex") or codex
    revision = hashlib.sha256((Path(root) / "macos_audio.py").read_bytes()).hexdigest()
    if windows.stale(mic, codex) or mic["mac_audio"].get("revision") != revision:
        # Codex here or the plugin changed: pick the matching Mac helper again.
        mic = keep(mic, probe(mic["host"], root))
        prepare_mac_audio(mic, codex, root)
        write_private(path, json.dumps(mic, indent=2) + "\n")
    raise SystemExit(windows.run_codex(mic, codex, args, config_dir(), shutil.which("ssh"), SSH[1:]))


ERRORS = (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError)
AGENT_COMMANDS = {"setup", "pair", "unpair", "unsetup", "build-relay"}
SETUP_PANES = ("setup", "setup-windows")  # herdr-plugin.toml pane ids
# Remedies for --json failures. "{host}" is the microphone computer when known.
FIXES = {
    "usage": ("agent", "Pass every value explicitly; see `codex-voice help`."),
    "invalid_host": ("agent", "Use an SSH alias or user@host; put ports, keys and proxies in ~/.ssh/config."),
    "invalid_name": ("agent", "Use a name of 1–40 letters, numbers, spaces, underscores or hyphens."),
    "herdr_unavailable": ("agent", "Install Herdr 0.9.3 or later on this work computer and put `herdr` on PATH."),
    "plugin_not_installed": ("agent", "Install the plugin.",
                             f"herdr plugin install --yes --ref {INSTALL_REF} ricardofelixb/herdr-codex-voice"),
    "codex_missing": ("agent", "Install Codex CLI on this work computer so `codex` is on PATH."),
    "backend_unavailable": ("agent", "Make `codex app-server daemon start` succeed on this work computer."),
    "launcher_conflict": ("human", "~/.local/bin/codex-voice belongs to another program; move it aside, "
                                   "then reinstall the plugin."),
    "unsupported_platform": ("agent", "Run Herdr and this plugin on macOS, Linux, WSL or Windows."),
    "unsupported_route": ("agent", "Pair a Mac microphone computer; a native Windows work computer cannot use "
                                   "other microphone computers yet."),
    "mac_helper_build_mismatch": ("agent", "Install the same Codex build on {host} as on this work computer (or "
                                           "pass --codex with a matching installed Codex here), then rerun setup. "
                                           "Ask before replacing a Codex version the person relies on."),
    "windows_relay_missing": ("agent", "Build the Windows voice relay from the plugin's source.",
                              "codex-voice build-relay --json"),
    "windows_relay_toolchain_missing": ("agent", "Install Rust with the MSVC toolchain (rustup, plus Visual Studio "
                                                 "Build Tools with C++; the installer may ask the person to approve), "
                                                 "then rerun setup."),
    "windows_relay_build_failed": ("agent", "Read the build output; the relay builds offline with Rust's MSVC "
                                            "toolchain and has no dependencies."),
    "ssh_missing": ("agent", "Add Windows' OpenSSH Client optional feature so ssh.exe is on PATH."),
    "windows_elevated": ("agent", "Run the command in a normal (non-elevated) Herdr pane on the Windows desktop, "
                                  "not an administrator session. Do not change accounts or policy."),
    "ssh_host_key_unknown": ("human", "Connect once interactively and verify {host}'s host key fingerprint; "
                                      "setup never accepts host keys.", "ssh {host} true"),
    "ssh_host_key_changed": ("human", "{host}'s host key changed. Verify the new key before replacing the "
                                      "old entry in ~/.ssh/known_hosts."),
    "ssh_auth_failed": ("agent", "Set up key-based SSH from this computer to {host} that works without prompts, "
                                 "for example by adding this computer's public key to {host}'s authorized_keys.",
                        "ssh -o BatchMode=yes {host} true"),
    "ssh_host_unresolved": ("agent", "Use a resolvable SSH alias or Tailscale name; check ~/.ssh/config."),
    "ssh_unreachable": ("agent", "Make {host} reachable: awake, online and running an SSH server."),
    "ssh_forwarding_denied": ("human", "Allow remote Unix socket forwarding in {host}'s OpenSSH server "
                                       "(AllowStreamLocalForwarding)."),
    "microphone_codex_missing": ("agent", "Install Codex CLI on {host} so `codex` is on its login-shell PATH, "
                                          "then rerun setup."),
    "microphone_probe_failed": ("agent", "Make Python 3 and Codex available in {host}'s login shell "
                                         "(Windows: `py -3` or `python`), then rerun setup."),
    "mac_command_line_tools_missing": ("human", "Install Apple's Command Line Tools on {host}, then rerun setup.",
                                       "xcode-select --install"),
    "windows_layout_unsupported": ("agent", "Use a standalone Codex install or the official npm @openai/codex "
                                            "package on this work computer."),
    "windows_home_not_cwd": ("agent", "Configure {host}'s OpenSSH server so sessions start in the user's home."),
    "windows_helper_build_mismatch": ("agent", "Install the same Codex build on {host} as on this work computer, "
                                               "then rerun setup. Ask before replacing a Codex version the person "
                                               "relies on; setup never upgrades Codex."),
    "pairing_invalid": ("agent", "Repair or remove the named file, then pair that microphone again."),
    "no_pairing": ("agent", "Pair a microphone computer.", "codex-voice setup HOST --json"),
    "timeout": ("agent", "Retry; if it repeats, check the network path to {host}."),
}


def fix(code, host=None):
    if code not in FIXES:
        return []
    actor, text, *command = FIXES[code]
    host = host or "HOST"
    return [step(actor, text.format(host=host), *(c.format(host=host) for c in command))]


def envelope(command, ok, result=None, error=None, next_steps=()):
    """The stable --json shape shared by every agent command."""
    return {"schema": SCHEMA, "command": command, "version": VERSION, "ok": ok,
            "result": result, "error": error, "next_steps": list(next_steps)}


def option(args, name):
    """Split `name VALUE` out of args; return (VALUE or None, the other args)."""
    if name not in args:
        return None, args
    index = args.index(name)
    if index + 1 == len(args):
        raise InputError("usage", f"{name} needs a value")
    return args[index + 1], args[:index] + args[index + 2:]


def perform(action, args, interactive):
    """Run a setup command; return (result, human lines, next steps)."""
    if action == "setup":
        codex, args = option(args, "--codex")
        if len(args) > 1 or (not args and not interactive):
            raise InputError("usage", "Usage: codex-voice setup SSH-HOST|--local [--codex PATH] [--json]")
        result = setup(args[0] if args else None, codex=codex)
        return result, setup_lines(result), setup_steps(result)
    if action == "pair":
        codex, args = option(args, "--codex")
        if len(args) != 2:
            raise InputError("usage", "Usage: codex-voice pair NAME SSH-HOST|--local [--codex PATH] [--json]")
        result = setup(args[1], args[0], codex)
        return result, setup_lines(result), setup_steps(result)
    if action == "unpair":
        if len(args) != 1:
            raise InputError("usage", "Usage: codex-voice unpair NAME|--default [--json]")
        result = unpair(args[0])
        label = "the default microphone" if result["default"] else f"microphone {args[0]}"
        return result, [("Forgot " if result["removed"] else "No saved ") + label], []
    if action == "build-relay":
        if args:
            raise InputError("usage", "Usage: codex-voice build-relay [--json]")
        relay = load_module("windows_host").build_relay(config_dir())
        return {"path": str(relay)}, [f"Windows voice relay: {relay}"], []
    if action == "unsetup":
        if [arg for arg in args if arg != "--purge"]:
            raise InputError("usage", "Usage: codex-voice unsetup [--purge] [--json]")
        result = unsetup("--purge" in args)
        lines = ["Removed shell integration. Open a new terminal to restore your previous codex command.",
                 *("Removed " + path for path in result["removed"])]
        steps = [step("human", "Open a new terminal, or reload the shell, so codex is the plain command again.")]
        if "--purge" in args:
            # Microphone computers may serve other work computers, so their cleanup is a person's call.
            steps += [step("agent", "Uninstall the Herdr plugin.", "herdr plugin uninstall herdr-codex-voice"),
                      step("human", "On a microphone Mac no other work computer uses, remove its helper after "
                                    "closing voice sessions.", "rm -rf ~/.local/share/herdr-codex-voice"),
                      step("human", "On a Windows microphone computer no other work computer uses, remove "
                                    "the .herdr-codex-voice folder in that user's home.")]
        return result, lines, steps
    raise InputError("usage", f"Unknown command {action}")


def load_doctor():
    try:
        root = plugin_root()
    except ERRORS:
        # A source checkout can diagnose itself before it is installed.
        root = Path(__file__).resolve().parent
        if not (root / "doctor.py").is_file():
            raise VoiceError("plugin_not_installed", "The Herdr plugin is not installed, so it cannot be diagnosed")
    return load_module("doctor", root)


def main(argv=None):
    action, *args = (sys.argv[1:] if argv is None else argv) or ["help"]
    if action == "doctor":
        try:
            doctor = load_doctor()
        except ERRORS as error:
            if "--json" not in args:
                raise
            code = error_code(error)
            print(json.dumps(envelope(action, False, error={"code": code, "message": str(error)},
                                      next_steps=fix(code)), indent=2))
            return 1
        return doctor.main(args)
    if action in AGENT_COMMANDS and "--json" in args:
        args = [arg for arg in args if arg != "--json"]
        host = None  # Named in remedies; the last plain argument of setup and pair.
        if action in ("setup", "pair"):
            try:
                rest = option(args, "--codex")[1]
            except InputError:
                rest = []
            host = next((arg for arg in rest[-1:] if arg != "--local"), None)
        try:
            result, _, steps = perform(action, args, interactive=False)
        except KeyboardInterrupt:
            print(json.dumps(envelope(action, False, error={"code": "interrupted", "message": "Interrupted"}),
                             indent=2))
            return 130
        except ERRORS as error:
            code = error_code(error)
            print(json.dumps(envelope(action, False, error={"code": code, "message": str(error)},
                                      next_steps=fix(code, host)), indent=2))
            return 2 if code == "usage" else 1
        print(json.dumps(envelope(action, True, result, next_steps=steps), indent=2))
        return 0
    if action == "install":
        print(f"Installed {install()}. Next: codex-voice setup YOUR-MICROPHONE-HOST")
    elif action in AGENT_COMMANDS:
        print("\n".join(perform(action, args, interactive=sys.stdin.isatty())[1]))
    elif action == "run":
        connect(args)
    elif action == "open-setup":
        subprocess.run([os.environ.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open", "--plugin", PLUGIN,
                        "--entrypoint", "setup-windows" if sys.platform == "win32" else "setup"], check=True)
    elif action in ("version", "--version"):
        print(f"codex-voice {VERSION}")
    else:
        print("codex-voice setup [SSH-HOST]   Pair once and enable codex in Herdr\n"
              "codex-voice setup --local     Use this computer's microphone without SSH\n"
              "codex-voice pair NAME HOST    Save a microphone for selection at Codex launch\n"
              "codex-voice unpair NAME       Forget a saved microphone (--default: the setup one)\n"
              "codex-voice doctor            Check installation, pairings and routes (--offline, --microphone NAME)\n"
              "codex-voice run [CODEX-ARGS]  Run with the saved microphone computer\n"
              "codex-voice unsetup          Remove the shell integration (--purge: also pairings and launcher)\n"
              "codex-voice build-relay      Windows work computer: build the voice relay from source\n"
              "Add --json to setup, pair, unpair, unsetup, build-relay or doctor for machine-readable output "
              "without prompts.\n"
              "See AGENT-INSTALL.md in the plugin for codes and exit statuses.")
    return 0


if __name__ == "__main__":
    status = 0
    try:
        status = main()
    except KeyboardInterrupt:
        status = 130
    except ERRORS as error:
        print("Codex Voice: " + str(error), file=sys.stderr)
        status = 2 if error_code(error) == "usage" else 1
    finally:
        # Herdr's setup popup closes with its process; keep messages readable
        # there, but never prompt agents or other non-interactive callers.
        if (os.environ.get("HERDR_PLUGIN_ENTRYPOINT_ID") in SETUP_PANES and "--json" not in sys.argv[1:]
                and sys.stdin.isatty()):
            try:
                input("\nPress Enter to close.")
            except (EOFError, KeyboardInterrupt):
                pass
    sys.exit(status)
