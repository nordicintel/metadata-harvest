import asyncio
import json
import ssl
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from metadata_harvest import get_pxweb_tables
from metadata_harvest.pxweb import (
    _context,
    _databases,
    _date,
    _languages,
    _new_page,
    _parse_url,
    _retry_after,
)


@pytest.fixture
def website(request):
    requests = []
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path if self.command == "GET" else "POST " + self.path)
            value = routes.get(
                (self.command, self.path), routes.get(self.path, (404, "missing", {}))
            )
            if callable(value):
                value = value()
            status, body, headers = value
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for key, val in headers.items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    secure = getattr(request, "param", False)
    if secure:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(
            "tests/fixtures/localhost-cert.pem", "tests/fixtures/localhost-key.pem"
        )
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield (
        f"{'https' if secure else 'http'}://127.0.0.1:{server.server_port}",
        routes,
        requests,
    )
    server.shutdown()
    server.server_close()
    thread.join()


def table(title="Table", date="2026-10-01", route="/pxweb/sv/db/-/t.px/"):
    return f'<ul><li><a href="{route}">{title}</a><time datetime="{date}"></time></li></ul>'


def test_matrix():
    providers = json.loads(
        Path("tests/fixtures/providers.json").read_text(encoding="utf-8")
    )
    assert len(providers) == 20
    assert sum(len(p["database_ids"]) for p in providers) == 45
    assert sum(len(p["database_ids"]) * len(p["languages"]) for p in providers) == 55


@pytest.mark.parametrize(
    "prefix", ["", "/Folkhalsodata", "/PXWeb_Ext", "/Pxwebb", "/pxweb"]
)
def test_route_equivalence(prefix):
    api = _parse_url(f"https://example.org{prefix}/API/V1/SV/A%20B/c/t.px?x=1#x")
    web = _parse_url(f"https://example.org{prefix}/pxweb/sv/A%20B/c/t.px")
    assert api == web
    assert api.database == "A B"
    assert "%2520" not in api.web()
    assert api.language == "sv"


@pytest.mark.parametrize(
    ("url_db", "supplied", "expected"),
    [
        (None, None, None),
        (None, [], []),
        (None, ["b"], ["b"]),
        ("a", None, ["a"]),
        ("a", [], ["a"]),
        ("a", ["a", "b"], ["a", "b"]),
    ],
)
def test_database_selection(url_db, supplied, expected):
    route = _parse_url("https://example.org/pxweb/sv/" + (url_db or ""))
    assert _databases(route, supplied) == expected


def test_languages():
    root = _parse_url("https://example.org/pxweb/")
    assert _languages(root, None) == ["en", "sv"]
    assert _languages(root, ["SV", "sv", "EN"]) == ["sv", "en"]
    assert _languages(_parse_url(root.web("sv")), []) == ["sv"]
    with pytest.raises(ValueError):
        _languages(root, ["swe"])


@pytest.mark.parametrize(
    "value",
    ["2026", "2026-10", "2026-10-06", "2026-10-06T12:30", "2026-10-06T12:30:01Z"],
)
def test_date_precision(value):
    assert _date(value) == value


@pytest.mark.parametrize("value", ["", "10/06/2026", "2026-02-30", "Yesterday"])
def test_invalid_dates(value):
    with pytest.raises(ValueError):
        _date(value)


def test_retry_after():
    assert _retry_after("3", 0) == 3
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    assert 28 <= _retry_after(future, 0) <= 30


def test_empty_selection_no_browser():
    assert (
        asyncio.run(get_pxweb_tables("https://example.org/pxweb/", database_ids=[]))
        == []
    )


