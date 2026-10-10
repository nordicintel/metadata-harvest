"""Refresh oppnadata through the validated portal command."""

import sys

from apps.operations.portal import main

if __name__ == "__main__":
    main(["refresh", *sys.argv[1:]])
