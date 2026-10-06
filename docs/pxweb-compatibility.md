# PXWeb compatibility evidence

Run date: 2026-10-06 (timestamps in raw evidence are UTC).

- Required matrix: 20 providers, 45 databases, 55 language/database combinations.
- API-form versus web-form harvests: **55/55 matched**, **7,167 records**, **0 omissions**.
- Per web-form invocation: **42** combinations needed no API enrichment; **13** used a flat search; **3** also needed parent-folder lookups.
- Browser-originated API responses: zero in every successful invocation. Explicit Playwright request-client enrichment is audited separately.
- Local validation: 66 pytest tests; Ruff lint and formatting checks. Live acceptance is separately invoked.
- Full acceptance flag: **False**. Discovery differences below remain visible; successful configured harvests do not conceal those differences.

CSN recovered all 20 dates absent from the flat search through five parent folders: 255/255 web-listed tables retained. Skogsstyrelsen retained 270 Swedish tables using four parent folders and 207 English tables using one. No API-only table was added.

## Discovery results

| Provider | Language | Result | Missing configured databases | Additional databases / failure |
|---|---|---|---|---|
| mi | sv | success | - | Konfliktstatistik |
| csn | sv | success | - | - |
| energi | sv | success | - | - |
| energi | en | success | - | - |
| sjv | sv | success | - | - |
| konjbar | sv | success | - | - |
| konjbar | en | success | - | - |
| konj | sv | success | - | TidigarePrognoser |
| lansstyr | sv | success | - | Exploatering_av_strander |
| fohm | sv | success | - | - |
| mcf | sv | success | - | - |
| mcf | en | success | - | - |
| fohm | en | success | - | - |
| slu | sv | success | - | OffStat_medelfel |
| slu | en | success | - | OffStat_medelfel |
| domstol | sv | success | - | - |
| domstol | en | success | - | - |
| kulturanalys | sv | success | - | - |
| konj | en | failed | - | en/TidigarePrognoser: Missing title/category name: https://prognos.konj.se/PxWeb/pxweb/en/TidigarePrognoser/TidigarePrognoser__20220928__f27_vinster/F2701.px/ |
| skog | sv | failed | - | sv/ssd: Unrecognized or incomplete database: https://pxweb.skogsstyrelsen.se/pxweb/sv/ssd/ |
| ehm | sv | success | - | Fritidskortet; Sålda mängder läkemedelssubstanser med möjlig miljöpåverkan |
| skog | en | failed | - | en/ssd: Unrecognized or incomplete database: https://pxweb.skogsstyrelsen.se/pxweb/en/ssd/ |
| tillvaxtanalys | sv | success | - | - |
| vgregion | sv | success | - | - |
| skolverket | sv | success | - | - |
| vasteras | sv | success | Dolda tabeller | - |
| sundsvall | sv | success | - | - |
| linkoping | sv | success | - | - |

## Configured combinations

