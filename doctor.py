"""Read-only diagnostics for people and agents.

Reports what is installed, which route each paired microphone uses, and what
still needs fixing. Nothing here changes pairings, shell files or remote
computers; the only side effect is the one every launch has, starting Codex's
managed daemon if needed. Audio is never opened, so a clean report does not
mean voice works: a person must still speak and listen.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent


def sibling(name):
    spec = importlib.util.spec_from_file_location("codex_voice_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


voice = sibling("voice")
audio = sibling("audio_host")
origin = sibling("client_origin")
windows = sibling("windows_host")
ERRORS = voice.ERRORS
UNIX_ROUTES = {"unix-frontend", "mac-desktop-frontend"}
COPY_ROUTES = {"windows-audio-helper", "mac-audio-helper"}

# Runs on a Unix microphone computer with the paired paths as arguments.
UNIX_DIAGNOSE = r'''import json, os, subprocess, sys
codex, node_dir, codex_home, helper = sys.argv[1:5]
env = dict(os.environ)
if node_dir:
    env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
if codex_home:
    env["CODEX_HOME"] = codex_home
info = {"platform": sys.platform, "python": "%d.%d.%d" % sys.version_info[:3],
        "codex_found": bool(codex) and os.path.isfile(codex) and os.access(codex, os.X_OK),
        "codex_version": None}
if info["codex_found"]:
    try:
        result = subprocess.run([codex, "--version"], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=20)
        lines = result.stdout.strip().splitlines()
        if result.returncode == 0 and lines:
            info["codex_version"] = lines[0][:200]
    except (OSError, subprocess.SubprocessError):
        pass
if helper:
    info["desktop"] = {"helper_found": os.path.isfile(helper)}
    if info["desktop"]["helper_found"]:
        try:
            result = subprocess.run([sys.executable, helper, "status"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=60)
            for line in reversed(result.stdout.splitlines()):
                if line.startswith("CODEX_VOICE_STATUS="):
                    info["desktop"].update(json.loads(line[len("CODEX_VOICE_STATUS="):]))
                    break
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
print("CODEX_VOICE_DIAG=" + json.dumps(info))
'''

MAC_PERMISSION = {
    "authorized": ("ok", None, "macOS allows {app} to use the microphone", None),
    "not_determined": ("warn", "mac_microphone_not_determined",
                       "macOS has not asked for {app}'s microphone permission yet",
                       "During the first /voice, click Allow for {app} on {host}."),
    "denied": ("fail", "mac_microphone_denied", "macOS denies {app} the microphone",
               "On {host}: System Settings → Privacy & Security → Microphone → turn on {app}."),
    "restricted": ("fail", "mac_microphone_restricted",
                   "A configuration profile or Screen Time restricts the microphone on {host}",
                   "Ask {host}'s administrator to allow microphone access for {app}."),
}


ORIGIN_FIXES = {
    "origin_not_typed": voice.step("human", "Type codex yourself in this pane; input sent by programs never "
                                            "chooses a microphone."),
    "origin_disconnected": voice.step("human", "Reconnect the computer you are typing from, then type codex again."),
    "origin_unavailable": voice.step("agent", "Reconnect this Herdr client through an SSH bridge that reports "
                                              "keyboard origins (canary/README.md)."),
    "origin_no_match": voice.step("agent", "Pair the computer the person types from.",
                                  "codex-voice pair NAME HOST --json"),
    "tailscale_cli_missing": voice.step("agent", "Install the Tailscale CLI on this work computer."),
    "tailscale_unidentified": voice.step("agent", "Check that the typing computer is on this tailnet."),
    "origin_lookup_failed": voice.step("agent", "Check that this pane's Herdr server is running and reachable, "
                                                "then retry."),
}


class Report:
    def __init__(self):
        self.checks = []

    def add(self, scope, check, status, message, code=None, next_step=None):
        entry = {"scope": scope, "check": check, "status": status, "message": message}
        if code:
            entry["code"] = code
        if next_step:
            entry["next_step"] = next_step
        self.checks.append(entry)

    def fail(self, scope, check, error, host=None):
        code = voice.error_code(error)
        fixes = voice.fix(code, host)
        self.add(scope, check, "fail", str(error), code, fixes[0] if fixes else None)


def first_line(argv, timeout=20):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.strip().splitlines()
    return lines[0].strip()[:200] if result.returncode == 0 and lines else None


def remote(argv, input=None, timeout=90):
    """Like voice.run, with room for a Mac to start its desktop helper."""
    result = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"{argv[0]} exited {result.returncode}")
    return result.stdout


def marked(output, prefix):
    for line in reversed(output.splitlines()):
        if line.startswith(prefix):
            return json.loads(line[len(prefix):])
    raise RuntimeError("The microphone computer printed no diagnostic result")


def manifest_version(root):
    try:
        match = re.search(r'^version\s*=\s*"([^"]+)"', (Path(root) / "herdr-plugin.toml").read_text(encoding="utf-8"),
                          re.M)
    except OSError:
        return None
    return match.group(1) if match else None


def refresh(path, mic):
    """The idempotent command that re-pairs this microphone."""
    host = "--local" if mic.get("local") else mic.get("host", "HOST")
    mode = " --audio-only" if mic.get("mac_audio") and sys.platform != "win32" else ""
    if path.name == "config.json":
        return f"codex-voice setup {shlex.quote(host)}{mode} --json"
    return f"codex-voice pair {shlex.quote(mic.get('label', 'NAME'))} {shlex.quote(host)}{mode} --json"


def herdr(report):
    info = {"found": False, "version": None, "plugin_enabled": None, "plugin_version": None,
            "plugin_root": None, "pane_last_input": None}
    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not binary:
        report.add("work", "herdr", "fail", "Herdr CLI not found on this computer", "herdr_unavailable",
                   voice.fix("herdr_unavailable")[0])
        return info
    info["found"] = True
    version = re.search(r"\d+\.\d+\.\d+", first_line([binary, "--version"], 10) or "")
    info["version"] = version.group(0) if version else None
    try:
        plugins = json.loads(voice.run([binary, "plugin", "list", "--plugin", voice.PLUGIN, "--json"]))
        plugins = [(bool(plugin.get("enabled")), plugin.get("plugin_root")) for plugin in plugins["result"]["plugins"]]
    except (*ERRORS, TypeError, AttributeError) as error:
        report.add("work", "plugin", "fail", f"Herdr could not list plugins: {error}", "herdr_unavailable",
                   voice.fix("herdr_unavailable")[0])
        return info
    if not plugins:
        report.add("work", "plugin", "fail", "The Herdr plugin is not installed", "plugin_not_installed",
                   voice.fix("plugin_not_installed")[0])
        return info
    info["plugin_enabled"], info["plugin_root"] = plugins[0]
    info["plugin_version"] = manifest_version(info["plugin_root"]) if info["plugin_root"] else None
    if info["plugin_enabled"]:
        report.add("work", "plugin", "ok", f"Herdr plugin {info['plugin_version'] or '(version unknown)'} "
                                           "is installed and enabled")
    else:
        report.add("work", "plugin", "fail", "The Herdr plugin is disabled, so codex runs without voice",
                   "plugin_disabled", voice.step("human", "Enable herdr-codex-voice in Herdr's plugin settings."))
    if version and tuple(map(int, version.group(0).split("."))) < (0, 9, 3):
        report.add("work", "herdr", "fail", f"Herdr {version.group(0)} is older than 0.9.3", "herdr_too_old",
                   voice.step("agent", "Update Herdr to 0.9.3 or later."))
    return info


def launcher(report):
    path = voice.launcher_path()
    info = {"path": str(path), "current": False}
    if sys.platform == "win32":
        reinstall = f"& {voice.ps_quote(sys.executable)} {voice.ps_quote(ROOT / 'voice.py')} install"
    else:
        reinstall = f"python3 {shlex.quote(str(ROOT / 'voice.py'))} install"
    fix = voice.step("agent", "Reinstall the launcher from the installed plugin.", reinstall)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        report.add("work", "launcher", "fail", f"{path} is missing", "launcher_missing", fix)
        return info
    if "herdr-codex-voice" not in text:
        report.add("work", "launcher", "fail", f"{path} belongs to another program", "launcher_conflict",
                   voice.fix("launcher_conflict")[0])
    elif text != (ROOT / "voice.py").read_text(encoding="utf-8"):
        report.add("work", "launcher", "fail", f"{path} differs from the installed plugin; an update did not finish",
                   "launcher_stale", fix)
    else:
        info["current"] = True
        report.add("work", "launcher", "ok", f"{path} matches the installed plugin")
    return info


def codex(report, choices, offline):
    info = {"found": False, "version": None, "build_commit": None, "package_layout": None}
    path = shutil.which("codex")
    if not path:
        report.add("work", "codex", "fail", "Codex CLI is not on PATH", "codex_missing", voice.fix("codex_missing")[0])
        return info
    info["found"] = True
    try:
        info["version"] = first_line([*voice.native(path), "--version"])
    except ERRORS:
        pass
    if info["version"]:
        report.add("work", "codex", "ok", f"Found {info['version']}")
    else:
        report.add("work", "codex", "fail", f"`codex --version` failed for {path}", "codex_broken",
                   voice.step("agent", "Reinstall Codex CLI on this work computer, then check `codex --version`."))
    copied = [mic for _, mic in choices if voice.route(mic)[0] in COPY_ROUTES]
    try:
        info["build_commit"] = audio.work_package(path)[1]
        info["package_layout"] = True
    except ERRORS as error:
        info["package_layout"] = False
        if copied or sys.platform == "win32":
            report.fail("work", "codex_package", error)
    # Only routes whose terminal runs on the microphone computer forward the backend socket.
    needed = sys.platform != "win32" and (not choices or any(voice.route(mic)[0] in UNIX_ROUTES for _, mic in choices))
    if offline or not needed:
        reason = " (--offline)" if offline else " (Codex manages its backend on Windows)" if sys.platform == "win32" else ""
        report.add("work", "backend", "skip", "Backend check skipped" + reason)
    else:
        try:
            voice.backend(path)
            report.add("work", "backend", "ok", "Codex's backend daemon socket is ready (this does not test voice)")
        except ERRORS as error:
            report.fail("work", "backend", error)
    return info


def shell(report):
    if sys.platform == "win32":
        return powershell(report)
    rc = voice.shell_file()
    info = {"file": str(rc) if rc else None, "installed": False, "detects_reload": False,
            "new_shells": None, "this_shell": None}
    if not rc:
        report.add("work", "shell", "warn", "Only Bash and Zsh get the automatic codex alias", "shell_unsupported",
                   voice.step("human", "Start Codex with the launcher in this shell.", "codex-voice run"))
        return info
    launcher_path = str(voice.launcher_path())
    block = voice.integration_block(rc)
    if not block or launcher_path not in block:
        state = "has an outdated codex block" if block else "has no codex block"
        report.add("work", "shell", "fail", f"{rc} {state}",
                   "shell_integration_stale" if block else "shell_integration_missing",
                   voice.step("agent", "Rerun setup for any paired microphone; it is idempotent.",
                              "codex-voice setup HOST --json"))
        return info
    info["installed"] = True
    info["detects_reload"] = voice.SHELL_MARKER in block
    try:
        result = subprocess.run([os.environ.get("SHELL", "/bin/bash"), "-ic", "alias codex"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
                                env={**os.environ, "HERDR_ENV": "1", "NO_HERDR": "1"})
        info["new_shells"] = launcher_path in result.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    if info["new_shells"]:
        report.add("work", "shell", "ok", f"New Herdr shells get the codex alias from {rc}")
    elif info["new_shells"] is False:
        report.add("work", "shell", "fail", f"A new interactive shell does not define the codex alias from {rc}",
                   "shell_alias_not_loaded",
                   voice.step("agent", f"Make sure interactive shells read {rc} and nothing later redefines codex."))
    else:
        report.add("work", "shell", "unknown", "Could not start a test shell to check the codex alias")
    this_shell(report, info, rc)
    return info


# Run by PowerShell after its profiles, as in a new Herdr pane: whether the block ran, then
# the command a plain `codex` invokes (an alias outranks a function, which outranks codex.cmd).
PROBE = "CODEX_VOICE_PROBE"
RESOLVE_CODEX = (f"'{PROBE}'; [string]$env:{voice.SHELL_MARKER}; "
                 "$c = $ExecutionContext.InvokeCommand.GetCommand('codex', 'All'); "
                 "if ($c) { [string]$c.CommandType; $c.Definition }")


def resolve_codex(output, launcher):
    """From RESOLVE_CODEX's output: (whether the block ran, or None if PowerShell printed nothing;
    None if plain codex runs exactly this launcher's function, else the command type it runs)."""
    if PROBE not in output:
        return None, None
    lines = output.split(PROBE)[-1].splitlines()[1:] + ["", ""]
    kind = lines[1].strip()
    if kind == "Function" and "\n".join(lines[2:]).strip() == voice.powershell_function(launcher):
        kind = None
    return lines[0].strip() == voice.SHELL_REVISION, kind


