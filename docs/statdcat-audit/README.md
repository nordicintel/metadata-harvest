# Migration evidence

The twelve `*-ledger.jsonl.gz` files connect every original metadata record to its new dataset and distribution. Each line records the original file and line, source identity, source-record SHA-256, and the destination of every source attribute. They are internal audit evidence, separate from the two public catalog archives.

`conversion.json` pins the original Git commit, input/output file hashes and the post-assembly verification. All 23,900 original source contributions reconstruct exactly from 18,502 accepted datasets. Grouping languages and Kolada scopes accounts for the reduced dataset count; unmatched datasets remain included.

The source record hash uses UTF-8 JSON with sorted keys, compact separators, Unicode preserved and non-finite numbers prohibited. Reproduce the saved-collection check with `scripts/verify_statdcat_migration.py`; the explicitly named legacy converter can recover the original contract from Git history.

[verification.json](verification.json) records final archive hashes and sizes, matching counts, test results and the separately identified live checks. The additional unchanged-input reuse run was skipped at the user's explicit request.
