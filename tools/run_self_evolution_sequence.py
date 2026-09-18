#!/usr/bin/env python3
"""Run same-family tasks with one persistent solver state.

This is intentionally a thin harness: the solver remains responsible for all
exploration and submissions; the harness only preserves state across tasks.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from src.agent.local_task_env import LocalTaskEnvironment
from tools.run_local_task import DashScopeLLM, LocalTaskDriver


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("tasks", nargs="+", help="task filenames in the selected fixture family")
    p.add_argument("--fixtures", type=Path, default=Path("/tmp/selfEvolutionTask/1-fixed-step"))
    p.add_argument("--model", default="qwen3.6-27b")
    p.add_argument("--max-rounds", type=int, default=12)
    p.add_argument("--state-root", type=Path)
    args = p.parse_args()
    state_root = args.state_root or Path(tempfile.mkdtemp(prefix="self_evolution_sequence_"))
    llm = DashScopeLLM(model=args.model)
    report = {"stateRoot": str(state_root), "tasks": []}
    for task_name in args.tasks:
        env = LocalTaskEnvironment(args.fixtures, task_name)
        driver = LocalTaskDriver(env, llm, max_rounds=args.max_rounds, state_root=state_root)
        try:
            result = driver.run()
            session_path = state_root / "task_session.json"
            experience_path = state_root / "task_experience.json"
            session = json.loads(session_path.read_text()) if session_path.exists() else {}
            experience = json.loads(experience_path.read_text()) if experience_path.exists() else {}
            report["tasks"].append({
                "task": task_name,
                "success": result["success"],
                "rounds": result["rounds"],
                "experienceHit": bool(session.get("experienceHit")),
                "skillCount": len(experience.get("skills") or []),
            })
            if not result["success"]:
                break
        finally:
            driver.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if len(report["tasks"]) == len(args.tasks) and all(x["success"] for x in report["tasks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
