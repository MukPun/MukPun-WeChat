#!/usr/bin/env python3
"""Build/check an ignored worker-code view while retaining private venv/data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

ROOT=Path(__file__).resolve().parents[1]


def public_files():
    result={}
    for directory in ('scripts','wechat_bridge','tests','tools'):
        for path in sorted((ROOT/directory).glob('*.py')):
            if path.is_symlink():raise ValueError('source_symlink')
            result[path.relative_to(ROOT).as_posix()]=path
    result['run_onebot_tests.py']=ROOT/'scripts/run_onebot_tests.py'
    return result


def sync(apply=False):
    runtime=ROOT/'.runtime'
    target=runtime/'verification'
    manifest={}
    mismatch=[]
    if apply:
        runtime.mkdir(exist_ok=True,mode=0o700)
        target.mkdir(exist_ok=True,mode=0o755)
    if runtime.is_symlink() or target.is_symlink():raise ValueError('runtime_symlink')
    for relative,source in public_files().items():
        raw=source.read_bytes()
        manifest[relative]=hashlib.sha256(raw).hexdigest()
        destination=target/relative
        if destination.is_symlink():raise ValueError('runtime_symlink')
        if not destination.exists() or destination.read_bytes()!=raw:
            mismatch.append(relative)
            if apply:
                destination.parent.mkdir(exist_ok=True,mode=0o755)
                info=destination.stat() if destination.exists() else None
                temporary=destination.with_name('.'+destination.name+'.sync-next')
                fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
                try:
                    with os.fdopen(fd,'wb',closefd=False) as stream:
                        stream.write(raw);stream.flush();os.fsync(fd)
                    if info and os.name=='posix':
                        os.fchmod(fd,stat.S_IMODE(info.st_mode));os.fchown(fd,info.st_uid,info.st_gid)
                    os.close(fd);fd=None
                    os.replace(temporary,destination)
                finally:
                    if fd is not None:os.close(fd)
                    temporary.unlink(missing_ok=True)
    if apply:
        (runtime/'worker-manifest.json').write_text(json.dumps(manifest,sort_keys=True)+'\n',encoding='utf-8')
    return {'stage':'runtime_code_synced' if apply else 'runtime_code_checked','ok':apply or not mismatch,
            'files':len(manifest),'changed':len(mismatch)}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    arguments=parser.parse_args()
    result=sync(arguments.apply);print(json.dumps(result));raise SystemExit(0 if result['ok'] else 1)
