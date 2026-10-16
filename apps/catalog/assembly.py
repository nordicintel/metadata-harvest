"""Offline incremental assembly from accepted contributions and portal metadata."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote

from apps.catalog.dependencies import _hash, configuration, dependencies
from apps.catalog.enrichment import enrich
from apps.catalog.filtering import publication_view
from apps.catalog.matching import PortalIndex
from apps.catalog.quality import validate_publication
from apps.configuration import load_configuration, use_configuration
from apps.harvest.registry import ADAPTERS
from apps.harvest.sources import inventory
from apps.migrations.profile_reader import read_dataset
from apps.model.composition import assemble, merge_codelists
from apps.model.models import CodeList, Dataset
from apps.model.vocabulary import PROFILE_VERSION
from apps.parsing.accepted import model_contributions
from apps.parsing.contributions import convert_codelist
from apps.paths import CONFIG
from apps.portal.snapshot import accepted_records
from apps.publication.artifacts import check_collection_sizes, manifest

logger = logging.getLogger(__name__)


class _Stage:
    """Log a build stage's start at DEBUG and its elapsed time at INFO."""

    def __init__(self, name, **fields):
        self.name = name
        self.fields = fields

    def __enter__(self):
        self.started = time.perf_counter()
        logger.debug("%s: starting", self.name)
        return self

    def __exit__(self, kind, error, traceback):
        detail = "".join(f" {k}={v}" for k, v in self.fields.items())
        if error is not None:
            logger.error(
                "%s: failed after %.1fs%s (%s: %s)",
                self.name,
                time.perf_counter() - self.started,
                detail,
                type(error).__name__,
                error,
            )
        else:
            logger.info(
                "%s: %.1fs%s", self.name, time.perf_counter() - self.started, detail
            )


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


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


