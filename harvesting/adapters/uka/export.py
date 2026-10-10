"""Repository compatibility import; implementation lives in retrieval._uka_export."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("retrieval._uka_export")
