"""Service bindings for tests, without a configuration directory."""

from registry import ServiceBinding


def binding(
    adapter: str,
    code: str,
    endpoint: str = "https://example.test/api",
    *,
    label: str | None = None,
    **options,
) -> ServiceBinding:
    return ServiceBinding(
        id=f"urn:nordicintel:service:{code}:{adapter}:test",
        adapter=adapter,
        source_code=code,
        endpoint_url=endpoint,
        publisher=None,
        order=0,
        options=options,
        label=label or code,
    )