def test_root_table_and_api_equivalence(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (200, table(), {})
    web = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    api = asyncio.run(get_pxweb_tables(host + "/api/v1/sv/db/"))
    assert web == api
    assert web[0]["category_path"] == []
    assert web[0]["path"] == "/"
    assert web[0]["published"] == "2026-10-01"
    assert web[0]["api_url"] == host + "/api/v1/sv/db/t.px"


def test_discovery_and_language_override(website):
    host, routes, _ = website
    routes["/pxweb/sv/"] = (200, '<a href="/pxweb/sv/db/">Database</a>', {})
    routes["/pxweb/sv/db/"] = (200, table(), {})
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/", ["en"]))
    assert len(rows) == 1
    assert rows[0]["language"] == "sv"


def test_missing_title_and_date_fail_aggregate(website):
    host, routes, requests = website
    routes["/pxweb/sv/bad/"] = (200, table(title="", route="/pxweb/sv/bad/-/t.px/"), {})
    routes["/pxweb/sv/db/"] = (200, table(), {})
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/", database_ids=["bad", "db"]))
    assert "/pxweb/sv/db/" in requests


def test_redirect_not_mislabeled(website):
    host, routes, _ = website
    routes["/pxweb/en/db/"] = (302, "", {"Location": "/pxweb/sv/db/"})
    routes["/pxweb/sv/db/"] = (200, table(), {})
    with pytest.raises(ExceptionGroup, match="Incomplete"):
        asyncio.run(get_pxweb_tables(host + "/pxweb/en/db/"))


def test_api_blocking_including_redirects(website):
    host, routes, requests = website
    routes["/"] = (
        200,
        """<script>
    fetch('/api/v1/direct').catch(()=>{});
    fetch('/redirect').catch(()=>{});
    fetch('/API/V1/upper').catch(()=>{});
    </script>""",
        {},
    )
    routes["/redirect"] = (302, "", {"Location": "/api/v1/redirected"})

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await _context(browser)
            page = await _new_page(context)
            await page.goto(host)
            await page.wait_for_timeout(300)
            await browser.close()

    asyncio.run(run())
    assert "/redirect" in requests
    assert not any("/api/v1/" in x.lower() for x in requests)


def test_nested_dynamic_tree_repeated_ids(website):
    host, routes, _ = website
    content = """<div role="tree"><button aria-expanded="false" onclick="this.outerHTML=`<ul><li><a href='/pxweb/sv/db/START__01/'>01. Category</a><ul><li><a href='/pxweb/sv/db/START__01/t.px/'>Title</a><time datetime='2026-10-01'></time></li></ul></li></ul>`">Expand</button></div>"""
    routes["/pxweb/sv/db/"] = (200, content, {})
    routes["/pxweb/sv/db/START__01/"] = (
        200,
        table(route="/pxweb/sv/db/START__01/t.px/"),
        {},
    )
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert rows[0]["path"] == "/01"
    assert rows[0]["category_path"] == [{"id": "01", "name": "01. Category"}]


def test_bracketed_dates_and_extensionless_ids(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        """<div class="AspNet-TreeView"><ul><li class="AspNet-TreeView-Root"><span>01. Category</span><ul><li class="AspNet-TreeView-Leaf"><a href="/pxweb/sv/db/START__01/A10/"><span class="tableofcontent_link">Table title <em>[2026-10-05]</em></span></a></li></ul></li></ul></div>""",
        {},
    )
    routes["/pxweb/sv/db/START__01/A10/"] = (
        200,
        '<div class="information_lastupdated_value">2026-10-05</div>',
        {},
    )
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert rows[0]["id"] == "A10"
    assert rows[0]["title"] == "Table title"
    assert rows[0]["published"] == "2026-10-05"
    assert rows[0]["path"] == "/01"


def test_flat_api_date_without_table_visit(website):
    host, routes, requests = website
    routes["/pxweb/sv/db/"] = (200, '<a href="/pxweb/sv/db/-/t.px/">Title</a>', {})
    routes["/api/v1/sv/db?filter=*&query=*"] = (
        200,
        json.dumps([dict(id="t.px", path="/", published="2026-10-05")]),
        {},
    )
    assert (
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))[0]["published"]
        == "2026-10-05"
    )
    assert "/pxweb/sv/db/-/t.px/" not in requests
    assert [x for x in requests if "/api/" in x] == ["/api/v1/sv/db?filter=*&query=*"]


