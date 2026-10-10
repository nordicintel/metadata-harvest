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
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "retrieval/distribution.py" in names
        assert all(n.startswith("retrieval/") or ".dist-info/" in n for n in names)
    source = max((root / "dist").glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
    with tarfile.open(source) as archive:
        files = [n.split("/", 1)[1] for n in archive.getnames() if "/" in n]
        assert all(
            n.startswith("retrieval/")
            or n
            in {"retrieval", "pyproject.toml", "README.md", "PKG-INFO", ".gitignore"}
            for n in files
        )
    import tomllib

    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    providers = set(project["entry-points"]["nordicintel.retrieval"])
    code = r"""
import asyncio, importlib.metadata as m, importlib.util, json, sys
from retrieval import retrieve_distribution
entries = m.entry_points(group='nordicintel.retrieval')
for entry in entries:
    entry.load()
assert importlib.util.find_spec('apps') is None
assert importlib.util.find_spec('harvesting') is None
assert importlib.util.find_spec('metadata_harvest') is None
installed = {d.metadata['Name'].lower() for d in m.distributions()}
assert not installed.intersection({'pydantic', 'zstandard', 'httpx', 'playwright', 'jsonschema'})
class Response:
    status = 200
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def raise_for_status(self): pass
    async def json(self, **kwargs):
        return {'id':['x'], 'size':[1], 'dimension':{'x':{'category':{'index':['01']}}}, 'value':[0]}
class Session:
    def __init__(self): self.calls=[]
    def post(self, url, **kwargs):
        self.calls.append((url,kwargs)); return Response()
distribution = {
 'dcat:accessURL':['https://example.org/data'],
 'ni:retrieval':{'source':{'adapter':'pxweb_v2','source_code':'scb','dataset_code':'01','language':'sv'},
                 'dimensions':[{'code':'x','categories':['01']}], 'options':{'max_cells':1}},
 'ni:structure':{'qb:component':[{'qb:order':1,'qb:dimension':{'dct:identifier':'x','qb:codeList':{'codes':[{'skos:notation':'01'}]}}}]}}
session=Session()
result=asyncio.run(retrieve_distribution(session,distribution,selection={'x':['01']}))
assert result['value']==[0]
assert session.calls==[('https://example.org/data?lang=sv&outputFormat=json-stat2',{'json':{'selection':[{'variableCode':'x','valueCodes':['01']}]}})]
assert not any(k.startswith(('apps','harvesting','metadata_harvest','pydantic','zstandard','httpx','playwright','jsonschema')) for k in sys.modules)
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
        f"Verified retrieval-only wheel and fixture dispatch: {len(providers)} providers"
    )


if __name__ == "__main__":
    check(Path(__file__).resolve().parents[1])