def powershell(report):
    """Windows: each PowerShell edition's console profile makes codex this launcher in Herdr panes."""
    profiles = voice.powershell_profiles()
    info = {"file": str(profiles[0][1]) if profiles else None, "files": [str(p) for _, p in profiles],
            "installed": False, "detects_reload": False, "new_shells": None, "this_shell": None}
    launcher_path = str(voice.launcher_path())
    if not profiles:
        report.add("work", "shell", "warn", "PowerShell was not found, so codex has no automatic integration",
                   "shell_unsupported", voice.step("human", "Start Codex with the launcher.",
                                                   voice.launch_command(launcher_path)))
        return info
    rerun = voice.step("agent", "Rerun setup for any paired microphone; it is idempotent.",
                       "codex-voice setup HOST --json")
    for executable, profile in profiles:
        edition = Path(executable).stem
        block = voice.integration_block(profile)
        if not block or voice.powershell_function(launcher_path) not in block:
            state = "has an outdated codex block" if block else "has no codex block"
            report.add("work", "shell", "fail", f"{profile} {state}",
                       "shell_integration_stale" if block else "shell_integration_missing", rerun)
            continue
        info["installed"] = True
        info["detects_reload"] = True
        # Starting PowerShell is the real test: an execution policy may refuse the profile,
        # and a later alias or function may still take codex.
        environment = {key: value for key, value in os.environ.items() if key != voice.SHELL_MARKER}
        try:
            result = subprocess.run([executable, "-NoLogo", "-NonInteractive", "-Command", RESOLVE_CODEX],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                                    timeout=30, env={**environment, "HERDR_ENV": "1"})
            loaded, other = resolve_codex(result.stdout, launcher_path)
        except (OSError, subprocess.SubprocessError):
            loaded, other = None, None
        if loaded and other is None:
            info["new_shells"] = info["new_shells"] is not False
            report.add("work", "shell", "ok", f"In new {edition} sessions in Herdr, codex runs this launcher")
        elif loaded:
            info["new_shells"] = False
            what = {"Alias": "an alias", "Function": "another function", "Application": "Codex without voice",
                    "": "nothing"}.get(other, other)
            report.add("work", "shell", "fail", f"In new {edition} sessions in Herdr, codex runs {what}: something "
                                                f"after the block in {profile} redefines it", "shell_alias_not_loaded",
                       voice.step("agent", "Rerun setup for any paired microphone; it moves the block to the end "
                                           "of the profile, after the later codex definition.",
                                  "codex-voice setup HOST --json"))
        elif loaded is False:
            info["new_shells"] = False
            report.add("work", "shell", "fail", f"{edition} does not run the codex block in {profile}; its execution "
                                                "policy may block profile scripts, or the profile stops on an error",
                       "shell_alias_not_loaded",
                       voice.step("human", f"Use a PowerShell whose policy runs your profile, or start Codex with "
                                           f"the launcher. The plugin never changes execution policy.",
                                  voice.launch_command(launcher_path)))
        else:
            report.add("work", "shell", "unknown", f"Could not start {edition} to check the codex command")
    this_shell(report, info, profiles[0][1])
    return info