def test_parent_folder_last_resort_cached_and_web_authoritative(website, caplog):
    host, routes, requests = website
    routes["/pxweb/sv/db/"] = (
        200,
        "".join(
            table(title=name, date="", route=f"/pxweb/sv/db/-/{name}.px/")
            for name in ("first", "second", "undated")
        ),
        {},
    )
    routes["/api/v1/sv/db?filter=*&query=*"] = (
        200,
        json.dumps(
            [
                dict(id="first.px", path="/wrong", published="2000-01-01"),
                dict(id="api-only.px", path="/", published="2000-01-01"),
            ]
        ),
        {},
    )
    routes["/api/v1/sv/db/"] = (
        200,
        json.dumps(
            [
                dict(id="first.px", type="t", updated="2026-10-05"),
                dict(id="second.px", type="t", updated="2026-10"),
                dict(id="api-only.px", type="t", updated="2026-10-01"),
                dict(id="subfolder", type="l"),
            ]
        ),
        {},
    )
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert [(x["id"], x["published"]) for x in rows] == [
        ("first.px", "2026-10-05"),
        ("second.px", "2026-10"),
    ]
    assert [x for x in requests if "/api/" in x] == [
        "/api/v1/sv/db?filter=*&query=*",
        "/api/v1/sv/db/",
    ]
    assert "Omitting undated web table" in caplog.text
    assert "undated.px" in caplog.text


def test_folder_failure_is_not_an_omission(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (200, table(date=""), {})
    routes["/api/v1/sv/db?filter=*&query=*"] = (200, "[]", {})
    routes["/api/v1/sv/db/"] = (503, "Unavailable", {})
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))


def test_empty_database_is_explicit(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        '<div class="empty-database">Database is empty</div>',
        {},
    )
    assert asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/")) == []
    routes["/pxweb/sv/db/"] = (200, '<div class="AspNet-TreeView"></div>', {})
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))


def test_unsupported_requires_explicit_evidence(website, caplog):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (404, "<p>Database does not exist</p>", {})
    assert asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/")) == []
    assert "Unsupported combination" in caplog.text
    routes["/pxweb/sv/db/"] = (404, "<p>Not found</p>", {})
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))


def test_transient_navigation_retry(website):
    host, routes, requests = website
    routes["/pxweb/sv/db/"] = lambda: (
        (429, "wait", {"Retry-After": "0"})
        if requests.count("/pxweb/sv/db/") == 1
        else (200, table(), {})
    )
    assert len(asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))) == 1
    assert requests.count("/pxweb/sv/db/") == 2


def test_folder_pages_and_repeated_ids(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        '<a href="/pxweb/sv/db/one/">One</a><a href="/pxweb/sv/db/two/">Two</a>',
        {},
    )
    for folder in ("one", "two"):
        routes[f"/pxweb/sv/db/{folder}/"] = (
            200,
            table(route=f"/pxweb/sv/db/{folder}/t.px/")
            + '<a href="/pxweb/sv/db/">Root</a>',
            {},
        )
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert [r["path"] for r in rows] == ["/one", "/two"]
    assert rows[0]["category_path"] == [{"id": "one", "name": "One"}]


def test_pagination(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        table() + '<a rel="next" href="?page=2">Next</a>',
        {},
    )
    routes["/pxweb/sv/db/?page=2"] = (200, table(route="/pxweb/sv/db/-/second.px/"), {})
    assert len(asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))) == 2


