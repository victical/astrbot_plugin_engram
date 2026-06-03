import pathlib
import sys
import importlib.util


ROOT = pathlib.Path(__file__).resolve().parents[1]
PARENT = ROOT.parent
for path in (str(PARENT), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

if "astrbot_plugin_engram" not in sys.modules:
    init_file = ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_engram",
        init_file,
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["astrbot_plugin_engram"] = module
    if spec.loader is not None:
        spec.loader.exec_module(module)