def this_shell(report, info, rc):
    """Whether the shell that started this command loaded the integration (exported marker)."""
    reload = voice.step("human", "Quit Codex in this pane and reload the shell once, or open a new pane.",
                        voice.reload_command(rc))
    marker = os.environ.get(voice.SHELL_MARKER)
    if os.environ.get("HERDR_ENV") != "1":
        report.add("work", "this_shell", "unknown",
                   "Not running in a Herdr pane; run doctor in a pane to check that pane's shell")
    elif marker == voice.SHELL_REVISION:
        info["this_shell"] = True
        report.add("work", "this_shell", "ok", "This Herdr pane's shell loaded the integration")
    elif marker or info["detects_reload"]:
        info["this_shell"] = False
        report.add("work", "this_shell", "warn",
                   "This Herdr pane's shell started before setup or an update, or does not read " + str(rc),
                   "shell_reload_needed", reload)
    else:
        report.add("work", "this_shell", "unknown",
                   "The installed shell block predates reload detection; rerun setup to enable it")


def selection(report, choices, capability, in_pane=False):
    """How codex launched in this pane would choose; `capability` None in a pane means Herdr did not answer."""
    info = {"mode": "none", "pane_last_input": capability, "selected": None}
    if not choices:
        return info
    if in_pane and capability is None:
        # connect() stops in exactly this case rather than guessing a microphone.
        info["mode"] = "error"
        report.add("routes", "this_pane", "fail", "A codex launched in this pane now would stop: Herdr did not "
                                                  "report whether it knows keyboard origins", "origin_lookup_failed",
                   ORIGIN_FIXES["origin_lookup_failed"])
    elif capability:
        info["mode"] = "automatic"
        identities = [("local",) if mic.get("local") else ("node", mic.get("tailscale_node_id"))
                      for _, mic in choices]
        for path, mic in choices:
            name = mic.get("label") or mic.get("host")
            if not mic.get("local") and not mic.get("tailscale_node_id"):
                report.add("routes", "identity", "warn",
                           f"{name} has no Tailscale identity, so automatic selection never chooses it",
                           "pairing_without_identity",
                           voice.step("agent", "Install and log into the Tailscale CLI on that computer, "
                                               "then pair it again.", refresh(path, mic)))
        duplicates = {identity for identity in identities if identity[-1] and identities.count(identity) > 1}
        if duplicates:
            report.add("routes", "identity", "fail",
                       "Several pairings are the same computer; automatic selection refuses to guess",
                       "duplicate_identity",
                       voice.step("agent", "Keep one pairing per computer.", "codex-voice unpair NAME --json"))
        try:
            selected = origin.select(choices)
        except RuntimeError as error:
            # Every origin error stops a launch in this pane; none of them is guessed around.
            report.add("routes", "this_pane", "fail", "A codex launched in this pane now would stop: " + str(error),
                       voice.error_code(error), ORIGIN_FIXES.get(voice.error_code(error)))
        else:
            if selected:
                info["selected"] = selected[1].get("label") or selected[1].get("host") or "this computer"
                report.add("routes", "this_pane", "ok",
                           f"A codex launched in this pane now would use {info['selected']}")
    elif len(choices) == 1:
        info["mode"] = "single"
        report.add("routes", "selection", "ok", "One microphone is paired; codex uses it without asking")
    elif capability is False:
        info["mode"] = "prompt"
        report.add("routes", "selection", "warn",
                   f"{len(choices)} microphones are paired and this Herdr does not report keyboard origins "
                   "(pane_last_input), so codex asks which one to use at each launch. Automatic selection needs "
                   "a Herdr server and SSH bridge with pane_last_input, such as the canary in the plugin's "
                   "canary/ directory; released Herdr 0.9.3 lacks it.", "automatic_selection_unavailable",
                   voice.step("human", "Choose the microphone when codex asks, or install the canary "
                                       "(canary/README.md)."))
    else:
        info["mode"] = "unknown"
        report.add("routes", "selection", "unknown",
                   f"{len(choices)} microphones are paired; run doctor inside a Herdr pane to check "
                   "whether Herdr selects one automatically (pane.last_input)")
    return info


