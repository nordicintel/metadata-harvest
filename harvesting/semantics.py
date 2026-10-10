"""Repository compatibility import; implementation lives in retrieval._semantics."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("retrieval._semantics")
