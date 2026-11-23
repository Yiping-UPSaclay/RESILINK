"""Experiment runner: config -> content-addressed run id -> Monte Carlo campaign -> results store + manifest."""

from resilink.experiments.runner import RunResult, compute_run_id, estimate, run_experiment

__all__ = ["RunResult", "compute_run_id", "estimate", "run_experiment"]
