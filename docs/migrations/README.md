# Explicit migrations and historical evidence

`apps.migrations.profile_reader` reads previous accepted profiles without writes.
The collections, profile2, entrysoft, audit and verification modules contain
explicit conversion/import tools. Their script entry points remain under `scripts/`.
Running ordinary harvesting or catalog commands does not run a corpus migration.

`statdcat-audit/`, `profile2-audit/` and `verification/` contain historical evidence
from earlier migrations and checks. Their recorded paths, dates and counts describe
those runs, not a fresh verification of the current checkout.

The repository restructure changes Python paths and resource locations only.
Dataset identifiers, profile version 2.0.0 and persisted source evidence are preserved.