def unix_microphone(report, scope, path, mic, work_version):
    host = mic["host"]
    facts = {}
    helper = ""
    if mic.get("platform") == "darwin":
        if mic.get("desktop_revision") != voice.desktop_revision(ROOT):
            report.add(scope, "desktop_helper", "warn",
                       "The Mac desktop helper predates this plugin; the next codex launch updates it",
                       "desktop_helper_stale", voice.step("agent", "Or update it now.", refresh(path, mic)))
        else:
            helper = mic.get("desktop_helper", "")
    command = shlex.join([mic.get("python") or "python3", "-c", UNIX_DIAGNOSE, mic.get("codex", ""),
                          mic.get("node_dir", ""), mic.get("codex_home", ""), helper])
    try:
        data = marked(remote([*voice.SSH, "-nT", host, command]), "CODEX_VOICE_DIAG=")
    except ERRORS as error:
        if voice.ssh_failure(str(error)) or isinstance(error, subprocess.TimeoutExpired):
            report.fail(scope, "ssh", error, host)
        else:
            report.add(scope, "ssh", "fail", f"The diagnostic could not run on {host}: {error}",
                       "microphone_probe_failed", voice.step("agent", "Pair it again.", refresh(path, mic)))
        return facts
    report.add(scope, "ssh", "ok", f"SSH to {host} works without prompts")
    facts["codex_version"] = data.get("codex_version")
    if not data.get("codex_found"):
        report.add(scope, "codex", "fail", f"Codex moved or was removed on {host}", "microphone_codex_moved",
                   voice.step("agent", "Pair it again to find Codex.", refresh(path, mic)))
    elif not facts["codex_version"]:
        report.add(scope, "codex", "fail", f"`codex --version` failed on {host}", "microphone_codex_broken",
                   voice.step("agent", f"Reinstall Codex CLI on {host}, then pair it again.", refresh(path, mic)))
    elif work_version and facts["codex_version"] != work_version:
        report.add(scope, "codex", "warn",
                   f"{host} runs {facts['codex_version']} but this computer runs {work_version}; "
                   "install the same Codex version on both if sessions fail", "codex_version_mismatch")
    else:
        report.add(scope, "codex", "ok", f"Found {facts['codex_version']} on {host}")
    if helper:
        desktop = data.get("desktop") or {}
        facts["mac_microphone"] = desktop.get("microphone", "unknown")
        if not desktop.get("helper_found"):
            report.add(scope, "desktop_helper", "fail", f"The Mac desktop helper is missing on {host}",
                       "mac_desktop_helper_missing", voice.step("agent", "Pair it again.", refresh(path, mic)))
        elif desktop.get("desktop_session") is False:
            report.add(scope, "desktop_session", "fail",
                       f"The SSH user is not the active desktop user on {host}", "mac_desktop_not_logged_in",
                       voice.step("human", f"Log into {host}'s desktop as the SSH user and keep that session active."))
        elif desktop.get("app_installed") is False:
            report.add(scope, "desktop_helper", "fail", f"The Codex Voice app is missing on {host}",
                       "mac_desktop_app_missing", voice.step("agent", "Pair it again.", refresh(path, mic)))
        else:
            permission(report, scope, host, "Codex Voice", facts["mac_microphone"])
    forwarding(report, scope, host)
    return facts


