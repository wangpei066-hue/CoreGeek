"""世界新闻：官方消息与民间传闻写入记忆与决策 JSON。

解析与启发式只生成 `officialPlan`/`folkPlan` 日志，当前不指挥采矿或宝藏动作。
"""
import json
import re

from .decision_log import trace, selected
from .grid import chebyshev, move_towards
from .protocol import Pos

DAILY_NEWS_LLM_LIMIT = 3
ORE_ALIASES = {
    "iron": ("铁矿", "铁资源", "铁", "iron"),
    "copper": ("铜矿", "铜资源", "铜", "copper"),
    "stone": ("石矿", "石头", "石料", "stone"),
}
DISRUPT_MARKERS = ("停工", "塌方", "无法采集", "无法产出", "禁采", "停产", "矿区关闭")
COORD_RE = re.compile(
    r"(?:坐标|位置|祭坛)?[（(]\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[)）]"
    r"|x\s*[=:：]\s*(-?\d+)\s*[,，\s]+y\s*[=:：]\s*(-?\d+)",
    re.IGNORECASE,
)
DAY_RE = re.compile(r"第\s*(\d+)\s*天")
KNOWN_COMBAT_ITEMS = {
    "Medicine", "DizzyWeapon", "Bomb", "WallFixer",
    "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
    "WallUpgradeVoucher1", "WallUpgradeVoucher2",
    "StationUpgradeVoucher1", "StationUpgradeVoucher2",
    "SmallRobotSummonOrder", "MiddleRobotSummonOrder",
    "LargeRobotSummonOrder", "BossRobotSummonOrder",
}
EXAMPLE_TASK_ITEMS = (
    "AcientTablet", "StarSand", "FlameBreath", "FrostPotion", "ThornAmulet", "IronWhistle",
)


def _blank_intel():
    return {
        "official": [],
        "legends": [],
        "forecast": {},
        "treasure": {},
        "llm_calls_by_day": {},
        "awaiting_llm": False,
    }


def intel_memory(state, persist=False):
    mem = state.policy_memory.get("world_intel")
    if mem is None:
        mem = _blank_intel()
        if persist:
            state.policy_memory["world_intel"] = mem
    return mem


def day_index(state):
    return (state.round_no or 0) // 130


def ingest_news(state):
    """每回合吸收快照里的官方消息与民间传闻，写入跨回合记忆。"""
    news = state.world_news
    if news is None:
        return intel_memory(state)
    official = (news.official_news or "").strip()
    folk = (news.folk_legends or "").strip()
    useful_official = official and official not in ("今日无重大新闻", "无重大新闻")
    if not useful_official and not folk and not state.policy_memory.get("world_intel"):
        return intel_memory(state)
    mem = intel_memory(state, persist=True)
    day = day_index(state)
    if useful_official and (not mem["official"] or mem["official"][-1]["text"] != official):
        mem["official"].append({"day": day, "round": state.round_no, "text": official})
        parsed = parse_official_forecast(official, day)
        mem["forecast"].update(parsed)
        trace(state, None, "official_news_ingested", "记录官方消息并更新矿石供需推断",
              day=day, ores=sorted(parsed), forecast=parsed)
    if folk and (not mem["legends"] or mem["legends"][-1]["text"] != folk):
        mem["legends"].append({"day": day, "round": state.round_no, "text": folk})
        trace(state, None, "folk_legend_ingested", "累计民间传闻，祭坛线索改由 LLM 解码",
              day=day, legends=len(mem["legends"]))
    if state.last_summon_treasure_result in (1, 4):
        mem.setdefault("treasure", {})["done"] = True
    return mem


def parse_official_forecast(text, event_day):
    """任务书5.1示例：当天还能采，随后两天停工且涨价。未匹配到的新闻不臆造。"""
    if not any(mark in text for mark in DISRUPT_MARKERS):
        return {}
    duration = 2
    found = re.search(r"(\d+)\s*天", text)
    if found:
        duration = max(1, int(found.group(1)))
    elif "后天" in text:
        duration = 2
    forecast = {}
    for ore, aliases in ORE_ALIASES.items():
        if any(alias in text for alias in aliases):
            forecast[ore] = {
                "event_day": event_day,
                "block_from": event_day + 1,
                "block_to": event_day + duration,
            }
    return forecast


def ore_blocked(state, mineral):
    memory = getattr(state, "news_memory", None)
    if memory is not None:
        from .news_memory import game_day
        return mineral in memory.banned_ores(game_day(state.round_no))
    spec = intel_memory(state).get("forecast", {}).get(mineral)
    if not spec:
        return False
    day = day_index(state)
    return spec["block_from"] <= day <= spec["block_to"]


def ores_to_stockpile(state):
    """停工前一天抢收，涨价窗口内不再囤。"""
    memory = getattr(state, "news_memory", None)
    if memory is not None:
        from .news_memory import game_day
        return memory.ores_to_stockpile(game_day(state.round_no))
    day = day_index(state)
    held = set()
    for ore, spec in intel_memory(state).get("forecast", {}).items():
        if spec["event_day"] <= day < spec["block_from"]:
            held.add(ore)
    return held


