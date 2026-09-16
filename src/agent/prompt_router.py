"""平台 LLM 日额度仲裁：自进化任务独占时让位；否则宝藏解码优先于矿价解析。"""
from __future__ import annotations

import json
import re

from .decision_log import trace
from .news_memory import NewsMemory, game_day
from .news_logging import log_news_event
from .protocol import MatchState


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    return text.strip()


def parse_json_object(text: str) -> dict:
    value = json.loads(_strip_fence(text))
    if not isinstance(value, dict):
        raise ValueError("LLM must return a JSON object")
    return value


COMBAT_SHOP_ITEMS = {
    "Medicine", "DizzyWeapon", "Bomb", "WallFixer",
    "WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2",
    "WallUpgradeVoucher1", "WallUpgradeVoucher2",
    "StationUpgradeVoucher1", "StationUpgradeVoucher2",
    "SmallRobotSummonOrder", "MiddleRobotSummonOrder",
    "LargeRobotSummonOrder", "BossRobotSummonOrder",
}
DAY_NIGHT_CYCLE = 130


def _task_item_catalog(state: MatchState) -> list:
    names = []
    for item in state.weapon_shop_list or []:
        if item.name and item.name not in COMBAT_SHOP_ITEMS and item.name not in names:
            names.append(item.name)
    return names


def make_ore_prompt(state: MatchState, memory: NewsMemory) -> str:
    day = game_day(state.round_no)
    news = state.world_news.official_news if state.world_news else ""
    schema = (
        '{"affectedOre":"iron|copper|stone","mineBannedDays":[int,...],'
        '"priceUpDays":[int,...],"notes":"简短说明"}'
    )
    return (
        "你是《未来战争》官方消息解析器。只根据本条官方消息推断矿价，不要使用民间传闻。"
        "游戏日从1起算；当天通常仍可采集，停工多从次日开始。"
        f"当前第{day}天（roundNo={state.round_no}，每天{DAY_NIGHT_CYCLE}回合）。"
        f"只返回一个JSON对象，不要Markdown：{schema}。"
        "没有明确矿种或停工/涨价措辞时：选最相关矿种，mineBannedDays 与 priceUpDays 用空数组，notes 说明依据不足。"
        "\n输入：" + json.dumps({"currentDay": day, "officialNews": news}, ensure_ascii=False)
    )


def make_treasure_prompt(state: MatchState, memory: NewsMemory) -> str:
    catalog = _task_item_catalog(state)
    width = state.map_info.width if state.map_info else None
    height = state.map_info.height if state.map_info else None
    schema = (
        '{"ready":bool,"altarPos":{"x":int,"y":int}|null,"items":[str,...],'
        '"openFromRound":int|null,"openToRound":int|null,'
        '"confidence":0.0,'
        '"notes":"一句中文：哪些字段有原文依据、缺什么"}'
    )
    rules = (
        "你只解读民间传闻，推断祭坛宝藏；不要解读官方消息，不要编造地图上没写的坐标或物品。\n"
        "规则：\n"
        f"- 地图范围 x∈[0,{width - 1 if width else '?'}]，y∈[0,{height - 1 if height else '?'}]；越界坐标必须改成 null。\n"
        f"- items 只能从 allowedTaskItems 里选，英文名必须完全一致；战斗/升级/召唤令不是献祭用品。当前可选：{catalog or ['（商店暂无任务用品）']}。\n"
        f"- 传闻说「第N天」时：openFromRound=(N-1)*{DAY_NIGHT_CYCLE}，openToRound=N*{DAY_NIGHT_CYCLE}-1。"
        f"当前 roundNo={state.round_no}，游戏日从1起算。\n"
        "- lastSummonTreasureResult：0未探测或非法，1成功，2地点/时间不对，3物品不对，4已空。2/3 时不要照抄 previousHypothesis。\n"
        "- previousHypothesis 仅供对照。传闻没写到的字段不要为了填满而沿用旧值；与新传闻冲突时以新传闻为准。\n"
        "置信度 confidence∈[0,1]，必须按证据打分，禁止无依据给高分：\n"
        "- 0.0–0.3：几乎没有坐标/物品原文，只是猜测。\n"
        "- 0.3–0.6：只抽出部分字段，或坐标/物品/时间互相矛盾。\n"
        "- 0.6–0.8：坐标和物品都有原文，窗口仍含糊。\n"
        "- 0.8–1.0：坐标、献祭物品、开启时间都能在传闻中找到对应句子。\n"
        "ready=true 仅当：altarPos、items 都有原文依据，且 confidence≥0.7。否则 ready=false。"
        "信息不足时仍可给出当前最佳猜测，但必须 ready=false、confidence 偏低，notes 写明缺什么。\n"
        f"只返回一个JSON对象，不要Markdown：{schema}。"
    )
    return rules + "\n输入：" + json.dumps({
        "roundNo": state.round_no,
        "gameDay": game_day(state.round_no),
        "map": {"width": width, "height": height},
        "legends": memory.data.get("legends", []),
        "allowedTaskItems": catalog,
        "previousHypothesis": memory.data.get("treasureHypothesis"),
        "lastSummonTreasureResult": state.last_summon_treasure_result,
        "resultCodeHint": {0: "未探测或非法", 1: "成功", 2: "地点或时间不对", 3: "物品不对", 4: "已空"},
    }, ensure_ascii=False)


