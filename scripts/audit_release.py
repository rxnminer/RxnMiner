#!/usr/bin/env python3
"""Check the submission payload without printing any matched secret values."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', 'node_modules', '.venv', '__pycache__', '.pytest_cache', '.astro', 'dist',
        'out', 'output', '.local', '.cache'}
PATTERNS = {
    'private deployment path': re.compile(r'/uniparser2/|/Users/[A-Za-z][\w.-]+/|[A-Z]:\\Users\\\d+\\'),
    'internal host': re.compile(r'\b(?:10\.\d{1,3}|100\.68|221\.194)\.\d{1,3}\.\d{1,3}\b'),
    'credential-shaped literal': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AIza[A-Za-z0-9_-]{25,})'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}


def payload():
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for name in names:
            if name.endswith(('.pyc', '.log', '.sqlite', '.sqlite3')):
                continue
            yield Path(folder) / name


def main():
    failures = []
    files = list(payload())
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if path.name.startswith('._') or path.name.endswith('.private.json') or path.name.startswith('HANDOFF'):
            failures.append(f'{rel}: private or platform metadata')
        if path.name.startswith('.env') and path.name != '.env.example':
            failures.append(f'{rel}: environment file')
        if path.stat().st_size >= 100 * 1024 * 1024:
            failures.append(f'{rel}: exceeds GitHub file limit')
        content = path.read_bytes()
        if content.startswith(b'version https://git-lfs.github.com/spec/v1'):
            failures.append(f'{rel}: unresolved Git LFS pointer')
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if path == Path(__file__).resolve():
            continue
        for label, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match:
                failures.append(f'{rel}:{text.count(chr(10), 0, match.start()) + 1}: {label}')
    for line in (ROOT / '.env.example').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            if any(word in key for word in ('KEY', 'TOKEN', 'SECRET')) and value.strip():
                failures.append(f'.env.example: {key} must be empty')
    media = json.loads((ROOT / 'website/demo-media.json').read_text())
    for entry in media['assets']:
        asset = ROOT / 'website/public/media' / entry['file']
        if not asset.is_file() or hashlib.sha256(asset.read_bytes()).hexdigest() != entry['sha256']:
            failures.append(f'media digest mismatch: {entry["file"]}')
    for script in ('scripts/verify_benchmark.py',):
        if subprocess.run([sys.executable, str(ROOT / script)], cwd=ROOT).returncode:
            failures.append(f'{script}: verification failed')
    if failures:
        print('\n'.join(failures))
        return 1
    print(f'Release audit passed: {len(files)} files; media, benchmark, adapters and vendored manifest verified.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
