"""Run Codex's exact-build voice helper on this Mac for a Codex terminal on another computer.

Only the helper's binary protocol crosses SSH; nothing is converted. A separate
background app, Codex Voice Audio, starts the stock helper so its microphone
requests have a desktop application identity. The Codex Voice app used when the
whole Codex terminal runs here is not involved and keeps its own permission.

Commands: install | status | check HELPER | helpers CODEX CODEX_HOME | run HELPER BUILD_COMMIT
"""

import glob
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path.home() / ".local/share/herdr-codex-voice"
SWIFT = r'''import AVFoundation
import Darwin
import Foundation

struct Request: Decodable {
    let program: String
    let env: [String: String]
    let socket: String
    let owner: Int32
}
let arguments = CommandLine.arguments
// This app's own microphone permission. Asking for the status never prompts.
if arguments.count == 3 && arguments[1] == "--microphone-status" {
    let status = AVCaptureDevice.authorizationStatus(for: .audio).rawValue
    try? "\(status)".write(toFile: arguments[2], atomically: true, encoding: .utf8)
    exit(0)
}
guard arguments.count == 2 else { exit(2) }
let job = URL(fileURLWithPath: arguments[1])
let directory = job.deletingLastPathComponent()
func write(_ name: String, _ text: String) throws {
    try text.write(to: directory.appendingPathComponent(name), atomically: true, encoding: .utf8)
}
func stop(_ pid: Int32) {
    if pid > 1 { kill(getpgid(pid) == pid ? -pid : pid, SIGKILL) }
}
func connectTo(_ path: String) -> Int32 {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    if fd < 0 { return -1 }
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    guard bytes.count < MemoryLayout.size(ofValue: address.sun_path) else { close(fd); return -1 }
    withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
    let connected = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    if connected != 0 { close(fd); return -1 }
    return fd
}
do {
    let request = try JSONDecoder().decode(Request.self, from: Data(contentsOf: job))
    guard request.owner > 1 && kill(request.owner, 0) == 0 else { exit(129) }
    let fd = connectTo(request.socket)
    guard fd >= 0 else {
        throw NSError(domain: "CodexVoiceAudio", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "could not reach the voice controller"])
    }
    let stream = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    let child = Process()
    child.executableURL = URL(fileURLWithPath: request.program)
    child.arguments = []
    child.environment = request.env
    child.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
    child.standardInput = stream
    child.standardOutput = stream
    child.standardError = FileHandle.nullDevice
    try child.run()
    // The helper holds its own copy; closing ours makes its exit an EOF for the controller.
    try? stream.close()
    let pid = child.processIdentifier
    defer { if child.isRunning { stop(pid) } }
    // The controller can disappear without warning, even via SIGKILL.
    let owner = DispatchSource.makeProcessSource(identifier: request.owner, eventMask: .exit, queue: .global())
    owner.setEventHandler { stop(pid); exit(129) }
    owner.resume()
    defer { owner.cancel() }
    guard kill(request.owner, 0) == 0 else { stop(pid); exit(129) }
    try write("pids", "\(getpid()) \(pid) \(getpgid(pid))")
    child.waitUntilExit()
    let code = child.terminationReason == .uncaughtSignal ? 128 + child.terminationStatus : child.terminationStatus
    try write("exit", "\(code)")
} catch {
    try? write("error", String(describing: error))
    try? write("exit", "1")
}
'''
INFO = {"CFBundleIdentifier": "dev.herdr.codex-voice.audio", "CFBundleName": "Codex Voice Audio",
        "CFBundleExecutable": "audio-runner", "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1", "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Use your microphone for Codex voice in a terminal on another computer."}
