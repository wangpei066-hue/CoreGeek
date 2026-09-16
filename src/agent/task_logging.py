"""任务诊断：进程 stderr + 判题器沙盒输出（下一回合 lastCmdResult）。"""
import json
import shlex
import sys

from .log_format import emit_stderr

MARKER = "PIONEER_TASK"


def log_task_exchange(event, sequence, payload, session, round_no):
    """完整记录平台收发数据；request/response 通过 sequence 配对。"""
    print(json.dumps({
        "marker": "PIONEER_TASK_EXCHANGE",
        "event": event,
        "sequence": sequence,
        "roundNo": round_no,
        "solverStage": session.get("stage", "idle"),
        "requestId": session.get("requestId"),
        "payload": payload,
    }, ensure_ascii=False), file=sys.stderr, flush=True)


def task_diagnostics(state, commands, previous_commands, solver_stage="idle", occupy_sandbox=True):
    pioneers = [r for r in state.team_our.roles if r.role_type == "pioneer"] if state.team_our else []
    event = "task_active" if state.phase_task else "task_idle"
    if any(c.get("action") == "acceptTask" for c in commands.values()):
        event = "accept_requested"
    pioneers_payload = [{"id": r.id, "health": r.health,
                         "pos": {"x": r.pos.x, "y": r.pos.y},
                         "command": commands.get(r.id),
                         "previousCommand": previous_commands.get(r.id),
                         "lastActionLegal": state.last_round_role_action_results.get(r.id)}
                        for r in pioneers]
    player_tasks = [{"taskType": t.task_type,
                     "taskPosition": {"x": t.task_position.x, "y": t.task_position.y},
                     "isValid": t.is_valid, "coldDownRounds": t.cold_down_rounds,
                     "timeoutRounds": t.timeout_rounds}
                    for t in state.team_our.player_tasks] if state.team_our else []
    team_id = state.team_our.team_id if state.team_our else None
    # 空闲回合不刷 stderr；接取/解题回合带 solver 细节。
    if event != "task_idle":
        title = "【自进化】申请接取任务" if event == "accept_requested" else f"【自进化】解题中 {solver_stage}"
        emit_stderr(
            MARKER, event, state.round_no, title=title,
            solverStage=solver_stage, teamId=team_id,
            pioneers=pioneers_payload, playerTasks=player_tasks,
            errors=[{"errorCode": e.error_code, "description": e.description} for e in state.errors],
            phaseTask=state.phase_task, lastCmdResult=state.last_cmd_result,
            llmResp=state.llm_resp,
        )
    if not state.phase_task or not occupy_sandbox:
        return ""  # 接取未生效、或正在等待工具/LLM时不占用沙盒。
    # 分片限制单次输出在接口的64KB内；每回合轮换，长原文可按chunkIndex重组。
    chunks = [state.phase_task[i:i + 4000] for i in range(0, len(state.phase_task), 4000)]
    index = (state.round_no or 0) % len(chunks)
    sandbox_record = {
        "marker": MARKER, "event": "task_active", "title": f"【自进化】原文分片 {index + 1}/{len(chunks)}",
        "roundNo": state.round_no, "teamId": team_id,
        "solverStage": solver_stage,
        "pioneers": pioneers_payload,
        "chunkIndex": index, "chunkCount": len(chunks),
        "phaseTaskChunk": chunks[index],
    }
    # 固定printf格式串，任务原文仅作为shell引用的数据，不执行任务中的命令。
    return "printf '%s\\n' " + shlex.quote(json.dumps(sandbox_record, ensure_ascii=False))