def test_stalled_expansion_fails(website, monkeypatch):
    import metadata_harvest.pxweb as module

    host, routes, requests = website
    monkeypatch.setattr(module, "_TIMEOUT", 200)
    monkeypatch.setattr(module, "_ATTEMPTS", 2)
    monkeypatch.setattr(module, "_retry_after", lambda *args: 0)
    routes["/pxweb/sv/db/"] = (
        200,
        '<div role="tree"><button aria-expanded="false">No progress</button></div>',
        {},
    )
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert requests.count("/pxweb/sv/db/") == 2


def test_host_redirect_keeps_installation_prefix(website):
    host, routes, _ = website
    routes["/"] = (302, "", {"Location": "/PREFIX/pxweb/sv/"})
    routes["/PREFIX/pxweb/sv/"] = (200, '<a href="/PREFIX/pxweb/sv/db/">DB</a>', {})
    routes["/PREFIX/pxweb/sv/db/"] = (
        200,
        table(route="/PREFIX/pxweb/sv/db/-/t.px/"),
        {},
    )
    rows = asyncio.run(get_pxweb_tables(host, ["sv"]))
    assert rows[0]["api_url"] == host + "/PREFIX/api/v1/sv/db/t.px"


def test_english_date_format_is_explicit():
    assert _date("Updated: 5/20/2026", "%m/%d/%Y") == "2026-05-20"
    assert _date("3/5/2026", "%m/%d/%Y") == "2026-03-05"
    with pytest.raises(ValueError):
        _date("3/5/2026")


def test_pxweb_category_does_not_replace_route_boundary():
    route = _parse_url("https://example.org/PXWeb/pxweb/sv/db/pxweb/table.px")
    assert route.root == "https://example.org/PXWeb/pxweb"
    assert route.tail == ("pxweb", "table.px")


@pytest.mark.parametrize("website", [True], indirect=True)
def test_native_certificate_recovery_in_discovery(website, caplog):
    host, routes, _ = website
    routes["/pxweb/"] = (302, "", {"Location": "/pxweb/sv/"})
    routes["/pxweb/sv/"] = (200, '<a href="/pxweb/sv/db/">Database</a>', {})
    routes["/pxweb/sv/db/"] = (200, table(), {})
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/", ["sv"]))
    assert len(rows) == 1
    assert "ignore_https_errors=True" in caplog.text


def test_delayed_branch_insertion(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        """<div role="tree"><button aria-expanded="false" onclick="this.setAttribute('aria-expanded','true');setTimeout(()=>{document.querySelector('#children').innerHTML=`<li><a href='/pxweb/sv/db/-/t.px/'>Title</a><time datetime='2026-10-01'></time></li>`},300)">Expand</button><ul id="children"></ul></div>""",
        {},
    )
    assert len(asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))) == 1


def test_dynamic_pagination(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        table()
        + """<button type="button" rel="next" onclick="document.querySelector('a').href='/pxweb/sv/db/-/second.px/';this.disabled=true">Next</button>""",
        {},
    )
    assert len(asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))) == 2


def test_real_start_category_is_preserved(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        '<a href="/pxweb/sv/db/START/">Start category</a>',
        {},
    )
    routes["/pxweb/sv/db/START/"] = (200, table(route="/pxweb/sv/db/START/t.px/"), {})
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert rows[0]["path"] == "/START"


def test_duplicate_web_links_preserve_one_record(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        table() + table(route="/pxweb/sv/db/-/t.px/?layout=compact"),
        {},
    )
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert len(rows) == 1


