"""Scoped Git publication shared by adapter and catalog collectors."""

import subprocess
from pathlib import Path

from apps.publication.artifacts import check_collection_sizes, publication_directory


def publish(message, *, data=Path("data/pxweb_v1"), verify=None):
    """Commit one allowed output directory; replay unrelated pushes without force."""

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], check=check, text=True, capture_output=True
        )

    root = Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    relative = publication_directory(root, Path(data))
    check_collection_sizes(data)
    staged = git("diff", "--cached", "--name-only", "-z").stdout.split("\0")
    if any(path and not path.startswith(relative + "/") for path in staged):
        raise RuntimeError("Refusing to publish unrelated staged files")
    git("add", "--", relative)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    if verify is not None:
        verify()
    git("commit", "-m", message)
    for attempt in range(3):
        if verify is not None:
            verify()
        result = git("push", "origin", "HEAD:main", check=False)
        if result.returncode == 0:
            return
        if attempt < 2:
            git("fetch", "origin", "main")
            if git("rebase", "origin/main", check=False).returncode:
                git("rebase", "--abort", check=False)
                raise RuntimeError(
                    "Publishing conflicted with a newer change; no force push was attempted"
                )
    raise RuntimeError(f"Could not publish harvest results: {result.stderr}")
