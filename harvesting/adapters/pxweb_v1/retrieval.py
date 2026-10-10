"""Repository compatibility import; implementation lives in retrieval.adapters.pxweb_v1."""

import sys
from importlib import import_module

sys.modules[__name__] = import_module("retrieval.adapters.pxweb_v1")
