"""Harvest authoritative PXWeb listings with bounded API enrichment."""

import asyncio
import calendar
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from itertools import product
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from playwright.async_api import Browser, Error, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)
_TIMEOUT = 60000
_ATTEMPTS = 4
_API = re.compile(r"/api/v1(?:/|$)", re.I)
_COLLAPSED = '.AspNet-TreeView-Expand, [role="tree"] [aria-expanded="false"]'


class _Unsupported(ValueError):
    """An explicit website message confirms an unavailable combination."""


class _UnusableDatabase(ValueError):
    """A candidate has no usable listing or lacks required descriptive metadata."""


class _ExpansionFailure(RuntimeError):
    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.retry_after = retry_after


async def _unsupported(page: Page):
    message = await page.locator("body").inner_text()
    if re.search(
        r"(?:database|language) (?:is not supported|not found|does not exist|is unavailable)"
        r"|(?:databasen|språket) (?:finns inte|stöds inte)",
        message,
        re.I,
    ):
        raise _Unsupported(f"Unsupported combination at {page.url}: {message[:300]}")


@dataclass(frozen=True)
class _Route:
    root: str
    language: str | None = None
    database: str | None = None
    tail: tuple[str, ...] = ()
    recognized: bool = True

    def web(self, language=None, database=None):
        parts = [language or self.language, database or self.database]
        return (
            self.root.rstrip("/")
            + "/"
            + "/".join(quote(part, safe="") for part in parts if part is not None)
            + ("/" if any(parts) else "")
        )


def _parse_url(url: str) -> _Route:
    if not isinstance(url, str):
        raise ValueError("url must be an absolute HTTP(S) URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be an absolute HTTP(S) URL")
    segments = [part for part in parsed.path.split("/") if part]
    api = next(
        (
            i
            for i in range(len(segments) - 1)
            if segments[i].lower() == "api" and segments[i + 1].lower() == "v1"
        ),
        None,
    )
    if api is not None:
        boundary, offset = api, 2
        root_parts = segments[:api] + ["pxweb"]
    else:
        candidates = [i for i, part in enumerate(segments) if part.lower() == "pxweb"]
        if not candidates:
            return _Route(
                urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
                recognized=False,
            )
        language_routes = [
            i
            for i in candidates
            if i + 1 < len(segments) and re.fullmatch("[a-zA-Z]{2}", segments[i + 1])
        ]
        boundary, offset = (
            (language_routes[0] if language_routes else candidates[-1]),
            1,
        )
        root_parts = segments[: boundary + 1]
    rest = [unquote(part) for part in segments[boundary + offset :]]
    language = (
        rest.pop(0).lower() if rest and re.fullmatch("[a-zA-Z]{2}", rest[0]) else None
    )
    database = rest.pop(0) if language and rest else None
    root = urlunsplit(
        (parsed.scheme, parsed.netloc, "/" + "/".join(root_parts), "", "")
    )
    return _Route(root, language, database, tuple(rest))


def _languages(route: _Route, languages: list[str] | None) -> list[str]:
    values = ["en", "sv"] if languages is None else languages
    if not isinstance(values, list) or any(
        not isinstance(x, str) or not re.fullmatch("[a-zA-Z]{2}", x) for x in values
    ):
        raise ValueError("languages must contain two-letter language codes")
    return (
        [route.language]
        if route.language
        else list(dict.fromkeys(x.lower() for x in values))
    )


def _databases(route: _Route, database_ids: list[str] | None) -> list[str] | None:
    if database_ids is not None and (
        not isinstance(database_ids, list)
        or any(
            not isinstance(x, str) or not x or "/" in unquote(x) for x in database_ids
        )
    ):
        raise ValueError("database_ids must contain nonempty database identifiers")
    if route.database is None and database_ids is None:
        return None
    return list(
        dict.fromkeys(
            ([route.database] if route.database else [])
            + [unquote(x) for x in database_ids or []]
        )
    )


def _numeric_date_format(values: list[str]) -> str | None:
    formats = set()
    for value in values:
        match = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/\d{4}(?!\d)", value)
        if match:
            first, second = map(int, match.groups())
            if first > 12:
                formats.add("%d/%m/%Y")
            if second > 12:
                formats.add("%m/%d/%Y")
    if len(formats) > 1:
        raise ValueError("Inconsistent numeric date formats in website listing")
    return next(iter(formats), None)


