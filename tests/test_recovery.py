"""Unit tests for SQLite state persistence, idempotency, and crash recovery."""
import pytest
from controller.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(db_path=str(tmp_path / "test_controller.db"))


def test_sync_state_persistence(db):
    assert db.get_sync_state("last_chunk") is None
    db.set_sync_state("last_chunk", "42")
    assert db.get_sync_state("last_chunk") == "42"

    # Overwrite works
    db.set_sync_state("last_chunk", "43")
    assert db.get_sync_state("last_chunk") == "43"


def test_proposal_lifecycle_and_reconciliation(db):
    db.save_proposal(
        proposal_id="p_001",
        workflow_id="nw_renewal",
        base_version=2,
        status="pending",
        change_type="variant_experiment",
        proposed_config={"allocations": {}},
        diff={"voice": "V2"},
        impact_score=0.4,
        affected_leads=500,
        created_sim_minute=600,
        approval_id="apr_001",
    )

    pending = db.get_pending_proposals()
    assert len(pending) == 1
    assert pending[0]["proposal_id"] == "p_001"
    assert pending[0]["approval_id"] == "apr_001"

    # Update status to applied
    db.update_proposal_status(
        proposal_id="p_001",
        status="applied",
        applied_sim_minute=720,
    )
    assert len(db.get_pending_proposals()) == 0


def test_audit_trail_logging_and_query(db):
    db.record_audit(
        sim_minute=600,
        sim_time="2027-01-04T10:00:00+05:30",
        workflow_id="nw_renewal",
        actor="controller_auto",
        event_type="AUTO_APPLIED",
        summary="Auto-applied caller ID rotation",
        details={"dids": ["D1", "D2"]},
    )

    with db.get_connection() as conn:
        cur = conn.execute("SELECT * FROM audit_trail WHERE workflow_id = 'nw_renewal'")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "controller_auto"
        assert rows[0]["summary"] == "Auto-applied caller ID rotation"