@pytest.mark.parametrize(
    ("code", "database", "table_id", "path", "date"),
    [
        (
            "mi",
            "Konjunkturlönestatistik",
            "Loner_efter_sektor_arsdata.px",
            "/Löneutveckling efter sektor",
            "2025-05-12",
        ),
        ("mcf", "PxData", "A10", "/A/A1", "2026-10-05"),
        (
            "slu",
            "OffStat",
            "AM_Areal_agoslag_SVL_tab.px",
            "/AllMark/Areal",
            "2026-05-27",
        ),
    ],
)
def test_observed_provider_markup(website, code, database, table_id, path, date):
    from urllib.parse import quote

    host, routes, _ = website
    content = Path(f"tests/fixtures/{code}-tree.html").read_text(encoding="utf-8")
    prefix = "/pxweb" if code == "slu" else "/PXWeb/pxweb"
    root = prefix + "/sv/" + quote(database, safe="") + "/"
    routes[root] = (200, content, {})
    import re
    from urllib.parse import unquote

    href = re.search(r'href="([^"]+)"', content)[1]
    routes[quote(unquote(href), safe="/")] = (
        200,
        f'<div class="information_lastupdated_value">{date}</div>',
        {},
    )
    rows = asyncio.run(get_pxweb_tables(host + root))
    assert len(rows) == 1
    assert (rows[0]["id"], rows[0]["path"], rows[0]["published"]) == (
        table_id,
        path,
        date,
    )
    assert "[" not in rows[0]["title"]


def test_tablelist_heading_and_adjacent_date(website):
    host, routes, _ = website
    routes["/pxweb/sv/db/"] = (
        200,
        """<ol class="tablelist_ol"><li class="tablelist_li"><div><p><a class="tablelist_linkHeading" href="/pxweb/sv/db/-/t.px/">Actual title</a></p><a class="tablelist_link" href="/pxweb/sv/db/-/t.px/">Choose variables</a><div>Uppdaterad:\n2026-10-05\n</div></div></li></ol>""",
        {},
    )
    routes["/pxweb/sv/db/-/t.px/"] = (
        200,
        '<div class="information_lastupdated_value">2026-10-05</div>',
        {},
    )
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert len(rows) == 1
    assert rows[0]["title"] == "Actual title"
    assert rows[0]["published"] == "2026-10-05"


def test_compact_date():
    assert _date("20250506") == "2025-05-06"


def test_stateful_expansion_retry_reloads_page(website):
    host, routes, requests = website
    initial = """<form method="post"><input type="submit" class="tableofcontent_action" value="Expand all"><div class="AspNet-TreeView"><span class="AspNet-TreeView-Expand"></span></div></form>"""
    routes["/pxweb/sv/db/"] = (200, initial, {})
    routes[("POST", "/pxweb/sv/db/")] = lambda: (
        (503, "Unavailable", {"Retry-After": "0"})
        if requests.count("POST /pxweb/sv/db/") == 1
        else (200, table(), {})
    )
    assert len(asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))) == 1
    assert requests == [
        "/pxweb/sv/db/",
        "POST /pxweb/sv/db/",
        "/pxweb/sv/db/",
        "POST /pxweb/sv/db/",
    ]


def test_ineffective_full_control_falls_back_to_branches(website, monkeypatch):
    import metadata_harvest.pxweb as module

    monkeypatch.setattr(module, "_TIMEOUT", 1000)
    host, routes, requests = website
    routes["/pxweb/sv/db/"] = (
        200,
        """<form onsubmit="return false"><input class="tableofcontent_action" type="submit"></form><div role="tree"><button type="button" aria-expanded="false" onclick="this.outerHTML=`<a href='/pxweb/sv/db/-/t.px/'>Title</a><time datetime='2026-10-01'></time>`">Expand</button></div>""",
        {},
    )
    assert len(asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))) == 1
    assert requests.count("/pxweb/sv/db/") == 2


def test_popup_redirect_cannot_reach_api(website):
    host, routes, requests = website
    routes["/"] = (200, "<script>window.open('/popup')</script>", {})
    routes["/popup"] = (302, "", {"Location": "/api/v1/popup"})

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await _context(browser)
            page = await _new_page(context)
            await page.goto(host)
            await page.wait_for_timeout(300)
            await browser.close()

    asyncio.run(run())
    assert "/popup" not in requests
    assert "/api/v1/popup" not in requests


