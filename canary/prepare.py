#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Verify the pinned Herdr source and optionally apply the community patch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

PACKAGE = Path(__file__).resolve().parent


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def metadata():
    manifest = json.loads((PACKAGE / 'manifest.json').read_text())
    for key in ('patch', 'base_manifest', 'source_manifest'):
        path = PACKAGE / manifest[key]
        if sha256(path) != manifest[key + '_sha256']:
            raise ValueError('Package checksum mismatch: ' + path.name)
    if sha256(PACKAGE / 'LICENSE.herdr') != manifest['license_sha256']:
        raise ValueError('Upstream license checksum mismatch')
    return manifest


def verify(source, phase='source'):
    manifest = metadata()
    entries = json.loads((PACKAGE / manifest[phase + '_manifest']).read_text())
    for entry in entries:
        relative = Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid source-manifest path')
        path = source / relative
        if entry['kind'] == 'symlink':
            # Git for Windows can check out a symlink as a file containing its target.
            data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            actual = hashlib.sha256(data).hexdigest()
        else:
            if path.is_symlink():
                raise ValueError('Unexpected source symlink: ' + entry['path'])
            actual = sha256(path)
        if actual != entry['sha256']:
            raise ValueError('Source checksum mismatch: ' + entry['path'])
    return len(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Apply only to the exact pinned base')
    options = parser.parse_args()
    source = options.source.resolve()
    manifest = metadata()
    if options.apply:
        if (source / '.git').exists():
            head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            if head != manifest['upstream']['commit']:
                raise ValueError('Checkout HEAD is not the pinned upstream commit')
            status = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True)
            if status.strip():
                raise ValueError('Use a separate clean checkout; this command never resets or cleans files')
        verify(source, 'base')
        patch = str(PACKAGE / manifest['patch'])
        subprocess.run(['git', 'apply', '--check', patch], cwd=source, check=True)
        subprocess.run(['git', 'apply', patch], cwd=source, check=True)
    count = verify(source)
    print(json.dumps({'version': manifest['version'], 'verified_files': count,
                      'patch_sha256': manifest['patch_sha256'],
                      'source_manifest_sha256': manifest['source_manifest_sha256']}, indent=2))


if __name__ == '__main__':
    main()
