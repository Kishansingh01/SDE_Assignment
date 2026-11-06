"""Unit tests for governance compliance, TV distance, and proposal generation."""
import pytest
from controller.governance import (
    build_diff,
    build_dry_run,
    build_proposal_payload,
    calculate_max_shift,
    can_auto_apply,
    detect_changed_dims,
    is_within_approver_hours,
)


@pytest.fixture
def base_config():
    return {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": {"V1": 1.0},
                "caller_id": {"D1": 0.5, "D2": 0.5},
            }
        },
        "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
    }


def test_tv_distance_and_max_shift(base_config):
    new_cfg = {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": {"V1": 0.5, "V2": 0.5},  # TV distance = 0.5 * (|1 - 0.5| + |0 - 0.5|) = 0.5
                "caller_id": {"D1": 0.5, "D2": 0.5},
            }
        },
        "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
    }
    shift = calculate_max_shift(base_config, new_cfg)
    assert shift == pytest.approx(0.5, rel=1e-4)
    dims = detect_changed_dims(base_config, new_cfg)
    assert dims == ["voice"]


def test_can_auto_apply_policy(base_config):
    policy = {
        "auto_apply": {
            "enabled": True,
            "allowed_dims": ["caller_id", "schedule"],
            "max_shift": 0.5,
        }
    }
    # Shifting caller_id within allowed_dims and shift <= 0.5
    valid_new_cfg = {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": {"V1": 1.0},
                "caller_id": {"D1": 0.7, "D2": 0.3},  # shift = 0.2
            }
        },
        "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
    }
    ok, reason = can_auto_apply(policy, base_config, valid_new_cfg)
    assert ok is True

    # Voice is not in allowed_dims
    disallowed_cfg = {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": {"V2": 1.0},
                "caller_id": {"D1": 0.5, "D2": 0.5},
            }
        },
        "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
    }
    ok, reason = can_auto_apply(policy, base_config, disallowed_cfg)
    assert ok is False
    assert "dims_not_allowed" in reason


def test_is_within_approver_hours():
    # 10:00 is minute 600
    assert is_within_approver_hours(600, [10, 19]) is True
    # 09:45 is minute 585
    assert is_within_approver_hours(585, [10, 19]) is False
    # 20:00 is minute 1200
    assert is_within_approver_hours(1200, [10, 19]) is False


def test_build_proposal_payload(base_config):
    new_cfg = {
        "cohorts": [],
        "allocations": {
            "default": {
                "welcome_message": {"W1": 1.0},
                "voice": {"V2": 1.0},
                "caller_id": {"D1": 0.5, "D2": 0.5},
            }
        },
        "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}},
    }
    dry_run = build_dry_run("default", "engagement_rate", 0.50, 0.08, 1000)
    prop = build_proposal_payload(
        workflow_id="wf_1",
        base_version=1,
        proposed_config=new_cfg,
        old_config=base_config,
        change_summary="Promote winning voice V2",
        rationale="Lift +8% p<0.01",
        affected_leads=1000,
        impact_score=0.45,
        dry_run=dry_run,
    )
    assert prop["workflow_id"] == "wf_1"
    assert prop["base_version"] == 1
    assert "allocations.voice" in prop["diff"]
    assert prop["impact_score"] == 0.45
    assert prop["dry_run"]["metric"] == "engagement_rate"

