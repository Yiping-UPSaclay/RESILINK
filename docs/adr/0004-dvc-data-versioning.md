# ADR-0004 — DVC for snapshots, releases and pipeline stages
Status: accepted (2026-11-16)

**Decision.** Non-deterministic `fetch` produces immutable raw snapshots tracked with `dvc add`;
deterministic stages (normalize → build → tiles) are `dvc.yaml` stages, so `dvc repro` rebuilds a release
bit-for-bit. DVC is open source under lakeFS stewardship (2025). Remote: local/NAS in P0–P2, S3-compatible
object storage (Scaleway) from P3. DuckLake 1.0 is the candidate if multi-writer time travel is needed.
