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
# 已有宝藏假设置信度超过此值时，传闻 LLM 优先于官方矿价 LLM。
FOLK_PRIORITY_CONFIDENCE = 0.5
_CN_DAY = r'(?:[0-9]+|[一二三四五六七八九十]+)'
_OPEN_TIME_IN_TEXT = re.compile(
    rf'第\s*{_CN_DAY}\s*[天日].{{0,16}}(?:开|启|召唤|解开|可进|窗口)'
    rf'|(?:开|启|召唤|解开|可进|窗口).{{0,16}}第\s*{_CN_DAY}\s*[天日]'
    rf'|回合\s*\d+',
    re.I,
)
COMBAT_ITEM_NAMES = {
    "Medicine", "DizzyWeapon", "Bomb", "WallFixer",
    "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
    "WallUpgradeVoucher1", "WallUpgradeVoucher2",
    "StationUpgradeVoucher1", "StationUpgradeVoucher2",
    "SmallRobotSummonOrder", "MiddleRobotSummonOrder",
    "LargeRobotSummonOrder", "BossRobotSummonOrder",
}
ORE_ALIASES = {
    "iron": ("铁矿区", "铁矿", "铁资源", "iron"),
    "copper": ("铜矿区", "铜矿", "铜资源", "copper"),
    "stone": ("石矿区", "石矿", "石资源", "石料", "石头", "stone"),
}
# 短名容易误伤（「铁」可出现在无关词里），仅在已有采矿语境时作为兜底。
ORE_SHORT = {"iron": "铁", "copper": "铜"}
DEFAULT_ORE_PRICES = {"stone": 1, "iron": 3, "copper": 5}
_CN_DAYS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}
_DISRUPT_RE = re.compile(
    r"停工|停产|停采|禁采|无法采集|无法开采|不能开采|停止开采|暂停开采|"
    r"中断开采|无法产出|矿区关闭|关闭矿区|封闭矿区|全面停工|暂停作业|停止作业"
)
_COLLAPSE_RE = re.compile(r"塌方|矿难|巷道.{0,6}受损|矿井.{0,8}事故")
_PRICE_RE = re.compile(
    r"涨价|稀缺|紧缺|回收价|收购价|价格上涨|价格上调|报价上调|供不应求|上浮|溢价"
)
_NEGATE_RE = re.compile(
    r"(?:未发生|并未发生|没有发生|不会发生).{0,8}(?:塌方|停工|矿难|停产)|"
    r"(?:不会|暂不|尚未|并未|未)(?:全面)?(?:停工|停产|禁采|停采|涨价)|"
    r"(?:不停工|未停工|不涨价|未涨价|无需停工)"
)
_RESUME_RE = re.compile(
    r"(?:即日起|现已|已经|正式)恢复(?:开采|生产|作业)|"
    r"已恢复(?:开采|生产)|结束停工|解除禁采|"
    r"修复(?:工程)?完成"
)
# 首发停工里常出现「需要2天才能恢复开采」——这是未来时，不能当复工。
_RESUME_FUTURE_RE = re.compile(
    r"(?:才能|方可|预计|需要|需|待).{0,16}恢复(?:开采|生产)|"
    r"(?:左右|上下).{0,8}(?:才能完成并)?恢复(?:开采|生产)"
)


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


def _normalize_official(text: str) -> str:
    text = (text or "").strip()
    trans = str.maketrans("０１２３４５６７８９，。；", "0123456789,.;")
    return text.translate(trans)


def _is_blank_official(text: str) -> bool:
    if not text:
        return True
    compact = re.sub(r"\s+", "", text)
    return compact in ("今日无重大新闻", "无重大新闻", "暂无官方消息", "无")


def _detect_ores(text: str) -> list:
    found = []
    mining = bool(re.search(r"矿|资源|回收|采集|开采|小贩|停工|停产|塌方", text))
    for ore, aliases in ORE_ALIASES.items():
        hit = False
        for alias in sorted(aliases, key=len, reverse=True):
            if alias.isascii():
                if re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE):
                    hit = True
                    break
            elif alias in text:
                hit = True
                break
        if not hit and mining:
            short = ORE_SHORT.get(ore)
            if short and short in text:
                hit = True
        if hit:
            found.append(ore)
    return found


