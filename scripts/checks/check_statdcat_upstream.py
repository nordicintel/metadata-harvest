"""Run bundled upstream StatDCAT shapes diagnostically, without network resolution."""

import json
from collections import Counter
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Namespace

from apps.harvest.contributions import convert_dataset
from scripts.live.check_statdcat_live import source

SH = Namespace("http://www.w3.org/ns/shacl#")
shapes = Path("schemas/statdcatap-3.0.0-diagnostic.ttl")
report = {"purpose": "diagnostic_only", "shapes": str(shapes), "checks": []}
for adapter, provider in [
    ("pxweb_v2", "scb"),
    ("pxweb_v1", "csn"),
    ("kolada", "kolada"),
    ("entryscape", "catalogalese"),
]:
    document = source(adapter, provider)
    model = convert_dataset(document, adapter=adapter).dataset
    graph = Graph().parse(data=json.dumps(model.to_jsonld()), format="json-ld")
    conforms, result, _ = validate(
        graph,
        shacl_graph=str(shapes),
        inference="none",
        do_owl_imports=False,
        advanced=False,
        js=False,
    )
    findings = []
    for node in result.subjects(predicate=SH.resultSeverity):
        findings.append(
            {
                "focus": str(result.value(node, SH.focusNode)),
                "path": str(result.value(node, SH.resultPath)),
                "message": str(result.value(node, SH.resultMessage)),
                "severity": str(result.value(node, SH.resultSeverity)),
            }
        )
    counts = Counter(item["path"] for item in findings)
    report["checks"].append(
        {
            "dataset": model.id,
            "conforms": bool(conforms),
            "finding_count": len(findings),
            "paths": dict(counts),
            "examples": findings[:15],
        }
    )
    print(adapter, bool(conforms), len(findings), flush=True)
target = Path("output/statdcat-upstream-diagnostics.json")
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
