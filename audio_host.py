"""Experimental Windows microphone host: only Codex's voice helper crosses SSH.

The work computer runs the native Codex terminal UI and backend. Codex runs from
a private copy of its package whose voice helper is replaced by a tiny shim that
execs one SSH connection to the Windows helper. SSH carries the helper's binary
stdin/stdout unchanged; audio itself goes directly from Windows to OpenAI.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

HELPER = Path("codex-resources/voice/bin/codex-voice-host")
PYTHONS = ("py -3", "python")
REMOTE_DIR = ".herdr-codex-voice"

# Runs on Windows through `<python> -`; stdin is the script, so no shell quoting.
PROBE = '''import glob, json, os, shutil, socket, subprocess, sys
home = os.path.expanduser("~")
codex_home = os.environ.get("CODEX_HOME") or os.path.join(home, ".codex")
patterns = [os.path.join(codex_home, "packages", "*", "releases", "*",
                         "codex-resources", "voice", "bin", "codex-voice-host.exe")]
codex = shutil.which("codex")
if codex:
    base = os.path.dirname(os.path.realpath(codex))
    patterns.append(os.path.join(os.path.dirname(base), "codex-resources", "voice", "bin",
                                 "codex-voice-host.exe"))
    patterns.append(os.path.join(base, "node_modules", "@openai", "**", "codex-voice-host.exe"))
helpers = {}
for pattern in patterns:
    for path in glob.glob(pattern, recursive=True):
        path = os.path.realpath(path)
        if path in helpers:
            continue
        try:
            result = subprocess.run([path, "--build-commit"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            helpers[path] = result.stdout.strip()
print("CODEX_VOICE=" + json.dumps({
    "platform": sys.platform, "hostname": socket.gethostname(),
    "home_is_cwd": os.path.realpath(os.getcwd()) == os.path.realpath(home),
    "helpers": [{"path": p, "build_commit": c} for p, c in helpers.items()]}))
'''

# Windows-side launcher. The environment is minimal on purpose: no GStreamer
# plugin discovery or registry, so the helper cannot pick up stray installs.
LAUNCHER = '''import os, subprocess, sys
KEEP = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "HOME", "USERPROFILE", "USERNAME", "LOCALAPPDATA",
        "APPDATA", "PROGRAMDATA", "COMPUTERNAME", "TEMP", "TMP"}
if not os.path.isfile(HELPER):
    sys.stderr.write("Codex voice helper moved; rerun codex-voice setup " + HOST + "\\n")
    raise SystemExit(127)
env = {k: v for k, v in os.environ.items() if k.upper() in KEEP}
env.update(GST_PLUGIN_PATH="", GST_PLUGIN_PATH_1_0="", GST_PLUGIN_SYSTEM_PATH="",
           GST_PLUGIN_SYSTEM_PATH_1_0="", GST_REGISTRY="NUL",
           GST_REGISTRY_UPDATE="no", GST_REGISTRY_FORK="no")
raise SystemExit(subprocess.call([HELPER], env=env))
'''

INSTALL = '''import os
from pathlib import Path
directory = Path.home() / REMOTE_DIR
directory.mkdir(exist_ok=True)
temporary = directory / (NAME + "." + str(os.getpid()) + ".tmp")
try:
    with open(temporary, "w", encoding="utf-8", newline="\\n") as file:
        file.write(SOURCE)
    os.replace(temporary, directory / NAME)
finally:
    if temporary.exists():
        temporary.unlink()
'''


def probe(host, ssh, run):
    errors = []
    for python in PYTHONS:
        try:
            output = run([*ssh, "-T", host, python + " -"], input=PROBE)
        except RuntimeError as error:
            errors.append(str(error))
            continue
        # Login banners may surround the result; only the marked line counts.
        for line in reversed(output.splitlines()):
            if line.startswith("CODEX_VOICE="):
                try:
                    data = json.loads(line.removeprefix("CODEX_VOICE="))
                except ValueError:
                    break
                if data.get("platform") == "win32":
                    return {**data, "python": python}
                break
    raise RuntimeError(errors[-1] if errors else "no Python 3 found")


# Mirrors codex.js: the wrapper's own Node picks the platform package.
NODE_RESOLVE = """const {createRequire} = require("module");
const os = process.platform === "android" ? "linux" : process.platform, arch = process.arch;
let path = null;
try { path = createRequire(process.argv[1]).resolve("@openai/codex-" + os + "-" + arch + "/package.json"); } catch {}
console.log(JSON.stringify({os, arch, path}));"""
TRIPLES = {("linux", "x64"): "x86_64-unknown-linux-musl", ("linux", "arm64"): "aarch64-unknown-linux-musl",
           ("darwin", "x64"): "x86_64-apple-darwin", ("darwin", "arm64"): "aarch64-apple-darwin"}


def npm_native(wrapper):
    """Find the native executable behind the official npm wrapper, as its codex.js does."""
    root = wrapper.parent.parent
    try:
        official = json.loads((root / "package.json").read_text()).get("name") == "@openai/codex"
    except (OSError, ValueError):
        official = False
    node = shutil.which("node")
    if wrapper.parent.name != "bin" or not official or not node:
        raise RuntimeError("This Codex install layout is not supported for Windows voice")
    try:
        result = subprocess.run([node, "-e", NODE_RESOLVE, str(wrapper)], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10, check=True)
        found = json.loads(result.stdout)
        triple = TRIPLES[found["os"], found["arch"]]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise RuntimeError("Could not determine Codex's native package; this platform is not supported") from None
    # Once resolved, that package alone is used, as codex.js does; no other is tried.
    vendor = (Path(found["path"]).parent if found["path"] else root) / "vendor"
    native = vendor / triple / "bin" / "codex"
    if not native.is_file():
        raise RuntimeError(f"Codex's native executable {native} is missing; reinstall Codex")
    return Path(os.path.realpath(native))


def work_package(codex):
    """Return the installed Codex package and its exact build commit."""
    real = Path(os.path.realpath(codex))
    if real.name == "codex.js":
        real = npm_native(real)
    package = real.parent.parent
    helper = package / HELPER
    if real.name != "codex" or real.parent.name != "bin" or not helper.is_file():
        raise RuntimeError("Windows microphone mode needs a Codex package with "
                           "codex-resources/voice; this Codex install layout is not supported")
    result = subprocess.run([str(helper), "--build-commit"], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10)
    commit = result.stdout.strip()
    if result.returncode or not commit:
        raise RuntimeError("Could not read this computer's Codex voice helper build")
    return package, commit


def stale(mic, codex):
    return work_package(codex)[1] != mic.get("audio", {}).get("build_commit")


def prepare(mic, codex, ssh, run):
    """Choose the Windows helper with this Codex's exact build and install its launcher."""
    _, commit = work_package(codex)
    if not mic["home_is_cwd"]:
        raise RuntimeError("The Windows SSH session does not start in the user's home directory")
    matches = sorted(h["path"] for h in mic["helpers"] if h["build_commit"] == commit)
    if not matches:
        found = ", ".join(sorted({h["build_commit"][:12] for h in mic["helpers"]})) or "none"
        raise RuntimeError(
            f"{mic['host']} has no Codex voice helper matching this computer's Codex build "
            f"{commit[:12]} (found: {found}). Install the same Codex version there, then rerun "
            "setup. Setup does not upgrade Codex or bypass the build check.")
    source = f"HELPER = {matches[0]!r}\nHOST = {mic['host']!r}\n" + LAUNCHER
    name = "host-" + hashlib.sha256(source.encode()).hexdigest()[:16] + ".py"
    script = f"REMOTE_DIR = {REMOTE_DIR!r}\nNAME = {name!r}\nSOURCE = {source!r}\n" + INSTALL
    run([*ssh, "-T", mic["host"], mic["python"] + " -"], input=script)
    # Relative to the SSH session's home and free of spaces, so cmd.exe and
    # PowerShell parse it the same way.
    mic["audio"] = {"build_commit": commit, "command": f"{mic['python']} {REMOTE_DIR}/{name}"}
    del mic["helpers"], mic["home_is_cwd"]