| Provider | Language | Database | Tables | Flat calls | Folder requests | API/web identity |
|---|---|---|---:|---:|---:|---|
| mi | sv | Konjunkturlönestatistik | 6 | 0 | 0 | matched |
| csn | sv | CSNstat | 255 | 1 | 5 | matched |
| energi | sv | Energimyndighetens_statistikdatabas | 398 | 1 | 0 | matched |
| sjv | sv | Jordbruksverkets statistikdatabas | 598 | 0 | 0 | matched |
| energi | en | Energimyndighetens_statistikdatabas | 397 | 1 | 0 | matched |
| konj | sv | SenastePrognosen | 74 | 1 | 0 | matched |
| konjbar | sv | KonjBar | 52 | 0 | 0 | matched |
| konj | sv | UrvalAvVariabler | 23 | 1 | 0 | matched |
| konjbar | en | KonjBar | 51 | 0 | 0 | matched |
| konj | sv | XLangtidsscenario | 84 | 1 | 0 | matched |
| fohm | sv | A_Folkhalsodata | 1703 | 1 | 0 | matched |
| lansstyr | sv | Miljotillstandet_i_skogslandskapet | 91 | 0 | 0 | matched |
| konj | en | SenastePrognosen | 74 | 1 | 0 | matched |
| mcf | sv | PxData | 39 | 0 | 0 | matched |
| konj | en | UrvalAvVariabler | 23 | 1 | 0 | matched |
| konj | en | XLangtidsscenario | 84 | 1 | 0 | matched |
| mcf | en | PxData | 39 | 1 | 0 | matched |
| fohm | en | A_Folkhalsodata | 442 | 0 | 0 | matched |
| slu | sv | OffStat | 98 | 0 | 0 | matched |
| slu | en | OffStat | 98 | 0 | 0 | matched |
| domstol | sv | DOMstat | 69 | 0 | 0 | matched |
| domstol | en | DOMstat | 69 | 0 | 0 | matched |
| skog | en | Skogsstyrelsens statistikdatabas | 207 | 1 | 1 | matched |
| linkoping | sv | Basomraden | 137 | 0 | 0 | matched |
| kulturanalys | sv | Statistikdatabas | 10 | 0 | 0 | matched |
| linkoping | sv | Delomraden | 256 | 0 | 0 | matched |
| linkoping | sv | Enkater | 15 | 0 | 0 | matched |
| ehm | sv | Detaljhandel med läkemedel | 20 | 0 | 0 | matched |
| skog | sv | Skogsstyrelsens statistikdatabas | 270 | 1 | 4 | matched |
| linkoping | sv | GeoUtskott | 157 | 0 | 0 | matched |
| ehm | sv | Läkemedelsprisindex | 2 | 0 | 0 | matched |
| linkoping | sv | Linkoping | 374 | 0 | 0 | matched |
| tillvaxtanalys | sv | Tillväxtanalys statistikdatabas | 48 | 0 | 0 | matched |
| linkoping | sv | Omvarld | 46 | 0 | 0 | matched |
| vgregion | sv | Arbetsmarknad och Näringsliv | 10 | 0 | 0 | matched |
| vasteras | sv | Arbetsmarknad | 3 | 0 | 0 | matched |
| vgregion | sv | Demografi | 10 | 0 | 0 | matched |
| linkoping | sv | Om_mig | 50 | 0 | 0 | matched |
| vasteras | sv | Befolkning | 6 | 0 | 0 | matched |
| vgregion | sv | Folkhälsoenkäten (Hälsa på lika villkor) | 6 | 0 | 0 | matched |
| linkoping | sv | RegSO | 16 | 0 | 0 | matched |
| vasteras | sv | Befolkningsprognos | 4 | 0 | 0 | matched |
| vgregion | sv | Gränsregional statistik Norge-Sverige | 21 | 0 | 0 | matched |
| vasteras | sv | Bostäder och byggande | 11 | 0 | 0 | matched |
| vgregion | sv | Personalstatistik Västra Götalandsregionen | 3 | 0 | 0 | matched |
| vasteras | sv | Dolda tabeller | 45 | 0 | 0 | matched |
| linkoping | sv | Samlad_planering | 358 | 0 | 0 | matched |
| vasteras | sv | Inkomster | 3 | 0 | 0 | matched |
| linkoping | sv | Skolkvalitet | 2 | 0 | 0 | matched |
| vasteras | sv | Ohälsa | 1 | 0 | 0 | matched |
| sundsvall | sv | Sundsvalls_statistikdatabas | 52 | 0 | 0 | matched |
| vasteras | sv | Utbildning | 3 | 0 | 0 | matched |
| linkoping | sv | Sureg14 | 179 | 0 | 0 | matched |
| skolverket | sv | Skolverkets_statistikdatabas | 30 | 0 | 0 | matched |
| linkoping | sv | SuregSoc | 45 | 0 | 0 | matched |

## Evidence and limits

- Full records, timestamps, request counts, sample DOM checks, code hashes and earlier failed attempts are retained in `output/compatibility-final.json` (local, ignored by Git). The runner supports `--resume` without erasing earlier attempts.
- Live checks were collected across the recorded implementation revisions while repairing rate-limit handling and language-selector discovery. Successful configured harvests were retained; affected checks were rerun. Later flat-rate-limit waiting and omission validation are covered by local regression tests.
- The independent audit visits one table per combination; it is not an all-table visual audit. Kulturanalys has different listing/table-page wording; the emitted title was separately confirmed on its rendered folder listing.
- Actual session-bearing web URLs are preserved. Only UUID-valued `rxid` differences are ignored for identity comparison.
- Website listing dates remain authoritative when parseable. Missing dates use the flat API result, then API parent folders. Listing and About-panel dates can differ; both are retained in sample evidence, while the library does not visit table pages.
- Discovery cannot manufacture missing labels or infer that an unexplained empty interface is a valid empty database. It raises an aggregate failure for those cases. Hidden databases absent from discovery can still be harvested explicitly.
- Scheduling, metadata rebuilding and persistent processing state remain subsequent work. Advance state only after rebuilding succeeds.
