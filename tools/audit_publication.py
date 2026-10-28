#!/usr/bin/env python3
"""Audit a source-only publication tree without printing possible secret values."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.test-tmp', '.pytest_cache', '.publish', '.runtime'}
PRIVATE_NAMES = {'forwarder.json', '.env', 'id_rsa', 'id_ed25519', 'id_ecdsa',
                 'api-token', 'storage-key', 'cursor-key', 'profile-keys', 'known_hosts', 'authorized_keys'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.ppk', '.sqlite', '.sqlite3', '.db', '.log', '.zip'}
RULES = {
    'private_key': re.compile(r'-----BEGIN (?:OPENSSH |RSA |DSA |EC |ENCRYPTED )?PRIVATE KEY-----'),
    'openssh_blob': re.compile(r'\bb3BlbnNzaC1rZXktdjE[A-Za-z0-9+/=]{20,}'),
    'github_token': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}'),
    'openai_style_key': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b'),
    'aws_access_key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'slack_token': re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}'),
    'credential_url': re.compile(r'https?://[^\s/:@]+:[^\s/@]+@'),
    'password_shape': re.compile(r'\b[A-Za-z]{2,20}\d{6,16}[!@#$%&]'),
}
ASSIGNMENT = re.compile(r'''\b(?:api_?key|access_?token|ws_reverse_token|password|passwd|passphrase|client_secret|private_key|token)\b['"]?\s*[:=]\s*['"]([^'"\r\n]{8,})['"]''', re.I)
FIXTURE_PREFIXES = ('synthetic', 'fixture', 'example', 'dummy', 'fake', 'test', 'not-', 'do-not-', 'private-',
                    'placeholder', 'redacted', 'secret-for-', 'a-test-', 'a-token-', 'token-for-', 'token-not-')
PNG_SHA = '264a52467f2becaec18c7373782130a967d33651ee981f2bcd7376f879fb37e2'


def audit(root):
    findings, files = [], []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            findings.append({'file': relative.as_posix(), 'rule': 'symlink'})
            continue
        if not path.is_file():
            continue
        name = relative.as_posix()
        raw = path.read_bytes()
        if path.name in PRIVATE_NAMES or path.suffix.lower() in PRIVATE_SUFFIXES or path.name.startswith('.env.'):
            findings.append({'file': name, 'rule': 'private_filename'})
            continue
        if path.suffix.lower() == '.png':
            if name != 'acceptance/image-output-test.png' or hashlib.sha256(raw).hexdigest() != PNG_SHA:
                findings.append({'file': name, 'rule': 'unreviewed_binary'})
                continue
        else:
            try:
                text = raw.decode('utf-8')
            except UnicodeError:
                findings.append({'file': name, 'rule': 'unreviewed_binary'})
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for rule, pattern in RULES.items():
                    if pattern.search(line):
                        findings.append({'file': name, 'line': number, 'rule': rule})
                for match in ASSIGNMENT.finditer(line):
                    value = match[1].lower()
                    if (value.startswith(FIXTURE_PREFIXES) or len(set(value)) <= 2 or
                            any(part in value for part in ('<', '${', 'your_', 'your-', 'invalid', 'mock'))):
                        continue
                    findings.append({'file': name, 'line': number, 'rule': 'credential_literal_review'})
        files.append({'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                      'git_blob_sha': hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()})
    return findings, files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', nargs='?', default='.')
    parser.add_argument('--manifest')
    args = parser.parse_args()
    findings, files = audit(Path(args.root).resolve(strict=True))
    if not findings and args.manifest:
        Path(args.manifest).write_text(json.dumps(files, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'ok': not findings, 'files': len(files), 'bytes': sum(f['bytes'] for f in files),
                      'findings': findings}, ensure_ascii=False))
    return int(bool(findings))


if __name__ == '__main__':
    sys.exit(main())