def clone(source, destination):
    # Hardlinks avoid duplicating the large helper; the shared files are never written.
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def prepare_package(package, commit, argv, cache, auth_sock=None):
    """Return a private, content-addressed copy of `package` whose helper runs `argv`."""
    shim = ("#!" + sys.executable + "\nimport os\ncommand = " + repr(list(argv)) + "\n"
            "env = dict(os.environ)\n"
            + (f"env['SSH_AUTH_SOCK'] = {auth_sock!r}\n" if auth_sock else "")
            + "os.execve(command[0], command, env)\n")
    if len(sys.executable.split()) != 1:
        raise RuntimeError("Python's path contains spaces; cannot create the voice shim")
    # Every copied file is fingerprinted so any change to the installation refreshes the copy.
    files = []
    for directory, names, others in os.walk(package):
        for name in sorted(names + others):
            path = os.path.join(directory, name)
            info = os.lstat(path)
            files.append([os.path.relpath(path, package), info.st_size, info.st_mtime_ns, info.st_ino,
                          os.readlink(path) if os.path.islink(path) else ""])
    key = hashlib.sha256(json.dumps([commit, str(package), sorted(files), shim]).encode()).hexdigest()[:24]
    final = cache / key
    if final.exists():
        return final / "bin/codex"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = cache / (".tmp-" + uuid.uuid4().hex)
    try:
        shutil.copytree(package, temporary, symlinks=True, copy_function=clone)
        helper = temporary / HELPER
        # A symlinked directory would make the unlink below reach the installed helper.
        if any((temporary / parent).is_symlink() for parent in tuple(HELPER.parents)[:3]) \
                or (temporary / "bin/codex").is_symlink() or not (temporary / "bin/codex").is_file() \
                or helper.is_symlink() or not helper.is_file():
            raise RuntimeError("Codex package layout is not supported for Windows voice")
        helper.parent.chmod(helper.parent.stat().st_mode | 0o700)
        helper.unlink()  # Never write through a hardlink to the installed helper.
        helper.write_text(shim)
        helper.chmod(0o700)
        try:
            temporary.rename(final)
        except OSError:
            if not final.exists():  # Otherwise a concurrent launch published it first.
                raise
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return final / "bin/codex"


def run_codex(mic, codex, args, ssh, cache):
    """Replace this process with native Codex using the Windows microphone."""
    package, commit = work_package(codex)
    if commit != mic["audio"]["build_commit"]:
        raise RuntimeError("Codex changed; rerun codex-voice setup " + mic["host"])
    if not ssh[0]:
        raise RuntimeError("OpenSSH client not found")
    argv = [*ssh, "-o", "ControlPath=none", "-T", mic["host"], mic["audio"]["command"]]
    executable = str(prepare_package(package, commit, argv, cache, os.environ.get("SSH_AUTH_SOCK")))
    os.execv(executable, [executable, *args])