def permission(report, scope, host, app, state):
    if state in MAC_PERMISSION:
        status, code, message, remedy = MAC_PERMISSION[state]
        report.add(scope, "microphone_permission", status, message.format(host=host, app=app), code,
                   voice.step("human", remedy.format(host=host, app=app)) if remedy else None)
    else:
        report.add(scope, "microphone_permission", "unknown", f"Could not read {app}'s microphone permission on {host}")


def forwarding(report, scope, host):
    """Check that the SSH server accepts the remote Unix socket forward each launch needs."""
    socket_path = "/tmp/hcv-check-" + uuid.uuid4().hex + ".sock"
    quoted = shlex.quote(socket_path)
    # The forward may be bound after the command starts, so wait for it briefly.
    command = (f"i=0; while [ ! -S {quoted} ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i+1)); done; "
               f"test -S {quoted}; s=$?; rm -f -- {quoted}; exit $s")
    with tempfile.TemporaryDirectory(prefix="hcv-") as directory:
        # Nothing listens here; the forward is removed before anything could connect.
        target = os.path.join(directory, "unused.sock")
        argv = [*voice.SSH, "-o", "ExitOnForwardFailure=yes", "-o", "ControlPath=none",
                "-R", socket_path + ":" + target, "-nT", host, command]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            report.fail(scope, "forwarding", error, host)
            return
    if result.returncode == 0:
        report.add(scope, "forwarding", "ok", f"{host} accepts remote Unix socket forwarding")
    elif result.returncode == 255:
        report.fail(scope, "forwarding", RuntimeError(result.stderr.strip() or "ssh failed"), host)
    else:
        report.add(scope, "forwarding", "unknown", f"The forwarded test socket did not appear on {host}")