def build(
    data: Path, output: Path, *, adapters=None, config_dir: Path = CONFIG
) -> dict:
    with use_configuration(load_configuration(config_dir)):
        from apps.catalog.inputs import inputs
        from apps.publication.archives import (
            ASSETS,
            _compress,
            _extract,
            previous_asset,
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
        build_started = time.perf_counter()
        logger.info("catalog build: adapters=%s", ",".join(adapters))
        with _Stage("fingerprint accepted inputs") as stage:
            baseline = inputs(data, adapters)
            stage.fields["inputs"] = len(baseline)
        with _Stage("hash build dependencies") as stage:
            extra = dependencies(data, config_dir=config_dir)
            config, overrides = configuration(config_dir)
            stage.fields["dependencies"] = len(extra)
            stage.fields["overrides"] = len(overrides)
        rule_dependencies = {
            k: v for k, v in extra.items() if not k.startswith("portal:")
        }
        catalog = data / "catalog"
        with _Stage("fingerprint previous catalog") as stage:
            catalog_baseline = manifest(catalog)
            previous = read(catalog / "state/current.json", {})
            stage.fields["files"] = len(catalog_baseline)
            stage.fields["previous_groups"] = len(previous.get("groups", {}))
            stage.fields["previous_profile"] = previous.get("profile_version")
        for adapter in adapters:
            if not any(
                n.startswith(adapter + "/metadata/") for n in baseline
            ) and not any(
                n.startswith(adapter + "/metadata/")
                for n in previous.get("accepted", {})
            ):
                raise ValueError(
                    f"No accepted metadata coverage for adapter: {adapter}"
                )
        if previous.get("profile_version") not in {"1.0.0", PROFILE_VERSION}:
            logger.warning(
                "previous catalog profile %s is unsupported; rebuilding every group",
                previous.get("profile_version"),
            )
            previous = {}
        elif previous.get("rules") != rule_dependencies:
            old_rules = previous.get("rules") or {}
            changed = sorted(
                k
                for k in set(rule_dependencies) | set(old_rules)
                if rule_dependencies.get(k) != old_rules.get(k)
            )
            logger.info(
                "publication rules changed since previous catalog (%s); "
                "every group will be rebuilt",
                ", ".join(changed),
            )
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
        with _Stage("group source collections by owner") as stage:
            for name in sorted(source_names):
                groups.setdefault(_group(name), []).append(name)
            stage.fields["collections"] = len(source_names)
            stage.fields["groups"] = len(groups)
        portal_path = data / "data_eu/oppnadata.jsonl.gz"
        if not portal_path.exists():
            logger.warning(
                "no portal snapshot at %s; content enrichment is not configured",
                portal_path,
            )
        with _Stage("load portal snapshot and build match index"):
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
                    _extract(
                        catalog / previous_asset(previous, kind), path, kind + ".jsonl"
                    )
                    extracted[kind] = path
                member = previous["groups"][group][kind]
                return (
                    extracted[kind],
                    member["offset"],
                    member["size"],
                    member["sha256"],
                )

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

                for position, (group, names) in enumerate(sorted(groups.items()), 1):
                    old = previous.get("groups", {}).get(group)
                    input_hashes = {n: baseline.get(n) for n in names}
                    group_started = time.perf_counter()
                    rebuild_reason = _rebuild_reason(
                        old, input_hashes, previous, rule_dependencies, index
                    )
                    logger.info(
                        "group %d/%d %s: %s (%d collections, reuse check %.1fs)",
                        position,
                        len(groups),
                        group,
                        "reusing previous output"
                        if rebuild_reason is None
                        else "rebuilding because " + rebuild_reason,
                        len(names),
                        time.perf_counter() - group_started,
                    )
                    if rebuild_reason is None:
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
                                    model = read_dataset(value)
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
                            input_started = time.perf_counter()
                            try:
                                raw = path.read_bytes()
                                insert_input(name, raw)
                                logger.debug(
                                    "group %s: loaded %s (%d bytes) in %.1fs",
                                    group,
                                    name,
                                    len(raw),
                                    time.perf_counter() - input_started,
                                )
                                current[name] = {
                                    "sha256": hashlib.sha256(raw).hexdigest(),
                                    "blob": _blob(path),
                                }
                            except Exception as error:
                                db.execute("ROLLBACK TO source_input")
                                previous_input = previous.get("accepted", {}).get(
                                    name, {}
                                )
                                try:
                                    raw = _accepted_blob(data, previous_input)
                                    insert_input(name, raw)
                                except Exception as recovery_error:
                                    raise ValueError(
                                        f"{name}: {error}; recovery: {recovery_error}"
                                    ) from recovery_error
                                current[name] = previous_input
                                logger.warning(
                                    "group %s: %s is unreadable (%s); "
                                    "recovered previous accepted blob %s",
                                    group,
                                    name,
                                    error,
                                    previous_input["blob"],
                                )
                                errors.append(
                                    {
                                        "collection": name,
                                        "error": str(error),
                                        "retained_blob": previous_input["blob"],
                                    }
                                )
                            finally:
                                db.execute("RELEASE source_input")
                        load_elapsed = time.perf_counter() - group_started
                        probes = []
                        group_counts = {key: 0 for key in counts}
                        group_metadata = temp / "group-catalog.jsonl"
                        assemble_started = time.perf_counter()
                        identities = 0
                        with group_metadata.open("wb") as out:
                            for (identity,) in db.execute(
                                "SELECT DISTINCT id FROM datasets ORDER BY id"
                            ).fetchall():
                                models = [
                                    Dataset.from_jsonld(json.loads(row[0]))
                                    for row in db.execute(
                                        "SELECT doc FROM datasets WHERE id=?",
                                        (identity,),
                                    )
                                ]
                                identities += 1
                                combined, schemes = assemble(
                                    list(model_contributions(models))
                                )
                                if identities % 1000 == 0:
                                    logger.info(
                                        "group %s: assembled %d dataset identities "
                                        "(%d datasets) in %.1fs",
                                        group,
                                        identities,
                                        group_counts["datasets"],
                                        time.perf_counter() - assemble_started,
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
                                        include_scb_pxweb_v1=config[
                                            "include_scb_pxweb_v1"
                                        ],
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
                        logger.info(
                            "group %s: rebuilt %d datasets (%d matched, %d unmatched, "
                            "%d ambiguous), %d distributions, %d codelists from %d "
                            "identities; load %.1fs, assemble+enrich %.1fs, "
                            "total %.1fs",
                            group,
                            group_counts["datasets"],
                            group_counts["matched"],
                            group_counts["unmatched"],
                            group_counts["ambiguous"],
                            group_counts["distributions"],
                            group_counts["codelists"],
                            identities,
                            load_elapsed,
                            time.perf_counter() - assemble_started,
                            time.perf_counter() - group_started,
                        )
                    except Exception as error:
                        logger.error(
                            "group %s: rebuild failed after %.1fs (%s: %s)",
                            group,
                            time.perf_counter() - group_started,
                            type(error).__name__,
                            error,
                        )
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
                        if (
                            previous.get("profile_version") != PROFILE_VERSION
                            or previous.get("dependencies") != extra
                        ):
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
                        logger.warning(
                            "group %s: retained previous output after failure", group
                        )
                    finally:
                        db.close()
            logger.info(
                "groups done: %d rebuilt, %d reused, %d retained in %.1fs",
                build_counts["rebuilt_groups"],
                build_counts["reused_groups"],
                build_counts["retained_groups"],
                time.perf_counter() - build_started,
            )
            for kind in ("catalog", "codelists"):
                content = temp / (kind + ".jsonl")
                asset = kind + ".jsonl.zst"
                with _Stage("write " + asset, bytes=content.stat().st_size) as stage:
                    if asset in previous.get("assets", {}) and previous.get(
                        "content_hashes", {}
                    ).get(kind) == _hash(content):
                        stage.fields["action"] = "copied unchanged archive"
                        shutil.copyfile(catalog / asset, output / asset)
                    else:
                        stage.fields["action"] = "compressed"
                        _compress(content, output / asset)
            content_hashes = {
                kind: _hash(temp / (kind + ".jsonl"))
                for kind in ("catalog", "codelists")
            }
        upstream = {}
        with _Stage("read upstream reports") as stage:
            for name in sorted(baseline):
                if "/reports/" in name:
                    try:
                        upstream[name] = read(data / name)
                    except Exception as error:
                        logger.warning("upstream report %s unreadable: %s", name, error)
                        errors.append({"collection": name, "error": str(error)})
            stage.fields["reports"] = len(upstream)
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
        finish = _Stage(
            "write candidate state, report and manifest",
            status=result["status"],
            errors=len(errors),
        )
        finish.__enter__()
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
        finish.__exit__(None, None, None)
        logger.info(
            "catalog build finished: status=%s datasets=%d distributions=%d "
            "codelists=%d matched=%d unmatched=%d ambiguous=%d errors=%d in %.1fs",
            result["status"],
            counts["datasets"],
            counts["distributions"],
            counts["codelists"],
            counts["matched"],
            counts["unmatched"],
            counts["ambiguous"],
            len(errors),
            time.perf_counter() - build_started,
        )
        return {**result, "build_counts": build_counts}


def _rebuild_reason(old, input_hashes, previous, rule_dependencies, index):
    """Explain why a group cannot reuse its previous output; None means reuse."""
    if not old:
        return "no previous output for this group"
    if previous.get("profile_version") != PROFILE_VERSION:
        return f"previous profile version {previous.get('profile_version')} differs"
    if old["inputs"] != input_hashes:
        changed = sorted(
            n
            for n in set(old["inputs"]) | set(input_hashes)
            if old["inputs"].get(n) != input_hashes.get(n)
        )
        shown = ", ".join(changed[:5]) + (" ..." if len(changed) > 5 else "")
        return f"{len(changed)} accepted collection(s) changed: {shown}"
    if previous.get("rules") != rule_dependencies:
        return "publication rules or code changed"
    if "matching_inputs" not in old:
        return "previous output has no matching inputs"
    if old.get("portal_fingerprint") != index.fingerprint(old["matching_inputs"]):
        return "portal match evidence changed"
    return None