def test_month_precision_and_listing_date_conventions():
    from metadata_harvest.pxweb import _numeric_date_format

    assert _date("September 2022") == "2022-09"
    assert _numeric_date_format(["22/09/2026", "01/02/2026"]) == "%d/%m/%Y"
    assert _numeric_date_format(["9/22/2026", "1/2/2026"]) == "%m/%d/%Y"
    with pytest.raises(ValueError):
        _numeric_date_format(["22/09/2026", "9/22/2026"])


def test_live_comparison_ignores_only_session_uuid():
    import runpy

    compare = runpy.run_path("scripts/check_pxweb_live.py")["comparable"]
    base = {
        "language": "sv",
        "database_id": "db",
        "path": "/",
        "id": "x",
        "title": "Title",
    }
    first = {
        **base,
        "web_url": "https://example.org/x?rxid=00000000-0000-0000-0000-000000000001&layout=wide",
    }
    second = {
        **base,
        "web_url": "https://example.org/x?rxid=00000000-0000-0000-0000-000000000002&layout=wide",
    }
    assert compare([first]) == compare([second])
    assert compare([first]) != compare(
        [{**second, "web_url": second["web_url"].replace("wide", "narrow")}]
    )
    assert "rxid=" in first["web_url"]


def test_cross_site_frame_redirect_cannot_reach_api(website):
    host, routes, requests = website
    frame_host = host.replace("127.0.0.1", "localhost")
    routes["/"] = (200, f'<iframe src="{frame_host}/frame"></iframe>', {})
    routes["/frame"] = (302, "", {"Location": "/api/v1/frame"})

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await _context(browser)
            page = await _new_page(context)
            await page.goto(host)
            await page.wait_for_timeout(300)
            await browser.close()

    asyncio.run(run())
    assert "/api/v1/frame" not in requests


def test_listing_dates_need_no_api(website):
    host, routes, requests = website
    routes["/pxweb/sv/db/"] = (200, table(), {})
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert rows[0]["published"] == "2026-10-01"
    assert not any("/api/" in path for path in requests)


def test_folder_enrichment_distinguishes_repeated_ids_and_encodes_paths(website):
    host, routes, requests = website
    routes["/pxweb/sv/db/"] = (
        200,
        '<a href="/pxweb/sv/db/A%20B/">First category</a>'
        '<a href="/pxweb/sv/db/C/">Second category</a>',
        {},
    )
    for folder in ("A%20B", "C"):
        routes[f"/pxweb/sv/db/{folder}/"] = (
            200,
            table(date="", route=f"/pxweb/sv/db/{folder}/t.px/"),
            {},
        )
    routes["/api/v1/sv/db?filter=*&query=*"] = (200, "[]", {})
    for folder, date in (("A%20B", "2026-01"), ("C", "2026-02")):
        routes[f"/api/v1/sv/db/{folder}/"] = (
            200,
            json.dumps([dict(type="t", id="t.px", updated=date)]),
            {},
        )
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert [(row["path"], row["published"]) for row in rows] == [
        ("/A B", "2026-01"),
        ("/C", "2026-02"),
    ]
    assert rows[0]["category_path"] == [{"id": "A B", "name": "First category"}]
    assert requests.count("/api/v1/sv/db/A%20B/") == 1
    assert requests.count("/api/v1/sv/db/C/") == 1


@pytest.mark.parametrize("http_date", [False, True])
def test_folder_retry_after_and_cache(website, monkeypatch, http_date):
    import metadata_harvest.pxweb as implementation

    host, routes, requests = website
    routes["/pxweb/sv/db/"] = (200, table(date=""), {})
    routes["/api/v1/sv/db?filter=*&query=*"] = (200, "[]", {})
    retry_after = (
        format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
        if http_date
        else "7"
    )
    replies = iter(
        [
            (429, "Rate limited", {"Retry-After": retry_after}),
            (200, json.dumps([dict(id="t.px", type="t", updated="2026-10-06")]), {}),
        ]
    )
    routes["/api/v1/sv/db/"] = lambda: next(replies)
    sleeps = []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if delay:
            sleeps.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(implementation.asyncio, "sleep", sleep)
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert rows[0]["published"] == "2026-10-06"
    assert requests.count("/api/v1/sv/db/") == 2
    assert requests.count("/api/v1/sv/db?filter=*&query=*") == 1
    assert len(sleeps) == 1
    assert 20 < sleeps[0] <= 30 if http_date else sleeps[0] == 7


