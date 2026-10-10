<!-- markdownlint-disable-file -->

<!-- prettier-ignore-file -->

<!-- cspell:disable -->

<!-- ruff: noqa -->

# Kolada optimized metadata scraper

Starting assumptions (backed by prior knowledge + data)

- The earliest municiplaity data is for year 1970 - Won't change
- The earliest OU data is for year 2003 - Won't change (might go up cause deleted kpis, never down)
- KPI's only ever cover predictions for two years ahead (might change but we will handle that in a year or two then) so last year for both muni data and ou data possible is: \_current_year + 2 = 2028 (calc should be actually made using datetime or sim)\_So we have:
    - OU data: range[2003, 2028]
    - Municipality data: range[1970, 2028]

- We have choosen not to retrieve/cover the absolute majority of useless municipality_group values which are just derivable comparison values. The exception being 40 or so which are usefull groupings/categorizations such as big towns or grouping based on population spans.
    - So all our valid/relevant municipality ids can be saved as a static set, which I've done in "schemas\kolada_municipalities.json", format is:
      {"municipality id": {"title": "municipality title", "type": "muni type (K|L|G)"}}
        - (if id starts with G it's a municipality group, but note they still correspond to actual data points)
        - These number 356 entities, won't change in near future, considered non changning

## Municipality Data Harvesting/Refreshing
