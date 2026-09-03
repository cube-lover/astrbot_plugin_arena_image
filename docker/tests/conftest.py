import os
import sys
from pathlib import Path


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path or os.getcwd()))


# docker/ directory, for src and direct docker-root imports
REPO_ROOT = Path(__file__).resolve().parents[1]
# repository root, for astrbot_plugin_arena_image
PROJECT_ROOT = Path(__file__).resolve().parents[2]

for root in (REPO_ROOT, PROJECT_ROOT):
    root_norm = _norm_path(str(root))
    if not any(_norm_path(p) == root_norm for p in sys.path):
        sys.path.insert(0, str(root))
