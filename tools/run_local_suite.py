#!/usr/bin/env python3
"""Run all six fixtures in original log order and retain cross-task experience."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from src.agent.local_task_env import LocalTaskEnvironment
from tools.run_local_task import DashScopeLLM, DEFAULT_BASE_URL, DEFAULT_MODEL, LocalTaskDriver


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, default=Path("/tmp/selfEvolutionTask/1-fixed-step"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("/tmp/local-task-report.json"))
    parser.add_argument("--tasks", nargs="+", help="仅运行指定任务文件名，顺序以此参数为准")
    args = parser.parse_args()
    try:
        llm = DashScopeLLM(base_url=args.base_url, model=args.model, enable_thinking=args.thinking)
    except RuntimeError as exc:
        parser.error(str(exc))
    manifest = json.loads((args.fixtures / "manifest.json").read_text(encoding="utf-8"))
    shared_state = Path(tempfile.mkdtemp(prefix="local_suite_state_"))
    task_order = args.tasks or manifest["task_order"]
    unknown = [task for task in task_order if task not in manifest["task_order"]]
    if unknown:
        parser.error("未知任务: " + ", ".join(unknown))
    results = []
    next_round = 1
    for task in task_order:
        env = LocalTaskEnvironment(args.fixtures, task)
        driver = LocalTaskDriver(env, llm, max_rounds=args.max_rounds, state_root=shared_state,
                                 start_round=next_round)
        try:
            result = driver.run()
        except Exception as exc:
            result = {"success": False, "rounds": None, "trace": driver.trace,
                      "error": f"{type(exc).__name__}: {exc}"}
        finally:
            driver.close()
        next_round = int(result.get("end_round") or next_round) + 1
        results.append({"task": task, **result})
        partial = {"model": args.model, "thinking": args.thinking,
                   "success_count": sum(item["success"] for item in results),
                   "total": len(task_order), "results": results,
                   "complete": False}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"task": task, "success": result["success"],
                          "rounds": result.get("rounds"), "error": result.get("error")}, ensure_ascii=False))
    report = {"model": args.model, "thinking": args.thinking,
              "success_count": sum(item["success"] for item in results), "total": len(results),
              "results": results, "complete": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": f"{report['success_count']}/{report['total']}",
                      "report": str(args.output)}, ensure_ascii=False))
    raise SystemExit(0 if report["success_count"] == report["total"] else 1)


if __name__ == "__main__":
    main()
