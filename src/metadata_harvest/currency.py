"""Publication rule shared by adapters that withhold unavailable datasets.

A dataset is published only while its value endpoint works in the current run and
its accepted document was verified (rebuilt, or confirmed unchanged against fresh
source signals) within the configured maximum age. Otherwise the last accepted
document is withheld in adapter state and restored automatically once both hold.
"""

from datetime import datetime, timedelta


def settle(
    keys,
    published: dict,
    withheld: dict,
    records: dict,
    working: dict,
    now: datetime,
    max_age: timedelta,
    counts: dict,
) -> None:
    """Place each key's accepted document in ``published`` or ``withheld``.

    ``records`` holds per-key state with ``verified_at``; ``working`` whether the
    key's endpoint answered in this run (absent means it did not).
    """
    for key in keys:
        info = records[key]
        was_withheld = key in withheld
        document = published.pop(key, None) or withheld.pop(key, None)
        if document is None:
            info["status"] = "missing"
            continue
        verified = info.get("verified_at")
        fresh = bool(verified) and now - datetime.fromisoformat(verified) <= max_age
        if working.get(key) and fresh:
            published[key] = document
            info["status"] = "published"
            info.pop("withheld_reason", None)
            counts["restored"] += was_withheld
        else:
            withheld[key] = document
            info["status"] = "withheld"
            info["withheld_reason"] = (
                "endpoint unavailable" if not working.get(key) else "stale"
            )
            counts["withheld"] += not was_withheld
