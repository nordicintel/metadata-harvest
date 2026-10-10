"""Repository compatibility import; implementation lives in retrieval.adapters.riksbank."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("retrieval.adapters.riksbank")
