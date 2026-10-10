"""Incremental offline assembly of the NordicIntel StatDCAT catalog."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote

from metadata_harvest.accepted import model_contributions
from metadata_harvest.artifacts import check_collection_sizes, manifest
from metadata_harvest.models import PROFILE_VERSION, CodeList, Dataset
from metadata_harvest.portal import (
    PortalIndex,
    accepted_records,
    enrich,
    publication_view,
)
from metadata_harvest.quality import validate_publication
from metadata_harvest.registry import ADAPTERS
from metadata_harvest.sources import inventory
from metadata_harvest.statdcat import assemble, convert_codelist, merge_codelists


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def configuration(data: Path) -> tuple[dict, dict]:
    path = data.parent / "catalog.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if type(config.get("include_scb_pxweb_v1", False)) is not bool:
        raise ValueError("include_scb_pxweb_v1 must be boolean")
    config.setdefault("include_scb_pxweb_v1", False)
    overrides = data.parent / "catalog-matches.json"
    matches = (
        json.loads(overrides.read_text(encoding="utf-8")) if overrides.exists() else {}
    )
    return config, matches


def dependencies(data: Path) -> dict:
    result = {}
    for name in ("catalog.toml", "catalog-matches.json"):
        path = data.parent / name
        result["config:" + name] = _hash(path) if path.exists() else None
    for name in ("oppnadata.jsonl.gz", "state.json"):
        path = data / "data_eu" / name
        result["portal:" + name] = _hash(path) if path.exists() else None
    # Actual implementation/model changes invalidate generated data.
    for name in (
        "models.py",
        "statdcat.py",
        "portal.py",
        "sources.py",
        "source-inventory.json",
        "profile.json",
        "accepted.py",
        "quality.py",
        "catalog_assembly.py",
    ):
        result["code:" + name] = _hash(Path(__file__).with_name(name))
    return result


def _blob(path: Path):
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path.parent,
        capture_output=True,
        text=True,
    )
    if root.returncode:
        return None
    relative = (
        path.resolve().relative_to(Path(root.stdout.strip()).resolve()).as_posix()
    )
    result = subprocess.run(
        ["git", "rev-parse", f"HEAD:{relative}"],
        cwd=path.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return None
    oid = result.stdout.strip()
    actual = subprocess.run(
        ["git", "hash-object", str(path.resolve())],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return oid if actual == oid else None


def _accepted_blob(data: Path, state: dict) -> bytes:
    oid = state.get("blob")
    if (
        not isinstance(oid, str)
        or len(oid) not in (40, 64)
        or any(c not in "0123456789abcdef" for c in oid)
    ):
        raise ValueError("No committed accepted Git blob is available")
    result = subprocess.run(
        ["git", "cat-file", "blob", oid], cwd=data, capture_output=True, check=True
    )
    if hashlib.sha256(result.stdout).hexdigest() != state["sha256"]:
        raise ValueError("Accepted Git blob checksum mismatch")
    return result.stdout


def _rows(raw: bytes):
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        for number, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                raise ValueError(f"Truncated accepted line {number}")
            yield json.loads(line)


def _group(name: str) -> str:
    source = unquote(Path(name).name.split("__", 1)[0])
    for item in inventory():
        if item["adapter"] == name.split("/")[0] and item["source_code"] == source:
            return item["owner"]["code"] if item["owner"] else source
    return source


def build(data: Path, output: Path, *, adapters=None) -> dict:
    from metadata_harvest.catalog import (
        ASSETS,
        _compress,
        _extract,
        inputs,
        read,
        write,
    )

    data, output = Path(data), Path(output)
    if (
        data.resolve() == output.resolve()
        or data.resolve() in output.resolve().parents
        or output.resolve() in data.resolve().parents
    ):
        raise ValueError("Candidate must be separate from source data")
    adapters = sorted(
        adapters
        if adapters is not None
        else [k for k, v in ADAPTERS.items() if v.available]
    )
    if any(a not in ADAPTERS or not ADAPTERS[a].available for a in adapters):
        raise ValueError("Unknown or unavailable catalog adapter")
    baseline = inputs(data, adapters)
    extra = dependencies(data)
    config, overrides = configuration(data)
    rule_dependencies = {k: v for k, v in extra.items() if not k.startswith("portal:")}
    catalog = data / "catalog"
    catalog_baseline = manifest(catalog)
    previous = read(catalog / "state/current.json", {})
    for adapter in adapters:
        if not any(n.startswith(adapter + "/metadata/") for n in baseline) and not any(
            n.startswith(adapter + "/metadata/") for n in previous.get("accepted", {})
        ):
            raise ValueError(f"No accepted metadata coverage for adapter: {adapter}")
    if previous.get("profile_version") != PROFILE_VERSION:
        previous = {}
    for asset, digest in previous.get("assets", {}).items():
        if (
            asset not in ASSETS
            or not (catalog / asset).exists()
            or _hash(catalog / asset) != digest
        ):
            raise ValueError("Missing or corrupt previous catalog asset")
    output.mkdir(parents=True, exist_ok=True)
    old_candidate = read(output / "result.json", {})
    for name in old_candidate.get("candidate", {}):
        path = (output / name).resolve()
        if not path.is_relative_to(output.resolve()):
            raise ValueError("Unsafe candidate path")
        path.unlink(missing_ok=True)
    (output / "result.json").unlink(missing_ok=True)
    source_names = {n for n in baseline if "/metadata/" in n or "/codelists/" in n}
    source_names.update(previous.get("accepted", {}))
    groups = {}
    for name in sorted(source_names):
        groups.setdefault(_group(name), []).append(name)
    portal_path = data / "data_eu/oppnadata.jsonl.gz"
    index = PortalIndex(
        accepted_records(portal_path.parent) if portal_path.exists() else [],
        overrides=overrides,
    )
    accepted = {}
    states = {}
    errors = []
    counts = {
        "datasets": 0,
        "distributions": 0,
        "codelists": 0,
        "matched": 0,
        "unmatched": 0,
        "ambiguous": 0,
        "filtered": 0,
        "quality_findings": 0,
    }
    build_counts = {"rebuilt_groups": 0, "reused_groups": 0, "retained_groups": 0}
    with TemporaryDirectory(prefix=".assembly-", dir=output) as temporary:
        temp = Path(temporary)
        extracted = {}

        def previous_range(kind, group):
            if kind not in extracted:
                path = temp / ("previous-" + kind + ".jsonl")
                _extract(catalog / (kind + ".7z"), path, kind + ".jsonl")
                extracted[kind] = path
            member = previous["groups"][group][kind]
            return (extracted[kind], member["offset"], member["size"], member["sha256"])

        with (
            (temp / "catalog.jsonl").open("wb") as metadata_out,
            (temp / "codelists.jsonl").open("wb") as codes_out,
        ):
            streams = {"catalog": metadata_out, "codelists": codes_out}

            def append(group, contents, group_counts, input_hashes, probes):
                state = {
                    "counts": group_counts,
                    "inputs": input_hashes,
                    "matching_inputs": probes,
                    "portal_fingerprint": index.fingerprint(probes),
                }
                for kind, payload in contents.items():
                    stream = streams[kind]
                    offset = stream.tell()
                    path, start, size, expected = (
                        (payload, 0, payload.stat().st_size, None)
                        if isinstance(payload, Path)
                        else payload
                    )
                    digest = hashlib.sha256()
                    remaining = size
                    with path.open("rb") as incoming:
                        incoming.seek(start)
                        while remaining:
                            chunk = incoming.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("Truncated accepted group")
                            digest.update(chunk)
                            stream.write(chunk)
                            remaining -= len(chunk)
                    if expected is not None and digest.hexdigest() != expected:
                        raise ValueError("Previous group checksum mismatch")
                    state[kind] = {
                        "offset": offset,
                        "size": size,
                        "sha256": digest.hexdigest(),
                    }
                states[group] = state
                for key, value in group_counts.items():
                    counts[key] += value

            for group, names in sorted(groups.items()):
                old = previous.get("groups", {}).get(group)
                input_hashes = {n: baseline.get(n) for n in names}
                if (
                    old
                    and old["inputs"] == input_hashes
                    and previous.get("rules") == rule_dependencies
                    and "matching_inputs" in old
                    and old.get("portal_fingerprint")
                    == index.fingerprint(old["matching_inputs"])
                ):
                    append(
                        group,
                        {kind: previous_range(kind, group) for kind in streams},
                        old["counts"],
                        input_hashes,
                        old["matching_inputs"],
                    )
                    for name in names:
                        accepted[name] = dict(previous["accepted"][name])
                        if (
                            accepted[name].get("blob") is None
                            and (data / name).exists()
                        ):
                            accepted[name]["blob"] = _blob(data / name)
                    build_counts["reused_groups"] += 1
                    continue
                db = sqlite3.connect(temp / "group.sqlite")
                db.executescript(
                    "DROP TABLE IF EXISTS datasets; DROP TABLE IF EXISTS codes; CREATE TABLE datasets(id TEXT, doc TEXT); CREATE INDEX dataset_id ON datasets(id); CREATE TABLE codes(id TEXT PRIMARY KEY, doc TEXT);"
                )
                current = {}
                try:

                    def add_code(scheme, db=db):
                        found = db.execute(
                            "SELECT doc FROM codes WHERE id=?", (scheme.id,)
                        ).fetchone()
                        if found:
                            scheme = merge_codelists(
                                CodeList.from_jsonld(json.loads(found[0])), scheme
                            )
                        db.execute(
                            "INSERT OR REPLACE INTO codes VALUES (?,?)",
                            (scheme.id, _json(scheme.to_jsonld())),
                        )

                    def insert_input(name, raw, db=db, add_code=add_code):
                        for value in _rows(raw):
                            if "/metadata/" in name:
                                model = Dataset.from_jsonld(value)
                                db.execute(
                                    "INSERT INTO datasets VALUES (?,?)",
                                    (model.id, _json(model.to_jsonld())),
                                )
                            else:
                                scheme = (
                                    CodeList.from_jsonld(value)
                                    if value.get("@type") == "skos:ConceptScheme"
                                    else convert_codelist(value)
                                )
                                add_code(scheme)

                    for name in names:
                        path = data / name
                        db.execute("SAVEPOINT source_input")
                        try:
                            raw = path.read_bytes()
                            insert_input(name, raw)
                            current[name] = {
                                "sha256": hashlib.sha256(raw).hexdigest(),
                                "blob": _blob(path),
                            }
                        except Exception as error:
                            db.execute("ROLLBACK TO source_input")
                            previous_input = previous.get("accepted", {}).get(name, {})
                            try:
                                raw = _accepted_blob(data, previous_input)
                                insert_input(name, raw)
                            except Exception as recovery_error:
                                raise ValueError(
                                    f"{name}: {error}; recovery: {recovery_error}"
                                ) from recovery_error
                            current[name] = previous_input
                            errors.append(
                                {
                                    "collection": name,
                                    "error": str(error),
                                    "retained_blob": previous_input["blob"],
                                }
                            )
                        finally:
                            db.execute("RELEASE source_input")
                    probes = []
                    group_counts = {key: 0 for key in counts}
                    group_metadata = temp / "group-catalog.jsonl"
                    with group_metadata.open("wb") as out:
                        for (identity,) in db.execute(
                            "SELECT DISTINCT id FROM datasets ORDER BY id"
                        ).fetchall():
                            models = [
                                Dataset.from_jsonld(json.loads(row[0]))
                                for row in db.execute(
                                    "SELECT doc FROM datasets WHERE id=?", (identity,)
                                )
                            ]
                            combined, schemes = assemble(
                                list(model_contributions(models))
                            )
                            for scheme in schemes:
                                add_code(scheme)
                            for dataset in combined:
                                probes.append(index.probe(dataset))
                                enriched = enrich(dataset, index)
                                status = enriched.match_evidence[-1]["status"]
                                group_counts[status] += 1
                                published = publication_view(
                                    enriched,
                                    include_scb_pxweb_v1=config["include_scb_pxweb_v1"],
                                )
                                group_counts["filtered"] += len(
                                    enriched.distributions
                                ) - len(published.distributions)
                                group_counts["quality_findings"] += len(
                                    validate_publication(published)
                                )
                                group_counts["datasets"] += 1
                                group_counts["distributions"] += len(
                                    published.distributions
                                )
                                out.write(
                                    (_json(published.to_jsonld()) + "\n").encode()
                                )
                    group_codes = temp / "group-codelists.jsonl"
                    with group_codes.open("wb") as out:
                        for (document,) in db.execute(
                            "SELECT doc FROM codes ORDER BY id"
                        ):
                            out.write((document + "\n").encode())
                            group_counts["codelists"] += 1
                    append(
                        group,
                        {
                            "catalog": group_metadata,
                            "codelists": group_codes,
                        },
                        group_counts,
                        input_hashes,
                        probes,
                    )
                    accepted.update(current)
                    build_counts["rebuilt_groups"] += 1
                except Exception as error:
                    errors.append(
                        {
                            "collection": group,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                    if not old:
                        raise ValueError(
                            f"Initial catalog cannot omit invalid input: {error}"
                        ) from error
                    if previous.get("dependencies") != extra:
                        raise ValueError(
                            "Cannot apply changed publication rules to unavailable source; restore accepted Git blobs"
                        ) from error
                    append(
                        group,
                        {kind: previous_range(kind, group) for kind in streams},
                        old["counts"],
                        old["inputs"],
                        old.get("matching_inputs", []),
                    )
                    accepted.update(
                        {
                            n: previous["accepted"][n]
                            for n in names
                            if n in previous["accepted"]
                        }
                    )
                    build_counts["retained_groups"] += 1
                finally:
                    db.close()
        for kind in ("catalog", "codelists"):
            content = temp / (kind + ".jsonl")
            if previous.get("content_hashes", {}).get(kind) == _hash(content):
                shutil.copyfile(catalog / (kind + ".7z"), output / (kind + ".7z"))
            else:
                _compress(content, output / (kind + ".7z"))
        content_hashes = {
            kind: _hash(temp / (kind + ".jsonl")) for kind in ("catalog", "codelists")
        }
    upstream = {}
    for name in sorted(baseline):
        if "/reports/" in name:
            try:
                upstream[name] = read(data / name)
            except Exception as error:
                errors.append({"collection": name, "error": str(error)})
    result = {
        "status": "completed_with_errors"
        if errors
        or any(
            r.get("errors") or r.get("status") not in (None, "success")
            for r in upstream.values()
            if isinstance(r, dict)
        )
        else "success",
        "counts": counts,
        "errors": errors,
        "upstream": upstream,
        "stages": {
            "content_enrichment": "completed"
            if portal_path.exists()
            else "not_configured",
            "semantic_enrichment": "source_mapping",
            "validation": "integrity_gated_descriptive_findings",
        },
    }
    assets = {name: _hash(output / name) for name in ASSETS}
    write(
        output / "state/current.json",
        {
            "profile_version": PROFILE_VERSION,
            "accepted": accepted,
            "groups": states,
            "assets": assets,
            "content_hashes": content_hashes,
            "dependencies": extra,
            "rules": rule_dependencies,
        },
    )
    write(output / "reports/latest.json", result)
    check_collection_sizes(output)
    write(
        output / "result.json",
        {
            "inputs": baseline,
            "dependencies": extra,
            "adapters": adapters,
            "catalog_baseline": catalog_baseline,
            "candidate": manifest(output),
        },
    )
    return {**result, "build_counts": build_counts}
