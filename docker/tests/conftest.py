import importlib.util
import os
import sys
from pathlib import Path


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path or os.getcwd()))


# docker/ directory, for src and direct docker-root imports
REPO_ROOT = Path(__file__).resolve().parents[1]
# repository root: the AstrBot plugin itself lives here (plugin-market layout)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PACKAGE = "astrbot_plugin_arena_image"

_repo_root_norm = _norm_path(str(REPO_ROOT))
if not any(_norm_path(p) == _repo_root_norm for p in sys.path):
    sys.path.insert(0, str(REPO_ROOT))


def _register_plugin_package() -> None:
    """Expose the repository root as the ``astrbot_plugin_arena_image`` package.

    AstrBot installs this repository into ``data/plugins/astrbot_plugin_arena_image``
    and imports it by directory name, so ``main.py`` uses relative imports such as
    ``from .bridge_client import ...``.  Registering the repository root under that
    package name reproduces the runtime layout without putting the root on
    ``sys.path``, where the plugin's ``main.py`` would shadow the bridge's
    ``src.main``.
    """
    if PLUGIN_PACKAGE in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PLUGIN_PACKAGE,
        PROJECT_ROOT / "__init__.py",
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_PACKAGE] = module
    spec.loader.exec_module(module)


_register_plugin_package()
