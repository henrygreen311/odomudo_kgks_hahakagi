#!/usr/bin/env python3
import subprocess
import time
import sys
import os
import signal

RUN_INDEX = False
RUN_SWEEPER = True

SHOW_INDEX_LOGS = False
SHOW_SWEEPER_LOGS = True
WAIT_SECONDS = 10

def launch_process(cmd, show_logs, name):
    if show_logs:
        stdout = None
        stderr = None
        log_status = "visible"
    else:
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
        log_status = "silenced"
    print(f"Starting {name} (logs: {log_status})...")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        return proc
    except Exception as e:
        print(f"Failed to start {name}: {e}")
        return None

def main():
    print("=" * 60)
    print("LAUNCHER SCRIPT")
    print("=" * 60)
    if RUN_INDEX:
        print(f"  index.py          will run (logs: {'ON' if SHOW_INDEX_LOGS else 'OFF'})")
    else:
        print("  index.py          will be SKIPPED")

    if RUN_SWEEPER:
        print(f"  sweeper/main.py   will run (logs: {'ON' if SHOW_SWEEPER_LOGS else 'OFF'})")
        if RUN_INDEX:
            print(f"     Waiting {WAIT_SECONDS}s after index.py starts")
    else:
        print("  sweeper/main.py   will be SKIPPED")
    print("=" * 60)

    processes = []

    if RUN_INDEX:
        proc = launch_process(["python3", "index.py"], SHOW_INDEX_LOGS, "index.py")
        if proc:
            processes.append(("index.py", proc))
        else:
            print("index.py failed to start. Exiting.")
            sys.exit(1)
    else:
        print("Skipping index.py (as configured).")

    if RUN_SWEEPER and RUN_INDEX:
        print(f"Waiting {WAIT_SECONDS} seconds...")
        time.sleep(WAIT_SECONDS)
    elif RUN_SWEEPER and not RUN_INDEX:
        print("Starting sweeper immediately (index.py is skipped).")

    if RUN_SWEEPER:
        proc = launch_process(["python3", "sweeper/main.py"], SHOW_SWEEPER_LOGS, "sweeper/main.py")
        if proc:
            processes.append(("sweeper/main.py", proc))
        else:
            print("sweeper/main.py failed to start. Exiting.")
            for name, p in processes:
                p.terminate()
            sys.exit(1)

    print("\nAll configured processes are running.")
    print("Press Ctrl+C to stop everything.\n")

    try:
        while True:
            time.sleep(1)
            for name, p in processes:
                if p.poll() is not None:
                    print(f"{name} exited unexpectedly (code {p.returncode}).")
                    for n, q in processes:
                        if q != p:
                            q.terminate()
                    sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        for name, p in processes:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                p.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except:
                    pass
            except Exception as e:
                print(f"Error stopping {name}: {e}")
        print("All processes terminated.")
        sys.exit(0)

if __name__ == "__main__":
    main()