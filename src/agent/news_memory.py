"""世界新闻累积、矿价日程与跨回合持久化。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .protocol import MatchState
from .decision_log import trace
from .log_format import headline
from .news_logging import log_folk_plan, log_news_event, log_official_plan

DAY_NIGHT_CYCLE = 130
TREASURE_ACT_CONFIDENCE = 0.7
COMBAT_ITEM_NAMES = {
    "Medicine", "DizzyWeapon", "Bomb", "WallFixer",
    "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
    "WallUpgradeVoucher1", "WallUpgradeVoucher2",
    "StationUpgradeVoucher1", "StationUpgradeVoucher2",
    "SmallRobotSummonOrder", "MiddleRobotSummonOrder",
    "LargeRobotSummonOrder", "BossRobotSummonOrder",
}
ORE_ALIASES = {
    "iron": ("iron", "铁", "铁矿"),
    "copper": ("copper", "铜", "铜矿"),
    "stone": ("stone", "石", "石矿", "石头"),
}
DEFAULT_ORE_PRICES = {"stone": 1, "iron": 3, "copper": 5}


def clamp_confidence(value) -> float:
    try:
        return max(0.0, min(1.0, round(float(value), 3)))
    except (TypeError, ValueError):
        return 0.0


def game_day(round_no: Optional[int]) -> int:
    """游戏日从 1 起算；roundNo 未知时按第 1 天。"""
    if round_no is None:
        return 1
    return int(round_no) // DAY_NIGHT_CYCLE + 1


def vendor_prices(state: MatchState) -> dict:
    prices = dict(DEFAULT_ORE_PRICES)
    for item in state.vendor_shop_list or []:
        if item.name in prices:
            prices[item.name] = item.price
    return prices


def heuristic_ore_effect(official_news: str, published_day: int) -> Optional[dict]:
    """LLM 失败时的弱假设：识别矿种 + 停工/涨价时间词。"""
    if not official_news or official_news in ("今日无重大新闻",):
        return None
    text = official_news
    ore = None
    for name, aliases in ORE_ALIASES.items():
        if any(a in text for a in aliases):
            ore = name
            break
    if ore is None:
        return None
    banned, price_up = [], []
    if re.search(r"明日|明天|次日", text) and re.search(r"停工|停产|无法采集|不能开采|全面停工", text):
        banned.extend([published_day + 1, published_day + 2])
        price_up.extend([published_day + 1, published_day + 2])
    elif re.search(r"今天|当日", text) and re.search(r"停工|停产", text):
        banned.append(published_day)
        price_up.append(published_day)
    if re.search(r"涨价|稀缺|回收价", text) and not price_up:
        price_up.extend([published_day + 1, published_day + 2])
    if not banned and not price_up:
        return None
    return {
        "affectedOre": ore,
        "mineBannedDays": sorted(set(banned)),
        "priceUpDays": sorted(set(price_up)),
        "notes": "heuristic",
        "source": "heuristic",
        "publishedDay": published_day,
    }


class NewsMemory:
    """落盘 state/news_memory.json：官方消息效应、传闻列表、宝藏假设与 LLM 额度。"""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "news_memory.json"
        self.data = {
            "context": None,
            "officialHash": None,
            "officialDay": None,
            "oreEffects": [],
            "officialPlan": {},
            "legends": [],
            "folkPlan": {},
            "treasureHypothesis": None,
            "treasureEmpty": False,
            "treasureStage": "idle",
            "buyTarget": None,
            "llmDay": None,
            "llmUsed": 0,
            "pendingConsumer": None,
            "pendingRound": None,
            "pendingPrompt": None,
            "lastOreParseDay": None,
            "lastTreasureDecodeDay": None,
            "needOreParse": False,
            "needTreasureDecode": False,
        }
        self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self.data.update(raw)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def reset(self):
        self.data = {
            "context": None,
            "officialHash": None,
            "officialDay": None,
            "oreEffects": [],
            "officialPlan": {},
            "legends": [],
            "folkPlan": {},
            "treasureHypothesis": None,
            "treasureEmpty": False,
            "treasureStage": "idle",
            "buyTarget": None,
            "llmDay": None,
            "llmUsed": 0,
            "pendingConsumer": None,
            "pendingRound": None,
            "pendingPrompt": None,
            "lastOreParseDay": None,
            "lastTreasureDecodeDay": None,
            "needOreParse": False,
            "needTreasureDecode": False,
        }
        self.save()

    def ingest(self, state: MatchState) -> None:
        if not state.team_our or not state.map_info:
            return
        base = next((r.pos for r in state.team_our.roles if r.role_type == "station"), None)
        context = [
            state.team_our.team_id,
            state.team_our.type,
            state.map_info.width,
            state.map_info.height,
            (base.x, base.y) if base else None,
        ]
        if self.data["context"] is not None and self.data["context"] != context:
            self.reset()
        prev_round = self.data.get("memoryRound")
        if prev_round is not None and state.round_no is not None and state.round_no < prev_round:
            self.reset()
        self.data["context"] = context
        self.data["memoryRound"] = state.round_no

        day = game_day(state.round_no)
        if self.data["llmDay"] != day:
            self.data["llmDay"] = day
            self.data["llmUsed"] = 0

        news = state.world_news
        official = (news.official_news if news else "") or ""
        folk = (news.folk_legends if news else "") or ""
        if official and official != self.data.get("officialHash"):
            self.data["officialHash"] = official
            self.data["officialDay"] = day
            meaningful = official.strip() and "无重大新闻" not in official
            if meaningful:
                self.data["needOreParse"] = True
                weak = heuristic_ore_effect(official, day)
                if weak:
                    self._upsert_ore_effect(weak)
                    if getattr(state, "decision_events", None) is not None:
                        trace(state, None, "ore_heuristic", "官方消息关键词启发式已写入矿价日程", effect=weak)
                log_news_event(
                    event="official_ingested", roundNo=state.round_no,
                    title=f"【新闻】官方消息 | {headline(official)}",
                    officialNews=official, oreEffect=weak,
                )
                log_official_plan(state.round_no, self.store_official_plan(state.round_no),
                                  source=(weak or {}).get("source") or "pending_llm")

        if folk and folk.strip():
            legends = self.data.setdefault("legends", [])
            if not legends or legends[-1].get("text") != folk:
                entry = {"day": day, "round": state.round_no, "text": folk}
                legends.append(entry)
                if not self.data.get("treasureEmpty"):
                    self.data["needTreasureDecode"] = True
                if getattr(state, "decision_events", None) is not None:
                    trace(state, None, "legend_appended", "民间传闻已累积，等待 LLM 解码",
                          day=day, legendCount=len(legends))
                log_news_event(
                    event="folk_ingested", roundNo=state.round_no,
                    title=f"【传闻】累计{len(legends)}条 | {headline(folk)}",
                    day=day, newLegend=folk,
                    legends=[row.get("text") for row in legends],
                )

        self.save()

    def _upsert_ore_effect(self, effect: dict) -> None:
        ore = effect.get("affectedOre")
        effects = [e for e in self.data.get("oreEffects", []) if e.get("affectedOre") != ore]
        effects.append(effect)
        self.data["oreEffects"] = effects

    def apply_ore_llm(self, payload: dict, published_day: int) -> None:
        ore = payload.get("affectedOre")
        if ore not in ORE_ALIASES:
            return
        effect = {
            "affectedOre": ore,
            "mineBannedDays": sorted({int(d) for d in payload.get("mineBannedDays", []) if isinstance(d, int) or str(d).isdigit()}),
            "priceUpDays": sorted({int(d) for d in payload.get("priceUpDays", []) if isinstance(d, int) or str(d).isdigit()}),
            "notes": payload.get("notes", ""),
            "source": "llm",
            "publishedDay": published_day,
        }
        # 兼容字符串数字
        effect["mineBannedDays"] = [int(d) for d in effect["mineBannedDays"]]
        effect["priceUpDays"] = [int(d) for d in effect["priceUpDays"]]
        self._upsert_ore_effect(effect)
        self.data["needOreParse"] = False
        self.data["lastOreParseDay"] = published_day
        plan = self.store_official_plan(self.data.get("memoryRound"))
        self.save()
        log_official_plan(self.data.get("memoryRound"), plan, source="llm")

    def apply_treasure_llm(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        items = [
            name for name in list(payload.get("items") or [])
            if isinstance(name, str) and name and name not in COMBAT_ITEM_NAMES
        ]
        confidence = clamp_confidence(payload.get("confidence", 0))
        ready = bool(payload.get("ready"))
        hyp = {
            "ready": ready,
            "altarPos": payload.get("altarPos"),
            "items": items,
            "openFromRound": payload.get("openFromRound"),
            "openToRound": payload.get("openToRound"),
            "confidence": confidence,
            "notes": str(payload.get("notes") or "")[:240],
            "source": "llm",
        }
        if hyp["altarPos"] and isinstance(hyp["altarPos"], dict):
            try:
                hyp["altarPos"] = {"x": int(hyp["altarPos"]["x"]), "y": int(hyp["altarPos"]["y"])}
            except (KeyError, TypeError, ValueError):
                hyp["altarPos"] = None
        else:
            hyp["altarPos"] = None
        if not hyp["altarPos"] or not hyp["items"] or confidence < TREASURE_ACT_CONFIDENCE:
            hyp["ready"] = False
        self.data["treasureHypothesis"] = hyp
        # 置信度不够就等后续传闻再解，不要把低分结果当成定论。
        self.data["needTreasureDecode"] = confidence < TREASURE_ACT_CONFIDENCE
        self.data["lastTreasureDecodeDay"] = self.data.get("llmDay")
        plan = self.store_folk_plan()
        self.save()
        log_folk_plan(self.data.get("memoryRound"), plan, source="llm")

    def worker_json(self, round_no: Optional[int]) -> dict:
        """官方消息标准决策 JSON：禁采/抢收/涨价日程。不指挥工人动作。"""
        day = game_day(round_no)
        effects = list(self.data.get("oreEffects") or [])
        return {
            "today": day,
            "oreEffects": effects,
            "bannedOres": sorted(self.banned_ores(day)),
            "stockpileOres": sorted(self.ores_to_stockpile(day)),
            "priceUpOres": sorted(self.price_boosted_ores(day)),
        }

    def pioneer_json(self) -> dict:
        """民间传闻标准决策 JSON：祭坛/物品/开启窗。不指挥开拓者动作。"""
        hyp = self.data.get("treasureHypothesis")
        return dict(hyp) if isinstance(hyp, dict) else {}

    def store_official_plan(self, round_no: Optional[int]) -> dict:
        plan = self.worker_json(round_no)
        self.data["officialPlan"] = plan
        return plan

    def store_folk_plan(self) -> dict:
        plan = self.pioneer_json()
        self.data["folkPlan"] = plan
        return plan

    def ores_to_stockpile(self, day: int) -> set:
        held = set()
        for effect in self.data.get("oreEffects", []):
            ore = effect.get("affectedOre")
            banned = effect.get("mineBannedDays") or []
            if ore and day not in banned and (day + 1) in banned:
                held.add(ore)
        return held

    def banned_ores(self, day: int) -> set:
        banned = set()
        for effect in self.data.get("oreEffects", []):
            ore = effect.get("affectedOre")
            if ore and day in effect.get("mineBannedDays", []):
                banned.add(ore)
        return banned

    def price_boosted_ores(self, day: int) -> set:
        boosted = set()
        for effect in self.data.get("oreEffects", []):
            ore = effect.get("affectedOre")
            if ore and day in effect.get("priceUpDays", []):
                boosted.add(ore)
        return boosted

    def budget_remaining(self) -> int:
        return max(0, 3 - int(self.data.get("llmUsed", 0)))

    def can_spend(self) -> bool:
        return self.budget_remaining() > 0 and self.data.get("pendingConsumer") is None

    def mark_pending(self, consumer: str, round_no: int, prompt: str) -> None:
        self.data["pendingConsumer"] = consumer
        self.data["pendingRound"] = round_no
        self.data["pendingPrompt"] = prompt
        self.data["llmUsed"] = int(self.data.get("llmUsed", 0)) + 1
        self.save()

    def clear_pending(self) -> None:
        self.data["pendingConsumer"] = None
        self.data["pendingRound"] = None
        self.data["pendingPrompt"] = None
        self.save()
