"""Retrieval is tested through a real local HTTP server and caller session."""

import asyncio
import importlib
import json
import subprocess
import sys
from itertools import product

import aiohttp
import pytest
from aiohttp import web


@pytest.mark.parametrize("adapter", ["pxweb_v1", "pxweb_v2"])
@pytest.mark.parametrize(
    "case",
    ["split", "sparse", "missing", "server", "wrong_codes", "cancel", "scalar_value"],
)
def test_retrieval_splits_and_reassembles_coordinates(adapter, case):
    async def run():
        module = importlib.import_module(
            f"metadata_harvest.adapters.{adapter}.retrieval"
        )
        requests = []

        async def handler(request):
            requests.append(request)
            if case == "missing":
                return web.Response(status=404)
            if case == "server":
                return web.Response(status=503)
            body = await request.json()
            if adapter == "pxweb_v1":
                selection = {q["code"]: q["selection"]["values"] for q in body["query"]}
            else:
                assert request.query.getall("lang") == ["sv"]
                assert request.query.getall("outputFormat") == ["json-stat2"]
                selection = {
                    q["variableCode"]: q["valueCodes"] for q in body["selection"]
                }
            ids = list(reversed(selection))
            codes = {key: list(reversed(selection[key])) for key in ids}
            if case == "wrong_codes":
                codes[ids[0]][0] = "unexpected"
            values, flags = {}, {}
            for i, coordinates in enumerate(product(*(codes[k] for k in ids))):
                coords = dict(zip(ids, coordinates, strict=True))
                if case == "sparse":
                    continue
                value = (
                    int(coords.get("x", "0")) * 10 + int(coords.get("y", "0"))
                    if case != "wrong_codes"
                    else 1
                )
                values[str(i)] = value
                if value == 12:
                    values[str(i)] = None
                    flags[str(i)] = ".."
            return web.json_response(
                {
                    "id": ids,
                    "size": [len(codes[k]) for k in ids],
                    "dimension": {k: {"category": {"index": codes[k]}} for k in ids},
                    "value": "malformed scalar" if case == "scalar_value" else values,
                    "status": flags,
                }
            )

        app = web.Application()
        app.router.add_route("*", "/data", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/data"
        try:
            async with aiohttp.ClientSession() as session:
                kwargs = dict(
                    provider_code="scb" if adapter == "pxweb_v2" else "mi",
                    dataset_code="T",
                    language="sv",
                    selection={"x": ["0", "1"], "y": ["0", "1", "2"]},
                    data_url=url + "?lang=sv&outputFormat=json-stat2"
                    if adapter == "pxweb_v2"
                    else url,
                    metadata_url=url + "?lang=sv&outputFormat=json-stat2"
                    if adapter == "pxweb_v2"
                    else url,
                    metadata={
                        f"/extension/{adapter}": {"max_cells": 2},
                        "/dimension": {"x": {}, "y": {}},
                    },
                )
                if case == "cancel":
                    task = asyncio.create_task(module.retrieve(session, **kwargs))
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                elif case in {"missing", "server", "wrong_codes", "scalar_value"}:
                    expected = (
                        LookupError
                        if case == "missing"
                        else aiohttp.ClientResponseError
                        if case == "server"
                        else ValueError
                    )
                    with pytest.raises(expected):
                        await module.retrieve(session, **kwargs)
                else:
                    result = await module.retrieve(session, **kwargs)
                    from metadata_harvest.validation import validate_retrieval

                    validate_retrieval(result, selection=kwargs["selection"])
                    assert result["id"] == ["x", "y"]
                    assert result["value"] == (
                        {} if case == "sparse" else [0, 1, 2, 10, 11, None]
                    )
                    if case == "split":
                        assert result["status"] == {"5": ".."}
                    assert len(requests) >= 3
                assert not session.closed
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_retrieval_import_does_not_load_harvesting():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; import metadata_harvest.adapters.pxweb_v1.retrieval; import metadata_harvest.adapters.pxweb_v2.retrieval; print(json.dumps(sorted(sys.modules)))",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    modules = json.loads(result.stdout)
    assert not any(
        name.startswith(("playwright", "httpx", "jsonschema")) for name in modules
    )


@pytest.mark.parametrize("code", ["ContentsCode", "Measure"])
def test_singleton_eliminated_code_matches_the_harvested_base_category(code):
    from metadata_harvest.retrieval import _fragment

    dimensions = {
        code: {
            "category": {"index": ["actual"]},
            "extension": {"nordicintel": {"elimination": True}},
        }
    }
    response = {
        "id": [code],
        "size": [1],
        "dimension": {
            code: {
                "category": {"index": ["EliminatedValue"]},
                "extension": {"elimination": True},
            }
        },
        "value": [0],
    }
    _, axes = _fragment(response, {code: ["actual"]}, dimensions)
    assert axes[code] == ["actual"]
    dimensions[code]["category"]["index"].append("other")
    with pytest.raises(ValueError):
        _fragment(response, {code: ["actual"]}, dimensions)


def test_eliminated_extra_dimension_and_sparse_flags_are_preserved():
    from metadata_harvest.retrieval import _fragment

    response = {
        "id": ["x", "area"],
        "size": [1, 1],
        "dimension": {
            "x": {"category": {"index": ["one"]}},
            "area": {"category": {"index": ["total"]}},
        },
        "value": {},
        "status": {"0": ".."},
    }
    dims = {"area": {"extension": {"nordicintel": {"elimination": True}}}}
    fragment, axes = _fragment(response, {"x": ["one"]}, dims)
    assert fragment["value"] == {} and fragment["status"] == {"0": ".."}
    assert axes["area"] == ["total"]
    dims["area"]["extension"]["nordicintel"]["elimination"] = False
    with pytest.raises(ValueError):
        _fragment(response, {"x": ["one"]}, dims)


def test_sundsvall_bootstraps_anonymous_gateway_using_the_callers_session():
    from metadata_harvest.adapters.pxweb_v1.retrieval import retrieve

    async def run():
        calls = []

        async def handler(request):
            calls.append(request.method)
            if request.method == "GET":
                response = web.json_response({"variables": []})
                response.set_cookie("anonymous", "ready")
                return response
            assert request.cookies.get("anonymous") == "ready"
            return web.json_response(
                {
                    "id": ["x"],
                    "size": [1],
                    "dimension": {"x": {"category": {"index": ["a"]}}},
                    "value": [0],
                }
            )

        app = web.Application()
        app.router.add_route("*", "/data", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/data"
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            ) as session:
                result = await retrieve(
                    session,
                    provider_code="sundsvall",
                    dataset_code="d",
                    language="sv",
                    selection={"x": ["a"]},
                    data_url=url,
                    metadata_url=url,
                    metadata={"/dimension": {"x": {}}},
                )
                assert result["value"] == [0] and not session.closed
            assert calls == ["GET", "POST"]
        finally:
            await runner.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize(
    "evidence", ["absent", "native_only", "harvested_only", "both"]
)
def test_synthetic_axis_requires_native_and_harvested_elimination_evidence(evidence):
    from metadata_harvest.retrieval import _fragment

    auxiliary = {"category": {"index": ["EliminatedValue"]}}
    if evidence in {"native_only", "both"}:
        auxiliary["extension"] = {"elimination": True}
    response = {
        "id": ["x", "ContentsCode"],
        "size": [1, 1],
        "dimension": {"x": {"category": {"index": ["a"]}}, "ContentsCode": auxiliary},
        "value": [0],
    }
    harvested = (
        {
            "ContentsCode": {
                "category": {"index": ["EliminatedValue"]},
                "extension": {"elimination": True},
            }
        }
        if evidence in {"harvested_only", "both"}
        else {}
    )
    if evidence == "both":
        fragment, _ = _fragment(
            response, {"x": ["a"]}, {}, *([harvested] if harvested else [])
        )
        assert fragment["value"] == [0]
    else:
        with pytest.raises(ValueError):
            _fragment(response, {"x": ["a"]}, {}, *([harvested] if harvested else []))
