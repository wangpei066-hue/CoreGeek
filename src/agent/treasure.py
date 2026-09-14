"""民间传闻宝藏：解码假设落地为买物 / 就位 / summonTreasure。"""
from __future__ import annotations

from typing import Optional

from .decision_log import selected, trace
from .news_logging import log_news_event
from .grid import build_blocked_set, chebyshev, move_towards
from .news_memory import NewsMemory, game_day
from .protocol import MatchState, Pos, Role

TREASURE_URGENCY_ROUNDS = 15
SUMMON_OK = 1
SUMMON_BAD_PLACE_OR_TIME = 2
SUMMON_BAD_ITEMS = 3
SUMMON_EMPTY = 4


def hypothesis_items(memory: NewsMemory) -> list:
    hyp = memory.data.get("treasureHypothesis") or {}
    return list(hyp.get("items") or [])


def altar_pos(memory: NewsMemory) -> Optional[Pos]:
    hyp = memory.data.get("treasureHypothesis") or {}
    pos = hyp.get("altarPos")
    if not isinstance(pos, dict):
        return None
    try:
        return Pos(int(pos["x"]), int(pos["y"]))
    except (KeyError, TypeError, ValueError):
        return None


def in_open_window(state: MatchState, memory: NewsMemory) -> bool:
    hyp = memory.data.get("treasureHypothesis") or {}
    start, end = hyp.get("openFromRound"), hyp.get("openToRound")
    if state.round_no is None:
        return False
    if start is None and end is None:
        # 未知窗口：ready 后允许尝试
        return bool(hyp.get("ready"))
    if start is not None and state.round_no < int(start):
        return False
    if end is not None and state.round_no > int(end):
        return False
    return True


def rounds_until_window(state: MatchState, memory: NewsMemory) -> Optional[int]:
    hyp = memory.data.get("treasureHypothesis") or {}
    start = hyp.get("openFromRound")
    if start is None or state.round_no is None:
        return None
    return int(start) - int(state.round_no)


def items_ready(pioneer: Role, memory: NewsMemory) -> bool:
    needed = hypothesis_items(memory)
    if not needed:
        return False
    bag = list(pioneer.backpack)
    for item in needed:
        if item not in bag:
            return False
        bag.remove(item)
    return True


def missing_items(pioneer: Role, memory: NewsMemory) -> list:
    needed = hypothesis_items(memory)
    bag = list(pioneer.backpack)
    missing = []
    for item in needed:
        if item in bag:
            bag.remove(item)
        else:
            missing.append(item)
    return missing


def treasure_should_claim_pioneer(state: MatchState, pioneer: Role, memory: NewsMemory) -> bool:
    """开启窗内且物品齐，或窗口将至且正在筹备时，占用开拓者。"""
    if memory.data.get("treasureEmpty"):
        return False
    hyp = memory.data.get("treasureHypothesis") or {}
    if not hyp.get("ready") or not altar_pos(memory) or not hypothesis_items(memory):
        return False
    if pioneer.health <= 0:
        return False
    if in_open_window(state, memory) and items_ready(pioneer, memory):
        return True
    until = rounds_until_window(state, memory)
    if until is not None and 0 < until <= TREASURE_URGENCY_ROUNDS:
        return True
    if memory.data.get("treasureStage") in ("gather", "wait_window", "approach", "summon"):
        # 已在执行链路上：白天继续买物；窗口内强制占用
        if in_open_window(state, memory) or missing_items(pioneer, memory):
            return True
    return False


def handle_summon_result(state: MatchState, memory: NewsMemory) -> None:
    code = state.last_summon_treasure_result
    if code == 0:
        return
    summoned = any(
        cmd.get("action") == "summonTreasure"
        for cmd in (state.last_sent_command or {}).values()
    )
    if not summoned and code not in (SUMMON_OK, SUMMON_EMPTY):
        return
    trace(state, None, "summon_result", "处理召唤宝藏结果码", result_code=code)
    log_news_event(
        event="summon_result", roundNo=state.round_no,
        title=f"【宝藏】召唤结果码 {code}",
        resultCode=code,
    )
    if code == SUMMON_OK or code == SUMMON_EMPTY:
        memory.data["treasureEmpty"] = True
        memory.data["treasureStage"] = "done"
        memory.data["needTreasureDecode"] = False
        memory.data["buyTarget"] = None
        memory.save()
        return
    if code == SUMMON_BAD_ITEMS:
        memory.data["treasureHypothesis"] = None
        memory.data["treasureStage"] = "idle"
        memory.data["buyTarget"] = None
        memory.data["needTreasureDecode"] = True
        memory.save()
        return
    if code == SUMMON_BAD_PLACE_OR_TIME:
        # 保留物品，请求重新解码窗口/坐标
        memory.data["needTreasureDecode"] = True
        memory.data["treasureStage"] = "wait_window"
        memory.save()