def windows_microphone(report, scope, path, mic, work_commit):
    host = mic["host"]
    facts = {}
    try:
        data = audio.diagnose(mic, voice.SSH, remote)
    except ERRORS as error:
        if voice.ssh_failure(str(error)) or isinstance(error, subprocess.TimeoutExpired):
            report.fail(scope, "ssh", error, host)
        else:
            report.add(scope, "ssh", "fail", f"The diagnostic could not run on {host}: {error}",
                       "microphone_probe_failed", voice.step("agent", "Pair it again.", refresh(path, mic)))
        return facts
    report.add(scope, "ssh", "ok", f"SSH to {host} works without prompts")
    facts["build_commit"] = data.get("build_commit")
    again = voice.step("agent", "Pair it again.", refresh(path, mic))
    if not data.get("launcher_found"):
        report.add(scope, "helper", "fail", f"The voice launcher is missing on {host}", "windows_launcher_missing", again)
    elif not data.get("helper_found") or not facts["build_commit"]:
        report.add(scope, "helper", "fail", f"Codex's voice helper moved or does not run on {host}",
                   "windows_helper_missing", again)
    elif not work_commit:
        report.add(scope, "helper", "unknown", f"This computer's Codex build is unknown, so {host}'s helper "
                                               "cannot be compared")
    elif facts["build_commit"] != work_commit:
        report.add(scope, "helper", "fail",
                   f"{host}'s voice helper is build {facts['build_commit'][:12]}, this computer's Codex is "
                   f"{work_commit[:12]}", "windows_helper_build_mismatch",
                   voice.fix("windows_helper_build_mismatch", host)[0])
    else:
        report.add(scope, "helper", "ok", f"{host}'s voice helper matches this computer's Codex build")
    facts["windows_microphone"] = audio.privacy_state(data.get("privacy"))
    if facts["windows_microphone"] == "allowed":
        report.add(scope, "microphone_permission", "ok", "Windows privacy settings allow desktop apps the microphone")
    elif facts["windows_microphone"] == "denied":
        report.add(scope, "microphone_permission", "fail", f"Windows privacy settings deny the microphone on {host}",
                   "windows_microphone_denied",
                   voice.step("human", f"On {host}: Settings → Privacy & security → Microphone → turn on "
                                       "Microphone access and Let desktop apps access your microphone."))
    else:
        report.add(scope, "microphone_permission", "unknown",
                   f"Could not read Windows microphone privacy settings on {host}")
    return facts


