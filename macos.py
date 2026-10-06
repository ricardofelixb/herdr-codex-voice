"""Launch Codex from a background Mac app, using the caller's existing SSH tty."""

import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path.home() / ".local/share/herdr-codex-voice"
SWIFT = r'''import Foundation
import Darwin

struct Request: Decodable {
    let program: String
    let args: [String]
    let env: [String: String]
    let tty: String
    let owner: Int32
}
let job = URL(fileURLWithPath: CommandLine.arguments[1])
let directory = job.deletingLastPathComponent()
func write(_ name: String, _ text: String) throws {
    try text.write(to: directory.appendingPathComponent(name), atomically: true, encoding: .utf8)
}
func stop(_ pid: Int32) {
    if pid > 1 { kill(getpgid(pid) == pid ? -pid : pid, SIGKILL) }
}
do {
    let request = try JSONDecoder().decode(Request.self, from: Data(contentsOf: job))
    guard request.owner > 1 && kill(request.owner, 0) == 0 else { exit(129) }
    let terminal = try FileHandle(forUpdating: URL(fileURLWithPath: request.tty))
    let child = Process()
    child.executableURL = URL(fileURLWithPath: request.program)
    child.arguments = request.args
    child.environment = request.env
    child.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
    child.standardInput = terminal
    child.standardOutput = terminal
    child.standardError = terminal
    try child.run()
    let pid = child.processIdentifier
    defer { if child.isRunning { stop(pid) } }
    // The SSH launcher can disappear before it receives our pid, even via SIGKILL.
    let owner = DispatchSource.makeProcessSource(identifier: request.owner, eventMask: .exit, queue: .global())
    owner.setEventHandler { stop(pid); exit(129) }
    owner.resume()
    defer { owner.cancel() }
    guard kill(request.owner, 0) == 0 else { stop(pid); exit(129) }
    try write("pids", "\(getpid()) \(child.processIdentifier) \(getpgid(child.processIdentifier))")
    child.waitUntilExit()
    let code = child.terminationReason == .uncaughtSignal ? 128 + child.terminationStatus : child.terminationStatus
    try write("exit", "\(code)")
} catch {
    try? write("error", String(describing: error))
    try? write("exit", "1")
}
'''
INFO = {"CFBundleIdentifier": "dev.herdr.codex-voice", "CFBundleName": "Codex Voice",
        "CFBundleExecutable": "desktop-runner", "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1", "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Use your microphone for native Codex voice in your remote terminal."}
ENTITLEMENTS = {"com.apple.security.device.audio-input": True}
DIGEST = hashlib.sha256(SWIFT.encode() + plistlib.dumps(INFO) + plistlib.dumps(ENTITLEMENTS)).hexdigest()
APP = ROOT / "apps" / DIGEST / "Codex Voice.app"


def check_desktop():
    if Path("/dev/console").stat().st_uid != os.getuid():
        raise RuntimeError("Log into the microphone Mac's desktop as the SSH user first")


def install():
    if (APP / "Contents/MacOS/desktop-runner").is_file():
        return
    if subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode:
        raise RuntimeError("Install Apple's Command Line Tools on the microphone Mac: xcode-select --install")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        app = Path(directory) / "Codex Voice.app"
        binary = app / "Contents/MacOS/desktop-runner"
        binary.parent.mkdir(parents=True)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(INFO))
        source = Path(directory) / "main.swift"
        source.write_text(SWIFT)
        entitlements = Path(directory) / "entitlements.plist"
        entitlements.write_bytes(plistlib.dumps(ENTITLEMENTS))
        subprocess.run(["/usr/bin/swiftc", str(source), "-o", str(binary)], check=True, capture_output=True, timeout=120)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime",
                        "--entitlements", str(entitlements), str(app)], check=True, capture_output=True, timeout=20)
        # Publish a complete bundle without rewriting the executable of a live session.
        APP.parent.mkdir(parents=True, exist_ok=True)
        app.rename(APP)


def send_signal(root, sig):
    try:
        app, child, group = map(int, (root / "pids").read_text().split())
        if child > 1:
            if group == child:
                os.killpg(group, sig)
            else:
                os.kill(child, sig)
    except (FileNotFoundError, ProcessLookupError):
        pass


def launch(program, args):
    check_desktop()
    if not APP.exists():
        raise RuntimeError("Desktop helper is missing; rerun codex-voice setup on the work computer")
    with tempfile.TemporaryDirectory(prefix="hcv-gui-", dir="/tmp") as directory:
        root = Path(directory)
        job = root / "job.json"
        job.write_text(json.dumps({"program": program, "args": args, "env": dict(os.environ),
                                   "tty": os.ttyname(0), "owner": os.getpid()}))
        job.chmod(0o600)
        def interrupted(sig, frame):
            raise SystemExit(128 + sig)
        exit_signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGQUIT)
        for sig in exit_signals:
            signal.signal(sig, interrupted)
        signal.signal(signal.SIGWINCH, lambda sig, frame: send_signal(root, sig))
        try:
            subprocess.run(["/usr/bin/open", "-g", "-j", "-n", str(APP), "--args", str(job)],
                           check=True, capture_output=True, timeout=15)
            deadline = time.monotonic() + 20
            started = False
            while not (root / "exit").exists():
                if (root / "pids").exists():
                    if not started:
                        send_signal(root, signal.SIGWINCH)
                        started = True
                    app = int((root / "pids").read_text().split()[0])
                    try:
                        os.kill(app, 0)
                    except ProcessLookupError:
                        if not (root / "exit").exists():
                            raise RuntimeError("Desktop helper exited without a status")
                elif time.monotonic() >= deadline:
                    raise RuntimeError("Desktop helper did not start; check the Mac's logged-in desktop")
                time.sleep(.1)
            if (root / "error").exists():
                raise RuntimeError((root / "error").read_text())
            return int((root / "exit").read_text())
        finally:
            for sig in (*exit_signals, signal.SIGWINCH):
                signal.signal(sig, signal.SIG_IGN)
            if not (root / "exit").exists():
                send_signal(root, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while (root / "pids").exists() and not (root / "exit").exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                if not (root / "exit").exists():
                    send_signal(root, signal.SIGKILL)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["install"]:
            install()
        else:
            sys.exit(launch(sys.argv[1], sys.argv[2:]))
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr
        sys.exit("Codex Voice: " + (detail or str(error)))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit("Codex Voice: " + str(error))
