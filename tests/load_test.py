"""Load test benchmark for public_03_load: measures events/sec, memory, and lag."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath("."))
import urllib.request
import json
import tracemalloc

def main():
    scenario = "scenarios/public_03_load.yaml"
    run_dir = "runs/load_test"
    db_path = "controller_load.db"

    # Clean previous run
    if os.path.exists(db_path):
        os.remove(db_path)

    print(f"Starting simulator serve on {scenario}...")
    server_cmd = [
        sys.executable, "-m", "sim", "serve",
        "--scenario", scenario,
        "--run-dir", run_dir,
        "--force",
        "--port", "8701"
    ]
    srv = subprocess.Popen(server_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        # Wait for server
        for _ in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8701/health", timeout=1) as res:
                    if res.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            print("Server failed to start")
            return

        print("Simulator ready. Launching controller for 1 simulated day (1440 min)...")
        from controller.loop import ControllerLoop

        ctrl = ControllerLoop(sim_url="http://127.0.0.1:8701", db_path=db_path)
        ctrl.bootstrap()

        tracemalloc.start()
        t0 = time.time()
        start_events = 0
        total_events = 0

        # Run 24 advances of 60 minutes = 1 simulated day (1440 minutes)
        for hour in range(24):
            th0 = time.time()
            st = ctrl.client.advance(minutes=60)
            sim_time = st.get("ts", "")
            sim_minute = st.get("sim_minute", 0)
            new_chunks = st.get("new_chunks", [])
            for ch in new_chunks:
                count = ctrl.ingestor.ingest_chunk(ch)
                total_events += count

            ctrl.reconcile_approvals(sim_minute, sim_time)

            # Optimization decisions
            for wid, spec in ctrl.workflows.items():
                cur_info = ctrl.client.get_workflow_config(wid)
                ctrl.current_configs[wid] = cur_info["config"]
                # Schedule
                ctrl.scheduler.optimize_schedule(wid, sim_minute, sim_time, spec, cur_info["version"], cur_info["config"])
                # Experiments
                ctrl.experiments.evaluate_experiments(wid, sim_minute, sim_time, spec, cur_info["version"], cur_info["config"])

            hour_time = time.time() - th0
            print(f"Simulated hour {hour+1}/24 ({sim_time}) completed in {hour_time:.2f}s, total events: {total_events}")

        total_time = time.time() - t0
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        events_per_sec = total_events / max(total_time, 1e-4)

        print("\n================ LOAD TEST BENCHMARK RESULTS ================")
        print(f"Scenario: public_03_load (1 simulated day)")
        print(f"Total events ingested: {total_events:,}")
        print(f"Total wall-clock time: {total_time:.2f} seconds ({total_time / 60:.2f} minutes)")
        print(f"Ingest Throughput: {events_per_sec:,.0f} events/second")
        print(f"Peak Memory (RSS/Trace): {peak / (1024 * 1024):.2f} MB")
        print(f"Target (< 10 minutes on 4 vCPU): {'PASSED' if total_time < 600 else 'FAILED'}")
        print("=============================================================")

        # Save benchmark report
        with open("reports/load_test_benchmark.txt", "w") as f:
            f.write(f"Scenario: public_03_load (1 simulated day)\n")
            f.write(f"Total events ingested: {total_events:,}\n")
            f.write(f"Total wall-clock time: {total_time:.2f} seconds ({total_time / 60:.2f} minutes)\n")
            f.write(f"Ingest Throughput: {events_per_sec:,.0f} events/second\n")
            f.write(f"Peak Memory: {peak / (1024 * 1024):.2f} MB\n")

    finally:
        srv.terminate()
        srv.wait()

if __name__ == "__main__":
    main()