def mac_audio_microphone(report, scope, path, mic, work_commit):
    """Only Codex's voice helper runs on the Mac, through Codex Voice Audio."""
    host, route = mic["host"], mic["mac_audio"]
    facts = {}
    again = voice.step("agent", "Pair it again.", refresh(path, mic))
    if route.get("revision") != hashlib.sha256((ROOT / "macos_audio.py").read_bytes()).hexdigest():
        report.add(scope, "audio_controller", "warn",
                   "The Mac audio controller predates this plugin; the next codex launch updates it",
                   "mac_audio_controller_stale", voice.step("agent", "Or update it now.", refresh(path, mic)))
    command = shlex.join([mic.get("python") or "python3", route["controller"], "check", route["helper"]])
    try:
        data = marked(remote([*voice.SSH, "-nT", host, command]), "CODEX_VOICE_STATUS=")
    except ERRORS as error:
        if voice.ssh_failure(str(error)) or isinstance(error, subprocess.TimeoutExpired):
            report.fail(scope, "ssh", error, host)
        else:
            report.add(scope, "ssh", "fail", f"The diagnostic could not run on {host}: {error}",
                       "microphone_probe_failed", again)
        return facts
    report.add(scope, "ssh", "ok", f"SSH to {host} works without prompts")
    facts["build_commit"] = data.get("build_commit")
    facts["mac_microphone"] = data.get("microphone", "unknown")
    if not data.get("helper_found") or not facts["build_commit"]:
        report.add(scope, "helper", "fail", f"Codex's voice helper moved or does not run on {host}",
                   "mac_helper_missing", again)
    elif not work_commit:
        report.add(scope, "helper", "unknown", f"This computer's Codex build is unknown, so {host}'s helper "
                                               "cannot be compared")
    elif facts["build_commit"] != work_commit:
        report.add(scope, "helper", "fail",
                   f"{host}'s voice helper is build {facts['build_commit'][:12]}, this computer's Codex is "
                   f"{work_commit[:12]}", "mac_helper_build_mismatch", voice.fix("mac_helper_build_mismatch", host)[0])
    else:
        report.add(scope, "helper", "ok", f"{host}'s voice helper matches this computer's Codex build")
    if data.get("desktop_session") is False:
        report.add(scope, "desktop_session", "fail", f"The SSH user is not the active desktop user on {host}",
                   "mac_desktop_not_logged_in",
                   voice.step("human", f"Log into {host}'s desktop as the SSH user and keep that session active."))
    elif data.get("app_installed") is False:
        report.add(scope, "desktop_helper", "fail", f"The Codex Voice Audio app is missing on {host}",
                   "mac_desktop_app_missing", again)
    else:
        permission(report, scope, host, "Codex Voice Audio", facts["mac_microphone"])
    return facts


def parse(args):
    options = {"json": False, "offline": False, "microphone": None}
    args = list(args)
    while args:
        arg = args.pop(0)
        if arg in ("--json", "--offline"):
            options[arg[2:]] = True
        elif arg == "--microphone" and args:
            options["microphone"] = args.pop(0)
        else:
            raise voice.InputError("usage", "Usage: codex-voice doctor [--json] [--offline] [--microphone NAME]")
    return options


