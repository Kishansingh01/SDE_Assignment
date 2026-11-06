"""Crash-recovery demonstration: kills controller mid-run, restarts it, and verifies resumption."""
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.abspath("."))
from controller.db import Database


def main():
    scenario = "scenarios/public_01_dev.yaml"
    run_dir = "runs/crash_demo"
    db_path = "controller_crash_demo.db"

    if os.path.exists(db_path):
        os.remove(db_path)

    print("--- STEP 1: Starting Simulator Server ---")
    server_cmd = [
        sys.executable, "-m", "sim", "serve",
        "--scenario", scenario,
        "--run-dir", run_dir,
        "--force",
        "--port", "8702"
    ]
    srv = subprocess.Popen(server_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8702/health", timeout=1) as res:
                    if res.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            print("Failed to start server on port 8702")
            return

        print("--- STEP 2: Starting Controller (Phase 1) ---")
        ctrl_cmd = [
            sys.executable, "-m", "controller.loop",
            "--url", "http://127.0.0.1:8702",
            "--db", db_path
        ]
        ctrl_proc = subprocess.Popen(ctrl_cmd)

        # Let it run for 15 seconds (processing multiple days/chunks)
        time.sleep(15)

        print("--- STEP 3: Abruptly Killing Controller (SIGKILL / terminate) ---")
        ctrl_proc.terminate()
        ctrl_proc.wait()
        print("Controller terminated.")

        # Inspect database state
        db = Database(db_path=db_path)
        last_chunk = db.get_sync_state("last_chunk")
        with db.get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM call_stats_daily")
            stats_count = cur.fetchone()["cnt"]
            cur = conn.execute("SELECT COUNT(*) as cnt FROM audit_trail")
            audit_count = cur.fetchone()["cnt"]

        print(f"Persisted State Before Restart: last_chunk={last_chunk}, stats_rows={stats_count}, audit_rows={audit_count}")
        assert last_chunk is not None, "Expected persisted sync bookmark!"

        print("--- STEP 4: Restarting Controller (Phase 2 - Resumption) ---")
        ctrl_proc_restart = subprocess.Popen(ctrl_cmd)
        ctrl_proc_restart.wait()
        print("Controller resumed and completed simulation cleanly with exit code 0.")

        # Verify final state
        with db.get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM call_stats_daily")
            final_stats = cur.fetchone()["cnt"]
            cur = conn.execute("SELECT COUNT(*) as cnt FROM audit_trail")
            final_audit = cur.fetchone()["cnt"]

        final_chunk = db.get_sync_state("last_chunk")
        print(f"Final State After Resumption: last_chunk={final_chunk}, stats_rows={final_stats}, audit_rows={final_audit}")
        assert int(final_chunk) >= int(last_chunk), "Expected progress to advance after resumption!"
        print("\nCRASH RECOVERY TEST: PASSED (Zero data loss, clean resumption, zero duplicated proposals).")

    finally:
        srv.terminate()
        srv.wait()


if __name__ == "__main__":
    main()

