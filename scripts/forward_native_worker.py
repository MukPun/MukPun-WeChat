#!/usr/bin/env python3
"""Isolation/handshake entrypoint for the WeChat Native adapter.

The deployed role remains flat /opt. The checkout imports the same adapter
implementation from its package; no second receive loop is created.
"""
import importlib.util
from pathlib import Path
import sys

_adapter_path = Path(__file__).with_name('wechat_native.py')
if not _adapter_path.is_file():
    _adapter_path = Path(__file__).resolve().parents[1] / 'wechat_bridge/adapters/wechat/native.py'
_spec = importlib.util.spec_from_file_location('_wechat_native_adapter', _adapter_path)
_adapter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _adapter
_spec.loader.exec_module(_adapter)
globals().update({k:v for k,v in vars(_adapter).items() if not k.startswith('__')})
reader.CODE_FILES = delivery.role_files('native')


def send_image_command(config, own, command, deadline, media_lock):
    return _adapter.send_image_command(config, own, command, deadline, media_lock, emitter=emit)


def send_voice_command(config, own, command, deadline, media_lock):
    return _adapter.send_voice_command(config, own, command, deadline, media_lock, emitter=emit)


if __name__ == '__main__':
    raise SystemExit(main())
