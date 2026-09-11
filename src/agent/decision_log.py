"""本地决策诊断：只记录证据，不参与决策，也不扩展比赛响应协议。"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json


def trace(state, role_id, code, message, **details):
    """由实际经过的决策分支记录原因，避免事后根据动作猜测。"""
    state.decision_events.append({
        "role_id": role_id, "code": code, "message": message, **details,
    })


def selected(state, role_id, command, reason):
    trace(state, role_id, "selected", reason, command=deepcopy(command))
    return command


def snapshot(state):
    roles = state.team_our.roles if state.team_our else []
    return {
        "round": state.round_no,
        "context": deepcopy(state.memory_context),
        "gold": state.team_our.gold_num if state.team_our else None,
        "roles": {r.id: {"position": asdict(r.pos), "health": r.health,
                          "backpack": dict(Counter(r.backpack)), "level": r.level,
                          "type": r.role_type} for r in roles},
    }


def build_report(state, commands, previous_commands, before, previous_snapshot, sequence, elapsed_ms, phase):
    roles = state.team_our.roles if state.team_our else []
    counts = Counter(r.role_type for r in roles)
    events = getattr(state, "decision_events", [])
    decisions = []
    for role in roles:
        if role.role_type not in ("worker", "pioneer"):
            continue
        command_key = role.id if role.id in commands else next(
            (key for key, cmd in commands.items()
             if cmd.get("action") == "attack" and str(cmd.get("controllerId")) == str(role.id)), None)
        command = commands.get(command_key)
        role_events = [event for event in events if event["role_id"] == role.id]
        decisions.append({
            "role_id": role.id, "role_type": role.role_type,
            "position": asdict(role.pos), "health": role.health,
            "backpack": dict(Counter(role.backpack)),
            "status": "action" if command else "idle",
            "command_key": command_key, "command": command,
            "events": role_events,
            "note": None if role_events else "策略未记录原因，不能推断。",
            "pending_build": state.worker_build_targets.get(role.id),
            "item_job": state.worker_item_jobs.get(role.id),
        })
    feedback = []
    for key in sorted(set(previous_commands) | set(state.last_round_role_action_results)):
        result = state.last_round_role_action_results.get(key)
        feedback.append({
            "command_key": key, "command": previous_commands.get(key),
            "result": result,
            "message": "系统报告成功" if result is True else (
                "系统报告失败；未提供与此指令绑定的具体原因" if result is False else "系统未提供执行结果"),
        })
    changes = []
    comparable = (previous_snapshot is not None and before["context"] == previous_snapshot["context"]
                  and isinstance(before["round"], int) and isinstance(previous_snapshot["round"], int)
                  and before["round"] > previous_snapshot["round"])
    if comparable:
        if before["gold"] != previous_snapshot["gold"]:
            changes.append({"field": "gold", "before": previous_snapshot["gold"], "after": before["gold"]})
        for key in sorted(set(before["roles"]) | set(previous_snapshot["roles"])):
            old, new = previous_snapshot["roles"].get(key), before["roles"].get(key)
            if old != new:
                changes.append({"role_id": key, "before": old, "after": new})
    return {
        "schema_version": 1, "sequence": sequence, "round": state.round_no,
        "phase": phase, "phase_basis": "策略按roundNo从0起算；官方起点尚待核验",
        "decision_ms": round(elapsed_ms, 3),
        "summary": {"gold": before["gold"], "weapons": sum(counts[t] for t in ("gatling", "railgun", "rocket")),
                    "walls": counts["wall"], "bases": [asdict(r) for r in roles if r.role_type == "station"],
                    "robots": len(state.robot.roles) if state.robot else 0},
        "roles": decisions, "events": events, "previous_feedback": feedback,
        "system_errors": [asdict(error) for error in state.errors],
        "observed_changes": changes,
        "changes_note": "仅为快照差异，不代表由上一条指令造成。" if comparable else "无同局更早快照可比较（首请求、重启或上下文变化）。",
    }


def render_text(report):
    summary = report["summary"]
    lines = [f"回合 {report['round']} | {report['phase']} | 金币 {summary['gold']} | "
             f"武器 {summary['weapons']} | 围墙 {summary['walls']} | 机器人 {summary['robots']}",
             f"策略耗时 {report['decision_ms']} ms；{report['phase_basis']}"]
    for base in summary["bases"]:
        lines.append(f"基地 {base['id']}：血量 {base['health']}，等级 {base['level']}")
    for role in report["roles"]:
        action = json.dumps(role["command"], ensure_ascii=False) if role["command"] else "待机（未下发指令）"
        lines.append(f"{role['role_type']} {role['role_id']} @ {role['position']}：{action}")
        lines.extend(f"  [{event['code']}] {event['message']} "
                     + json.dumps({k: v for k, v in event.items() if k not in ('role_id', 'code', 'message', 'command')}, ensure_ascii=False)
                     for event in role["events"])
        if role["note"]:
            lines.append(f"  {role['note']}")
    for event in report["previous_feedback"]:
        lines.append(f"上一指令 {event['command_key']}：{event['message']}；"
                     + json.dumps(event['command'], ensure_ascii=False))
    for error in report["system_errors"]:
        lines.append("系统错误（未归因到角色）：" + json.dumps(error, ensure_ascii=False))
    lines.append(report["changes_note"])
    lines.extend(json.dumps(change, ensure_ascii=False) for change in report["observed_changes"])
    return "\n".join(lines) + "\n"


def write_report(log_dir, report):
    stem = f"decision_{report['sequence']:06d}"
    for suffix, text in ((".json", json.dumps(report, ensure_ascii=False, indent=2)),
                         (".txt", render_text(report))):
        with (log_dir / (stem + suffix)).open("x", encoding="utf-8") as stream:
            stream.write(text)
