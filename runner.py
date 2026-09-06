#!/usr/bin/env python3
import subprocess
import time
import sys
import os
import signal

RUN_INDEX = True
RUN_SWEEPER = True
SHOW_INDEX_LOGS = True
SHOW_SWEEPER_LOGS = False
WAIT_SECONDS = 10

def launch_process(cmd, show_logs, name):
    if show_logs:
        stdout = None
        stderr = None
    else:
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
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
    print(f"Index.py = {'ON' if RUN_INDEX else 'OFF'}")
    print(f"sweeper/main.py = {'ON' if RUN_SWEEPER else 'OFF'}")

    processes = []

    if RUN_INDEX:
        proc = launch_process(["python3", "index.py"], SHOW_INDEX_LOGS, "index.py")
        if proc:
            processes.append(("index.py", proc))
        else:
            sys.exit(1)

    if RUN_SWEEPER and RUN_INDEX:
        time.sleep(WAIT_SECONDS)

    if RUN_SWEEPER:
        proc = launch_process(["python3", "sweeper/main.py"], SHOW_SWEEPER_LOGS, "sweeper/main.py")
        if proc:
            processes.append(("sweeper/main.py", proc))
        else:
            for name, p in processes:
                p.terminate()
            sys.exit(1)

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
        for name, p in processes:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                p.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except:
                    pass
            except Exception:
                pass
        sys.exit(0)

if __name__ == "__main__":
    main()