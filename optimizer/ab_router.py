"""
Manifest A/B router — routes agent requests to manifest variants for
controlled experiments on manifest clarity and operation guidance.

Variants are immutable snapshots of manifest content. The router
assigns agents deterministically (by agent_id hash) to a variant bucket
so the same agent always sees the same manifest for the duration of an experiment.

Usage:
    router = ABRouter.get_active()
    variant = router.get_variant(agent_id="agent_42")
    manifest_content = variant.manifest_override or load_default_manifest()
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

# ---------------------------------------------------------------------------
# Variant definition
# ---------------------------------------------------------------------------

@dataclass
class ManifestVariant:
    variant_id: str             # e.g. "control", "v1_clearer_examples"
    description: str
    traffic_weight: float       # 0.0 – 1.0, must sum to 1.0 across experiment
    manifest_override: Optional[dict] = None  # None = use default manifest
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------

@dataclass
class Experiment:
    experiment_id: str
    name: str
    variants: list[ManifestVariant]
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    active: bool = True

    def is_live(self) -> bool:
        now = time.time()
        if not self.active:
            return False
        if self.end_time and now > self.end_time:
            return False
        return True

    def validate(self) -> None:
        total = sum(v.traffic_weight for v in self.variants)
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"Experiment '{self.experiment_id}': variant weights sum to {total:.3f}, expected 1.0"
            )
        ids = [v.variant_id for v in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate variant_ids in experiment '{self.experiment_id}'")


# ---------------------------------------------------------------------------
# Assignment result
# ---------------------------------------------------------------------------

@dataclass
class VariantAssignment:
    agent_id: str
    experiment_id: str
    variant_id: str
    variant: ManifestVariant
    assignment_bucket: float    # 0.0–1.0 deterministic hash value


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class ABRouter:
    """
    Deterministic A/B router based on MD5 hash of agent_id.
    Same agent → same variant for the lifetime of the experiment.
    """

    _instance: Optional["ABRouter"] = None

    def __init__(self) -> None:
        self._experiments: dict[str, Experiment] = {}
        self._assignment_log: list[VariantAssignment] = []
        self._load_default_experiments()

    @classmethod
    def get_active(cls) -> "ABRouter":
        if cls._instance is None:
            cls._instance = ABRouter()
        return cls._instance

    def _load_default_experiments(self) -> None:
        """Seed with the initial manifest clarity experiment."""
        exp = Experiment(
            experiment_id="exp_001_manifest_clarity",
            name="Manifest clarity: richer examples vs baseline",
            variants=[
                ManifestVariant(
                    variant_id="control",
                    description="Current manifest — baseline",
                    traffic_weight=0.50,
                    manifest_override=None,
                ),
                ManifestVariant(
                    variant_id="v1_richer_examples",
                    description="Adds 3 extra examples per operation with edge cases",
                    traffic_weight=0.50,
                    manifest_override=None,  # loaded lazily if needed
                ),
            ],
        )
        exp.validate()
        self._experiments[exp.experiment_id] = exp

    def register_experiment(self, experiment: Experiment) -> None:
        experiment.validate()
        self._experiments[experiment.experiment_id] = experiment

    def get_active_experiments(self) -> list[Experiment]:
        return [e for e in self._experiments.values() if e.is_live()]

    def get_variant(
        self, agent_id: str, experiment_id: Optional[str] = None
    ) -> VariantAssignment:
        """
        Assign an agent to a variant. If experiment_id is None, uses the
        first active experiment. Returns 'control' variant if no active experiment.
        """
        active = self.get_active_experiments()
        if not active:
            return self._control_fallback(agent_id)

        exp = (
            self._experiments[experiment_id]
            if experiment_id and experiment_id in self._experiments
            else active[0]
        )

        bucket = self._hash_bucket(agent_id, exp.experiment_id)
        variant = self._pick_variant(bucket, exp.variants)

        assignment = VariantAssignment(
            agent_id=agent_id,
            experiment_id=exp.experiment_id,
            variant_id=variant.variant_id,
            variant=variant,
            assignment_bucket=bucket,
        )
        self._assignment_log.append(assignment)
        return assignment

    @staticmethod
    def _hash_bucket(agent_id: str, experiment_id: str) -> float:
        """Deterministic bucket [0.0, 1.0) from (agent_id, experiment_id)."""
        key = f"{agent_id}:{experiment_id}"
        digest = hashlib.md5(key.encode()).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF

    @staticmethod
    def _pick_variant(bucket: float, variants: list[ManifestVariant]) -> ManifestVariant:
        cumulative = 0.0
        for v in variants:
            cumulative += v.traffic_weight
            if bucket < cumulative:
                return v
        return variants[-1]  # safety fallback

    def _control_fallback(self, agent_id: str) -> VariantAssignment:
        control = ManifestVariant(
            variant_id="control",
            description="Default — no active experiment",
            traffic_weight=1.0,
        )
        return VariantAssignment(
            agent_id=agent_id,
            experiment_id="none",
            variant_id="control",
            variant=control,
            assignment_bucket=0.0,
        )

    # ---------------------------------------------------------------------------
    # Assignment analytics (feeds SelectionAnalytics)
    # ---------------------------------------------------------------------------

    def get_assignment_counts(self) -> dict[str, dict[str, int]]:
        """Returns {experiment_id: {variant_id: count}}."""
        counts: dict[str, dict[str, int]] = {}
        for a in self._assignment_log:
            counts.setdefault(a.experiment_id, {})
            counts[a.experiment_id][a.variant_id] = (
                counts[a.experiment_id].get(a.variant_id, 0) + 1
            )
        return counts

    def flush_log(self) -> list[VariantAssignment]:
        """Return and clear the assignment log (for periodic export)."""
        log = list(self._assignment_log)
        self._assignment_log.clear()
        return log
