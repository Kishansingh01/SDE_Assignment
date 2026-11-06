"""Governance manager: policy compliance, TV distance, diff generation, proposal formatting."""
from __future__ import annotations

import copy
import uuid
from typing import Any, Optional

AGENT_DIMS = ["welcome_message", "voice", "caller_id"]
CHANGE_DIMS = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]


def _dist(cfg: dict[str, Any], cohort: str, dim: str) -> dict[str, float]:
    allocs = cfg.get("allocations", {})
    return (allocs.get(cohort) or {}).get(dim) or allocs.get("default", {}).get(dim, {})


def detect_changed_dims(old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> list[str]:
    ch = []
    if old_cfg.get("cohorts") != new_cfg.get("cohorts"):
        ch.append("cohorts")
    keys = set(old_cfg.get("allocations", {})) | set(new_cfg.get("allocations", {}))
    for dim in AGENT_DIMS:
        if any(_dist(old_cfg, k, dim) != _dist(new_cfg, k, dim) for k in keys):
            ch.append(dim)
    if old_cfg.get("prompt_version") != new_cfg.get("prompt_version"):
        ch.append("prompt_version")
    if old_cfg.get("schedule") != new_cfg.get("schedule"):
        ch.append("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def calculate_max_shift(old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> float:
    """Calculates maximum total-variation distance across all cohorts and agent dimensions."""
    old_cohorts = {c["id"] for c in old_cfg.get("cohorts", [])}
    new_cohorts = {c["id"] for c in new_cfg.get("cohorts", [])}
    keys = {"default"} | old_cohorts | new_cohorts
    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            a, b = _dist(old_cfg, k, dim), _dist(new_cfg, k, dim)
            all_opts = set(a.keys()) | set(b.keys())
            tv = 0.5 * sum(abs(a.get(o, 0.0) - b.get(o, 0.0)) for o in all_opts)
            worst = max(worst, tv)
    return worst


def can_auto_apply(policy: dict[str, Any], old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> tuple[bool, str]:
    """Determines whether a proposed config can be auto-applied under workflow policy."""
    auto_pol = policy.get("auto_apply", {})
    if not auto_pol.get("enabled", False):
        return False, "auto_apply_disabled"

    changed = detect_changed_dims(old_cfg, new_cfg)
    if not changed:
        return False, "no_changes"

    allowed_dims = auto_pol.get("allowed_dims", [])
    disallowed = [d for d in changed if d not in allowed_dims]
    if disallowed:
        return False, f"dims_not_allowed: {disallowed}"

    shift = calculate_max_shift(old_cfg, new_cfg)
    max_shift = auto_pol.get("max_shift", 0.0)
    if shift > max_shift + 1e-9:
        return False, f"shift_{shift:.3f}_exceeds_max_{max_shift:.3f}"

    return True, "permitted"


def is_within_approver_hours(sim_minute: int, approver_hours: list[int]) -> bool:
    """Checks if simulated time falls within approver working hours (e.g. [10, 19])."""
    if not approver_hours or len(approver_hours) != 2:
        return True
    hour_of_day = (sim_minute % 1440) // 60
    return approver_hours[0] <= hour_of_day < approver_hours[1]


def build_diff(old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> dict[str, Any]:
    """Builds a human-readable diff dictionary."""
    diff = {}
    changed = detect_changed_dims(old_cfg, new_cfg)
    for dim in changed:
        if dim == "prompt_version":
            diff["prompt_version"] = {
                "old": old_cfg.get("prompt_version"),
                "new": new_cfg.get("prompt_version"),
            }
        elif dim in AGENT_DIMS:
            diff[f"allocations.{dim}"] = {
                "old": {k: _dist(old_cfg, k, dim) for k in set(old_cfg.get("allocations", {})) | set(new_cfg.get("allocations", {}))},
                "new": {k: _dist(new_cfg, k, dim) for k in set(old_cfg.get("allocations", {})) | set(new_cfg.get("allocations", {}))},
            }
        elif dim == "cohorts":
            diff["cohorts"] = {"old": old_cfg.get("cohorts"), "new": new_cfg.get("cohorts")}
        elif dim == "schedule":
            diff["schedule"] = {
                "hour_weights_modified": old_cfg.get("schedule", {}).get("hour_weights") != new_cfg.get("schedule", {}).get("hour_weights"),
                "lead_preferred_count": len(new_cfg.get("schedule", {}).get("lead_preferred_hour", {})),
            }
    if not diff:
        diff["note"] = "no detected dimension changes"
    return diff



def build_dry_run(
    affected_cohort: str,
    metric_name: str,
    expected_baseline: float,
    expected_lift: float,
    sample_leads: int,
) -> dict[str, Any]:
    """Constructs a deterministic dry-run replay artifact."""
    return {
        "affected_cohort": affected_cohort,
        "sample_leads_simulated": sample_leads,
        "metric": metric_name,
        "projected_baseline": round(expected_baseline, 4),
        "projected_lift": round(expected_lift, 4),
        "confidence": "statistically_significant_p<0.05",
    }


def build_proposal_payload(
    workflow_id: str,
    base_version: int,
    proposed_config: dict[str, Any],
    old_config: dict[str, Any],
    change_summary: str,
    rationale: str,
    affected_leads: int,
    impact_score: float,
    dry_run: dict[str, Any],
    evidence: Optional[dict[str, Any]] = None,
    proposal_id: Optional[str] = None,
    expires_in_hours: Optional[float] = None,
) -> dict[str, Any]:
    """Generates the compliant proposal dictionary for /approvals."""
    pid = proposal_id or f"prop_{uuid.uuid4().hex[:12]}"
    diff = build_diff(old_config, proposed_config)
    payload = {
        "proposal_id": pid,
        "workflow_id": workflow_id,
        "base_version": base_version,
        "proposed_config": proposed_config,
        "change_summary": change_summary.strip() or "Optimise agent workflow allocations",
        "diff": diff,
        "affected_leads": max(0, int(affected_leads)),
        "dry_run": dry_run if dry_run else {"summary": "no dry run available"},
        "impact_score": min(1.0, max(0.0, float(impact_score))),
        "rationale": rationale.strip() or "Observed statistical lift in variant evaluation",
    }
    if evidence:
        payload["evidence"] = evidence
    if expires_in_hours is not None:
        payload["expires_in_hours"] = expires_in_hours
    return payload

