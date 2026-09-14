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


def make_ore_prompt(state: MatchState, memory: NewsMemory) -> str:
    day = game_day(state.round_no)
    news = state.world_news.official_news if state.world_news else ""
    schema = (
        '{"affectedOre":"iron|copper|stone","mineBannedDays":[int,...],'
        '"priceUpDays":[int,...],"notes":"简短说明"}'
    )
    return (
        "你是《未来战争》官方消息解析器。根据官方消息推断哪种矿石受影响、"
        "哪些游戏日无法采集、哪些游戏日小贩回收价上涨。"
        f"游戏日从1起算；当前为第{day}天（roundNo={state.round_no}，每天130回合）。"
        f"只返回一个JSON对象，不要Markdown：{schema}。"
        "若无明确影响，affectedOre 仍选最相关矿种，空数组表示无禁采/无涨价。"
        "\n输入：" + json.dumps({"currentDay": day, "officialNews": news}, ensure_ascii=False)
    )


def make_treasure_prompt(state: MatchState, memory: NewsMemory) -> str:
    shop = [{"name": i.name, "price": i.price} for i in (state.weapon_shop_list or [])]
    schema = (
        '{"ready":bool,"altarPos":{"x":int,"y":int}|null,"items":[str,...],'
        '"openFromRound":int|null,"openToRound":int|null,"confidence":0-1}'
    )
    return (
        "你是《未来战争》民间传闻解读器。根据多日传闻推断祭坛宝藏："
        "地点坐标、需献祭的任务用品英文名、可开启的回合闭区间、是否已信息充足。"
        "地图宽高见输入；物品名必须来自 weaponShopList 中的任务用品。"
        f"只返回一个JSON对象，不要Markdown：{schema}。"
        "信息不足时 ready=false，仍可给出当前最佳猜测坐标/物品。"
        "\n输入：" + json.dumps({
            "roundNo": state.round_no,
            "map": {"width": state.map_info.width, "height": state.map_info.height} if state.map_info else None,
            "legends": memory.data.get("legends", []),
            "weaponShopList": shop,
            "previousHypothesis": memory.data.get("treasureHypothesis"),
            "lastSummonTreasureResult": state.last_summon_treasure_result,
        }, ensure_ascii=False)
    )


class PromptRouter:
    """消费 llmResp，并在无自进化任务时申请日额度 prompt。"""

    def __init__(self, memory: NewsMemory):
        self.memory = memory

    def consume_llm_resp(self, state: MatchState) -> None:
        pending = self.memory.data.get("pendingConsumer")
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
                title=f"【LLM】宝藏解码完成 ready={bool((hyp or {}).get('ready'))}",
                consumer="treasure", promptText=prompt_text, llmRespRaw=text,
                parsedJson=payload, parseOk=True, applied=True, hypothesis=hyp,
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
                parsedJson=payload, parseOk=True, applied=True, effects=effects,
            )
        self.memory.clear_pending()

    def request_prompt(self, state: MatchState) -> str:
        """phaseTask 活跃时返回空；否则按 宝藏 > 矿价 申请至多 1 次。"""
        if state.phase_task:
            return ""
        if (self.memory.data.get("pendingConsumer")
                and self.memory.data.get("pendingRound") == state.round_no
                and self.memory.data.get("pendingPrompt")):
            return self.memory.data["pendingPrompt"]
        if not self.memory.can_spend():
            return ""

        if self.memory.data.get("needTreasureDecode") and not self.memory.data.get("treasureEmpty"):
            prompt = make_treasure_prompt(state, self.memory)
            self.memory.mark_pending("treasure", state.round_no, prompt)
            trace(state, None, "llm_request", "申请宝藏解码 LLM", used=self.memory.data["llmUsed"])
            log_news_event(
                event="prompt_sent", consumer="treasure", roundNo=state.round_no,
                title="【LLM】发送宝藏解码 prompt",
                promptText=prompt, used=self.memory.data["llmUsed"],
            )
            return prompt

        if self.memory.data.get("needOreParse"):
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

        return ""
