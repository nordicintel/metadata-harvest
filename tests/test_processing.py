"""Two independent placeholder stages with transactional proposals."""

from harvesting.processing import StageResult, process_dataset


def test_inactive_stages_do_not_mutate_or_validate():
    document = {"trusted": True}
    result, outcomes = process_dataset(document)
    assert result == document
    assert [r.status for r in outcomes] == ["not_configured", "not_configured"]


def test_stage_order_and_failed_proposal_isolation(monkeypatch):
    import harvesting.processing as processing

    monkeypatch.setattr(processing, "_validate", lambda before, after: None)

    def content(document):
        return StageResult(
            "completed", {"/label": "added"}, [{"url": "https://example.org"}]
        )

    def semantic(document):
        assert document["label"] == "added"
        raise ValueError("one bad assignment")

    result, outcomes = process_dataset({"label": "source"}, stages=(content, semantic))
    assert result["label"] == "added"
    assert outcomes[-1].status == "failed"
    assert "one bad assignment" in outcomes[-1].errors[0]


def test_rejected_proposal_retains_input(monkeypatch):
    import harvesting.processing as processing

    def reject(before, after):
        raise ValueError("Invalid replacement")

    monkeypatch.setattr(processing, "_validate", reject)
    result, outcomes = process_dataset(
        {"label": "source"},
        stages=(lambda d: StageResult("completed", {"/label": None}),),
    )
    assert result == {"label": "source"}
    assert outcomes[0].status == "failed"


def test_real_proposals_validate_and_protect_source_contract():
    from harvesting.mapping import map_metadata
    from tests.test_pxweb_v2 import metadata, record

    document = map_metadata(metadata(), record(), "pxweb_v2")

    def good(document):
        return StageResult(
            "completed",
            {"/extension/nordicintel/description": "Additional description"},
        )

    result, outcomes = process_dataset(document, stages=(good,))
    assert outcomes[0].status == "completed"
    assert result["extension"]["nordicintel"]["description"] == "Additional description"
    for changes in (
        {"/value": [1]},
        {"/extension/nordicintel/dataset_code": "changed"},
        {"/dimension/Region/category/index": {"different": 0}},
        {"/extension/pxweb_v2/source_record": {}},
    ):
        result, outcomes = process_dataset(
            document,
            stages=(lambda d, changes=changes: StageResult("completed", changes),),
        )
        assert result == document
        assert outcomes[0].status == "failed"
