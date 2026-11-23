---
name: add-source
description: Register and fetch a new open-data (or operator) source for RESILINK with drift detection and recorded tests. Use when asked to add a dataset, connector or data provider.
---

# Add a data source

1. Inspect the portal first: `uv run resilink etl describe <id>` after adding a draft entry, or probe the
   API directly. Known technologies: Opendatasoft Explore v2.1 (ODRÉ) and Data Fair (Enedis — the old
   Opendatasoft paths return HTTP 410). Verify field names and the territory filter field (code vs name).
2. Add the entry to `configs/sources.yaml`: connector, dataset id, `territory_field`/`territory_value`,
   licence, sensitivity tier, id strategy (native key or fingerprint), priority, notes on caveats.
3. New portal technology? Add a connector class in `packages/resilink/src/resilink/etl/connectors.py`
   implementing `describe()` (fields, geometry flag, licence, modified, count) and `fetch()`.
4. Fetch a snapshot: `uv run resilink etl fetch <id> --territory configs/territories/bdr.yaml`.
   Never bypass a DriftError silently — report the diff; `--accept-drift` is a human decision.
5. Add a contract test with `@pytest.mark.network @pytest.mark.vcr` in `tests/integration/test_etl.py` and
   record once with `uv run pytest tests/integration/test_etl.py --record-mode=once`.
6. Security: do not ingest geometry an operator has withdrawn (e.g. RTE since 2026-06-16); respect
   licences (ODbL share-alike for OSM-derived tables).