def find_weapon_shop(state: MatchState):
    if not state.map_info:
        return None
    for zone in state.map_info.zones:
        if zone.neutral_type == "weaponShop":
            return zone
    return None


def item_price(name: str, state: MatchState) -> int:
    for item in state.weapon_shop_list or []:
        if item.name == name:
            return item.price
    return 15


def decide_treasure_action(pioneer: Role, state: MatchState, memory: NewsMemory,
                           blocked: set, reserved: set) -> Optional[dict]:
    """推进宝藏状态机；返回开拓者指令或 None。"""
    handle_summon_result(state, memory)
    if memory.data.get("treasureEmpty"):
        return None
    hyp = memory.data.get("treasureHypothesis") or {}
    if not hyp.get("ready") or not altar_pos(memory) or not hypothesis_items(memory):
        return None

    width, height = state.map_info.width, state.map_info.height
    altar = altar_pos(memory)
    missing = missing_items(pioneer, memory)

    if missing:
        memory.data["treasureStage"] = "gather"
        memory.save()
        target_item = memory.data.get("buyTarget") if memory.data.get("buyTarget") in missing else missing[0]
        memory.data["buyTarget"] = target_item
        memory.save()
        shop = find_weapon_shop(state)
        if shop is None:
            trace(state, pioneer.id, "treasure_no_shop", "无武器商店，无法购买任务用品")
            return None
        cost = item_price(target_item, state)
        if chebyshev(pioneer.pos, shop.pos) <= 1:
            if state.team_our.gold_num < cost:
                trace(state, pioneer.id, "treasure_no_gold", "购买任务用品金币不足", item=target_item, cost=cost)
                return None
            if len(pioneer.backpack) >= pioneer.back_pack_capability:
                trace(state, pioneer.id, "treasure_backpack_full", "背包已满，无法购买任务用品")
                return None
            return selected(state, pioneer.id, {"action": "buy", "name": target_item, "num": 1},
                            "购买宝藏献祭用品")
        step = move_towards(pioneer.pos, shop.pos, blocked | reserved, width, height)
        if step:
            reserved.add((step.x, step.y))
            return selected(state, pioneer.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]},
                            "前往武器商店购买宝藏用品")
        return None

    memory.data["buyTarget"] = None
    if not in_open_window(state, memory):
        memory.data["treasureStage"] = "wait_window"
        memory.save()
        until = rounds_until_window(state, memory)
        # 窗口未到：提前靠近祭坛（保持一格外等待，避免误站位）
        if until is not None and until <= TREASURE_URGENCY_ROUNDS:
            if chebyshev(pioneer.pos, altar) > 2:
                step = move_towards(pioneer.pos, altar, blocked | reserved, width, height)
                if step:
                    reserved.add((step.x, step.y))
                    return selected(state, pioneer.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]},
                                    "开启窗口将至，提前靠近祭坛")
        trace(state, pioneer.id, "treasure_wait_window", "物品已齐，等待开启窗口",
              until=until, day=game_day(state.round_no))
        return None

    memory.data["treasureStage"] = "approach"
    memory.save()
    if chebyshev(pioneer.pos, altar) <= 1:
        memory.data["treasureStage"] = "summon"
        memory.save()
        items = hypothesis_items(memory)
        return selected(
            state, pioneer.id,
            {"action": "summonTreasure", "targetPos": [{"x": altar.x, "y": altar.y}], "item": items},
            "献祭任务用品召唤宝藏",
        )
    step = move_towards(pioneer.pos, altar, blocked | reserved, width, height)
    if step:
        reserved.add((step.x, step.y))
        return selected(state, pioneer.id, {"action": "move", "targetPos": [{"x": step.x, "y": step.y}]},
                        "前往祭坛召唤宝藏")
    return None
