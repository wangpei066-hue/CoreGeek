"""世界新闻诊断：进程 stderr + 判题器沙盒输出（下一回合 lastCmdResult）。

对齐 task_logging.PIONEER_TASK：平台下载日志可搜 NEWS_INFER。
"""
import json
import shlex
import sys

MARKER = "NEWS_INFER"
_NEWS_EVENT_CODES = {
    "ore_heuristic", "legend_appended", "ore_decoded", "treasure_decoded",
    "llm_request", "llm_empty", "llm_parse_failed", "summon_result",
}


def log_news_event(**payload):
    """即时事件（ingest/解码）写入 stderr，与沙盒诊断共用 marker。"""
    print(json.dumps({"marker": MARKER, **payload}, ensure_ascii=False), file=sys.stderr, flush=True)


def _clip(text, limit=800):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(+{len(text) - limit})"


def news_diagnostics(state, memory, commands, previous_commands):
    """汇总本回合新闻记忆；无自进化任务且无其它沙盒命令时，经 executeCmd 回传。"""
    news = state.world_news
    official = news.official_news if news else ""
    folk = news.folk_legends if news else ""
    data = memory.data if memory else {}
    legends = data.get("legends") or []
    pioneer_cmds = {}
    if state.team_our:
        for role in state.team_our.roles:
            if role.role_type == "pioneer":
                pioneer_cmds[str(role.id)] = {
                    "command": commands.get(role.id),
                    "previousCommand": previous_commands.get(role.id),
                    "lastActionLegal": state.last_round_role_action_results.get(role.id),
                    "pos": {"x": role.pos.x, "y": role.pos.y},
                    "backpack": list(role.backpack),
                }

    infer_events = [
        e for e in getattr(state, "decision_events", [])
        if e.get("code") in _NEWS_EVENT_CODES
    ]
    if data.get("pendingConsumer"):
        event = f"await_{data['pendingConsumer']}"
    elif any(e.get("code") in ("ore_decoded", "treasure_decoded") for e in infer_events):
        event = "decoded"
    elif any(e.get("code") in ("ore_heuristic", "legend_appended") for e in infer_events):
        event = "ingested"
    else:
        event = "snapshot"

    record = {
        "marker": MARKER,
        "event": event,
        "roundNo": state.round_no,
        "teamId": state.team_our.team_id if state.team_our else None,
        "worldNews": {
            "officialNews": _clip(official),
            "folkLegends": _clip(folk),
        },
        "needOreParse": bool(data.get("needOreParse")),
        "needTreasureDecode": bool(data.get("needTreasureDecode")),
        "llmDay": data.get("llmDay"),
        "llmUsed": data.get("llmUsed"),
        "pendingConsumer": data.get("pendingConsumer"),
        "pendingRound": data.get("pendingRound"),
        "oreEffects": data.get("oreEffects") or [],
        "legendCount": len(legends),
        "lastLegend": legends[-1] if legends else None,
        "treasureHypothesis": data.get("treasureHypothesis"),
        "treasureStage": data.get("treasureStage"),
        "treasureEmpty": bool(data.get("treasureEmpty")),
        "inferEvents": infer_events,
        "pioneers": pioneer_cmds,
        "lastSummonTreasureResult": state.last_summon_treasure_result,
        "vendorShopList": [{"name": i.name, "price": i.price} for i in (state.vendor_shop_list or [])],
    }
    # 与 PIONEER_TASK 相同：stderr 始终打一份；是否进平台下载不保证。
    print(json.dumps({**record, "llmResp": _clip(state.llm_resp, 1200)}, ensure_ascii=False),
          file=sys.stderr, flush=True)

    if state.phase_task:
        # 自进化占用沙盒期间不抢 executeCmd。
        return ""

    sandbox_record = {
        "marker": MARKER,
        "event": event,
        "roundNo": state.round_no,
        "teamId": record["teamId"],
        "worldNews": record["worldNews"],
        "needOreParse": record["needOreParse"],
        "needTreasureDecode": record["needTreasureDecode"],
        "llmUsed": record["llmUsed"],
        "pendingConsumer": record["pendingConsumer"],
        "oreEffects": record["oreEffects"],
        "legendCount": record["legendCount"],
        "lastLegend": record["lastLegend"],
        "treasureHypothesis": record["treasureHypothesis"],
        "treasureStage": record["treasureStage"],
        "treasureEmpty": record["treasureEmpty"],
        "inferEvents": infer_events,
        "pioneers": pioneer_cmds,
        "lastSummonTreasureResult": state.last_summon_treasure_result,
    }
    return "printf '%s\\n' " + shlex.quote(json.dumps(sandbox_record, ensure_ascii=False))
