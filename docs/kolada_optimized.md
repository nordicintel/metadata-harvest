<!-- markdownlint-disable-file -->

<!-- prettier-ignore-file -->

<!-- cspell:disable -->

<!-- ruff: noqa -->

# Kolada optimized metadata scraper

Starting assumptions (backed by prior knowledge + data)

- The earliest municiplaity data is for year 1970 - Won't change
- The earliest OU data is for year 2003 - Won't change (might go up cause deleted kpis, never down)
- KPI's only ever cover predictions for two years ahead (might change but we will handle that in a year or two then) so last year for both muni data and ou data possible is: \_current_year + 2 = 2028 (calc should be actually made using datetime or sim)\_So we have:
    - Municipality data: range[1970, 2028]

- BUT since ou data are actual reported values colleced from organizations, they can not and are never a future year so:
    - OU data: range[2003, 2026]

- We have choosen not to retrieve/cover the absolute majority of useless municipality_group values which are just derivable comparison values. The exception being 40 or so which are usefull groupings/categorizations such as big towns or grouping based on population spans.
    - So all our valid/relevant municipality ids can be saved as a static set, which I've done in "schemas\kolada_municipalities.json", format is:
      {"municipality id": {"title": "municipality title", "type": "muni type (K|L|G)"}}
        - (if id starts with G it's a municipality group, but note they still correspond to actual data points)
        - These number 356 entities, won't change in near future, considered non changning

## Municipality Data Harvesting/Refreshing

This simple script fetches all municiplaity data added or delted since the given date, and manages a week of changes in less than 10 min compared to the current dumbass version taking over an hour, upwards of two hours just to perform a single day refresh.

This means that even for totaly new kpis the output contains all relevant data points to construct the full dimensions. For existing kpis, we update the existing dimensions based on the new data.

```python
import json
import random
from datetime import datetime

import requests
from tqdm import tqdm

BASE_URL = "https://api.kolada.se/v3"
SHUFFLE = True
# Shuffling order of years, results in more even distribution of
# values between different requests since most values come from later years.

BATCH_SIZE = 25
PER_PAGE = 5000
TIMEOUT = 150
SAVE_FOLDER = "output/kolada"

MUNICIPALITY_YEARS = list(range(1970, datetime.now().year + 3))

OU_YEARS = list(range(2003, datetime.now().year + 1))
OU_KPI_IDS = []
ALL_KPIS = []
ALL_OU_KPIS = []


with open("schemas/kolada_municipalities.json", encoding="utf-8") as f:
    municipalities = json.load(f)

MUNICIPALITY_IDS = list(municipalities.keys())

print(f"Loaded {len(MUNICIPALITY_IDS)} municipality IDs.\n")
print(f"Loaded {len(MUNICIPALITY_YEARS)} municipality years.\n")


def _make_request(url: str, params: dict | None = None):
    """Make a GET request to the specified URL with optional parameters and return the JSON response."""
    response = requests.get(url, params=params, timeout=TIMEOUT)

    response.raise_for_status()
    print(f"URL: {response.url}, Status Code: {response.status_code}")
    return response.json()


def _batch_items(items: list, batch_size: int) -> list[list]:
    """Split a list of items into batches of the specified size."""
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def _get_paginated(url: str, page: int = 1, params: dict = {}):
    """Retrieve paginated results from the specified URL with optional query parameters as keyword arguments."""
    results = []

    params["per_page"] = PER_PAGE
    params["page"] = page

    while True:
        data = _make_request(url, params=params)
        values = data.get("values", [])
        results.extend(values)
        if len(values) < PER_PAGE:
            break
        if "next_url" not in data:
            break
        params["page"] += 1
    return results


def _parse_municipality_data(raw_values: list[dict]) -> list[dict]:
    """Parse raw municipality data into a structured format."""

    parsed_values = []

    for value_raw in raw_values:
        base_record = {
            "kpi": value_raw.get("kpi"),
            "period": value_raw.get("period"),
            "municipality": value_raw.get("municipality"),
        }

        for value_entry in value_raw.get("values", []):
            record = base_record.copy()
            record.update(value_entry)
            parsed_values.append(record)

    return parsed_values


def _fetch_municipality_data(url: str, from_date: str | None = None):
    """Fetch municipality data from the specified URL starting from the given date."""
    params = {}
    if from_date:
        params["from_date"] = from_date
    all_raw_values = _get_paginated(url, params=params)
    return _parse_municipality_data(all_raw_values)


def _fetch_oudata(url: str, from_date: str | None = None):
    """Fetch oudata from the specified URL starting from the given date."""
    params = {}
    if from_date:
        params["from_date"] = from_date
    all_raw_values = _get_paginated(url, params=params)
    return _parse_municipality_data(all_raw_values)


def get_kpis():
    """Retrieve all KPIs from the Kolada API."""
    return _get_paginated(f"{BASE_URL}/kpi")


ALL_KPIS = get_kpis()
ALL_OU_KPIS = [kpi for kpi in ALL_KPIS if kpi.get("has_ou_data", False)]
ALL_OU_KPI_IDS = [kpi["id"] for kpi in ALL_OU_KPIS]
print(f"Loaded {len(ALL_KPIS)} KPIs in total.")
print(f"Loaded {len(ALL_OU_KPIS)} OU KPI ids.")


def _generate_oudata_urls(
    kpi_ids: list[str],
    years: list[int],
) -> list[str]:
    """Generate a list of oudata URLs for the specified KPI IDs, and years."""

    if SHUFFLE:
        random.shuffle(kpi_ids)
        random.shuffle(years)

    muni_batches = _batch_items(kpi_ids, BATCH_SIZE)
    year_batches = _batch_items(years, BATCH_SIZE)

    url_products = []
    for muni_batch in muni_batches:
        for year_batch in year_batches:
            selection = {"kpi": muni_batch, "year": year_batch}
            url_products.append(selection)  # type: ignore

    urls = []

    for entry in url_products:
        url = f"{BASE_URL}/oudata"
        url += "/kpi/" + ",".join(entry["kpi"])
        url += "/year/" + ",".join(map(str, entry["year"]))
        urls.append(url)

    return urls


def _generate_data_urls(
    municipality_ids: list[str],
    years: list[int],
) -> list[str]:
    """Generate a list of data URLs for the specified municipality IDs and years."""

    if SHUFFLE:
        random.shuffle(municipality_ids)
        random.shuffle(years)

    muni_batches = _batch_items(municipality_ids, BATCH_SIZE)
    year_batches = _batch_items(years, BATCH_SIZE)

    url_products = []
    for muni_batch in muni_batches:
        for year_batch in year_batches:
            selection = {"municipality": muni_batch, "year": year_batch}
            url_products.append(selection)  # type: ignore

    urls = []

    for entry in url_products:
        url = f"{BASE_URL}/data"
        url += "/municipality/" + ",".join(entry["municipality"])
        url += "/year/" + ",".join(map(str, entry["year"]))
        urls.append(url)

    return urls


def fetch_municipality_data(from_date: str | None = None):
    """Fetch municipality data from a given date, defaults to yesterday"""
    if from_date is None:
        from datetime import date, timedelta

        from_date = (date.today() - timedelta(days=1)).isoformat()

    urls = _generate_data_urls(MUNICIPALITY_IDS, MUNICIPALITY_YEARS)
    all_data = []
    for url in tqdm(urls):
        url_results = _fetch_municipality_data(url, from_date=from_date)
        print(f"Fetched {len(url_results)} records from {url}")
        all_data.extend(url_results)
    return all_data


def fetch_oudata(from_date: str | None = None):
    """Fetch oudata from a given date, defaults to yesterday"""
    if from_date is None:
        from datetime import date, timedelta

        from_date = (date.today() - timedelta(days=1)).isoformat()

    urls = _generate_oudata_urls(ALL_OU_KPI_IDS, OU_YEARS)
    all_data = []
    for url in tqdm(urls):
        url_results = _fetch_oudata(url, from_date=from_date)
        print(f"Fetched {len(url_results)} records from {url}")
        all_data.extend(url_results)
    return all_data


if __name__ == "__main__":
    from_date = "2026-08-07"
    data = fetch_municipality_data(from_date=from_date)
    # kpis = set(record.get("kpi") for record in data)
    new_data = {}
    for record in data:
        kpi = record.get("kpi")
        if kpi not in new_data:
            new_data[kpi] = []
        record.pop("kpi", None)
        record.pop("count", None)
        new_data[kpi].append(record)

    with open("output/kolada/new_data.json", "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=4)

    new_oudata = {}
    oudata = fetch_oudata(from_date=from_date)
    for record in oudata:
        kpi = record.get("kpi")
        if kpi not in new_oudata:
            new_oudata[kpi] = []
        record.pop("kpi", None)
        record.pop("count", None)
        new_oudata[kpi].append(record)

    with open("output/kolada/new_oudata.json", "w", encoding="utf-8") as f:
        json.dump(new_oudata, f, ensure_ascii=False, indent=4)

# End of script
```

NOTE. We should also stop performing such extensive and expensive searches just for trying to determine a non official "real" value to use for the updated field. Instead we simply rely on the following logic:

1. If "publication_date" is available:

- If its a future date --> updated = publication_date - one year
- If its a past date or today --> updated = publication_date

2. If "publication_date" is NULL:

- If the corresponding data-eu/oppnadata entry has a defined "modified" date --> updated = modified

3. If neither "publication_date" nor "modified" date from data-eu is available:

- updated = f"{last_data_year + 1}-01-01"
-   - If that date is in the future --> updated = todays date




LAST IMPORTANT NOTE.
The output data sometimes includes deltations. Whenever records return showing deleted values, and the kpi is still available through the /kpi endpoint (otherwise just delete kpi from our collection since its no longer supported), then we should refetch those municipality and or ou data dimensions from scratch by using one single kpi along with all the possible years (different between oudata and data see above) to ensure we have a clean record of that metadata.