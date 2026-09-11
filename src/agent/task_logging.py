"""任务诊断：进程 stderr + 判题器沙盒输出（下一回合 lastCmdResult）。"""
import json
import shlex
import sys


MARKER = "PIONEER_TASK"


def task_diagnostics(state, commands, previous_commands, solver_stage="idle"):
    pioneers = [r for r in state.team_our.roles if r.role_type == "pioneer"] if state.team_our else []
    record = {
        "marker": MARKER,
        "solverStage": solver_stage,
        "roundNo": state.round_no,
        "teamId": state.team_our.team_id if state.team_our else None,
        "event": "task_active" if state.phase_task else "task_idle",
        "pioneers": [{"id": r.id, "health": r.health,
                      "pos": {"x": r.pos.x, "y": r.pos.y},
                      "command": commands.get(r.id),
                      "previousCommand": previous_commands.get(r.id),
                      "lastActionLegal": state.last_round_role_action_results.get(r.id)}
                     for r in pioneers],
        "playerTasks": [{"taskType": t.task_type,
                         "taskPosition": {"x": t.task_position.x, "y": t.task_position.y},
                         "isValid": t.is_valid, "coldDownRounds": t.cold_down_rounds,
                         "timeoutRounds": t.timeout_rounds}
                        for t in state.team_our.player_tasks] if state.team_our else [],
        "errors": [{"errorCode": e.error_code, "description": e.description} for e in state.errors],
    }
    if any(c.get("action") == "acceptTask" for c in commands.values()):
        record["event"] = "accept_requested"
    # stdout/stderr 是否进入下载文件由平台决定；不把它当作唯一日志通道。
    print(json.dumps({**record, "phaseTask": state.phase_task,
                      "lastCmdResult": state.last_cmd_result, "llmResp": state.llm_resp}, ensure_ascii=False),
          file=sys.stderr, flush=True)
    if not state.phase_task:
        return ""  # 接取当回合尚未确认任务生效，禁止提前调用沙盒。
    # 分片限制单次输出在接口的64KB内；每回合轮换，长原文可按chunkIndex重组。
    chunks = [state.phase_task[i:i + 4000] for i in range(0, len(state.phase_task), 4000)]
    index = (state.round_no or 0) % len(chunks)
    sandbox_record = {"marker": MARKER, "event": "task_active",
                      "roundNo": state.round_no, "teamId": record["teamId"],
                      "solverStage": solver_stage,
                      "pioneers": record["pioneers"],
                      "chunkIndex": index, "chunkCount": len(chunks),
                      "phaseTaskChunk": chunks[index]}
    # 固定printf格式串，任务原文仅作为shell引用的数据，不执行任务中的命令。
    return "printf '%s\\n' " + shlex.quote(json.dumps(sandbox_record, ensure_ascii=False))
