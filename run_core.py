#!/usr/bin/env python3
"""Start the FastAPI server and Celery worker together from the project root.

    python run_core.py           # API + worker (default)
    python run_core.py --api     # API only
    python run_core.py --worker  # worker only

The two processes run as children of this script. Ctrl-C (SIGINT) or SIGTERM
shuts both down cleanly.
"""
import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _python() -> str:
    """Return the current Python executable path."""
    return sys.executable


def _env() -> dict:
    """Inherit the current environment (picks up .env values if already loaded)."""
    return os.environ.copy()


def start_api() -> subprocess.Popen:
    print("[run_core] starting API  →  http://localhost:8000")
    return subprocess.Popen(
        [_python(), "run_api.py"],
        cwd=ROOT,
        env=_env(),
    )


def start_worker() -> subprocess.Popen:
    print("[run_core] starting Celery worker  →  queues: extractor, normalizer, executor")
    return subprocess.Popen(
        [
            _python(), "-m", "celery",
            "-A", "worker.celery_app",
            "worker",
            "--concurrency=4",
            "-Q", "extractor,normalizer,executor",
            "--loglevel=info",
        ],
        cwd=ROOT,
        env=_env(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run API and/or Celery worker.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--api", action="store_true", help="Start API only")
    group.add_argument("--worker", action="store_true", help="Start worker only")
    args = parser.parse_args()

    processes: list[subprocess.Popen] = []

    if args.api:
        processes.append(start_api())
    elif args.worker:
        processes.append(start_worker())
    else:
        processes.append(start_api())
        processes.append(start_worker())

    def _shutdown(signum, frame):
        print("\n[run_core] shutting down…")
        for p in processes:
            if p.poll() is None:
                p.terminate()
        for p in processes:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Wait — exit if either child dies unexpectedly
    while True:
        for p in processes:
            rc = p.poll()
            if rc is not None:
                name = "API" if p.args[1] == "run_api.py" else "worker"
                print(f"[run_core] {name} exited with code {rc}")
                _shutdown(None, None)
        signal.pause()


if __name__ == "__main__":
    main()
