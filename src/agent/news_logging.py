"""世界新闻/长上下文：平台下载搜 NEWS_INFER。

stderr 只打有内容的事件（带中文 title）；无自进化且本回合有新闻活动时经 printf 回传。
"""
import json
import shlex

from .log_format import clip, emit_stderr

MARKER = "NEWS_INFER"
_ACTIVITY_CODES = {
    "ore_heuristic", "legend_appended", "ore_decoded", "treasure_decoded",
    "llm_request", "llm_empty", "llm_parse_failed", "summon_result",
    "official_ingested", "folk_ingested", "prompt_sent", "llm_output",
    "official_plan", "folk_plan",
    "official_news_ingested", "folk_legend_ingested",
    "news_llm_prompt", "news_llm_applied", "news_llm_unparsed",
    "llm_skipped",
}


def log_news_event(event, roundNo=None, title="", **payload):
    emit_stderr(MARKER, event, roundNo, title=title, **payload)


def log_official_plan(round_no, plan, source=""):
    """官方消息标准决策 JSON（仅记录，不指挥工人）。"""
    plan = plan or {}
    effects = plan.get("oreEffects") or []
    src = source or ((effects[0].get("source") if effects else "") or "")
    ores = "、".join(e.get("affectedOre") or "?" for e in effects) or "无"
    banned = "、".join(plan.get("bannedOres") or []) or "无"
    stock = "、".join(plan.get("stockpileOres") or []) or "无"
    log_news_event(
        event="official_plan", roundNo=round_no,
        title=f"【官方】决策JSON {ores} 今禁采[{banned}] 抢收[{stock}] source={src}",
        plan=plan,
    )


def log_folk_plan(round_no, plan, source=""):
    """民间传闻标准决策 JSON（仅记录，不指挥开拓者）。"""
    plan = plan or {}
    src = source or plan.get("source") or ""
    pos = plan.get("altarPos")
    pos_s = f"({pos['x']},{pos['y']})" if isinstance(pos, dict) and "x" in pos else "未知"
    items = "、".join(plan.get("items") or []) or "未知"
    conf = plan.get("confidence")
    try:
        conf_s = f"{float(conf):.2f}"
    except (TypeError, ValueError):
        conf_s = "?"
    log_news_event(
        event="folk_plan", roundNo=round_no,
        title=f"【传闻】决策JSON ready={bool(plan.get('ready'))} conf={conf_s} 祭坛{pos_s} 物品[{items}] source={src}",
        plan=plan,
    )


def news_diagnostics(state, memory, commands, previous_commands):
    """沙盒回传本回合新闻摘要；stderr 快照改由 ingest/prompt 事件承担。"""
    news = state.world_news
    official = (news.official_news if news else "") or ""
    folk = (news.folk_legends if news else "") or ""
    data = memory.data if memory else {}
    legends = data.get("legends") or []
    infer_events = [
        e for e in getattr(state, "decision_events", [])
        if e.get("code") in _ACTIVITY_CODES
    ]
    pending = data.get("pendingConsumer")
    llm_raw = (state.llm_resp or "").strip()
    if pending:
        event = f"await_{pending}"
    elif any(e.get("code") in ("ore_decoded", "treasure_decoded", "news_llm_applied", "llm_output")
             for e in infer_events):
        event = "decoded"
    elif any(e.get("code") in ("prompt_sent", "news_llm_prompt", "llm_request") for e in infer_events):
        event = "prompt_sent"
    elif infer_events:
        event = "ingested"
    else:
        event = "snapshot"

    if not infer_events and not llm_raw:
        return ""
    if not state.team_our or not state.map_info or state.phase_task:
        return ""

    pioneers = {}
    for role in state.team_our.roles:
        if role.role_type != "pioneer":
            continue
        pioneers[str(role.id)] = {
            "command": commands.get(role.id),
            "previousCommand": previous_commands.get(role.id),
            "lastActionLegal": state.last_round_role_action_results.get(role.id),
            "pos": {"x": role.pos.x, "y": role.pos.y},
            "backpack": list(role.backpack),
        }
    sandbox_record = {
        "marker": MARKER,
        "event": event,
        "roundNo": state.round_no,
        "title": f"【新闻】{event}",
        "teamId": state.team_our.team_id if state.team_our else None,
        "worldNews": {"officialNews": clip(official, 1500), "folkLegends": clip(folk, 1500)},
        "promptText": clip(data.get("pendingPrompt") or "", 3500),
        "llmRespRaw": clip(llm_raw, 2000),
        "pendingConsumer": pending,
        "oreEffects": data.get("oreEffects") or [],
        "officialPlan": data.get("officialPlan") or {},
        "folkPlan": data.get("folkPlan") or {},
        "legendCount": len(legends),
        "legends": [row.get("text") for row in legends],
        "treasureHypothesis": data.get("treasureHypothesis"),
        "inferEvents": infer_events,
        "pioneers": pioneers,
        "lastSummonTreasureResult": state.last_summon_treasure_result,
    }
    return "printf '%s\\n' " + shlex.quote(json.dumps(sandbox_record, ensure_ascii=False))
