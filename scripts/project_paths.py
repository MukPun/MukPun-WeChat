"""Paths derived from the Git checkout; instance data is never tracked."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / '.runtime'
VERIFICATION = RUNTIME_ROOT / 'verification'
CONFIG = RUNTIME_ROOT / 'forwarder.json'
NATIVE_ASSET = RUNTIME_ROOT / 'assets' / 'wx-v1.2.0-voice-format-linux-x64-gnu'
ASTRBOT_DATA = PROJECT_ROOT.parent / 'AstrBot' / 'data'
