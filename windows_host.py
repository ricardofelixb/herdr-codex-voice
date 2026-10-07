"""Native Windows work computer with a Mac microphone (experimental).

Codex's terminal and backend stay on Windows; only its voice helper runs on the
Mac. Codex runs from a private, content-addressed copy of its installed package
in which codex-voice-host.exe is the plugin's relay (windows/relay). The relay
runs one fixed `ssh -T` to macos_audio.py on the Mac, which starts the Mac's
Codex helper with exactly the same build through the Codex Voice Audio app.
Helper frames cross SSH unchanged. Installed Codex files are never modified and
nothing on Windows is evaluated by a shell.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess

ROOT = Path(__file__).resolve().parent
RELAY_SOURCES = ("windows/relay/Cargo.toml", "windows/relay/Cargo.lock", "windows/relay/src/main.rs")
RELAY_MAGIC = "herdr-codex-voice-relay 1"
# Passed to ssh.exe in case Codex's filtered helper environment lacks them;
# the relay accepts no others.
RELAY_ENVIRONMENT = ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "USERNAME", "USERDOMAIN", "SYSTEMROOT",
                     "WINDIR", "SYSTEMDRIVE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "TEMP", "TMP")


def sibling(name):
    spec = importlib.util.spec_from_file_location("codex_voice_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audio = sibling("audio_host")


class WindowsHostError(RuntimeError):
    """A failure with a stable `code` for --json callers."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def elevated():
    """Whether this Windows process runs with an elevated administrator token."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (ImportError, AttributeError, OSError):
        return False


def require_desktop_user():
    # Codex's daemon refuses elevated launchers, and files made here must stay
    # readable by the normal desktop user. Never work around this with policy.
    if elevated():
        raise WindowsHostError("windows_elevated",
                               "This is an elevated administrator session (for example an administrator's SSH "
                               "login). Run this in a normal Herdr pane on the Windows desktop instead")


def relay_digest():
    digest = hashlib.sha256()
    for name in RELAY_SOURCES:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes() + b"\0")
    return digest.hexdigest()[:24]


def relay_path(state):
    """Where the relay built from this plugin's pinned source is kept."""
    return Path(state) / "relay" / relay_digest() / "codex-voice-relay.exe"


def build_relay(state):
    """Build the relay from this plugin's source with Cargo, offline; idempotent."""
    path = relay_path(state)
    if path.is_file():
        return path
    cargo = shutil.which("cargo")
    if not cargo:
        raise WindowsHostError("windows_relay_toolchain_missing",
                               "Building the Windows voice relay needs Rust with the MSVC toolchain; "
                               "install it, then rerun setup")
    target = Path(state) / "relay-build"
    result = subprocess.run([cargo, "build", "--release", "--locked", "--offline",
                             "--manifest-path", str(ROOT / "windows/relay/Cargo.toml"), "--target-dir", str(target)],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900)
    built = target / "release" / "codex-voice-relay.exe"
    if result.returncode or not built.is_file():
        raise WindowsHostError("windows_relay_build_failed",
                               "Could not build the Windows voice relay:\n" + result.stderr.strip()[-2000:])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    shutil.copyfile(built, temporary)
    os.replace(temporary, path)
    return path


def relay_config(commit, ssh, args, environ=None):
    """The relay's configuration: magic line, then one validated key=value per line."""
    environ = os.environ if environ is None else environ
    if not re.fullmatch(r"[\w.-]{1,64}", commit, flags=re.ASCII):
        raise WindowsHostError("windows_layout_unsupported", f"Unexpected Codex build identifier {commit!r}")
    if not Path(ssh).is_absolute() or Path(ssh).name.lower() != "ssh.exe":
        raise WindowsHostError("ssh_missing", "Windows OpenSSH client (ssh.exe) not found")
    lines = [RELAY_MAGIC, "build_commit=" + commit, "ssh=" + str(ssh)]
    lines += [f"env={name}={environ[name]}" for name in RELAY_ENVIRONMENT if environ.get(name)]
    lines += ["arg=" + arg for arg in args]
    if len(args) > 64 or any(character in line for line in lines for character in "\r\n\0"):
        raise WindowsHostError("windows_relay_config", "A voice relay setting contains an unsupported character")
    return "\n".join(lines) + "\n"


def remote_command(mic):
    """The one command the relay asks the Mac to run, quoted for its POSIX login shell."""
    route = mic["mac_audio"]
    return shlex.join([mic["python"], route["controller"], "run", route["helper"], route["build_commit"]])


def marked(output, prefix):
    for line in reversed(output.splitlines()):
        if line.startswith(prefix):
            return json.loads(line[len(prefix):])
    raise WindowsHostError("microphone_probe_failed", "The microphone Mac printed no result")


def select_helper(host, commit, helpers):
    matches = sorted(helper["path"] for helper in helpers if helper.get("build_commit") == commit)
    if not matches:
        found = ", ".join(sorted({str(helper.get("build_commit"))[:12] for helper in helpers})) or "none"
        raise WindowsHostError(
            "mac_helper_build_mismatch",
            f"{host} has no Codex voice helper matching this computer's Codex build {commit[:12]} "
            f"(found: {found}). Install the same Codex version there, then rerun setup. "
            "Setup does not upgrade Codex or bypass the build check.")
    return matches[0]


def pair(mic, codex, controller, revision, ssh, run):
    """Choose the Mac helper with this Codex's exact build and record the route in `mic`."""
    _, commit = audio.work_package(codex)
    command = shlex.join([mic["python"], controller, "helpers", mic.get("codex", ""), mic.get("codex_home", "")])
    helpers = marked(run([*ssh, "-T", mic["host"], command]), "CODEX_VOICE_HELPERS=")
    mic["mac_audio"] = {"controller": controller, "revision": revision, "build_commit": commit,
                        "helper": select_helper(mic["host"], commit, helpers)}


def stale(mic, codex):
    return audio.work_package(codex)[1] != mic.get("mac_audio", {}).get("build_commit")


def run_codex(mic, codex, args, state, ssh, options):
    """Run native Codex here with the Mac's microphone and return its exit status."""
    package, commit = audio.work_package(codex)
    if commit != mic["mac_audio"]["build_commit"]:
        raise WindowsHostError("mac_helper_build_mismatch", "Codex changed; rerun codex-voice setup " + mic["host"])
    relay = relay_path(state)
    if not relay.is_file():
        raise WindowsHostError("windows_relay_missing", "The Windows voice relay is not built; run codex-voice build-relay")
    if not ssh:
        raise WindowsHostError("ssh_missing", "Windows OpenSSH client (ssh.exe) not found")
    config = relay_config(commit, ssh, [*options, "-T", mic["host"], remote_command(mic)])
    executable = audio.private_copy(package, commit, Path(state) / "windows-voice-packages",
                                    {"codex-voice-host.exe": relay.read_bytes(),
                                     "codex-voice-host.relay": config.encode()})
    # Codex reads Ctrl+C itself; this process only waits and passes on its status.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    return subprocess.call([str(executable), *args])
