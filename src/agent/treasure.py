"""民间传闻宝藏：解码假设落地为买物 / 就位 / summonTreasure。"""
from __future__ import annotations

from typing import Optional

from .decision_log import selected, trace
from .news_logging import log_news_event
from .grid import build_blocked_set, chebyshev, move_towards
from .news_memory import NewsMemory, game_day, TREASURE_ACT_CONFIDENCE, clamp_confidence
from .protocol import MatchState, Pos, Role

TREASURE_URGENCY_ROUNDS = 15
TREASURE_BUY_FROM_DAY = 4  # 第四天起才买任务用品/召唤令/基地券；前三天金币留给升炮。
SUMMON_OK = 1
EARLY_GAME_BUY_ALLOW = frozenset({
    "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
    "WallUpgradeVoucher1", "WallUpgradeVoucher2",
    "Medicine", "WallFixer",
})
EMERGENCY_BUY_ALLOW = frozenset({"Bomb", "DizzyWeapon"})


def treasure_buys_allowed(state: MatchState) -> bool:
    """第四天之前不买祭坛任务用品。已买到手的仍可献祭。"""
    return game_day(state.round_no) >= TREASURE_BUY_FROM_DAY


def shop_buy_allowed(name: str, state: MatchState, emergency: bool = False) -> bool:
    """第四天前只买升炮/升墙/药/修复包；三炮二级后允许买一次基地券。高压才买炸弹眩晕。"""
    if not name:
        return False
    if game_day(state.round_no) >= TREASURE_BUY_FROM_DAY:
        return True
    if emergency and name in EMERGENCY_BUY_ALLOW:
        return True
    if name == "StationUpgradeVoucher1":
        from .brain import station_first_upgrade_pending, structure_priority_day
        if station_first_upgrade_pending(state) or structure_priority_day(state):
            return True
    return name in EARLY_GAME_BUY_ALLOW


SUMMON_BAD_PLACE_OR_TIME = 2
SUMMON_BAD_ITEMS = 3
SUMMON_EMPTY = 4


def hypothesis_confidence(memory: NewsMemory) -> float:
    hyp = memory.data.get("treasureHypothesis") or {}
    return clamp_confidence(hyp.get("confidence", 0))


def hypothesis_actionable(memory: NewsMemory) -> bool:
    hyp = memory.data.get("treasureHypothesis") or {}
    return bool(
        hyp.get("ready")
        and altar_pos(memory)
        and hypothesis_items(memory)
        and hypothesis_confidence(memory) >= TREASURE_ACT_CONFIDENCE
    )


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
    if not hypothesis_actionable(memory):
        return False
    if pioneer.health <= 0:
        return False
    if in_open_window(state, memory) and items_ready(pioneer, memory):
        return True
    missing = missing_items(pioneer, memory)
    can_shop = treasure_buys_allowed(state)
    until = rounds_until_window(state, memory)
    if until is not None and 0 < until <= TREASURE_URGENCY_ROUNDS:
        return (not missing) or can_shop
    if memory.data.get("treasureStage") in ("gather", "wait_window", "approach", "summon"):
        if in_open_window(state, memory) and not missing:
            return True
        if missing and can_shop:
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
    if not hypothesis_actionable(memory):
        if hyp.get("ready") and hypothesis_confidence(memory) < TREASURE_ACT_CONFIDENCE:
            trace(state, pioneer.id, "treasure_low_confidence",
                  "传闻 JSON 置信度不足，不开拓者去买物或召唤",
                  confidence=hypothesis_confidence(memory), threshold=TREASURE_ACT_CONFIDENCE)
        return None

    width, height = state.map_info.width, state.map_info.height
    altar = altar_pos(memory)
    missing = missing_items(pioneer, memory)

    if missing:
        if not treasure_buys_allowed(state):
            trace(state, pioneer.id, "treasure_buy_deferred",
                  "第四天前不买任务用品，金币留给武器升级",
                  day=game_day(state.round_no), missing=missing)
            return None
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
