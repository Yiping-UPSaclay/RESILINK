# ADR-0005 — Sensitivity tiers and dissemination
Status: accepted (2026-11-16)

**Context.** Since 2026-06-16 RTE publishes no geometry on ODRÉ "pour des raisons de sécurité publique".
**Decision.** Tiers public < internal < restricted on every table, op and result. Asset-level
vulnerability (per-asset failure probabilities, causal chains) is restricted for real territories;
synthetic data is public. No ingestion of superseded RTE geometry; HV geometry from OSM (ODbL, kept
separable). No public release of asset-level vulnerability; supervisors review dissemination.