ENTITLEMENTS = {"com.apple.security.device.audio-input": True}
DIGEST = hashlib.sha256(SWIFT.encode() + plistlib.dumps(INFO) + plistlib.dumps(ENTITLEMENTS)).hexdigest()
APP = ROOT / "apps" / DIGEST / "Codex Voice Audio.app"
BINARY = APP / "Contents/MacOS/audio-runner"
PERMISSIONS = {"0": "not_determined", "1": "restricted", "2": "denied", "3": "authorized"}
HELPER_TAIL = ("codex-resources", "voice", "bin", "codex-voice-host")
START_TIMEOUT = 20  # seconds for the app to start and connect
# The helper gets a minimal environment, as Codex gives it: no GStreamer
# plugin discovery or registry, so stray installations are never loaded.
KEEP = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
GSTREAMER = {"GST_PLUGIN_PATH": "", "GST_PLUGIN_PATH_1_0": "", "GST_PLUGIN_SYSTEM_PATH": "",
             "GST_PLUGIN_SYSTEM_PATH_1_0": "", "GST_REGISTRY": "/dev/null",
             "GST_REGISTRY_UPDATE": "no", "GST_REGISTRY_FORK": "no"}


def check_desktop():
    if Path("/dev/console").stat().st_uid != os.getuid():
        raise RuntimeError("Log into this Mac's desktop as the SSH user first")


def install():
    if BINARY.is_file():
        return
    if subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode:
        raise RuntimeError("Install Apple's Command Line Tools on the microphone Mac: xcode-select --install")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        app = Path(directory) / APP.name
        binary = app / "Contents/MacOS/audio-runner"
        binary.parent.mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(INFO))
        source = Path(directory) / "main.swift"
        source.write_text(SWIFT)
        entitlements = Path(directory) / "entitlements.plist"
        entitlements.write_bytes(plistlib.dumps(ENTITLEMENTS))
        subprocess.run(["/usr/bin/swiftc", str(source), "-o", str(binary)], check=True, capture_output=True, timeout=180)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime",
                        "--entitlements", str(entitlements), str(app)], check=True, capture_output=True, timeout=20)
        # Publish a complete bundle without rewriting the executable of a live session.
        APP.parent.mkdir(parents=True, exist_ok=True)
        app.rename(APP)


