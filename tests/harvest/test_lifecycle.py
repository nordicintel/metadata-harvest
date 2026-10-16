"""The shared lifecycle, exercised once with a toy adapter."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from apps.harvest.source_store import SourceStore
from harvest import lifecycle
from harvest.lifecycle import (
    Adapter,
    Dropped,
    Scope,
    Stored,
    Task,
    Unreachable,
)
from tests.support.binding import binding

NOW = datetime(2026, 10, 16, 6, tzinfo=UTC)
BINDING = binding("toy", "toy", languages=["sv"], retry_cooldown_seconds=0)


class Source:
    """A provider: a listing of items, some of which may be down or broken."""

    def __init__(self, items, *, down=(), broken=(), dropped=(), listing_down=False):
        self.items = dict(items)
        self.down, self.broken, self.dropped = set(down), set(broken), set(dropped)
        self.listing_down = listing_down
        self.calls = []
        self.closed = False

    async def listing(self):
        self.calls.append("listing")
        if self.listing_down:
            raise Unreachable("listing down")
        return sorted(self.items)

    async def item(self, code):
        self.calls.append(code)
        if code in self.down:
            self.down.discard(code)  # the retry succeeds
            raise Unreachable(f"{code} down")
        if code in self.broken:
            raise ValueError(f"{code} broken")
        return self.items[code]

    async def close(self):
        self.closed = True


async def discover(run):
    async def load():
        codes = await run.client.listing()
        run.store.put_shared("toy", "listing.json", codes, retrieved_at=run.stamp)
        wanted = run.selection.get("subjects")
        return [
            Task(f"sv/{code}", code)
            for code in codes
            if wanted is None or code in wanted
        ]

    return [Scope("listing", load)]


async def fetch(run, task):
    if task.item in run.client.dropped:
        return Dropped("skipped")
    body = await run.client.item(task.item)
    return Stored(responses={"body": body}, info={"size": len(body)})


FORGOTTEN = []

TOY = Adapter(
    name="toy",
    provider_code="toy",
    workflow_file="harvest-toy.yml",
    client=lambda binding: Source({}),
    discover=discover,
    fetch=fetch,
    options={"subject": "subjects"},
    counts=("skipped",),
    forget=lambda run, key: FORGOTTEN.append(key),
)


def execute(source, data, output, *, clock=NOW, **selection):
    return asyncio.run(
        lifecycle.run(
            TOY, BINDING, data, output, selection=selection, client=source, clock=clock
        )
    )


def harvest(tmp_path, source, name="out", **options):
    """Run against the published data, then collect the output into it."""
    data = tmp_path / "data"
    result = execute(source, data, tmp_path / name, **options)
    lifecycle.collect(TOY, data, tmp_path / name)
    return result


def entries(tmp_path):
    return SourceStore(tmp_path / "data/source", "toy").entries()


def records(tmp_path):
    return lifecycle.load_state(tmp_path / "data", "datasets")["datasets"]


def test_first_run_stores_every_listed_item_and_collect_applies_it(tmp_path):
    source = Source({"a": "A", "b": "B"})
    result = harvest(tmp_path, source)
    assert result["status"] == "success"
    assert result["counts"]["rebuilt"] == 2 and result["published"] == {"sv": 2}
    assert set(entries(tmp_path)) == {"toy/a/sv", "toy/b/sv"}
    assert records(tmp_path)["sv/a"] == {
        "checked_at": NOW.isoformat(),
        "verified_at": NOW.isoformat(),
        "error": None,
        "size": 1,
        "status": "published",
    }
    assert (tmp_path / "data/source/toy/_shared/listing.json").exists()
    assert source.closed is False  # the caller's client is the caller's to close


def test_second_run_copies_the_baseline_and_keeps_unchanged_bytes(tmp_path):
    harvest(tmp_path, Source({"a": "A", "b": "B"}))
    before = (tmp_path / "data/source/toy/a.sv.json").read_bytes()
    result = harvest(tmp_path, Source({"a": "A", "b": "BB"}), "second")
    assert result["counts"]["unchanged"] == 1 and result["counts"]["rebuilt"] == 1
    assert (tmp_path / "data/source/toy/a.sv.json").read_bytes() == before
    assert result["base_manifest"] and set(result["manifest"]) >= {
        lifecycle.STATE,
        lifecycle.REPORT,
    }


def test_endpoint_failure_is_retried_once_after_the_cooldown(tmp_path):
    source = Source({"a": "A"}, down={"a"})
    result = harvest(tmp_path, source)
    assert source.calls == ["listing", "a", "a"]
    assert result["status"] == "success" and result["counts"]["rebuilt"] == 1


def test_metadata_failure_is_not_retried_and_keeps_the_previous_item(tmp_path):
    harvest(tmp_path, Source({"a": "A"}))
    source = Source({"a": "A"}, broken={"a"})
    result = harvest(tmp_path, source, "second")
    assert source.calls == ["listing", "a"]
    assert result["status"] == "completed_with_errors"
    assert result["errors"] == [
        {"stage": "metadata", "resource": "sv/a", "error": "ValueError: a broken"}
    ]
    assert records(tmp_path)["sv/a"]["status"] == "published"
    assert "toy/a/sv" in entries(tmp_path)


def test_unreachable_item_is_withheld_and_restored_when_it_answers(tmp_path):
    harvest(tmp_path, Source({"a": "A"}))

    class Down(Source):
        async def item(self, code):
            raise Unreachable("still down")

    result = harvest(tmp_path, Down({"a": "A"}), "second")
    assert result["withheld"] == ["sv/a"] and result["counts"]["withheld"] == 1
    assert records(tmp_path)["sv/a"]["withheld_reason"] == "endpoint unavailable"
    assert "toy/a/sv" in entries(tmp_path)  # files stay while withheld
    result = harvest(tmp_path, Source({"a": "A"}), "third")
    assert result["withheld"] == [] and result["counts"]["restored"] == 1


def test_stale_item_is_withheld_even_when_the_endpoint_answers(tmp_path):
    harvest(tmp_path, Source({"a": "A"}))
    later = NOW + timedelta(days=90)
    result = harvest(tmp_path, Source({"a": "A"}, broken={"a"}), "second", clock=later)
    assert result["withheld"] == ["sv/a"]
    assert records(tmp_path)["sv/a"]["withheld_reason"] == "stale"


def test_unbounded_complete_listing_removes_unlisted_items(tmp_path):
    FORGOTTEN.clear()
    harvest(tmp_path, Source({"a": "A", "b": "B"}))
    result = harvest(tmp_path, Source({"a": "A"}), "second")
    assert result["counts"]["removed"] == 1
    assert set(entries(tmp_path)) == {"toy/a/sv"} and "sv/b" not in records(tmp_path)
    assert FORGOTTEN == ["sv/b"]


def test_bounded_run_touches_only_the_selected_items(tmp_path):
    harvest(tmp_path, Source({"a": "A", "b": "B"}))
    source = Source({"a": "A"})
    result = harvest(tmp_path, source, "second", subjects=["a"])
    assert source.calls == ["listing", "a"]
    assert result["counts"]["removed"] == 0 and result["selection"] == {
        "subjects": ["a"],
        "languages": ["sv"],
    }
    assert set(entries(tmp_path)) == {"toy/a/sv", "toy/b/sv"}


def test_scope_failure_keeps_its_known_items_and_is_reported(tmp_path):
    harvest(tmp_path, Source({"a": "A"}))
    result = harvest(tmp_path, Source({"a": "A"}, listing_down=True), "second")
    assert result["errors"][0]["resource"] == "*/listing"
    assert result["errors"][0]["stage"] == "endpoint"
    assert result["withheld"] == ["sv/a"] and "toy/a/sv" in entries(tmp_path)
    assert TOY.scope_failures(result) == []  # an endpoint failure is not discovery


def test_dropped_item_is_forgotten_and_counted(tmp_path):
    harvest(tmp_path, Source({"a": "A", "b": "B"}))
    result = harvest(tmp_path, Source({"a": "A", "b": "B"}, dropped={"b"}), "second")
    assert result["counts"]["skipped"] == 1
    assert set(entries(tmp_path)) == {"toy/a/sv"} and "sv/b" not in records(tmp_path)


def test_collect_refuses_a_changed_baseline_and_foreign_paths(tmp_path):
    harvest(tmp_path, Source({"a": "A"}))
    execute(Source({"a": "AA"}), tmp_path / "data", tmp_path / "second")
    (tmp_path / "data/source/toy/intruder.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="baseline changed"):
        lifecycle.collect(TOY, tmp_path / "data", tmp_path / "second")
    (tmp_path / "data/source/toy/intruder.json").unlink()
    (tmp_path / "second/notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        lifecycle.collect(TOY, tmp_path / "data", tmp_path / "second")


def test_output_inside_data_and_unconfigured_languages_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="separate"):
        execute(Source({}), tmp_path / "data", tmp_path / "data/out")
    with pytest.raises(ValueError, match="Unconfigured"):
        execute(Source({}), tmp_path / "data", tmp_path / "out", languages=["en"])
