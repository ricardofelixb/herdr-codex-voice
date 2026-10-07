#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a verified Herdr community canary into a separate output directory."""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--target-dir', type=Path, help='Optional separate Cargo cache')
    parser.add_argument('--target', help='Rust target triple; omit for the native host')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--tests', action='store_true', help='Execute only the focused canary tests')
    parser.add_argument('--tests-only', action='store_true', help='Run focused tests without a release binary build')
    options = parser.parse_args()
    if options.jobs < 1:
        parser.error('--jobs must be positive')
    source = options.source.resolve()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = prepare.metadata()
    prepare.verify(source)
    build = manifest['build']
    env = dict(os.environ)
    env.update(RUSTUP_TOOLCHAIN=build['rust'], CARGO_BUILD_JOBS=str(options.jobs),
               CARGO_INCREMENTAL='0', CARGO_PROFILE_RELEASE_OPT_LEVEL=str(build['opt_level']),
               CARGO_PROFILE_RELEASE_LTO='false', CARGO_PROFILE_RELEASE_CODEGEN_UNITS=str(build['codegen_units']),
               CARGO_PROFILE_RELEASE_DEBUG='0', CARGO_PROFILE_RELEASE_STRIP='debuginfo',
               LIBGHOSTTY_VT_OPTIMIZE=build['ghostty_optimize'],
               HERDR_BUILD_CHANNEL=build['channel'], HERDR_BUILD_ID=build['id'],
               HERDR_BUILD_COMMIT=build['commit'])
    target_dir = (options.target_dir or output / 'target').resolve()
    env['CARGO_TARGET_DIR'] = str(target_dir)
    env.setdefault('ZIG_GLOBAL_CACHE_DIR', str(output / 'zig-cache'))
    env.setdefault('ZIG_LOCAL_CACHE_DIR', str(output / 'zig-local-cache'))
    rust = subprocess.check_output(['rustc', '--version'], env=env, text=True).strip()
    zig = subprocess.check_output([env.get('ZIG', 'zig'), 'version'], env=env, text=True).strip()
    if not rust.startswith('rustc ' + build['rust'] + ' ') or zig != build['zig']:
        raise ValueError('Install the pinned Rust and Zig versions first')
    rust_verbose = subprocess.check_output(['rustc', '-vV'], env=env, text=True)
    host = next(line.split(': ', 1)[1] for line in rust_verbose.splitlines() if line.startswith('host: '))
    target = options.target or host
    report = {'version': manifest['version'], 'target': target,
              'base_commit': manifest['upstream']['commit'],
              'patch_sha256': manifest['patch_sha256'], 'source_manifest_sha256': manifest['source_manifest_sha256'],
              'license_sha256': manifest['license_sha256'], 'rust': rust, 'zig': zig,
              'focused_tests': {}, 'artifact_executed': False}
    common = ['--locked', '--release', '-p', 'herdr', '--bin', 'herdr']
    if options.target:
        common += ['--target', options.target]
    if options.offline:
        common += ['--offline']
    if options.tests or options.tests_only:
        if target != host:
            raise ValueError('Run tests natively; this script does not install an emulator')
        tests = [('input_origin', 12), ('protocol::endpoint::tests', 10),
                 ('server::client_transport::tests::dedicated_client_shell_handshake_uses_surface_viewport', 1),
                 ('api::schema::tests::generated_protocol_schema_artifact_is_current', 1)]
        for test, expected in tests:
            process = subprocess.Popen(['cargo', 'test', *common, test], cwd=source, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            passed = 0
            for line in process.stdout:
                print(line, end='', flush=True)
                match = re.search(r'test result: ok\. (\d+) passed', line)
                if match:
                    passed += int(match.group(1))
            code = process.wait()
            report['focused_tests'][test] = {'exit': code, 'passed': passed, 'expected': expected}
            (output / 'focused-tests.json').write_text(json.dumps(report, indent=2) + '\n')
            if code != 0 or passed != expected:
                raise RuntimeError('Focused test failed or did not match the pinned test count: ' + test)
    if options.tests_only:
        print(json.dumps(report, indent=2))
        return
    subprocess.run(['cargo', 'build', *common], cwd=source, env=env, check=True)
    built_dir = target_dir / target if options.target else target_dir
    suffix = '.exe' if 'windows' in target else ''
    name = 'herdr-' + target + '-' + manifest['version'] + suffix
    artifact = output / name
    shutil.copy2(built_dir / 'release' / ('herdr' + suffix), artifact)
    report.update(artifact=name, binary_sha256=prepare.sha256(artifact), build_exit=0)
    (output / (name + '.json')).write_text(json.dumps(report, indent=2) + '\n')
    (output / (name + '.sha256')).write_text(report['binary_sha256'] + '  ' + name + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
