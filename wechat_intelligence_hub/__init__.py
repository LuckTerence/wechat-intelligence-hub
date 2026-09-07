"""WeChat Intelligence Hub Engine Wrapper."""
import sys
from pathlib import Path

_proj_dir = Path(__file__).resolve().parent.parent / "projects" / "wechat-intelligence-hub"
if str(_proj_dir) not in sys.path:
    sys.path.insert(0, str(_proj_dir))

try:
    from engine import whitelist
except ImportError:
    pass
