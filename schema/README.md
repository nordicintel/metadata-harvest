# Schema seeds

Inputs for the schema session: `tabular.schema.json` (the worker-document shape,
JSON-stat2 with an `extension.nordicintel` block), `provider.schema.json`,
`retrieval-result.schema.json` and `source-configuration.schema.json` (the shape of
the three files under `config/`), with the merge rules in `composition.py`, the
period helpers in `normalization.py` and the validators in `validation.py`.

`tabular`, `provider` and `retrieval-result` were imported from
nordicintel/nordicintel-schemas commit 77a7a4665fea6f3c2be8f286d560b79317f08dd7
under the accompanying Apache-2.0 licence (`LICENSE`).