def test_flat_rate_limit_waits_without_second_request(website, monkeypatch):
    import metadata_harvest.pxweb as implementation

    host, routes, requests = website
    routes["/pxweb/sv/db/"] = (200, table(date=""), {})
    routes["/api/v1/sv/db?filter=*&query=*"] = (
        429,
        "Rate limited",
        {"Retry-After": "7"},
    )
    sleeps = []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        if delay:
            sleeps.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(implementation.asyncio, "sleep", sleep)
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/db/"))
    assert sleeps == [7]
    assert [path for path in requests if "/api/" in path] == [
        "/api/v1/sv/db?filter=*&query=*"
    ]


def test_discovery_excludes_language_selector(website):
    host, routes, requests = website
    routes["/pxweb/sv/"] = (
        200,
        '<select class="cboSelectLanguages"><option value="sv">Swedish</option></select>'
        '<select id="database"><option value="db/">Statistics</option></select>',
        {},
    )
    routes["/pxweb/sv/db/"] = (200, table(), {})
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/"))
    assert len(rows) == 1
    assert rows[0]["database_id"] == "db"
    assert "/pxweb/sv/sv/" not in requests


@pytest.mark.parametrize("defect", ["empty", "title", "category"])
def test_discovery_skips_unusable_candidates_but_explicit_selection_fails(
    website, caplog, defect
):
    host, routes, _ = website
    routes["/pxweb/sv/"] = (
        200,
        '<a href="/pxweb/sv/bad/">Extra database</a>'
        '<a href="/pxweb/sv/db/">Usable database</a>',
        {},
    )
    invalid = {
        "empty": '<div class="AspNet-TreeView"></div>',
        "title": table(title="", route="/pxweb/sv/bad/-/bad.px/"),
        "category": table(route="/pxweb/sv/bad/unknown/bad.px/"),
    }[defect]
    # Even valid rows from an unusable candidate must not leak into the result.
    routes["/pxweb/sv/bad/"] = (
        200,
        invalid
        if defect == "empty"
        else table(route="/pxweb/sv/bad/-/good.px/") + invalid,
        {},
    )
    routes["/api/v1/sv/bad?filter=*&query=*"] = (200, "[]", {})
    routes["/pxweb/sv/db/"] = (200, table(), {})
    rows = asyncio.run(get_pxweb_tables(host + "/pxweb/sv/"))
    assert [(row["database_id"], row["id"]) for row in rows] == [("db", "t.px")]
    assert "Skipping unusable discovered database sv/bad" in caplog.text
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/bad/"))
    with pytest.raises(ExceptionGroup):
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/", database_ids=["bad"]))


def test_discovery_server_errors_are_not_skipped(website, monkeypatch):
    import metadata_harvest.pxweb as implementation

    host, routes, requests = website
    monkeypatch.setattr(implementation, "_ATTEMPTS", 1)
    routes["/pxweb/sv/"] = (
        200,
        '<a href="/pxweb/sv/bad/">Unavailable</a><a href="/pxweb/sv/db/">Usable</a>',
        {},
    )
    routes["/pxweb/sv/bad/"] = (503, "Temporarily unavailable", {})
    routes["/pxweb/sv/db/"] = (200, table(), {})
    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(get_pxweb_tables(host + "/pxweb/sv/"))
    assert "HTTP 503" in str(caught.value.exceptions[0])
    assert "/pxweb/sv/db/" in requests
