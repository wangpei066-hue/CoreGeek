"""平台 LLM 日额度仲裁：自进化任务独占时让位；否则按置信度在官方/传闻间择优。"""
from __future__ import annotations

import json
import re

from .decision_log import trace
from .news_memory import NewsMemory, game_day, joined_legend_text, legend_mentions_open_time
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
    history = []
    for row in memory.data.get("officialHistory") or []:
        if isinstance(row, dict) and row.get("text"):
            history.append({
                "heardOnDay": row.get("day"),
                "heardAtRound": row.get("round"),
                "text": row.get("text"),
            })
    return (
        "你是《未来战争》官方消息解析器。综合 officialHistory 全部原文与 previousOreEffects 推断矿价/禁采日程，"
        "不要使用民间传闻。游戏日从1起算；首发停工通知里「今天还能抢采、明日停工」时，禁采从次日开始。"
        f"当前第{day}天（roundNo={state.round_no}，每天{DAY_NIGHT_CYCLE}回合）。"
        "规则："
        "- 明确写停工/禁采/塌方及工期时：mineBannedDays/priceUpDays 必须是完整游戏日整数列表"
        "（例如明日停工修2天且今天=2 → [3,4]）；priceUpDays 通常与禁采日相同。"
        "禁止只把日程写在 notes 里而让两个数组为空——空数组会被当成「无禁采/无涨价」。"
        "- 「修复仍在进行/无法采集」是进度确认：必须保留 previousOreEffects 中尚未结束的禁采日，"
        "再把当前日并入；禁止只返回当前日而丢掉更早推出的后续禁采日（例如旧窗[3,4]时不得改成只含[3]）。"
        "- 「恢复开采/修复完成/即日起恢复」：对该矿返回空的 mineBannedDays 与 priceUpDays，用于清除旧禁采。"
        "- 没有明确矿种或停工/涨价/恢复措辞时：选最相关矿种，两个数组都为空，notes 说明依据不足（系统会保留旧窗）。"
        f"只返回一个JSON对象，不要Markdown：{schema}。"
        "\n输入：" + json.dumps({
            "currentDay": day,
            "officialNews": news,
            "officialHistory": history,
            "previousOreEffects": memory.data.get("oreEffects") or [],
        }, ensure_ascii=False)
    )


