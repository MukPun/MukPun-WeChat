#!/usr/bin/env python3
"""Disposable offline Native proof: 128MiB volume, >32MiB save/restore."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import time


def main():
    assert os.geteuid()==0
    root=Path(__file__).resolve().parent
    stage=root/'context-staging/verification/scripts'
    def load(name):
        s=importlib.util.spec_from_file_location('_storage_proof_'+name,stage/(name+'.py'))
        m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
    module,owner=load('session_runtime_host'),load('session_runtime')
    module.legacy.ASSET=root/'assets/wx-v1.0.0-beta.8-linux-x64-gnu'
    owner.private_directory(owner.STATE/'selftests')
    path=owner.STATE/'selftests'/('large-storage-'+secrets.token_hex(6))
    owner.private_directory(path)
    baseline={k:module.owned.identity('/proc/self/ns/'+k) for k in ('net','user')}
    network,business=module.tap.read_network(),module.tap.business_snapshot()
    production=module.tap.details('astrbot-wechat-session-owner.service').get('MainPID')
    host=None
    try:
        host=module.SessionHost(path,offline=True,seconds=120)
        host.unit='astrbot-wechat-login-storage-proof-'+secrets.token_hex(6)+'.service'
        host.start()
        ready=owner.wait_ready(host,maximum=45)
        assert ready['accounts']==0 and not ready['restored']
        before=host.fingerprints()
        fs=os.fstatvfs(host.data_fd);assert fs.f_blocks*fs.f_frsize==128*1024*1024
        fd=os.open('storage-proof.bin',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=host.data_fd)
        try:
            os.fchown(fd,host.client_uid,host.client_uid)
            block=b'\0'*65536
            for _ in range(640):os.write(fd,block)
            os.fsync(fd)
        finally:os.close(fd)
        checkpoint=host.checkpoint()
        assert checkpoint.bytes>40*1024*1024
        closed=host.close();host=None
        assert owner.cleanup_ok(closed)
        host=module.SessionHost(path,offline=True,seconds=120)
        host.unit='astrbot-wechat-login-storage-proof-'+secrets.token_hex(6)+'.service'
        host.start()
        ready=owner.wait_ready(host,maximum=45)
        assert ready['accounts']==0 and ready['restored'] is True
        after=host.fingerprints()
        assert all(before[k]==after[k] for k in ('api_token','storage_key','cursor_key'))
        fd=os.open('storage-proof.bin',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=host.data_fd)
        try:
            assert os.fstat(fd).st_size==40*1024*1024
            h=hashlib.sha256()
            while True:
                raw=os.read(fd,65536)
                if not raw:break
                h.update(raw)
            expected=hashlib.sha256()
            for _ in range(640):expected.update(b'\0'*65536)
            assert h.digest()==expected.digest()
        finally:os.close(fd)
        closed=host.close();host=None
        assert owner.cleanup_ok(closed)
        flags,failures=module.tap.compare_baselines(baseline,network,business)
        assert not failures and module.tap.details('astrbot-wechat-session-owner.service').get('MainPID')==production
        print(json.dumps({'stage':'large_storage_proof','ok':True,'storage_bytes':128*1024*1024,
            'restored_test_bytes':40*1024*1024,'no_login':True,'no_qr':True,
            'production_owner_unchanged':True,**flags}))
    finally:
        if host is not None:host.close()
        owner.remove_test_state(path)


if __name__=='__main__':main()