def _date(value: str, date_format: str | None = None) -> str:
    value = value.strip()
    # Website metadata labels are removed, never guessed from arbitrary body text.
    value = re.sub(
        r"^(?:Updated|Uppdaterad|Published|Publicerad|Modified|Modifierad)\s*:\s*",
        "",
        value,
        flags=re.I,
    )
    # Both US and British forms occur in English websites. Resolve ambiguous
    # values only using unambiguous dates from the same listing, not language.
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", value):
        date_format = date_format or _numeric_date_format([value])
        if date_format:
            return datetime.strptime(value, date_format).date().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        datetime.strptime(value, "%Y-%m-%d")
        return value
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}", value):
        datetime.strptime(value, "%Y-%m")
        return value
    if re.fullmatch(r"\d{4}", value):
        return value
    named_month = re.fullmatch(r"([A-Za-zåäöÅÄÖ]+)\s+(\d{4})", value)
    if named_month:
        months = {
            name.lower(): index
            for index, name in enumerate(calendar.month_name)
            if name
        }
        months.update(
            {
                name: index
                for index, name in enumerate(
                    "januari februari mars april maj juni juli augusti september oktober november december".split(),
                    1,
                )
            }
        )
        month = months.get(named_month[1].lower())
        if month:
            return f"{named_month[2]}-{month:02}"
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?",
        value,
    ):
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.replace(" ", "T")
    raise ValueError(f"Unparseable table date: {value!r}")


