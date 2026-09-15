"""本地决策诊断：只记录证据，不参与决策，也不扩展比赛响应协议。"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json


from .log_format import command_text, emit_stderr
from .news_logging import log_folk_plan, log_official_plan

CONSOLE_MARKER = "STRATEGY_DECISION"
WEAPON_BUILD_NAMES = ("gatling", "railgun", "rocket")
WALL_EVENT_CODES = {
    "persistent_wall_plan", "wall_material_blocked", "wall_no_stone", "wall_route_blocked",
    "funnel_layout", "opening_no_candidate", "stones_reserved_for_late_day",
}
WEAPON_EVENT_CODES = {
    "opening_rockets_first", "await_weapons", "opening_no_gold", "weapon_assignment",
    "build_conditions", "no_build_candidate", "weapon_upgrade_funding_gap",
    "weapon_upgrade_job_waiting_funds", "weapon_upgrade_job_transferred",
    "upgrade_job_preempted", "no_free_weapon", "weapon_cooldown", "no_target_in_range",
    "pioneer_voucher_job", "pioneer_voucher_wait_gold", "pioneer_buys_voucher",
}
PIONEER_EVENT_CODES = {
    "pioneer_task", "task_yields_to_defense", "task_yields_to_voucher",
    "task_not_enough_time", "no_pioneer_action", "pioneer_task_active_at_night",
    "treasure_buy_deferred", "treasure_wait_window", "treasure_wait_open_day",
    "treasure_decoded", "legend_appended",
}
ECONOMY_EVENT_CODES = {
    "income_mine", "cashout_priority", "sale_unreachable", "sale_too_late",
    "backpack_full", "no_reachable_mine", "sell_threshold",
}


def _all_events(report):
    return list(report.get("events") or [])


def _events_with_codes(events, codes):
    return [
        {k: v for k, v in event.items() if k != "command"}
        for event in events if event.get("code") in codes
    ]


def _role_line(role):
    cmd = role.get("command")
    return f"{role.get('role_type')} {role.get('role_id')} {command_text(cmd)}"


def emit_console_report(report):
    """stderr 按类分行：总览 / 武器 / 墙 / 自进化 / 经济。完整细节仍写 decision_*.json。"""
    summary = report.get("summary") or {}
    diag = report.get("diagnostics") or {}
    events = _all_events(report)
    round_no = report.get("round")
    roles = report.get("roles") or []
    role_bits = [_role_line(role) for role in roles]
    alerts = [a.get("code") for a in (diag.get("alerts") or [])]
    emit_stderr(
        CONSOLE_MARKER, "round", round_no,
        title="【回合】{phase} R{round} | 金{gold} | 武器{weapons} 墙{walls} | {roles}".format(
            phase=report.get("phase") or "?",
            round=round_no,
            gold=summary.get("gold"),
            weapons=summary.get("weapons"),
            walls=summary.get("walls"),
            roles="；".join(role_bits) if role_bits else "无角色",
        ),
        phase=report.get("phase"),
        gold=summary.get("gold"),
        weapons=summary.get("weapons"),
        walls=summary.get("walls"),
        robots=summary.get("robots"),
        alerts=alerts,
        roles=[
            {"id": role["role_id"], "type": role["role_type"],
             "status": role["status"], "command": role["command"]}
            for role in roles
        ],
    )
    news_plans = report.get("newsPlans") or {}
    official = news_plans.get("official") or {}
    folk = news_plans.get("folk") or {}
    if official.get("oreEffects"):
        log_official_plan(round_no, official)
    if folk:
        log_folk_plan(round_no, folk)

    weapon_actions, wall_actions, pioneer_actions, economy_actions = [], [], [], []
    for role in roles:
        cmd = role.get("command") or {}
        action = cmd.get("action")
        row = {"roleId": role["role_id"], "roleType": role["role_type"],
               "text": f"{role['role_id']} {command_text(cmd)}", "action": action,
               "name": cmd.get("name"), "targetPos": cmd.get("targetPos")}
        if action == "build" and cmd.get("name") in WEAPON_BUILD_NAMES:
            weapon_actions.append(row)
        elif action == "build" and cmd.get("name") == "wall":
            wall_actions.append(row)
        elif action == "collect":
            codes = {e.get("code") for e in (role.get("events") or [])}
            if "income_mine" in codes:
                economy_actions.append(row)
            else:
                wall_actions.append(row)
        elif action in ("buy", "use") and cmd.get("name") and (
                "Weapon" in (cmd.get("name") or "") or "Voucher" in (cmd.get("name") or "")
                or cmd.get("name") in WEAPON_BUILD_NAMES):
            weapon_actions.append(row)
        elif action in ("acceptTask", "submitAnswer", "summonTreasure"):
            pioneer_actions.append({**row, "taskAnswer": cmd.get("taskAnswer"), "item": cmd.get("item")})
        elif action == "buy" and cmd.get("name") and "Voucher" not in (cmd.get("name") or "") and "Weapon" not in (cmd.get("name") or ""):
            pioneer_actions.append(row)
        elif action == "sell":
            economy_actions.append(row)
        elif action == "attack":
            weapon_actions.append({**row, "controllerId": cmd.get("controllerId")})

    standing = diag.get("weapons") or []
    types = {}
    for w in standing:
        types[w.get("type")] = types.get(w.get("type"), 0) + 1
    standing_text = "、".join(f"{k}×{v}" for k, v in types.items()) or "无"
    round_text = "、".join(a["text"] for a in weapon_actions) or "无建造/开火"
    pending = (diag.get("weapon_upgrade_deadline") or {}).get("pending") or []
    emit_stderr(
        "BUILD_WEAPON", "status", round_no,
        title=f"【武器】已建 {standing_text} | 待升级{len(pending)} | 本回合 {round_text}",
        standing=[
            {"id": w.get("id"), "type": w.get("type"), "pos": w.get("pos"),
             "level": w.get("level"), "health": w.get("health"), "cooldown": w.get("cooldown")}
            for w in standing
        ],
        upgrade={
            "pending": pending,
            "goldRequired": (diag.get("weapon_upgrade_deadline") or {}).get("gold_required"),
            "goldAvailable": (diag.get("weapon_upgrade_deadline") or {}).get("gold_available"),
            "fundingGap": (diag.get("weapon_upgrade_deadline") or {}).get("funding_gap"),
        },
        thisRound=weapon_actions,
        events=_events_with_codes(events, WEAPON_EVENT_CODES),
    )

    primary = diag.get("primary") or {}
    outer = diag.get("outer") or {}
    missing_n = len(primary.get("missing") or [])
    wall_round = "、".join(a["text"] for a in wall_actions) or "无砌墙/采石"
    planned = primary.get("planned")
    built = primary.get("built")
    emit_stderr(
        "BUILD_WALL", "status", round_no,
        title=f"【围墙】一层 {built or 0}/{planned or 0} 缺口{missing_n} | 本回合 {wall_round}",
        primary={
            "planned": primary.get("planned"), "built": primary.get("built"),
            "missing": (primary.get("missing") or [])[:20],
            "missingCount": missing_n,
        },
        outer={
            "planned": outer.get("planned"), "built": outer.get("built"),
            "missingCount": len(outer.get("missing") or []),
            "unlocked": diag.get("outer_unlocked"),
        },
        thisRound=wall_actions,
        events=_events_with_codes(events, WALL_EVENT_CODES),
    )

    pioneer_round = "、".join(a["text"] for a in pioneer_actions) or "无接取/提交"
    pioneer_ev = _events_with_codes(events, PIONEER_EVENT_CODES)
    if pioneer_ev and pioneer_round == "无接取/提交":
        pioneer_round = pioneer_ev[0].get("message") or pioneer_ev[0].get("code")
    emit_stderr(
        "PIONEER_TASK", "round", round_no,
        title=f"【自进化】{pioneer_round}",
        thisRound=pioneer_actions,
        events=pioneer_ev,
        itemJobs=[
            {"roleId": role["role_id"], "job": role.get("item_job")}
            for role in roles if role.get("item_job")
        ],
    )

    eco_ev = _events_with_codes(events, ECONOMY_EVENT_CODES)
    notable = [e for e in eco_ev if e.get("code") != "income_mine"]
    if economy_actions or notable:
        eco_round = "、".join(a["text"] for a in economy_actions) or (
            notable[0].get("message") if notable else "经济事件"
        )
        emit_stderr(
            "ECONOMY", "status", round_no,
            title=f"【经济】{eco_round}",
            thisRound=economy_actions,
            actors=[
                {"id": a.get("id"), "ores": a.get("ore_counts"), "value": a.get("quoted_ore_value"),
                 "selling": a.get("selling_committed")}
                for a in (diag.get("actors") or [])
            ],
            events=eco_ev,
        )



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


def _news_plans(state):
    memory = getattr(state, "news_memory", None)
    if memory is None:
        return {"official": {}, "folk": {}}
    return {
        "official": memory.store_official_plan(state.round_no),
        "folk": memory.data.get("folkPlan") or memory.pioneer_json(),
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
    from .diagnostics import diagnostics
    try:
        metrics = diagnostics(state, commands, previous_snapshot, comparable, previous_commands)
    except Exception as exc:
        metrics = {'alerts': [{'code': 'DIAGNOSTICS_ERROR', 'message': str(exc)}]}
    return {
        "schema_version": 2, "sequence": sequence, "round": state.round_no,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "diagnostics": metrics,
        "phase": phase, "phase_basis": "策略按roundNo从0起算；官方起点尚待核验",
        # phaseTask 是 pioneer 接取任务后由系统返回的任务原文；写入决策日志，
        # 使接取动作与后续收到的任务内容可以在同一日志序列中关联。
        "phase_task": state.phase_task,
        "decision_ms": round(elapsed_ms, 3),
        "summary": {"gold": before["gold"], "weapons": sum(counts[t] for t in ("gatling", "railgun", "rocket")),
                    "walls": counts["wall"], "bases": [asdict(r) for r in roles if r.role_type == "station"],
                    "robots": len(state.robot.roles) if state.robot else 0},
        "roles": decisions, "events": events, "previous_feedback": feedback,
        "system_errors": [asdict(error) for error in state.errors],
        "observed_changes": changes,
        "newsPlans": _news_plans(state),
        "changes_note": "仅为快照差异，不代表由上一条指令造成。" if comparable else "无同局更早快照可比较（首请求、重启或上下文变化）。",
    }


def render_text(report):
    summary = report["summary"]
    lines = [f"回合 {report['round']} | {report['phase']} | 金币 {summary['gold']} | "
             f"武器 {summary['weapons']} | 围墙 {summary['walls']} | 机器人 {summary['robots']}",
             f"策略耗时 {report['decision_ms']} ms；{report['phase_basis']}"]
    for event in report["events"]:
        if event["role_id"] is None:
            lines.append(event["message"] + " " + json.dumps(
                {k: v for k, v in event.items() if k not in ("role_id", "code", "message")}, ensure_ascii=False))
    diagnostic = report.get('diagnostics', {})
    for alert in diagnostic.get('alerts', []):
        lines.append('【重点】' + json.dumps(alert, ensure_ascii=False))
    for key in ('primary', 'outer', 'outer_unlocked', 'gold_delta', 'actors', 'weapons', 'newsPlans'):
        if key in diagnostic or key in report:
            payload = diagnostic.get(key) if key in diagnostic else report.get(key)
            if payload:
                lines.append(f'{key}: ' + json.dumps(payload, ensure_ascii=False))
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


def emit_console_report(report):
    """向判题平台可见的 stderr 输出一行可检索的完整决策摘要。

    本地 JSON 保存完整事件；控制台仅保留每个角色的最终动作和原因，避免把
    任务原文、背包明细或重复路径事件刷满平台输出。
    """
    role_reports = []
    for role in report["roles"]:
        reasons = [{k: v for k, v in event.items() if k not in ('role_id', 'command')}
                   for event in role["events"]]
        role_reports.append({
            "id": role["role_id"], "type": role["role_type"],
            "pos": role["position"], "status": role["status"],
            "health": role["health"], "backpackCounts": role["backpack"],
            "commandKey": role["command_key"], "command": role["command"],
            "reasons": reasons, "pendingBuild": role["pending_build"],
            "itemJob": role["item_job"],
        })
    record = {
        "marker": CONSOLE_MARKER, "sequence": report["sequence"],
        "roundNo": report["round"], "phase": report["phase"],
        "summary": report["summary"], "roles": role_reports,
        "globalEvents": [event for event in report["events"] if event["role_id"] is None],
        "previousFeedback": report["previous_feedback"],
        "systemErrors": report["system_errors"],
        "observedChanges": report["observed_changes"],
        "decisionMs": report["decision_ms"],
        "schemaVersion": report['schema_version'],
        "timestampUtc": report['timestamp_utc'],
        "diagnostics": report.get('diagnostics', {}),
    }
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)
