"""Back-compat shim: ``prompt_telemetry`` was renamed to ``telemetrify``.

Any ``import prompt_telemetry`` (or ``prompt_telemetry.X``) is silently
forwarded to ``telemetrify`` (or ``telemetrify.X``). This keeps old Stop
hooks, launchd plists, and external scripts working through the transition.

Update your imports — this shim will go away.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

_PREFIX = "prompt_telemetry"
_TARGET = "telemetrify"


class _ForwardLoader(importlib.abc.Loader):
    def create_module(self, spec):
        new_name = _TARGET + spec.name[len(_PREFIX):]
        return importlib.import_module(new_name)

    def exec_module(self, module):
        return None


class _ForwardFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != _PREFIX and not fullname.startswith(_PREFIX + "."):
            return None
        spec = importlib.util.spec_from_loader(fullname, _ForwardLoader())
        spec.submodule_search_locations = []
        return spec


if not any(isinstance(f, _ForwardFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _ForwardFinder())

warnings.warn(
    "prompt_telemetry has been renamed to telemetrify; update your imports.",
    DeprecationWarning,
    stacklevel=2,
)

sys.modules[__name__] = importlib.import_module(_TARGET)