def status():
    """Desktop session, app and this app's microphone permission, for diagnostics."""
    result = {"desktop_session": True, "app_installed": BINARY.is_file(), "microphone": "unknown"}
    try:
        check_desktop()
    except (OSError, RuntimeError):
        result["desktop_session"] = False
    if not (result["desktop_session"] and result["app_installed"]):
        return result
    with tempfile.TemporaryDirectory(prefix="hcv-audio-", dir="/tmp") as directory:
        answer = Path(directory) / "status"
        try:
            subprocess.run(["/usr/bin/open", "-g", "-j", "-n", str(APP), "--args", "--microphone-status", str(answer)],
                           check=True, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return result
        deadline = time.monotonic() + 15
        while not answer.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if answer.exists():
            result["microphone"] = PERMISSIONS.get(answer.read_text().strip(), "unknown")
    return result


def build_commit(helper):
    try:
        result = subprocess.run([str(helper), "--build-commit"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return result.stdout.strip() or None


def helpers(codex, codex_home):
    """Every installed Codex voice helper on this Mac with its exact build."""
    home = codex_home or str(Path.home() / ".codex")
    patterns = [os.path.join(home, "packages", "*", "releases", "*", *HELPER_TAIL)]
    if codex:
        real = Path(os.path.realpath(codex))
        if real.name == "codex.js":
            # The official npm package keeps native builds in platform packages,
            # nested in its own node_modules or hoisted beside it.
            package = real.parent.parent
            for parent in (package / "node_modules" / "@openai", package.parent):
                patterns.append(os.path.join(str(parent), "codex-darwin-*", "vendor", "*", *HELPER_TAIL))
        else:
            patterns.append(os.path.join(str(real.parent.parent), *HELPER_TAIL))
    found = {}
    for pattern in patterns:
        for path in glob.glob(pattern):
            path = os.path.realpath(path)
            if path not in found and os.path.isfile(path):
                commit = build_commit(path)
                if commit:
                    found[path] = commit
    return [{"path": path, "build_commit": commit} for path, commit in sorted(found.items())]


def send_signal(root, sig):
    try:
        _, child, group = map(int, (root / "pids").read_text().split())
        if child > 1:
            if group == child:
                os.killpg(group, sig)
            else:
                os.kill(child, sig)
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass


def pump(connection, source=0, sink=1):
    """Copy bytes both ways until the helper closes its end; returns when it does."""
    def upstream():
        try:
            while True:
                data = os.read(source, 65536)
                if not data:
                    break
                connection.sendall(data)
        except OSError:
            pass
        finally:
            # The helper sees EOF on its stdin, as if Codex had closed it.
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, daemon=True).start()
    while True:
        data = connection.recv(65536)
        if not data:
            return
        view = memoryview(data)
        while view:
            view = view[os.write(sink, view):]


def run(helper, commit, source=0, sink=1):
    """Serve one Codex voice session over stdin/stdout (SSH's channel); return the helper's exit status."""
    helper = Path(helper)
    if helper.parts[-len(HELPER_TAIL):] != HELPER_TAIL or not helper.is_file():
        raise RuntimeError(f"{helper} is not a Codex voice helper; rerun codex-voice setup on the work computer")
    if build_commit(helper) != commit:
        raise RuntimeError("Codex on this Mac no longer matches the work computer's build; "
                           "rerun codex-voice setup on the work computer")
    check_desktop()
    if not BINARY.is_file():
        raise RuntimeError("Codex Voice Audio is missing; rerun codex-voice setup on the work computer")
    env = {key: os.environ[key] for key in KEEP if key in os.environ}
    env.update(GSTREAMER, PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    with tempfile.TemporaryDirectory(prefix="hcv-audio-", dir="/tmp") as directory:
        root = Path(directory)
        address = str(root / "s")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(address)
        listener.listen(1)
        listener.settimeout(START_TIMEOUT)
        job = root / "job.json"
        job.write_text(json.dumps({"program": str(helper), "env": env, "socket": address, "owner": os.getpid()}))
        job.chmod(0o600)

        def interrupted(sig, frame):
            raise SystemExit(128 + sig)
        exit_signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGQUIT)
        for sig in exit_signals:
            signal.signal(sig, interrupted)
        try:
            subprocess.run(["/usr/bin/open", "-g", "-j", "-n", str(APP), "--args", str(job)],
                           check=True, capture_output=True, timeout=15)
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                detail = (root / "error").read_text() if (root / "error").exists() else ""
                raise RuntimeError("Codex Voice Audio did not start; check this Mac's logged-in desktop. "
                                   + detail) from None
            listener.close()
            with connection:
                connection.settimeout(None)
                pump(connection, source, sink)
            deadline = time.monotonic() + 10
            while not (root / "exit").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            if (root / "error").exists():
                raise RuntimeError((root / "error").read_text())
            return int((root / "exit").read_text()) if (root / "exit").exists() else 1
        finally:
            for sig in exit_signals:
                signal.signal(sig, signal.SIG_IGN)
            if not (root / "exit").exists():
                send_signal(root, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while (root / "pids").exists() and not (root / "exit").exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                if not (root / "exit").exists():
                    send_signal(root, signal.SIGKILL)


def main(args):
    if args == ["install"]:
        install()
    elif args == ["status"]:
        print("CODEX_VOICE_STATUS=" + json.dumps(status()))
    elif len(args) == 3 and args[0] == "helpers":
        print("CODEX_VOICE_HELPERS=" + json.dumps(helpers(args[1], args[2])))
    elif len(args) == 2 and args[0] == "check":
        info = {"helper_found": Path(args[1]).is_file(), "build_commit": build_commit(args[1]), **status()}
        print("CODEX_VOICE_STATUS=" + json.dumps(info))
    elif len(args) == 3 and args[0] == "run":
        return run(args[1], args[2])
    else:
        raise RuntimeError("Usage: macos_audio.py install | status | check HELPER | helpers CODEX CODEX_HOME "
                           "| run HELPER COMMIT")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr
        sys.exit("Codex Voice: " + (detail or str(error)))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit("Codex Voice: " + str(error))