def ores_in_spike(state):
    memory = getattr(state, "news_memory", None)
    if memory is not None:
        from .news_memory import game_day
        day = game_day(state.round_no)
        return memory.price_boosted_ores(day) | memory.banned_ores(day)
    day = day_index(state)
    spiked = set()
    for ore, spec in intel_memory(state).get("forecast", {}).items():
        if spec["block_from"] <= day <= spec["block_to"]:
            spiked.add(ore)
    return spiked


def parse_legend_clues(mem, state):
    blob = "\n".join(item["text"] for item in mem.get("legends", []))
    plan = {}
    coords = []
    for match in COORD_RE.finditer(blob):
        x = match.group(1) or match.group(3)
        y = match.group(2) or match.group(4)
        coords.append({"x": int(x), "y": int(y)})
    if coords:
        plan["altar"] = coords[-1]
    items = []
    shop_names = [i.name for i in (state.weapon_shop_list or [])]
    for name in list(shop_names) + list(EXAMPLE_TASK_ITEMS):
        if name in KNOWN_COMBAT_ITEMS:
            continue
        if name and name in blob and name not in items:
            items.append(name)
    if items:
        plan["items"] = items
    days = [int(d) for d in DAY_RE.findall(blob)]
    if days:
        plan["openDay"] = max(0, days[-1] - 1) if days[-1] >= 1 else days[-1]
    return plan


def merge_treasure_plan(mem, update):
    plan = mem.setdefault("treasure", {})
    if plan.get("done"):
        return
    if update.get("altar"):
        plan["altar"] = update["altar"]
    if update.get("items"):
        plan["items"] = update["items"]
    if "openDay" in update:
        plan["openDay"] = update["openDay"]


def parse_intel_llm(text):
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        value = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def maybe_prompt(state):
    """官方消息 / 民间传闻已拆到 PromptRouter 两条 LLM；此处不再发混合 prompt。"""
    mem = state.policy_memory.get("world_intel") if getattr(state, "policy_memory", None) else None
    if isinstance(mem, dict):
        mem["awaiting_llm"] = False
    return "", ""


def treasure_ready(state):
    mem = intel_memory(state)
    plan = mem.get("treasure") or {}
    if plan.get("done") or state.last_summon_treasure_result in (1, 4):
        return False
    return bool(plan.get("altar") and plan.get("items"))


def decide_treasure(role, state, blocked, reserved):
    """开拓者买齐任务用品后，在开启日前往祭坛 summonTreasure。"""
    if role.role_type != "pioneer" or role.health <= 0:
        return False, None
    ingest_news(state)
    if not treasure_ready(state):
        return False, None
    plan = intel_memory(state)["treasure"]
    items = list(plan.get("items") or [])
    missing = [name for name in items if name not in role.backpack]
    from .brain import find_zone, item_cost
    if missing:
        from .treasure import treasure_buys_allowed
        from .news_memory import game_day
        if not treasure_buys_allowed(state):
            trace(state, role.id, "treasure_buy_deferred",
                  "第四天前不买任务用品，金币留给武器升级",
                  day=game_day(state.round_no), missing=missing)
            return False, None
        shop = find_zone(state, "weaponShop")
        if shop is None:
            trace(state, role.id, "treasure_shop_missing", "传闻已抽出用品，但快照没有武器商店")
            return False, None
        name = missing[0]
        cost = item_cost(name, state)
        if chebyshev(role.pos, shop.pos) <= 1:
            if state.team_our.gold_num < cost or len(role.backpack) >= role.back_pack_capability:
                trace(state, role.id, "treasure_buy_blocked", "金币或背包不足以购买祭坛用品", item=name)
                return False, None
            return True, selected(state, role.id, {"action": "buy", "name": name, "num": 1}, "购买民间传闻所需任务用品")
        step = move_towards(role.pos, shop.pos, blocked | reserved, state.map_info.width, state.map_info.height)
        if step:
            reserved.add((step.x, step.y))
            return True, selected(state, role.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, "前往商店购买祭坛用品")
        return False, None
    open_day = plan.get("openDay")
    if isinstance(open_day, int) and day_index(state) < open_day:
        trace(state, role.id, "treasure_wait_open_day", "用品已齐，等待传闻中的开启日", open_day=open_day)
        return False, None
    altar = Pos(plan["altar"]["x"], plan["altar"]["y"])
    if chebyshev(role.pos, altar) <= 1:
        return True, selected(state, role.id, {
            "action": "summonTreasure",
            "targetPos": [{"x": altar.x, "y": altar.y}],
            "item": items,
        }, "在祭坛献祭任务用品召唤宝藏")
    step = move_towards(role.pos, altar, blocked | reserved, state.map_info.width, state.map_info.height)
    if step:
        reserved.add((step.x, step.y))
        return True, selected(state, role.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]}, "前往传闻中的祭坛")
    trace(state, role.id, "treasure_altar_unreachable", "祭坛坐标当前不可达", altar=plan["altar"])
    return False, None
