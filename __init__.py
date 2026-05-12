"""HermesA2A v3 plugin — auto-discovered and loaded by Hermes Agent."""

try:
    from .src.plugin import HermesA2AV3Plugin, __version__
except ImportError:
    # Fallback for pytest rootdir = tests (plugin root is on path but not a package)
    import sys
    from pathlib import Path
    _src = Path(__file__).parent / "src"
    if _src.exists() and str(_src) not in sys.path:
        sys.path.insert(0, str(Path(__file__).parent))
    from src.plugin import HermesA2AV3Plugin, __version__

__all__ = ["HermesA2AV3Plugin", "__version__"]