def _retry_after(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(
                    0,
                    (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds(),
                )
            except (ValueError, TypeError):
                pass
    return 0.75 * 2**attempt


async def _context(browser: Browser, insecure=False):
    context = await browser.new_context(
        service_workers="block", ignore_https_errors=insecure
    )

    context._pxweb_pages = set()

    async def intercept(route):
        if _API.search(unquote(urlsplit(route.request.url).path)):
            logger.debug("Blocked PXWeb API request: %s", route.request.url)
            await route.abort()
            return
        try:
            frame = route.request.frame
            allowed = (
                frame.page in context._pxweb_pages and frame == frame.page.main_frame
            )
        except Error:
            allowed = False
        if not allowed:
            # Harvesting navigates observed links itself. Popups and subframes
            # cannot inherit the main target's Chromium redirect denylist.
            logger.debug("Blocked unsolicited page request: %s", route.request.url)
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", intercept)
    context.set_default_timeout(_TIMEOUT)
    return context


async def _network_guard(context, page):
    session = await context.new_cdp_session(page)
    await session.send("Network.enable")
    markers = ["".join(chars) for chars in product("aA", "pP", "iI")]
    await session.send(
        "Network.setBlockedURLs",
        {
            "urls": [
                f"*/{marker}/{version}1*"
                for marker in markers
                for version in ("v", "V")
            ]
        },
    )


async def _new_page(context):
    page = await context.new_page()
    await _network_guard(context, page)
    context._pxweb_pages.add(page)
    return page


async def _goto(page: Page, url: str):
    if _API.search(urlsplit(url).path):
        raise ValueError(f"Refusing API navigation: {url}")
    for attempt in range(_ATTEMPTS):
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=_TIMEOUT
            )
            if response is None:
                raise RuntimeError(f"No navigation response: {url}")
            if response.status in {429, 500, 502, 503, 504}:
                if attempt == _ATTEMPTS - 1:
                    raise RuntimeError(f"HTTP {response.status}: {url}")
                await asyncio.sleep(
                    _retry_after(response.headers.get("retry-after"), attempt)
                )
                continue
            if response.status >= 400:
                if response.status in {400, 404}:
                    await _unsupported(page)
                raise RuntimeError(f"HTTP {response.status}: {url}")
            return
        except Error as error:
            if "CERT_" in str(error) or attempt == _ATTEMPTS - 1:
                raise
            await asyncio.sleep(_retry_after(None, attempt))


async def _entry(page: Page, route: _Route) -> _Route:
    if route.recognized and route.language:
        return route
    await _goto(page, route.web() if route.recognized else route.root)
    actual = _parse_url(page.url)
    if actual.recognized and actual.language:
        return _Route(actual.root)
    links = await page.locator("a[href]").evaluate_all("(els)=>els.map(e=>e.href)")
    candidates = [
        _parse_url(x)
        for x in links
        if x.startswith(("http://", "https://")) and not _API.search(urlsplit(x).path)
    ]
    candidates = [x for x in candidates if x.recognized]
    roots = {x.root for x in candidates}
    if not roots and actual.recognized:
        return _Route(actual.root)
    if len(roots) != 1:
        raise ValueError(
            f"Cannot identify an unambiguous PXWeb entry point: {page.url}"
        )
    return _Route(candidates[0].root)


async def _check_language(page: Page, language: str, database: str | None = None):
    await _unsupported(page)
    route = _parse_url(page.url)
    if route.language != language:
        raise RuntimeError(
            f"Language redirect: requested {language}, received {route.language}: {page.url}"
        )
    if database is not None and route.database != database:
        raise RuntimeError(
            f"Database redirect: requested {database}, received {route.database}: {page.url}"
        )
    declared = await page.locator("html").get_attribute("lang")
    if declared and declared.lower().split("-")[0] != language:
        raise RuntimeError(
            f"Content language mismatch: requested {language}, HTML declares {declared}: {page.url}"
        )
    return route


async def _discover(page: Page, root: _Route, language: str) -> list[str]:
    await _goto(page, root.web(language))
    actual = await _check_language(page, language)
    if actual.database:
        return [actual.database]
    links = await page.locator("a[href], select option[value]").evaluate_all(
        """els=>els.filter(e=>!e.closest('select.cboSelectLanguages,select[id*="language" i],select[name*="language" i]')).map(e=>e.href || new URL(e.value,location.href).href)"""
    )
    databases = []
    for link in links:
        if not link.startswith(("https://", "http://")):
            continue
        candidate = _parse_url(link)
        if (
            candidate.root.lower() == actual.root.lower()
            and candidate.language == language
            and candidate.database
        ):
            databases.append(candidate.database)
    if not databases:
        raise RuntimeError(
            f"No database links or supported selection controls: {page.url}"
        )
    return list(dict.fromkeys(databases))


# Extract only data belonging to each table row/node; never borrow page-wide dates.
_DOM = r"""() => {
 const text=e=>(e?.textContent||'').replace(/\s+/g,' ').trim();
 const own=e=> {const c=e.cloneNode(true); c.querySelectorAll('ul,ol,[role=group],img,button').forEach(x=>x.remove()); return text(c).replace(/^[»+−]\s*/, '');};
 const links=[...document.querySelectorAll('a[href]')];
 return links.filter(a=>(!a.closest('.tablelist_li') || a.matches('.tablelist_linkHeading')) && (/\.px\/?$/i.test(new URL(a.href).pathname) || a.querySelector('.tableofcontent_link') || a.matches('[data-table-id]') || a.closest('.AspNet-TreeView-Leaf'))).map(a=> {
  const node=a.closest('li,tr,[role=treeitem],.table-item') || a.parentElement;
  const machine=node.querySelector('time[datetime],[data-updated],[data-published]');
  const updated=node.querySelector('img[src*="fileUpdated" i]');
  const modified=node.querySelector('img[src*="fileModified" i]');
  const displayed=a.querySelector('.tableofcontent_link > em');
  const listDate=node.matches('.tablelist_li') ? node.textContent.match(/(?:Uppdaterad|Updated)\s*:\s*([^\r\n]+)/i)?.[1]?.trim() : '';
  const date=machine?.getAttribute('datetime')||machine?.getAttribute('data-updated')||machine?.getAttribute('data-published')||updated?.getAttribute('title')||updated?.getAttribute('alt')||modified?.getAttribute('title')||(displayed ? text(displayed).replace(/^\[|\]$/g,'') : '')||listDate||text(node.querySelector('.updated,.published,.date,td:last-child:not(:first-child)'));
  const label=(a.querySelector('.tableofcontent_link')||a).cloneNode(true);
  if(displayed) label.querySelectorAll('em').forEach(e=>e.remove());
  const ancestors=[];
  let parent=node.parentElement?.closest('li,[role=treeitem]');
  while(parent){ancestors.unshift({name:own(parent),href:parent.querySelector(':scope > a')?.href||''});parent=parent.parentElement?.closest('li,[role=treeitem]');}
  return {href:a.href,title:text(label),date,ancestors,machineDate:!!machine};
 });
}"""


def _identity(raw: dict, language: str, database: str, folder_names: dict):
    route = _parse_url(raw["href"])
    if route.language != language or route.database != database or not route.tail:
        raise ValueError(f"Table route mismatch: {raw['href']}")
    table_id = route.tail[-1]
    tokens = tuple(x for segment in route.tail[:-1] for x in segment.split("__") if x)
    ancestors = raw["ancestors"]
    # ASP.NET routes contain a synthetic root preceding the actual ancestry.
    if ancestors:
        if len(tokens) < len(ancestors):
            raise ValueError(f"Category ancestry mismatch: {raw['href']}")
        categories = tokens[-len(ancestors) :]
        names = [x["name"] for x in ancestors]
        # Some trees include their database root as an ancestor as well as in
        # the flattened routing prefix. It is not an API category segment.
        if (
            "__" in route.tail[0]
            and len(categories) == len(tokens)
            and categories[0] in {database, "START"}
        ):
            categories, names = categories[1:], names[1:]
    else:
        categories = tokens
        if categories == ("-",):
            categories = ()
        elif (
            route.tail
            and "__" in route.tail[0]
            and categories[0] in {database, "START"}
        ):
            categories = categories[1:]
        names = [
            folder_names.get(categories[: i + 1], "") for i in range(len(categories))
        ]
    if urlsplit(raw["href"]).hostname == "statistik.linkoping.se":
        names = [
            name or category for name, category in zip(names, categories, strict=True)
        ]
    return table_id, categories, names


def _record(
    raw: dict, root: _Route, language: str, database: str, folder_names: dict
) -> dict:
    table_id, categories, names = _identity(raw, language, database, folder_names)
    if not raw["title"] or any(not name for name in names):
        raise _UnusableDatabase(f"Missing title/category name: {raw['href']}")
    api_root = root.root.rsplit("/", 1)[0] + "/api/v1"
    return {
        "id": table_id,
        "path": "/" + "/".join(categories),
        "category_path": [
            {"id": key, "name": name}
            for key, name in zip(categories, names, strict=True)
        ],
        "title": raw["title"],
        "score": 1.0,
        "published": _date(raw["date"], raw.get("date_format")),
        "api_url": api_root
        + "/"
        + "/".join(
            quote(x, safe="") for x in (language, database, *categories, table_id)
        ),
        "web_url": raw["href"],
        "language": language,
        "database_id": database,
    }


async def _activate(page: Page, selector: str):
    element = page.locator(selector).first
    postback = await element.evaluate(
        "e=>(e.type === 'submit' && !!e.form) || (e.getAttribute('onclick')||'').includes('__doPostBack')"
    )
    if postback:
        async with page.expect_navigation(wait_until="domcontentloaded") as navigation:
            await element.evaluate("(e)=>e.click()")
        response = await navigation.value
        if response is None or response.status >= 400:
            if response is None or response.status in {429, 500, 502, 503, 504}:
                raise _ExpansionFailure(
                    f"Expansion navigation failed: {page.url}",
                    response.headers.get("retry-after") if response else None,
                )
            raise RuntimeError(f"Expansion HTTP {response.status}: {page.url}")
    else:
        signature = """()=>JSON.stringify([...document.querySelectorAll('a[href],time,[data-updated],.empty-database')].map(e=>[e.getAttribute('href'),e.textContent,e.getAttribute('datetime')]))"""
        before = await page.evaluate(signature)
        loaded = await element.evaluate(
            "e=>!!e.closest('li,[role=treeitem]')?.querySelector('ul li,ol li,[role=group] a')"
        )
        await element.evaluate("(e)=>e.click()")
        if not loaded:
            await page.wait_for_function(
                f"(before)=>({signature})() !== before", arg=before
            )
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_function(
        """()=>!document.querySelector('[aria-busy="true"],.loading:not([style*="display: none"])')"""
    )


async def _harvest(
    page: Page, root: _Route, language: str, database: str
) -> list[dict]:
    pending = [root.web(language, database)]
    visited = set()
    records = {}
    folder_names = {}
    all_tables = []
    collection = getattr(page.context, "_pxweb_collection", None)
    if collection is not None:
        collection.update(raw_tables=all_tables, folder_names=folder_names, root=root)
    while pending:
        url = pending.pop(0)
        if url in visited:
            continue
        if len(visited) >= 10000:
            raise RuntimeError("Folder traversal exceeded 10000 pages")
        await _goto(page, url)
        actual = await _check_language(page, language, database)
        root = _Route(actual.root)
        if collection is not None:
            collection["root"] = root
        visited.add(url)
        if (
            await page.locator(_COLLAPSED).count()
            and await page.locator(".tableofcontent_action").count()
        ):
            try:
                await _activate(page, ".tableofcontent_action")
            except PlaywrightTimeoutError:
                logger.warning(
                    "Full-tree control ineffective at %s; reloading and expanding branches",
                    url,
                )
                await _goto(page, url)
                await _check_language(page, language, database)
        states = set()
        while await page.locator(_COLLAPSED).count():
            state = await page.locator(_COLLAPSED).evaluate_all(
                '(els)=>els.map(e=>e.outerHTML).join("")'
            )
            if state in states:
                raise RuntimeError(f"Incomplete expansion: {page.url}")
            states.add(state)
            try:
                await _activate(page, _COLLAPSED)
            except PlaywrightTimeoutError as error:
                raise _ExpansionFailure(f"Expansion stalled at {page.url}") from error
        await _check_language(page, language, database)
        raw_tables, links = [], []
        pagination_states = set()
        while True:
            current = await page.evaluate(_DOM)
            if collection is not None:
                # Capture visible rows before a later page/expansion can fail.
                all_tables.extend(current)
            signature = tuple((row["href"], row["date"]) for row in current)
            if signature in pagination_states:
                raise RuntimeError(f"Pagination stopped making progress: {page.url}")
            pagination_states.add(signature)
            raw_tables.extend(current)
            links.extend(
                await page.locator("a[href]").evaluate_all(
                    '(els)=>els.map(e=>({href:e.href,name:e.textContent.trim(),next:e.rel==="next" || !!e.closest(".pagination,[role=navigation][aria-label*=pagination i]")}))'
                )
            )
            next_control = 'button[rel="next"],button[aria-label="Next page"],button[aria-label="Nästa sida"],[data-next-page]'
            next_button = page.locator(next_control).first
            if (
                not await next_button.count()
                or not await next_button.is_enabled()
                or await next_button.get_attribute("aria-disabled") == "true"
            ):
                unsupported = page.locator(
                    '.pagination button:not(:disabled):not([aria-disabled="true"]):not([aria-current])'
                )
                if await unsupported.count():
                    raise RuntimeError(f"Unresolved pagination controls: {page.url}")
                break
            await _activate(page, next_control)
            await _check_language(page, language, database)
        date_format = _numeric_date_format([raw["date"] for raw in raw_tables])
        for link in links:
            if not link["href"].startswith(("https://", "http://")):
                continue
            candidate = _parse_url(link["href"])
            if any(raw["href"] == link["href"] for raw in raw_tables):
                continue
            if (
                candidate.root.lower() != root.root.lower()
                or candidate.language != language
                or candidate.database != database
            ):
                continue
            if candidate.tail and not candidate.tail[-1].lower().endswith(".px"):
                tokens = tuple(
                    x for segment in candidate.tail for x in segment.split("__")
                )
                if (
                    candidate.tail
                    and "__" in candidate.tail[0]
                    and tokens[0] in {database, "START"}
                ):
                    tokens = tokens[1:]
                folder_names[tokens] = link["name"]
                # Fully expanded trees already include these folder listings.
                represented = any(
                    link["href"] == ancestor["href"]
                    for raw in raw_tables
                    for ancestor in raw["ancestors"]
                )
                if not represented:
                    pending.append(link["href"])
            if link["next"]:
                pending.append(link["href"])
        if not raw_tables and not any(x not in visited for x in pending):
            body = await page.locator("body").inner_text()
            if not re.search(
                r"no tables|inga tabeller|database is empty|databasen är tom",
                body,
                re.I,
            ):
                raise _UnusableDatabase(f"No usable database listing: {page.url}")
        for raw in raw_tables:
            raw["date_format"] = date_format
        if collection is None:
            all_tables.extend(raw_tables)
    if collection is not None:
        collection["complete"] = True
    try:
        await _enrich_missing(page, root, language, database, all_tables, folder_names)
    except Exception as error:
        if collection is None:
            raise
        collection["complete"] = False
        collection["errors"].append({"stage": "enrichment", "message": str(error)})
    if collection is not None:
        return _partial_records(collection, language, database)
    for raw in all_tables:
        if not raw["date"]:
            table_id, categories, names = _identity(
                raw, language, database, folder_names
            )
            if not raw["title"] or any(not name for name in names):
                raise _UnusableDatabase(f"Missing title/category name: {raw['href']}")
            omission = dict(
                language=language,
                database_id=database,
                path="/" + "/".join(categories),
                id=table_id,
            )
            page.context._pxweb_api.setdefault("omitted", []).append(omission)
            logger.warning(
                "Omitting undated web table: %s",
                omission,
                extra={"omitted_table": omission},
            )
            continue
        record = _record(raw, root, language, database, folder_names)
        key = (
            record["language"],
            record["database_id"],
            record["path"],
            record["id"],
        )
        if key in records and records[key] != record:
            existing = records[key]
            if {k: v for k, v in existing.items() if k != "web_url"} != {
                k: v for k, v in record.items() if k != "web_url"
            }:
                raise ValueError(f"Conflicting duplicate table: {key}")
            record["web_url"] = min(existing["web_url"], record["web_url"])
        records[key] = record
    return list(records.values())


def _partial_records(collection, language, database):
    """Extract independent rows while preserving observed-but-invalid identities."""
    records = {}
    for raw in collection.get("raw_tables", []):
        table_id = None
        try:
            route = _parse_url(raw["href"])
            if route.language == language and route.database == database and route.tail:
                table_id = route.tail[-1]
                collection["observed_ids"].add(table_id)
            record = _record(
                raw, collection["root"], language, database, collection["folder_names"]
            )
            key = (record["path"], record["id"])
            if key in records and any(
                records[key][field] != record[field]
                for field in record
                if field != "web_url"
            ):
                raise ValueError(f"Conflicting table listing: {table_id}")
            records[key] = record
        except Exception as error:
            if table_id:
                collection["invalid_ids"].add(table_id)
            else:
                collection["complete"] = False
            issue = {
                "stage": "extraction",
                "id": table_id,
                "url": raw.get("href"),
                "message": str(error),
            }
            if issue not in collection["errors"]:
                collection["errors"].append(issue)
    return sorted(records.values(), key=lambda record: (record["path"], record["id"]))


async def _collect_database(browser, url, language, database):
    """Workflow-only collection; public get_pxweb_tables stays strict."""
    collection = {
        "complete": False,
        "observed_ids": set(),
        "invalid_ids": set(),
        "errors": [],
    }
    api_state = {}

    async def harvest(page):
        page.context._pxweb_api = api_state
        page.context._pxweb_collection = collection
        root = await _entry(page, _parse_url(url))
        return await _complete_harvest(page, root, language, database)

    try:
        async with asyncio.timeout(1800):
            records = await _in_context(browser, harvest)
    except Exception as error:
        collection["complete"] = False
        collection["errors"].append(
            {"stage": "navigation", "message": f"{type(error).__name__}: {error}"}
        )
        records = _partial_records(collection, language, database)
    return {
        "language": language,
        "database_id": database,
        "complete": collection["complete"],
        "observed_ids": sorted(collection["observed_ids"]),
        "invalid_ids": sorted(collection["invalid_ids"]),
        "errors": collection["errors"],
        "records": records,
    }


async def _api_listing(
    page: Page, root: _Route, language: str, database: str
) -> list[dict]:
    state = page.context._pxweb_api
    if state.get("attempted"):
        if state.get("error"):
            raise state["error"]
        return state["rows"]
    state["attempted"] = True
    url = (
        root.root.rsplit("/", 1)[0]
        + "/api/v1/"
        + "/".join(quote(x, safe="") for x in (language, database))
        + "?filter=*&query=*"
    )
    try:
        response = await page.context.request.get(
            url, timeout=_TIMEOUT, max_redirects=0
        )
        try:
            if response.status == 429:
                delay = _retry_after(response.headers.get("retry-after"), 0)
                logger.warning(
                    "API flat listing rate-limited; waiting %s seconds before failing: %s",
                    delay,
                    url,
                )
                await asyncio.sleep(delay)
            if not response.ok:
                raise RuntimeError(
                    f"Single API enrichment request failed: HTTP {response.status}: {url}"
                )
            rows = await response.json()
            if not isinstance(rows, list) or not all(
                isinstance(row, dict) for row in rows
            ):
                raise ValueError(f"API search returned no table listing: {url}")
            state["rows"] = rows
            return rows
        finally:
            await response.dispose()
    except Exception as error:
        state["error"] = error
        raise


async def _api_folder(page, root, language, database, categories):
    """Read and cache a web-derived parent folder, retrying transient failures."""
    state = page.context._pxweb_api
    cache = state.setdefault("folders", {})
    if categories in cache:
        value = cache[categories]
        if isinstance(value, Exception):
            raise value
        return value
    url = (
        root.root.rsplit("/", 1)[0]
        + "/api/v1/"
        + "/".join(quote(part, safe="") for part in (language, database, *categories))
        + "/"
    )
    try:
        for attempt in range(_ATTEMPTS):
            try:
                state["folder_request_count"] = state.get("folder_request_count", 0) + 1
                response = await page.context.request.get(
                    url, timeout=_TIMEOUT, max_redirects=0
                )
            except Error as error:
                if "CERT_" in str(error) or attempt == _ATTEMPTS - 1:
                    raise
                await asyncio.sleep(_retry_after(None, attempt))
                continue
            try:
                if (
                    response.status in {429, 500, 502, 503, 504}
                    and attempt < _ATTEMPTS - 1
                ):
                    delay = _retry_after(response.headers.get("retry-after"), attempt)
                    logger.warning(
                        "Retrying API parent folder after HTTP %s in %s seconds: %s",
                        response.status,
                        delay,
                        url,
                    )
                    await asyncio.sleep(delay)
                    continue
                if not response.ok:
                    raise RuntimeError(
                        f"API parent-folder request failed: HTTP {response.status}: {url}"
                    )
                entries = await response.json()
                if not isinstance(entries, list) or not all(
                    isinstance(x, dict) for x in entries
                ):
                    raise ValueError(f"API parent folder returned no listing: {url}")
                cache[categories] = entries
                return entries
            finally:
                await response.dispose()
    except Exception as error:
        cache[categories] = error
        raise


def _fill_date(row, entry):
    for field in ("updated", "published"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            try:
                row["date"] = _date(value)
            except ValueError:
                continue
            row["date_format"] = None
            return


async def _enrich_missing(page, root, language, database, rows, folder_names):
    missing = []
    for row in rows:
        try:
            _date(row["date"], row.get("date_format"))
        except ValueError:
            row["date"] = ""
        if not row["date"] or not row["title"]:
            missing.append(row)
    if not missing:
        return
    entries = await _api_listing(page, root, language, database)
    index = {}
    for entry in entries:
        if not isinstance(entry.get("id"), str) or not isinstance(
            entry.get("path"), str
        ):
            continue
        key = (
            tuple(unquote(part) for part in entry["path"].split("/") if part),
            unquote(entry["id"]),
        )
        if key in index and index[key] != entry:
            raise ValueError(f"Conflicting API metadata for {key}")
        index[key] = entry
    for row in missing:
        table_id, categories, _ = _identity(row, language, database, folder_names)
        entry = index.get((categories, table_id))
        entry = entry or {}
        if not row["date"]:
            _fill_date(row, entry)
        if not row["title"] and isinstance(entry.get("title"), str):
            row["title"] = entry["title"].strip()
        if not row["date"]:
            entries = await _api_folder(page, root, language, database, categories)
            matches = [
                entry
                for entry in entries
                if entry.get("type") == "t" and entry.get("id") == table_id
            ]
            if len(matches) > 1:
                raise ValueError(
                    f"Conflicting API folder metadata for {categories}/{table_id}"
                )
            if matches:
                _fill_date(row, matches[0])


async def _in_context(browser: Browser, operation):
    for insecure in (False, True):
        context = await _context(browser, insecure)
        try:
            return await operation(await _new_page(context))
        except Error as error:
            if insecure or "CERT_" not in str(error):
                raise
            logger.warning(
                "Certificate validation failed; retrying in a new context with ignore_https_errors=True: %s",
                error,
            )
        finally:
            await context.close()


def _failure(scope: str, error: Exception) -> RuntimeError:
    failure = RuntimeError(f"{scope}: {error}")
    failure.__cause__ = error
    return failure


async def _complete_harvest(page: Page, root: _Route, language: str, database: str):
    for attempt in range(_ATTEMPTS):
        try:
            return await _harvest(page, root, language, database)
        except _ExpansionFailure as error:
            if attempt == _ATTEMPTS - 1:
                raise
            logger.warning(
                "Restarting failed stateful expansion for %s/%s: %s",
                language,
                database,
                error,
            )
            await asyncio.sleep(_retry_after(error.retry_after, attempt))
            # _harvest navigates afresh and rebuilds every branch from clean DOM.


async def get_pxweb_tables(
    url: str, languages: list[str] | None = None, database_ids: list[str] | None = None
) -> list[dict]:
    """Return complete DOM-derived table records from a PXWeb installation.

    URL languages override the language list. URL databases are combined with
    supplied IDs; only None without a URL database requests website discovery.
    Discovery skips candidates with no usable listing or missing titles/category
    labels, with diagnostics. Explicit selections remain strict. Unsupported
    combinations are logged and skipped; other failures raise ExceptionGroup.
    Website listings determine membership, paths, titles and available dates.
    At most one flat API listing per language/database fills missing fields,
    matching both table ID and full path. API-only entries are never added.
    Still-undated web tables use their API parent folders as a last resort,
    with one cached lookup per folder and bounded transient retries. Remaining undated tables are omitted
    with diagnostics identifying their language, database, path and ID.
    The function never writes output files or visits individual table pages.
    """
    route = _parse_url(url)
    selected_languages = _languages(route, languages)
    selected_databases = _databases(route, database_ids)
    if not selected_languages or selected_databases == []:
        return []
    results, failures = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for language in selected_languages:

                async def discover(page, language=language):
                    root = await _entry(page, route)
                    databases = selected_databases
                    if databases is None:
                        databases = await _discover(page, root, language)
                    return root, databases

                try:
                    root, databases = await _in_context(browser, discover)
                except _Unsupported as error:
                    logger.warning("%s", error)
                    continue
                except Exception as error:
                    failures.append(_failure(f"{language} discovery", error))
                    continue
                for database in databases:
                    api_state = {}

                    async def harvest(
                        page,
                        root=root,
                        language=language,
                        database=database,
                        api_state=api_state,
                    ):
                        page.context._pxweb_api = api_state
                        return await _complete_harvest(page, root, language, database)

                    try:
                        results.extend(await _in_context(browser, harvest))
                    except _Unsupported as error:
                        logger.warning("%s", error)
                    except _UnusableDatabase as error:
                        if selected_databases is None:
                            logger.warning(
                                "Skipping unusable discovered database %s/%s: %s",
                                language,
                                database,
                                error,
                                extra={
                                    "skipped_database": {
                                        "language": language,
                                        "database_id": database,
                                        "reason": str(error),
                                    }
                                },
                            )
                        else:
                            failures.append(_failure(f"{language}/{database}", error))
                    except Exception as error:
                        failures.append(_failure(f"{language}/{database}", error))
        finally:
            await browser.close()
    if failures:
        raise ExceptionGroup("Incomplete PXWeb harvest", failures)
    return sorted(
        results, key=lambda x: (x["language"], x["database_id"], x["path"], x["id"])
    )
