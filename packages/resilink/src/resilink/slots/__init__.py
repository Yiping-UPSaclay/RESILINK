"""Built-in slot definitions v0.1 (ADR-0008 §3, blueprint §6.4), registered through ``resilink.slots`` entry points.

Seven model slots answer the modelling questions of an episode; two engine-level slots say how the episode is run
(orchestrator) and how simulated worlds are drawn (sampler). Version 0.1 matches the ``episode`` orchestrator 0.1
(Sprint 0's fixed sequence); the event-driven kernel (step E4) will add triggers and events to these definitions.
Anticipated slots (degradation, resources, policy) are designed with the first thesis that needs them.
"""

from __future__ import annotations

from resilink.plugin_api import (
    ConsequenceModel,
    DependencyModel,
    FragilityModel,
    HazardModel,
    Orchestrator,
    PropagationModel,
    RestorationModel,
    Sampler,
    ServiceModel,
    SlotDefinition,
)

HAZARD = SlotDefinition(
    id="hazard", version="0.1.0", protocol=HazardModel, default_model="flood_footprints",
    question="What hazard intensity does each asset see in each scenario?",
    owns=("hazard.intensity",), checks=("intensity_non_negative",))
FRAGILITY = SlotDefinition(
    id="fragility", version="0.1.0", protocol=FragilityModel, default_model="lognormal_library",
    question="Which damage state does each asset reach, given the hazard intensity?",
    owns=("damage.state", "damage.functionality"), checks=("probability_in_unit_interval", "monotone_in_intensity"))
DEPENDENCY = SlotDefinition(
    id="dependency", version="0.1.0", protocol=DependencyModel, default_model="no_dependencies",
    question="Which asset of another system does each asset depend on, and with what backup autonomy?",
    checks=("dependents_and_supporters_in_different_systems",))
SERVICE = SlotDefinition(
    id="service", version="0.1.0", protocol=ServiceModel, cardinality="per_subsystem",
    default_model="connectivity", execution=("in_process", "worker"),
    question="Within one system, which nodes are served given the assets that work?",
    checks=("no_damage_full_service", "monotone_in_assets_up"))
PROPAGATION = SlotDefinition(
    id="propagation", version="0.1.0", protocol=PropagationModel, default_model="topological_temporal",
    question="How do outages spread within and across systems over time, and when does service recover?",
    owns=("service.served", "service.level", "outage.hours"), checks=("no_damage_no_outage",))
RESTORATION = SlotDefinition(
    id="restoration", version="0.1.0", protocol=RestorationModel, default_model="independent_repair",
    question="When is each damaged element repaired?",
    owns=("repair.start_h", "repair.finish_h"), checks=("no_damage_no_repair", "repair_times_non_negative"))
CONSEQUENCE = SlotDefinition(
    id="consequence", version="0.1.0", protocol=ConsequenceModel, cardinality="many", default_model="people_hours",
    question="What does the loss of service cost?", checks=("no_outage_zero_loss",))
ORCHESTRATOR = SlotDefinition(
    id="orchestrator", version="0.1.0", protocol=Orchestrator, default_model="episode", engine_level=True,
    ensembles=False, question="Which time loop composes the models of an experiment?")
SAMPLER = SlotDefinition(
    id="sampler", version="0.1.0", protocol=Sampler, default_model="nested_mc", engine_level=True,
    ensembles=False, question="How are simulated worlds drawn, and with which random-number streams?")

BUILTIN_SLOTS = (HAZARD, FRAGILITY, DEPENDENCY, SERVICE, PROPAGATION, RESTORATION, CONSEQUENCE, ORCHESTRATOR,
                 SAMPLER)

__all__ = ["BUILTIN_SLOTS", "CONSEQUENCE", "DEPENDENCY", "FRAGILITY", "HAZARD", "ORCHESTRATOR", "PROPAGATION",
           "RESTORATION", "SAMPLER", "SERVICE"]
