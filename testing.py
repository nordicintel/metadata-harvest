import json
import os
import random
from datetime import datetime

import requests
from tenacity import retry, stop_after_attempt, wait_fixed
from tqdm import tqdm

BASE_URL = "https://api.kolada.se/v3"
SHUFFLE = True
# Shuffling order of years, results in more even distribution of
# values between different requests since most values come from later years.

BATCH_SIZE = 25
PER_PAGE = 5000
TIMEOUT = 180
SAVE_FOLDER = "output/kolada"

MUNICIPALITY_YEARS = list(range(1970, datetime.now().year + 3))

with open("schemas/kolada_municipalities.json", encoding="utf-8") as f:
    municipalities = json.load(f)

MUNICIPALITY_IDS = list(municipalities.keys())

print(f"Loaded {len(MUNICIPALITY_IDS)} municipality IDs.\n")
print(f"Loaded {len(MUNICIPALITY_YEARS)} municipality years.\n")


@retry(stop=stop_after_attempt(5), wait=wait_fixed(5))
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
    print(f"Fetching url {url} with params: {params}")

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


def get_kpis():
    """Retrieve all KPIs from the Kolada API."""
    return _get_paginated(f"{BASE_URL}/kpi")


def _generate_data_urls(
    municipality_ids: list[str],
    years: list[int],
) -> list[str]:
    """Generate a list of data URLs for the specified municipality IDs, KPI IDs, and years."""

    if SHUFFLE:
        random.shuffle(municipality_ids)
        random.shuffle(years)

    print(
        f"Generating data URLs for {len(municipality_ids)} municipalities and {len(years)} years."
    )

    muni_batches = _batch_items(municipality_ids, BATCH_SIZE)
    year_batches = _batch_items(years, BATCH_SIZE)

    # print(
    #     f"Created {len(muni_batches)} municipality batches and {len(year_batches)} year batches."
    # )

    url_products = []
    for muni_batch in muni_batches:
        for year_batch in year_batches:
            selection = {"municipality": muni_batch, "year": year_batch}
            # print(f"Created URL selection: {selection}")
            url_products.append(selection)  # type: ignore

    urls = []

    for entry in url_products:
        url = f"{BASE_URL}/data"
        url += "/municipality/" + ",".join(entry["municipality"])
        url += "/year/" + ",".join(map(str, entry["year"]))
        urls.append(url)

    return urls


def _construct_data_fetch_file(
    from_date: str | None = None, filename: str | None = None
):
    if not from_date:
        from_date = "1970-01-01"
    urls = _generate_data_urls(MUNICIPALITY_IDS, MUNICIPALITY_YEARS)
    # print(f"Generated {len(urls)} data URLs.")
    if not filename:
        todays_date = datetime.now().strftime("%Y-%m-%d")
        filename = f"data_fetch_{from_date}_{todays_date}.txt"

    file_path = f"{SAVE_FOLDER}/{filename}"

    if os.path.exists(file_path):
        return file_path

    print(f"Creating data fetch file at: {file_path}")
    with open(file_path, "w", encoding="utf-8") as f:
        for i, url in enumerate(urls, start=1):
            f.write(json.dumps({"index": i, "url": url, "from_date": from_date}) + "\n")

    return file_path


def _append_to_jsonl(data: dict, file_path: str):
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def download_data(data_fetch_file: str, output_jsonl: str):
    """Download data from the URLs listed in the data fetch file and write the results to the output JSONL file."""

    if not os.path.exists(data_fetch_file):
        raise FileNotFoundError(f"Data fetch file not found: {data_fetch_file}")

    start_index = 1
    if os.path.exists(output_jsonl):
        start_index = sum(1 for _ in open(output_jsonl, encoding="utf-8")) + 1

    data_jobs = []
    total_jobs_in_file = sum(1 for _ in open(data_fetch_file, encoding="utf-8"))
    with open(data_fetch_file, encoding="utf-8") as f_in:
        for _i, line in enumerate(f_in, start=start_index):
            entry = json.loads(line)
            data_jobs.append(entry)

    for job in tqdm(
        data_jobs,
        desc="Downloading data",
        total=total_jobs_in_file,
        initial=start_index - 1,
    ):
        url = job["url"]
        from_date = job["from_date"]
        data = _get_paginated(url, params={"from_date": from_date})
        _append_to_jsonl(
            {"url": url, "from_date": from_date, "data": data}, output_jsonl
        )


def fetch_data_since(output_jsonl: str, from_date: str | None = None):
    """Fetch data since the specified date and write the URLs to the output JSONL file."""
    data_fetch_file = _construct_data_fetch_file(
        from_date=from_date, filename=output_jsonl
    )
    download_data(data_fetch_file, output_jsonl)


if __name__ == "__main__":
    from_date = "2026-10-07"
    output_jsonl = f"data_since_{from_date}.jsonl"
    fetch_data_since(output_jsonl, from_date=from_date)
