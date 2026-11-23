# ADR-0001 — Canonical data model + source adapters
Status: accepted (2026-11-16)

**Context.** Heterogeneous open data now, richer operator data later; the platform must stay data-agnostic.
**Decision.** Every source enters through a connector (fetch + describe) and an adapter that maps it to the
canonical tables (nodes, edges, dependencies, service_links, grid_cells, hazards, exposure) with stable
`rid`s (`<layer>.<class>:<ns>:<key>`, native keys or 1 m geometry fingerprints), provenance and a
sensitivity tier. A generic YAML mapping adapter imports operator files without code. Schema drift
(removed field / geometry / licence change) fails the fetch until a human accepts it.
**Consequences.** One analysis path regardless of source; adapters are small and testable (cassettes).
Verified 2026-11-16: ODRÉ (Opendatasoft v2.1) and Enedis (now Data Fair) need two connector types.