def diagnose(offline=False, only=None):
    report = Report()
    work = {"platform": sys.platform, "machine": platform.machine(), "python": platform.python_version(),
            "shell": Path(os.environ.get("SHELL", "")).name or None, "plugin": voice.VERSION,
            "diagnosed_root": str(ROOT), "in_herdr_pane": os.environ.get("HERDR_ENV") == "1"}
    if sys.platform in ("linux", "darwin"):
        report.add("work", "platform", "ok", f"{sys.platform} work computers are supported")
    elif sys.platform == "win32":
        report.add("work", "platform", "warn", "Native Windows work computers are experimental and can use only a "
                                               "Mac microphone computer", "experimental_route")
        if windows.elevated():
            report.add("work", "elevation", "fail", "This is an elevated administrator session; setup and codex "
                                                    "must run in a normal Herdr pane", "windows_elevated",
                       voice.fix("windows_elevated")[0])
    else:
        report.add("work", "platform", "fail", f"{sys.platform} work computers are not supported",
                   "unsupported_platform", voice.fix("unsupported_platform")[0])
    work["herdr"] = herdr(report)
    work["launcher"] = launcher(report)
    directory = None
    try:
        directory = voice.config_dir()
        choices = voice.microphones(directory)
    except ERRORS as error:
        report.fail("work", "pairings", error)
        choices = []
    else:
        if not choices:
            report.add("work", "pairings", "fail", "No microphone computer is paired", "no_pairing",
                       voice.fix("no_pairing")[0])
    work["codex"] = codex(report, choices, offline)
    work["shell"] = shell(report)
    if sys.platform == "win32" and directory is not None:
        relay = windows.relay_path(directory)
        work["relay"] = {"path": str(relay), "built": relay.is_file()}
        if relay.is_file():
            report.add("work", "relay", "ok", "The Windows voice relay is built from this plugin's source")
        else:
            report.add("work", "relay", "fail", "The Windows voice relay for this plugin version is not built",
                       "windows_relay_missing", voice.fix("windows_relay_missing")[0])
    socket_path, pane = os.environ.get("HERDR_SOCKET_PATH"), os.environ.get("HERDR_PANE_ID")
    in_pane = bool(socket_path and pane)
    capability = origin.capable(socket_path) if in_pane else None
    work["herdr"]["pane_last_input"] = capability
    routes = selection(report, choices, capability, in_pane)
    microphones = []
    for path, mic in choices:
        entry = voice.describe(path, mic)
        if only is not None and only not in (entry["name"], entry["host"]):
            continue
        scope = "microphone:" + (entry["name"] or entry["host"] or "this computer")
        if mic.get("local"):
            report.add(scope, "route", "ok", "Uses this computer's microphone directly")
        elif sys.platform == "win32" and entry["route"] != "mac-audio-helper":
            report.add(scope, "route", "fail", "A Windows work computer can use only a Mac microphone computer",
                       "unsupported_route", voice.fix("unsupported_route")[0])
        elif offline:
            report.add(scope, "route", "skip", f"Remote checks for {entry['host']} skipped (--offline)")
        elif entry["route"] == "mac-audio-helper":
            report.add(scope, "route", "ok", "Codex runs here; only its voice helper runs on the Mac (experimental)")
            commit = work["codex"]["build_commit"]
            if mic.get("work_codex"):  # Chosen with setup --codex instead of PATH.
                try:
                    commit = audio.work_package(mic["work_codex"])[1]
                except ERRORS as error:
                    report.fail(scope, "codex_package", error)
                    commit = None
            entry.update(mac_audio_microphone(report, scope, path, mic, commit))
        elif entry["route"] == "windows-audio-helper":
            report.add(scope, "route", "ok", "Windows microphone: only Codex's voice helper runs there "
                                             "(experimental)")
            entry.update(windows_microphone(report, scope, path, mic, work["codex"]["build_commit"]))
        else:
            report.add(scope, "route", "ok", "Codex's terminal runs there; the backend stays here")
            entry.update(unix_microphone(report, scope, path, mic, work["codex"]["version"]))
        microphones.append(entry)
    if only is not None and not microphones:
        report.add("work", "pairings", "fail", f"No paired microphone is named {only}", "no_pairing",
                   voice.fix("no_pairing")[0])
    failures = [check for check in report.checks if check["status"] == "fail"]
    steps = []
    for check in failures + [check for check in report.checks if check["status"] == "warn"]:
        if check.get("next_step") and check["next_step"] not in steps:
            steps.append(check["next_step"])
    return {"schema": voice.SCHEMA, "command": "doctor", "version": voice.VERSION, "ok": not failures,
            "error": {"code": failures[0].get("code", "failed"), "message": failures[0]["message"]} if failures else None,
            "work_host": work, "microphones": microphones, "route_selection": routes,
            "checks": report.checks, "next_steps": steps,
            # Nothing here hears or plays audio; only a person can confirm voice.
            "voice_verified": False, "physical_verification": voice.PHYSICAL_CHECKS}


def lines(report):
    yield f"Codex Voice {report['version']}: automated checks (audio itself is not tested)"
    for check in report["checks"]:
        yield f"  {check['status'].upper():7} {check['scope']}: {check['message']}"
        fix = check.get("next_step")
        if fix and check["status"] in ("fail", "warn"):
            yield "          → " + fix["text"] + (f"  [{fix['command']}]" if fix.get("command") else "")
    yield "A person must still confirm on each microphone computer:"
    yield from ("  - " + check for check in report["physical_verification"])


def main(args):
    try:
        options = parse(args)
    except ValueError as error:
        if "--json" in args:
            print(json.dumps(voice.envelope("doctor", False, error={"code": "usage", "message": str(error)}), indent=2))
        else:
            print("Codex Voice: " + str(error), file=sys.stderr)
        return 2
    try:
        report = diagnose(options["offline"], options["microphone"])
    except ERRORS as error:
        if not options["json"]:
            raise
        code = voice.error_code(error)
        print(json.dumps(voice.envelope("doctor", False, error={"code": code, "message": str(error)},
                                        next_steps=voice.fix(code)), indent=2))
        return 1
    if options["json"]:
        print(json.dumps(report, indent=2))
    else:
        print("\n".join(lines(report)))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