class PromptRouter:
    """消费 llmResp；无自进化时申请日额度 prompt（官方未命中优先且每天至多 1 次，传闻保底 1 次）。"""

    def __init__(self, memory: NewsMemory):
        self.memory = memory

    def consume_llm_resp(self, state: MatchState) -> None:
        pending = self.memory.data.get("pendingConsumer")
        if state.phase_task:
            if pending:
                log_news_event(
                    event="llm_skipped", roundNo=state.round_no,
                    title="【LLM】自进化占用通道，跳过新闻消费",
                    consumer=pending,
                )
                self.memory.clear_pending()
            return
        if not pending:
            return
        # 同回合重试：不重复消费
        if self.memory.data.get("pendingRound") == state.round_no:
            return
        prompt_text = self.memory.data.get("pendingPrompt") or ""
        text = (state.llm_resp or "").strip()
        if not text:
            if state.round_no is not None and self.memory.data.get("pendingRound") is not None:
                if state.round_no > self.memory.data["pendingRound"]:
                    trace(state, None, "llm_empty", "等待中的新闻/宝藏 LLM 响应为空，清除 pending 以便重试",
                          consumer=pending)
                    log_news_event(
                        event="llm_empty", roundNo=state.round_no,
                        title=f"【LLM】{pending} 响应为空",
                        consumer=pending, promptText=prompt_text,
                    )
                    self.memory.clear_pending()
            return
        try:
            payload = parse_json_object(text)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            trace(state, None, "llm_parse_failed", "新闻/宝藏 LLM 输出无法解析", error=str(exc), consumer=pending)
            log_news_event(
                event="llm_output", roundNo=state.round_no,
                title=f"【LLM】{pending} 输出无法解析",
                consumer=pending, promptText=prompt_text, llmRespRaw=text,
                parsedJson=None, parseOk=False, applied=False, error=str(exc),
            )
            self.memory.clear_pending()
            return
        if pending == "treasure":
            self.memory.apply_treasure_llm(payload)
            hyp = self.memory.data.get("treasureHypothesis")
            trace(state, None, "treasure_decoded", "民间传闻 LLM 解码完成", hypothesis=hyp)
            log_news_event(
                event="llm_output", roundNo=state.round_no,
                title=f"【LLM】宝藏解码完成 ready={bool((hyp or {}).get('ready'))} conf={(hyp or {}).get('confidence')}",
                consumer="treasure", promptText=prompt_text, llmRespRaw=text,
                parsedJson=payload, parseOk=True, applied=True, plan=hyp,
            )
        elif pending == "ore":
            day = self.memory.data.get("officialDay") or game_day(state.round_no)
            self.memory.apply_ore_llm(payload, day)
            effects = self.memory.data.get("oreEffects")
            trace(state, None, "ore_decoded", "官方消息 LLM 解码完成", effects=effects)
            ore = payload.get("affectedOre")
            log_news_event(
                event="llm_output", roundNo=state.round_no,
                title=f"【LLM】矿价解码完成 {ore or '?'}",
                consumer="ore", promptText=prompt_text, llmRespRaw=text,
                parsedJson=payload, parseOk=True, applied=True,
                plan=self.memory.worker_json(state.round_no),
            )
        self.memory.clear_pending()

    def request_prompt(self, state: MatchState) -> str:
        """无自进化时每回合至多 1 条。启发式未命中的官方消息优先，但每天最多送 1 次；
        民间传闻若仍待解码，至少预留 1 次成功送推。"""
        if state.phase_task:
            return ""
        if (self.memory.data.get("pendingConsumer")
                and self.memory.data.get("pendingRound") == state.round_no
                and self.memory.data.get("pendingPrompt")):
            return self.memory.data["pendingPrompt"]
        if not self.memory.can_spend():
            return ""

        folk_needed = self.memory.folk_needs_prompt()
        official_needed = self.memory.official_needs_prompt()
        folk_unsent = folk_needed and not self.memory.data.get("treasurePromptSent")
        # 最后 1 次额度留给尚未送出的传闻，避免官方占满后传闻当天一次都没有。
        if official_needed and (self.memory.budget_remaining() > 1 or not folk_unsent):
            prompt = make_ore_prompt(state, self.memory)
            self.memory.mark_pending("ore", state.round_no, prompt)
            self.memory.data["needOreParse"] = False
            self.memory.data["lastOreParseDay"] = game_day(state.round_no)
            self.memory.save()
            trace(state, None, "llm_request", "申请矿价新闻 LLM", used=self.memory.data["llmUsed"])
            log_news_event(
                event="prompt_sent", consumer="ore", roundNo=state.round_no,
                title="【LLM】发送矿价解析 prompt",
                promptText=prompt, used=self.memory.data["llmUsed"],
            )
            return prompt

        if folk_needed:
            prompt = make_treasure_prompt(state, self.memory)
            self.memory.mark_pending("treasure", state.round_no, prompt)
            trace(state, None, "llm_request", "申请宝藏解码 LLM", used=self.memory.data["llmUsed"])
            log_news_event(
                event="prompt_sent", consumer="treasure", roundNo=state.round_no,
                title="【LLM】发送宝藏解码 prompt",
                promptText=prompt, used=self.memory.data["llmUsed"],
            )
            return prompt

        return ""
