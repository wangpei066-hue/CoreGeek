"""世界新闻：官方消息影响采矿/卖矿时机；民间传闻拼祭坛宝藏。

解析与启发式是策略参数，不是官方合法坐标或固定任务用品表。
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
        merge_treasure_plan(mem, parse_legend_clues(mem, state))
        trace(state, None, "folk_legend_ingested", "累计民间传闻并尝试抽出祭坛线索",
              day=day, legends=len(mem["legends"]), treasure=mem.get("treasure"))
    if state.last_summon_treasure_result in (1, 4):
        mem.setdefault("treasure", {})["done"] = True
    apply_llm_result(state, mem)
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
    spec = intel_memory(state).get("forecast", {}).get(mineral)
    if not spec:
        return False
    day = day_index(state)
    return spec["block_from"] <= day <= spec["block_to"]


def ores_to_stockpile(state):
    """停工前一天抢收，涨价窗口内不再囤。"""
    day = day_index(state)
    held = set()
    for ore, spec in intel_memory(state).get("forecast", {}).items():
        if spec["event_day"] <= day < spec["block_from"]:
            held.add(ore)
    return held


def ores_in_spike(state):
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


def apply_llm_result(state, mem):
    if not mem.get("awaiting_llm"):
        return
    parsed = parse_intel_llm(state.llm_resp or "")
    mem["awaiting_llm"] = False
    if not parsed:
        trace(state, None, "news_llm_unparsed", "新闻/传闻 LLM 返回无法解析，继续用启发式")
        return
    for row in parsed.get("forecast") or []:
        ore = row.get("ore")
        if ore in ORE_ALIASES:
            mem.setdefault("forecast", {})[ore] = {
                "event_day": int(row.get("eventDay", day_index(state))),
                "block_from": int(row.get("blockFromDay", day_index(state) + 1)),
                "block_to": int(row.get("blockToDay", day_index(state) + 2)),
            }
    update = {}
    if isinstance(parsed.get("altar"), dict) and isinstance(parsed["altar"].get("x"), int) and isinstance(parsed["altar"].get("y"), int):
        update["altar"] = {"x": parsed["altar"]["x"], "y": parsed["altar"]["y"]}
    if isinstance(parsed.get("items"), list) and all(isinstance(n, str) and n for n in parsed["items"]):
        update["items"] = parsed["items"]
    if isinstance(parsed.get("openDay"), int):
        update["openDay"] = parsed["openDay"]
    merge_treasure_plan(mem, update)
    trace(state, None, "news_llm_applied", "已合并新闻/传闻 LLM 推断", treasure=mem.get("treasure"), forecast=mem.get("forecast"))


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
    """自进化未占用 prompt 时，用每日额度推断新闻停工和祭坛条件。不发 executeCmd。"""
    if state.phase_task:
        return "", ""
    mem = intel_memory(state)
    if mem.get("treasure", {}).get("done"):
        return "", ""
    apply_llm_result(state, mem)
    news = state.world_news
    has_signal = bool(mem.get("legends") or mem.get("official"))
    if not has_signal:
        return "", ""
    plan = mem.get("treasure") or {}
    complete = bool(plan.get("altar") and plan.get("items"))
    heuristic_forecast = bool(mem.get("forecast"))
    official = (news.official_news or "").strip() if news else ""
    need_forecast = official and official not in ("今日无重大新闻", "无重大新闻") and not heuristic_forecast
    if complete and not need_forecast:
        return "", ""
    day = str(day_index(state))
    calls = mem.setdefault("llm_calls_by_day", {})
    if calls.get(day, 0) >= DAILY_NEWS_LLM_LIMIT:
        return "", ""
    calls[day] = calls.get(day, 0) + 1
    mem["awaiting_llm"] = True
    prompt = (
        "你是比赛世界新闻分析器。根据官方消息推断矿石停工/涨价窗口；根据累计民间传闻推断祭坛坐标、献祭物品英文名、开启日。"
        "开启日按 roundNo 从0起算的天数（0=第一天）。只返回一个JSON对象，不要Markdown："
        '{"forecast":[{"ore":"iron","eventDay":0,"blockFromDay":1,"blockToDay":2}],'
        '"altar":{"x":12,"y":8},"items":["AcientTablet"],"openDay":3}'
        "未知字段请省略，不要编造坐标或物品。\n"
        + json.dumps({
            "roundNo": state.round_no,
            "day": day_index(state),
            "official": [row["text"] for row in mem.get("official", [])[-4:]],
            "legends": [row["text"] for row in mem.get("legends", [])[-8:]],
            "shopItems": [i.name for i in (state.weapon_shop_list or [])],
            "currentTreasure": plan,
        }, ensure_ascii=False)
    )
    trace(state, None, "news_llm_prompt", "提交新闻/传闻推断 prompt", day=day, calls=calls[day])
    return prompt, ""


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