def _parse_duration(text: str, default: int = 2) -> int:
    """停工/修复持续几天；读不到则默认 2（任务书示例）。上限 5，避免误吃无关数字。"""
    patterns = (
        r"(?:需要|需时|需|为期|工期|修复|停工|停产|封闭|关闭|持续|连续|暂停)[^\d一二两三四五]{0,8}(\d+)\s*天",
        r"(\d+)\s*天\s*(?:左右|上下)",
        r"约\s*(\d+)\s*天",
        r"为期\s*(\d+)\s*天",
        r"停工\s*(\d+)\s*天",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return max(1, min(int(match.group(1)), 5))
    match = re.search(r"(?:需要|需|为期|修复|停工|连续)\s*([一二两三四五])\s*天", text)
    if match:
        return _CN_DAYS[match.group(1)]
    match = re.search(r"([一二两三四五])\s*天\s*(?:左右|上下)", text)
    if match:
        return _CN_DAYS[match.group(1)]
    return default


def _parse_start_offset(text: str, has_disrupt: bool) -> Optional[int]:
    """相对发布日的禁采起始偏移。任务书示例里『今天还能抢采』与『明日停工』同时出现时，以明日为准。"""
    if re.search(r"明日|明天|次日|翌日", text):
        return 1
    if "大后天" in text:
        return 3
    if "后天" in text:
        return 2
    if re.search(r"即日起|立即(?:停|关|禁|封)|即刻|从即日起", text):
        return 0
    if has_disrupt and re.search(r"今天|今日|当日|今夜|今晚", text):
        return 0
    if has_disrupt:
        return 1
    return None


def heuristic_ore_effects(official_news: str, published_day: int) -> list:
    """从官方消息抽出各矿种的禁采/涨价日；匹配失败不编造。"""
    text = _normalize_official(official_news)
    if _is_blank_official(text) or _NEGATE_RE.search(text):
        return []
    ores = _detect_ores(text)
    if not ores:
        return []
    disrupt = bool(_DISRUPT_RE.search(text) or _COLLAPSE_RE.search(text))
    priced = bool(_PRICE_RE.search(text))
    if not disrupt and not priced:
        return []
    start_offset = _parse_start_offset(text, disrupt)
    duration = _parse_duration(text) if disrupt else _parse_duration(text, default=2)
    banned, price_up = [], []
    if disrupt and start_offset is not None:
        start = published_day + start_offset
        banned = list(range(start, start + duration))
        price_up = list(banned)
    elif priced:
        if re.search(r"今天|今日|当日|即日起", text):
            price_up = [published_day]
        else:
            start = published_day + (start_offset if start_offset is not None else 1)
            price_up = list(range(start, start + duration))
    if not banned and not price_up:
        return []
    return [
        {
            "affectedOre": ore,
            "mineBannedDays": sorted(set(banned)),
            "priceUpDays": sorted(set(price_up)),
            "notes": "heuristic",
            "source": "heuristic",
            "publishedDay": published_day,
        }
        for ore in ores
    ]


def heuristic_ore_effect(official_news: str, published_day: int) -> Optional[dict]:
    """LLM 失败时的弱假设：识别矿种 + 停工/涨价时间窗。多矿种时返回第一条。"""
    effects = heuristic_ore_effects(official_news, published_day)
    return effects[0] if effects else None


def joined_legend_text(legends) -> str:
    return " ".join(str(row.get("text") or "") for row in (legends or []) if isinstance(row, dict))


def legend_mentions_open_time(text: str) -> bool:
    """正文是否明确写了开启日/回合。听到传闻的那天、上古传说里的「开启」都不算。"""
    return bool(text and _OPEN_TIME_IN_TEXT.search(text))


def is_resume_official(text: str) -> bool:
    """是否为「现在已恢复」通报。首发里的『N天才能恢复开采』不算。"""
    if not text:
        return False
    if _RESUME_FUTURE_RE.search(text):
        return False
    return bool(_RESUME_RE.search(text))


def _int_day_list(values) -> list:
    days = []
    for value in values or []:
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            days.append(int(value))
    return sorted(set(days))


def merge_ore_effect(previous: Optional[dict], incoming: dict, resume: bool) -> dict:
    """进度确认与旧日程取并集；恢复开采则清空；空且非恢复则保留旧窗。"""
    effect = dict(incoming)
    if resume:
        effect["mineBannedDays"] = []
        effect["priceUpDays"] = []
        return effect
    new_banned = _int_day_list(effect.get("mineBannedDays"))
    new_price = _int_day_list(effect.get("priceUpDays"))
    if previous and not new_banned and not new_price:
        effect["mineBannedDays"] = _int_day_list(previous.get("mineBannedDays"))
        effect["priceUpDays"] = _int_day_list(previous.get("priceUpDays"))
        return effect
    if previous:
        new_banned = sorted(set(_int_day_list(previous.get("mineBannedDays"))) | set(new_banned))
        new_price = sorted(set(_int_day_list(previous.get("priceUpDays"))) | set(new_price))
    effect["mineBannedDays"] = new_banned
    effect["priceUpDays"] = new_price
    return effect


def _days_from_notes(notes: str) -> list:
    """LLM 偶尔把日程只写在 notes 里，尝试捞回数字日（避开「工期2天」这类）。"""
    text = notes or ""
    found = []
    for match in re.finditer(
        r"(?:禁采|停工|涨价)(?:日|天)?[为是:：]\s*([0-9]+(?:\s*[、,，和及至\-–~到]+\s*[0-9]+)*)",
        text,
    ):
        found.extend(int(x) for x in re.findall(r"[0-9]+", match.group(1)))
    for match in re.finditer(r"第\s*([0-9]+)\s*[、,，和及]\s*([0-9]+)\s*天", text):
        found.extend([int(match.group(1)), int(match.group(2))])
    return sorted({d for d in found if 1 <= d <= 20})


def fill_empty_ore_effect(effect: dict, official_text: str, published_day: int) -> dict:
    """LLM 交空窗时的兜底：优先 notes 里的日，再跑启发式。"""
    if effect.get("mineBannedDays") or effect.get("priceUpDays"):
        return effect
    if is_resume_official(official_text):
        return effect
    notes_days = _days_from_notes(str(effect.get("notes") or ""))
    # notes「禁采日为3和4」常见；工期「需要2天」也会出现 2，过滤掉单独的工期数字需靠语境。
    # 若 notes 同时出现 ≥2 个合理日，采用它们。
    if len(notes_days) >= 2:
        effect = dict(effect)
        effect["mineBannedDays"] = notes_days
        effect["priceUpDays"] = list(notes_days)
        effect["notes"] = (effect.get("notes") or "") + " | filled_from_notes"
        return effect
    for weak in heuristic_ore_effects(official_text, published_day):
        if weak.get("affectedOre") == effect.get("affectedOre"):
            effect = dict(effect)
            effect["mineBannedDays"] = list(weak.get("mineBannedDays") or [])
            effect["priceUpDays"] = list(weak.get("priceUpDays") or [])
            effect["notes"] = (effect.get("notes") or "") + " | filled_from_heuristic"
            return effect
    # 矿种对不上时，仍可用启发式第一条（同文通常只有一个矿）
    weak = heuristic_ore_effect(official_text, published_day)
    if weak and (weak.get("mineBannedDays") or weak.get("priceUpDays")):
        effect = dict(effect)
        if not effect.get("affectedOre"):
            effect["affectedOre"] = weak["affectedOre"]
        if effect.get("affectedOre") == weak.get("affectedOre"):
            effect["mineBannedDays"] = list(weak.get("mineBannedDays") or [])
            effect["priceUpDays"] = list(weak.get("priceUpDays") or [])
            effect["notes"] = (effect.get("notes") or "") + " | filled_from_heuristic"
    return effect


class NewsMemory:
    """落盘 state/news_memory.json：官方消息效应、传闻列表、宝藏假设与 LLM 额度。"""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "news_memory.json"
        self.data = {
            "context": None,
            "officialHash": None,
            "officialDay": None,
            "officialHistory": [],
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
            "orePromptSent": False,
            "treasurePromptSent": False,
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
            "officialHistory": [],
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
            "orePromptSent": False,
            "treasurePromptSent": False,
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
            self.data["orePromptSent"] = False
            self.data["treasurePromptSent"] = False

        news = state.world_news
        official = (news.official_news if news else "") or ""
        folk = (news.folk_legends if news else "") or ""
        if official and official != self.data.get("officialHash"):
            self.data["officialHash"] = official
            self.data["officialDay"] = day
            meaningful = official.strip() and "无重大新闻" not in official
            if meaningful:
                # 暂时不用启发式：官方原文变化后固定走矿价 LLM（每天至多 1 次）。
                # effects = heuristic_ore_effects(official, day)
                # for weak in effects:
                #     self._upsert_ore_effect(weak)
                # self.data["needOreParse"] = not bool(effects)
                # if effects and getattr(state, "decision_events", None) is not None:
                #     trace(state, None, "ore_heuristic", "官方消息关键词启发式已写入矿价日程",
                #           effects=effects)
                # weak = effects[0] if effects else None
                history = self.data.setdefault("officialHistory", [])
                if not history or history[-1].get("text") != official:
                    history.append({"day": day, "round": state.round_no, "text": official})
                self.data["needOreParse"] = True
                current = list(self.data.get("oreEffects") or [])
                log_news_event(
                    event="official_ingested", roundNo=state.round_no,
                    title=f"【新闻】官方消息 | {headline(official)}",
                    officialNews=official,
                    oreEffect=current[0] if current else None,
                    oreEffects=current,
                    officialHistory=[row.get("text") for row in history],
                )
                log_official_plan(state.round_no, self.store_official_plan(state.round_no),
                                  source="pending_llm")

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
        previous = next(
            (row for row in (self.data.get("oreEffects") or []) if row.get("affectedOre") == ore),
            None,
        )
        incoming = {
            "affectedOre": ore,
            "mineBannedDays": _int_day_list(payload.get("mineBannedDays")),
            "priceUpDays": _int_day_list(payload.get("priceUpDays")),
            "notes": payload.get("notes", ""),
            "source": "llm",
            "publishedDay": published_day,
        }
        # 与旧日程取并集，避免「仍在修复」把 Day2 推出的 [3,4] 盖成 [3]。
        official = self.data.get("officialHash") or ""
        resume = is_resume_official(official)
        effect = merge_ore_effect(previous, incoming, resume=resume)
        # #651：LLM 把「禁采日为3和4」只写进 notes、数组交空 → 禁采/涨价/抢收全丢。
        if not resume:
            effect = fill_empty_ore_effect(effect, official, published_day)
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
        if not legend_mentions_open_time(joined_legend_text(self.data.get("legends"))):
            hyp["openFromRound"] = None
            hyp["openToRound"] = None
        self.data["treasureHypothesis"] = hyp
        # 这一批原文已经解过；等新传闻或召唤失败 2/3 再问，避免同一批低分重刷额度。
        self.data["needTreasureDecode"] = False
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

    def folk_needs_prompt(self) -> bool:
        return bool(self.data.get("needTreasureDecode") and not self.data.get("treasureEmpty"))

    def last_folk_confidence(self) -> float:
        """上一轮（最近一次）宝藏 LLM 落地的置信度；尚无假设则为 0。"""
        hyp = self.data.get("treasureHypothesis")
        if not isinstance(hyp, dict):
            hyp = self.data.get("folkPlan")
        if not isinstance(hyp, dict):
            return 0.0
        return clamp_confidence(hyp.get("confidence", 0))

    def folk_priority_over_official(self) -> bool:
        """传闻待解且上次置信度已 >0.5 时，优先送推民间传闻。"""
        return self.folk_needs_prompt() and self.last_folk_confidence() > FOLK_PRIORITY_CONFIDENCE

    def official_needs_prompt(self) -> bool:
        """官方原文有变化待解，且当天还没送过矿价 LLM。"""
        return bool(self.data.get("needOreParse") and not self.data.get("orePromptSent"))

    def mark_pending(self, consumer: str, round_no: int, prompt: str) -> None:
        self.data["pendingConsumer"] = consumer
        self.data["pendingRound"] = round_no
        self.data["pendingPrompt"] = prompt
        self.data["llmUsed"] = int(self.data.get("llmUsed", 0)) + 1
        if consumer == "ore":
            self.data["orePromptSent"] = True
        elif consumer == "treasure":
            self.data["treasurePromptSent"] = True
        self.save()

    def clear_pending(self) -> None:
        self.data["pendingConsumer"] = None
        self.data["pendingRound"] = None
        self.data["pendingPrompt"] = None
        self.save()
