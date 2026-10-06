import json
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import macos
import voice


class MacDesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / 'Codex Voice.app'
        self.app.mkdir()

    def test_request_uses_existing_terminal_and_owner_and_exit_code(self):
        captured = {}
        def opened(argv, **kwargs):
            job = Path(argv[-1])
            captured.update(json.loads(job.read_text()))
            captured['job'] = job
            self.assertEqual(job.stat().st_mode & 0o777, 0o600)
            self.assertEqual(job.parent.stat().st_mode & 0o777, 0o700)
            (job.parent / 'exit').write_text('7')
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(macos, 'APP', self.app), patch.object(macos, 'check_desktop'), \
             patch.object(macos.os, 'ttyname', return_value='/dev/ttys-test'), \
             patch.object(macos.signal, 'signal'), patch.object(macos.subprocess, 'run', side_effect=opened):
            args = ['--cd', "/work/repo ' with spaces", 'literal $(hostname)']
            self.assertEqual(macos.launch('/bin/codex', args), 7)
            self.assertEqual(captured['args'], args)
            self.assertEqual(captured['tty'], '/dev/ttys-test')
            self.assertEqual(captured['owner'], os.getpid())
            self.assertFalse(captured['job'].parent.exists())

    def test_launch_failure_does_not_signal_unrelated_processes(self):
        with patch.object(macos, 'APP', self.app), patch.object(macos, 'check_desktop'), \
             patch.object(macos.os, 'ttyname', return_value='/dev/ttys-test'), \
             patch.object(macos.signal, 'signal'), patch.object(macos.os, 'kill') as kill, \
             patch.object(macos.os, 'killpg') as killpg, \
             patch.object(macos.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['open'])):
            with self.assertRaises(subprocess.CalledProcessError):
                macos.launch('/bin/codex', [])
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_resize_and_shutdown_only_target_childs_own_group(self):
        with patch.object(macos.os, 'kill') as kill, patch.object(macos.os, 'killpg') as killpg:
            (self.root / 'pids').write_text('50 51 51')
            macos.send_signal(self.root, signal.SIGWINCH)
            killpg.assert_called_once_with(51, signal.SIGWINCH)
            (self.root / 'pids').write_text('50 51 1')
            macos.send_signal(self.root, signal.SIGTERM)
            kill.assert_called_once_with(51, signal.SIGTERM)
            (self.root / 'pids').write_text('50 1 1')
            macos.send_signal(self.root, signal.SIGKILL)
            self.assertEqual(killpg.call_count, 1)
            self.assertEqual(kill.call_count, 1)

    def test_setup_reuses_an_unchanged_desktop_binary(self):
        binary = self.app / 'Contents/MacOS/desktop-runner'
        binary.parent.mkdir(parents=True)
        binary.touch()
        with patch.object(macos, 'APP', self.app), patch.object(macos, 'check_desktop'), \
             patch.object(macos.subprocess, 'run') as run:
            macos.install()
            run.assert_not_called()

    def test_macos_ssh_routes_through_desktop_helper(self):
        mic = dict(host='my-mic', codex='/bin/codex', node_dir='', codex_home='',
                   platform='darwin', python='/python path/python3', desktop_helper='/app path/macos.py')
        command = voice.ssh_command(mic, '/tmp/backend.sock', ['--cd', '/project'])
        self.assertIn("'/python path/python3' '/app path/macos.py' /bin/codex --remote", command[-1])
        mic['platform'] = 'linux'
        self.assertNotIn('macos.py', voice.ssh_command(mic, '/tmp/backend.sock', [])[-1])

    def test_helper_upgrade_is_automatic_and_only_runs_once(self):
        (self.root / 'macos.py').write_text('helper source')
        mic = dict(host='my-mic', hostname='another-computer', codex='/bin/codex',
                   node_dir='', codex_home='', platform='darwin', python='/bin/python3',
                   desktop_helper='/state/macos.py', desktop_revision='old')
        path = self.root / 'config.json'
        path.write_text(json.dumps(mic))
        plugins = json.dumps({'result': {'plugins': [{'enabled': True, 'plugin_root': str(self.root)}]}})
        def deploy(data, root):
            data['desktop_revision'] = voice.desktop_revision(root)
        with patch.object(voice, 'config_dir', return_value=self.root), \
             patch.object(voice, 'run', return_value=plugins), \
             patch.object(voice.shutil, 'which', return_value='/bin/codex'), \
             patch.object(voice, 'probe', return_value=mic.copy()) as probe, \
             patch.object(voice, 'prepare_desktop', side_effect=deploy) as prepare, \
             patch.object(voice, 'backend', return_value='/tmp/backend.sock'), \
             patch.object(sys.stdin, 'isatty', return_value=True), \
             patch.object(sys.stdout, 'isatty', return_value=True), \
             patch.object(os, 'execvp', side_effect=SystemExit):
            for _ in range(2):
                with self.assertRaises(SystemExit):
                    voice.connect([])
            prepare.assert_called_once()
            probe.assert_called_once_with('my-mic')
        self.assertEqual(json.loads(path.read_text())['desktop_revision'], voice.desktop_revision(self.root))

    def test_different_work_computers_keep_their_own_helper_revision(self):
        mic = dict(host='my-mic', platform='darwin', python=sys.executable,
                   desktop_root=str(self.root / 'state'))
        execute = subprocess.run
        def ssh(argv, **kwargs):
            return execute(['sh', '-c', argv[-1]], **kwargs)
        paths = []
        for source in (b'# old\n', b'# new\r\n'):
            (self.root / 'macos.py').write_bytes(source)
            with patch.object(voice.subprocess, 'run', side_effect=ssh):
                voice.prepare_desktop(mic, self.root)
            paths.append(Path(mic['desktop_helper']))
            self.assertEqual(paths[-1].read_bytes(), source)
            self.assertEqual(mic['desktop_revision'], voice.desktop_revision(self.root))
            self.assertEqual(paths[-1].stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(*paths)
        self.assertEqual(paths[0].read_text(), '# old\n')

    def test_missing_python_or_helper_reports_repair_without_a_traceback(self):
        for python, helper in (('/not-installed/python3', __file__),
                               (sys.executable, '/not-installed/macos.py')):
            mic = dict(host='my-mic', codex='/bin/true', node_dir='', codex_home='',
                       platform='darwin', python=python, desktop_helper=helper)
            command = voice.ssh_command(mic, '/tmp/backend.sock', [])
            result = subprocess.run(['sh', '-c', command[-1]], text=True, capture_output=True)
            self.assertEqual(result.returncode, 127)
            self.assertIn('rerun codex-voice setup my-mic', result.stderr)
            self.assertNotIn('Traceback', result.stderr)


@unittest.skipUnless(sys.platform == 'darwin', 'requires macOS process notifications and Swift')
class MacProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        app = cls.root / 'Codex Voice.app'
        with patch.object(macos, 'ROOT', cls.root), patch.object(macos, 'APP', app):
            macos.install()
        cls.binary = app / 'Contents/MacOS/desktop-runner'

    def test_owner_death_before_and_after_start_leaves_no_child(self):
        for stage in ('before', 'after'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                master, slave = pty.openpty()
                self.addCleanup(os.close, master)
                self.addCleanup(os.close, slave)
                owner = subprocess.Popen(['/bin/sleep', '30'])
                app = None
                try:
                    job = root / 'job.json'
                    job.write_text(json.dumps({'program': '/bin/sleep', 'args': ['30'],
                                               'env': dict(os.environ), 'tty': os.ttyname(slave),
                                               'owner': owner.pid}))
                    if stage == 'before':
                        owner.kill()
                        owner.wait(timeout=5)
                    app = subprocess.Popen([str(self.binary), str(job)])
                    if stage == 'after':
                        deadline = time.monotonic() + 5
                        while not (root / 'pids').exists() and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue((root / 'pids').exists(), 'helper did not start')
                        child = int((root / 'pids').read_text().split()[1])
                        # Simulate losing the startup acknowledgement: only the
                        # Swift parent watch can clean up after an uncatchable kill.
                        (root / 'pids').unlink()
                        owner.kill()
                        owner.wait(timeout=5)
                    self.assertEqual(app.wait(timeout=5), 129)
                    if stage == 'after':
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            try:
                                os.kill(child, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(.01)
                        else:
                            self.fail('child survived its owner')
                    else:
                        self.assertFalse((root / 'pids').exists())
                finally:
                    if owner.poll() is None:
                        owner.kill()
                    owner.wait(timeout=5)
                    if app is not None:
                        if app.poll() is None:
                            app.kill()
                        app.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
