"""Kill the process during review, restart, approve: exactly one refund row (milestone 7).

Uses file-backed SQLite for both business data and checkpoints so it runs anywhere; the
Postgres variant (AsyncPostgresSaver) needs the compose stack and is in test_postgres.py.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CHILD = Path(__file__).with_name("durability_child.py")


def run_child(mode: str, workdir) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "services" / "refund_desk")]),
           "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(CHILD), mode, str(workdir)], capture_output=True, text=True,
                          env=env, timeout=120, cwd=ROOT)


def refunds(r) -> int:
    return int(re.search(r"refunds=(\d+)", r.stdout).group(1))


def test_kill_during_review_then_resume_issues_exactly_one_refund(tmp_path):
    paused = run_child("pause", tmp_path)
    assert paused.returncode == 9 and "PAUSED refunds=0" in paused.stdout, paused.stdout + paused.stderr

    resumed = run_child("resume", tmp_path)                       # a different process
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert refunds(resumed) == 1

    replay = run_child("resume", tmp_path)                        # approve again: thread already finished
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert refunds(replay) == 1