def make_treasure_prompt(state: MatchState, memory: NewsMemory) -> str:
    catalog = _task_item_catalog(state)
    width = state.map_info.width if state.map_info else None
    height = state.map_info.height if state.map_info else None
    schema = (
        '{"ready":bool,"altarPos":{"x":int,"y":int}|null,"items":[str,...],'
        '"openFromRound":int|null,"openToRound":int|null,'
        '"confidence":0.0,'
        '"notes":"一句中文：哪些字段有正文依据、缺什么"}'
    )
    x_max = width - 1 if width else "?"
    y_max = height - 1 if height else "?"
    rules = (
        "你只解读 legends[].text 正文里的民间传闻，推断祭坛宝藏。"
        "不要解读官方消息。不要编造正文里没有的坐标、物品或开启时间。\n"
        "规则：\n"
        f"- 地图范围 x∈[0,{x_max}]，y∈[0,{y_max}]；越界坐标必须改成 null。\n"
        f"- items 只能从 allowedTaskItems 里选，英文名必须完全一致；战斗/升级/召唤令不是献祭用品。"
        f"当前可选：{catalog or ['（商店暂无任务用品）']}。"
        "正文没有对应描述（石板/粉末/火焰等）时 items 用 []。\n"
        "- legends[].heardOnDay / heardAtRound 只表示「哪一天听到这条」，不是祭坛开启日。\n"
        f"- now.gameDay / now.roundNo 只表示现在，禁止用来填开启窗口。每天{DAY_NIGHT_CYCLE}回合，游戏日从1起算。\n"
        f"- 开启窗口：仅当正文明确写「第N天开启/可召唤/解开」时，"
        f"openFromRound=(N-1)*{DAY_NIGHT_CYCLE}，openToRound=N*{DAY_NIGHT_CYCLE}-1。"
        "上古传说、方位、听到日都不是开启时间。没有开启日原文时两个字段必须 null，"
        "禁止用 now、heardOnDay 或 previousHypothesis 填。\n"
        "- altarPos：正文必须出现具体数字坐标（如 (12,8) 或 x=12,y=8）。"
        "只有「西部」「东侧」等方位 → 必须 null，不要猜地图中点。\n"
        "- lastSummonTreasureResult：0未探测或非法，1成功，2地点/时间不对，3物品不对，4已空。"
        "2/3 时不要照抄 previousHypothesis。\n"
        "- previousHypothesis 仅供对照。无新正文依据的字段不要沿用。\n"
        "置信度 confidence∈[0,1]，必须按正文证据打分：\n"
        "- 0.0–0.3：几乎没有坐标/物品原文，只是猜测。\n"
        "- 0.3–0.6：只抽出部分字段（例如有物品无坐标），或字段互相矛盾。\n"
        "- 0.6–0.8：坐标和物品都有原文，窗口仍含糊。\n"
        "- 0.8–1.0：坐标、献祭物品、开启时间都能在正文中找到对应句子。\n"
        "ready=true 仅当：altarPos、items 都有正文依据，且 confidence≥0.7。否则 ready=false。"
        "信息不足时仍可给出当前最佳猜测，但必须 ready=false，notes 写明缺什么。\n"
        f"只返回一个JSON对象，不要Markdown：{schema}。"
    )
    legends = []
    for row in memory.data.get("legends") or []:
        if not isinstance(row, dict):
            continue
        legends.append({
            "heardOnDay": row.get("heardOnDay", row.get("day")),
            "heardAtRound": row.get("heardAtRound", row.get("round")),
            "text": row.get("text"),
        })
    prev = memory.data.get("treasureHypothesis")
    if isinstance(prev, dict):
        prev = dict(prev)
        if not legend_mentions_open_time(joined_legend_text(memory.data.get("legends"))):
            prev["openFromRound"] = None
            prev["openToRound"] = None
    return rules + "\n输入：" + json.dumps({
        "now": {"roundNo": state.round_no, "gameDay": game_day(state.round_no)},
        "map": {"width": width, "height": height},
        "legends": legends,
        "allowedTaskItems": catalog,
        "previousHypothesis": prev,
        "lastSummonTreasureResult": state.last_summon_treasure_result,
        "resultCodeHint": {0: "未探测或非法", 1: "成功", 2: "地点或时间不对", 3: "物品不对", 4: "已空"},
    }, ensure_ascii=False)


class PromptRouter:
    """消费 llmResp；无自进化时申请日额度 prompt。
    官方原文变化：当天固定至多 1 次矿价 LLM；其余额度给传闻（最多约 2 次）。"""

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
        """无自进化时每回合至多 1 条。
        默认官方原文变化优先占当天 1 次矿价 LLM；若上次宝藏置信度 >0.5 且传闻仍待解，则传闻优先。"""
        if state.phase_task:
            return ""
        if (self.memory.data.get("pendingConsumer")
                and self.memory.data.get("pendingRound") == state.round_no
                and self.memory.data.get("pendingPrompt")):
            return self.memory.data["pendingPrompt"]
        if not self.memory.can_spend():
            return ""

        folk_hot = self.memory.folk_priority_over_official()
        want_ore = self.memory.official_needs_prompt()
        want_folk = self.memory.folk_needs_prompt()

        # 默认官方优先。上次宝藏置信度 >0.5 时传闻优先占一次；
        # 当天已送过传闻且官方仍待解，则改送官方，避免饿死矿价 LLM。
        if want_folk and folk_hot:
            if want_ore and self.memory.data.get("treasurePromptSent"):
                return self._send_ore_prompt(state)
            return self._send_treasure_prompt(state)
        if want_ore:
            return self._send_ore_prompt(state)
        if want_folk:
            return self._send_treasure_prompt(state)
        return ""
    def _send_ore_prompt(self, state: MatchState) -> str:
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

    def _send_treasure_prompt(self, state: MatchState) -> str:
        prompt = make_treasure_prompt(state, self.memory)
        self.memory.mark_pending("treasure", state.round_no, prompt)
        trace(state, None, "llm_request", "申请宝藏解码 LLM", used=self.memory.data["llmUsed"])
        log_news_event(
            event="prompt_sent", consumer="treasure", roundNo=state.round_no,
            title="【LLM】发送宝藏解码 prompt",
            promptText=prompt, used=self.memory.data["llmUsed"],
        )
        return prompt