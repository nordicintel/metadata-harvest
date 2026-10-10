"""UkaClient against recorded UKÄ responses: retries, 404s, links and HEAD checks."""

import gzip

import pytest

from apps.harvest.metadata_adapters.uka.client import UkaClient, Unreachable
from tests.support.uka_support import (
    FIXTURES,
    RECORDED,
    UKA,
    FakeUka,
    export_text,
    run_with_uka,
)


@pytest.fixture
def fake() -> FakeUka:
    return FakeUka()


TOTALS = "/api/totals/67"
FILE = "/uploads/1777535369-andel-disputerade-av-fou-personal_2025_-v1.csv"
ENTRY = RECORDED["files"][FILE]
TEXT = export_text(ENTRY["file"].removesuffix(".gz"))
SIZE = len(gzip.decompress((FIXTURES / "exports" / ENTRY["file"]).read_bytes()))


def _with_client(fake, call, **options):
    """Run ``call(client)`` with a client on a routed session; return both."""
    options.setdefault("retry_delay", 0)

    async def main(session):
        client = UkaClient(session=session, **options)
        return await call(client), client

    return run_with_uka(fake, main)


def test_get_json_retries_server_errors(fake):
    fake.failures[TOTALS] = 2
    body, client = _with_client(fake, lambda c: c.totals(67))

    assert body == RECORDED["responses"][TOTALS]
    assert client.request_count == 3
    assert fake.requests == [TOTALS] * 3


@pytest.mark.parametrize(
    ("status", "attempts"), [(500, 3), (503, 3), (429, 3), (400, 1), (403, 1)]
)
def test_only_transient_errors_are_retried(fake, status, attempts):
    fake.overrides[TOTALS] = (status, "error")

    with pytest.raises(Unreachable, match=str(status)):
        _with_client(fake, lambda c: c.totals(67), attempts=3)
    assert fake.requests == [TOTALS] * attempts


def test_missing_resources(fake):
    async def call(client):
        with pytest.raises(Unreachable, match="404"):
            await client.totals(99999)
        return (
            await client.export(f"{UKA}/uploads/missing.csv"),
            await client.available(f"{UKA}/uploads/missing.csv"),
        )

    results, client = _with_client(fake, call)
    assert results == (None, False)
    assert client.downloaded_bytes == 0


def test_export_link_and_download(fake):
    async def call(client):
        link = await client.export_link(67, "2025")
        return link, await client.export(link)

    (link, export), client = _with_client(fake, call)
    assert link == UKA + FILE
    assert export.text == TEXT
    assert export.last_modified == ENTRY["last_modified"]
    assert client.downloaded_bytes == SIZE


def test_period_without_export_has_no_link(fake):
    key = "/api/totals/67?export=all&export_type=csv&year=1999"
    fake.overrides[key] = (200, {"error": "No dataset for indicator ID: 67 and year"})
    link, _ = _with_client(fake, lambda c: c.export_link(67, "1999"))
    assert link is None


def test_malformed_link_answer_is_unreachable(fake):
    key = "/api/totals/67?export=all&export_type=csv&year=2025"
    fake.overrides[key] = (200, {"unexpected": True})
    with pytest.raises(Unreachable, match="no export link"):
        _with_client(fake, lambda c: c.export_link(67, "2025"))


def test_available_uses_head_without_downloading(fake):
    result, client = _with_client(fake, lambda c: c.available(UKA + FILE))
    assert result is True
    assert fake.requests == [f"HEAD {FILE}"]
    assert client.downloaded_bytes == 0
