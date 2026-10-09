"""Real local HTTP transport for both Kolada scopes; no upstream traffic."""

import asyncio

import aiohttp
import pytest
from aiohttp import web


@pytest.mark.parametrize("scope", ["municipality", "ou"])
@pytest.mark.parametrize("case", ["normal", "empty", "404", "500", "invalid", "cancel"])
def test_retrieve(scope, case):
    from metadata_harvest.adapters.kolada.retrieval import retrieve

    async def run():
        calls = []

        async def handler(request):
            calls.append(request.path)
            if case in {"404", "500"}:
                return web.Response(status=int(case))
            if case == "cancel":
                await asyncio.sleep(0.1)
            rows = (
                []
                if case == "empty"
                else [
                    {
                        "kpi": "N1",
                        scope: "B",
                        "period": 2025,
                        "values": [{"gender": "T", "value": 0, "isdeleted": False}],
                    },
                    {
                        "kpi": "N1",
                        scope: "A",
                        "period": 2025,
                        "values": [{"gender": "T", "value": None, "status": "Privacy"}],
                    },
                ]
            )
            return web.json_response({"values": rows, "next_url": None})

        app = web.Application()
        app.router.add_get("/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/data/kpi/N1"
        try:
            async with aiohttp.ClientSession() as session:
                selection = {scope: ["B", "A"], "year": ["2025"]}
                if case == "invalid":
                    selection[scope] = []
                coroutine = retrieve(
                    session,
                    provider_code="kolada",
                    dataset_code=f"{scope}/N1",
                    language="sv",
                    selection=selection,
                    data_url=url,
                    metadata_url=None,
                    metadata={
                        "/dimension": {
                            scope: {"category": {"index": ["A", "B"]}},
                            "year": {"category": {"index": ["2025"]}},
                        },
                        "/extension/kolada": {"kpi_id": "N1", "scope": scope},
                    },
                )
                error = {
                    "404": LookupError,
                    "500": aiohttp.ClientResponseError,
                    "invalid": ValueError,
                    "cancel": TimeoutError,
                }.get(case)
                if error:
                    with pytest.raises(error):
                        await asyncio.wait_for(
                            coroutine, 0.01 if case == "cancel" else 5
                        )
                    return
                result = await coroutine
                assert result["id"] == [scope, "year"]
                assert result["size"] == [2, 1]
                assert result["value"] == ({} if case == "empty" else {"0": 0})
                if case != "empty":
                    assert result["status"] == {"1": "p"}
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_split_paginated_results_follow_requested_coordinates():
    from metadata_harvest.adapters.kolada.retrieval import retrieve

    async def run():
        calls = []

        async def handler(request):
            areas = (
                request.path.split("/municipality/")[1].split("/year/")[0].split(",")
            )
            page = int(request.query["page"])
            calls.append((areas, page))
            chosen = areas[:12] if page == 1 else areas[12:]
            return web.json_response(
                {
                    "values": [
                        {
                            "kpi": "N1",
                            "municipality": area,
                            "period": 2025,
                            "values": [
                                {"gender": "T", "value": int(area), "status": ""}
                            ],
                        }
                        for area in reversed(chosen)
                    ],
                    "next_url": "next" if page == 1 and len(areas) > 12 else None,
                }
            )

        app = web.Application()
        app.router.add_get("/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        areas = [str(i) for i in reversed(range(26))]
        try:
            async with aiohttp.ClientSession() as session:
                result = await retrieve(
                    session,
                    provider_code="kolada",
                    dataset_code="municipality/N1",
                    language="sv",
                    selection={"year": ["2025"], "municipality": areas},
                    data_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/data/kpi/N1",
                    metadata_url=None,
                    metadata={
                        "/extension/kolada": {"scope": "municipality", "kpi_id": "N1"},
                        "/dimension": {
                            "year": {"category": {"index": ["2025"]}},
                            "municipality": {"category": {"index": areas}},
                        },
                    },
                )
                assert result["value"] == {
                    str(i): int(area) for i, area in enumerate(areas)
                }
                assert len(calls) == 3
        finally:
            await runner.cleanup()

    asyncio.run(run())
