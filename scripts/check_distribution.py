"""Verify built archives and a plain wheel installation in an isolated environment."""

import json
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


def check(root: Path):
    wheel = max((root / "dist").glob("*.whl"), key=lambda p: p.stat().st_mtime)
    source = max((root / "dist").glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
    expected = {
        "dataset-metadata.schema.json",
        "provider.schema.json",
        "retrieval-result.schema.json",
        "LICENSE",
    }
    with zipfile.ZipFile(wheel) as archive:
        assert expected <= {
            Path(n).name for n in archive.namelist() if "/schemas/" in n
        }
    with tarfile.open(source) as archive:
        assert expected <= {
            Path(n).name for n in archive.getnames() if "/schemas/" in n
        }
    providers = set()
    for config in (
        "providers.json",
        "providers.pxweb_v2.json",
        "providers.kolada.json",
    ):
        providers.update(
            p["code"] for p in json.loads((root / config).read_text(encoding="utf-8"))
        )
    code = """
import importlib.metadata as m, importlib.resources as r, json, sys
entries = m.entry_points(group='nordicintel.retrieval')
for entry in entries:
    entry.load()
assert not any(k.startswith(('httpx', 'playwright', 'jsonschema')) for k in sys.modules)
installed = {d.metadata['Name'].lower() for d in m.distributions()}
assert not installed.intersection({'httpx', 'playwright', 'jsonschema'})
assert len(list(r.files('metadata_harvest').joinpath('schemas').iterdir())) >= 4
print(json.dumps(sorted(e.name for e in entries)))
"""
    with tempfile.TemporaryDirectory(prefix="metadata-wheel-") as temporary:
        env = Path(temporary)
        venv.EnvBuilder(with_pip=True).create(env)
        python = env / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        result = subprocess.run(
            [str(python), "-I", "-c", code],
            check=True,
            capture_output=True,
            text=True,
            cwd=env,
        )
        assert set(json.loads(result.stdout)) == providers
    print(
        f"Verified wheel + sdist schemas and plain retrieval installation: {len(providers)} providers"
    )


if __name__ == "__main__":
    check(Path(__file__).resolve().parents[1])
