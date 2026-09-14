"""世界新闻累积、矿价日程与跨回合持久化。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .protocol import MatchState
from .decision_log import trace
from .news_logging import log_news_event

DAY_NIGHT_CYCLE = 130
ORE_ALIASES = {
    "iron": ("iron", "铁", "铁矿"),
    "copper": ("copper", "铜", "铜矿"),
    "stone": ("stone", "石", "石矿", "石头"),
}
DEFAULT_ORE_PRICES = {"stone": 1, "iron": 3, "copper": 5}


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
            "legends": [],
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
            "legends": [],
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
                    log_news_event(event="ore_heuristic", roundNo=state.round_no, effect=weak)

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
                log_news_event(event="legend_appended", roundNo=state.round_no, day=day,
                               legendCount=len(legends), text=folk[:500])

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
        self.save()

    def apply_treasure_llm(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        hyp = {
            "ready": bool(payload.get("ready")),
            "altarPos": payload.get("altarPos"),
            "items": list(payload.get("items") or []),
            "openFromRound": payload.get("openFromRound"),
            "openToRound": payload.get("openToRound"),
            "confidence": payload.get("confidence", 0),
        }
        if hyp["altarPos"] and isinstance(hyp["altarPos"], dict):
            try:
                hyp["altarPos"] = {"x": int(hyp["altarPos"]["x"]), "y": int(hyp["altarPos"]["y"])}
            except (KeyError, TypeError, ValueError):
                hyp["altarPos"] = None
                hyp["ready"] = False
        self.data["treasureHypothesis"] = hyp
        self.data["needTreasureDecode"] = False
        self.data["lastTreasureDecodeDay"] = self.data.get("llmDay")
        if hyp.get("ready") and hyp.get("altarPos") and hyp.get("items"):
            if self.data.get("treasureStage") in (None, "idle"):
                self.data["treasureStage"] = "gather"
        self.save()

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
