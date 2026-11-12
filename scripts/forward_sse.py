"""Compatibility export for the WeChat adapter SSE implementation."""
import importlib.util
from pathlib import Path
import sys

try:
    from wechat_bridge.adapters.wechat import sse as _adapter
except ModuleNotFoundError:
    _path = Path(__file__).with_name('wechat_sse.py')
    if not _path.is_file():
        _path = Path(__file__).resolve().parents[1] / 'wechat_bridge/adapters/wechat/sse.py'
    _spec = importlib.util.spec_from_file_location('_wechat_sse', _path)
    _adapter = sys.modules.get(_spec.name)
    if _adapter is None:
        _adapter = importlib.util.module_from_spec(_spec)
        sys.modules[_spec.name] = _adapter
        _spec.loader.exec_module(_adapter)
globals().update({k:v for k,v in vars(_adapter).items() if not k.startswith('__')})
