"""新闻记忆、LLM 日额度仲裁与矿价启发式。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.news_memory import (
    NewsMemory, game_day, heuristic_ore_effect, heuristic_ore_effects, vendor_prices,
)
from src.agent.prompt_router import PromptRouter, parse_json_object
from src.agent.protocol import MatchState, MapInfo, TeamOur, WorldNews, ShopItem, Zone, Pos, Role
from src.agent.brain import best_ore_to_sell, nearest_mine


IRON_COLLAPSE = (
    "矿业管理局紧急通报：北部铁矿区昨夜发生严重矿井塌方事故。"
    "矿区将于明日全面停工，进行巷道加固。修复工程通常需要2天左右。"
)


class NewsMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = NewsMemory(Path(self.temp.name))

    def _state(self, round_no=0, official="", folk="", team_id="t1"):
        state = MatchState()
        state.round_no = round_no
        state.map_info = MapInfo(width=41, height=32, zones=[])
        state.team_our = TeamOur(
            type="challenger", team_id=team_id, team_name="A",
            gold_num=75, total_score=0, player_tasks=[],
            roles=[Role(id=10013, pos=Pos(10, 24), role_type="station", health=1500, level=1)],
        )
        state.world_news = WorldNews(official_news=official, folk_legends=folk)
        state.decision_events = []
        return state

    def test_game_day(self):
        self.assertEqual(game_day(0), 1)
        self.assertEqual(game_day(129), 1)
        self.assertEqual(game_day(130), 2)

    def test_heuristic_iron_ban(self):
        effect = heuristic_ore_effect(IRON_COLLAPSE, 1)
        self.assertEqual(effect["affectedOre"], "iron")
        self.assertEqual(effect["mineBannedDays"], [2, 3])
        self.assertEqual(effect["priceUpDays"], [2, 3])

    def test_heuristic_reads_duration_and_immediate_start(self):
        three = heuristic_ore_effect("铜矿区即日起停产，预计需要3天恢复开采。", 2)
        self.assertEqual(three["affectedOre"], "copper")
        self.assertEqual(three["mineBannedDays"], [2, 3, 4])
        two_cn = heuristic_ore_effect("石矿明日全面停工，修复需要两天左右。", 1)
        self.assertEqual(two_cn["affectedOre"], "stone")
        self.assertEqual(two_cn["mineBannedDays"], [2, 3])

    def test_heuristic_price_only_and_collapse_without_停工(self):
        priced = heuristic_ore_effect("小贩通报：铜资源回收价上调，市场紧缺。", 1)
        self.assertEqual(priced["affectedOre"], "copper")
        self.assertEqual(priced["mineBannedDays"], [])
        self.assertEqual(priced["priceUpDays"], [2, 3])
        collapse = heuristic_ore_effect("北部铁矿区昨夜发生严重矿井塌方，主巷道受损。", 1)
        self.assertEqual(collapse["mineBannedDays"], [2, 3])

    def test_heuristic_skips_negation_and_blank(self):
        self.assertIsNone(heuristic_ore_effect("今日无重大新闻", 1))
        self.assertIsNone(heuristic_ore_effect("铁矿区评估后确认未停工，暂不涨价。", 1))
        self.assertIsNone(heuristic_ore_effect("安全监察部门表示不会全面停工。", 1))
        self.assertEqual(heuristic_ore_effects("无重大新闻", 1), [])

    def test_heuristic_multiple_ores(self):
        effects = heuristic_ore_effects("铁矿与铜矿明日同时禁采，停工2天。", 1)
        ores = {row["affectedOre"]: row["mineBannedDays"] for row in effects}
        self.assertEqual(ores["iron"], [2, 3])
        self.assertEqual(ores["copper"], [2, 3])

    def test_ingest_official_and_legend(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="西部有一石门")
        self.memory.ingest(state)
        self.assertFalse(self.memory.data["needOreParse"])
        self.assertTrue(self.memory.data["needTreasureDecode"])
        self.assertEqual(self.memory.banned_ores(2), {"iron"})
        self.assertEqual(self.memory.data["legends"][-1]["text"], "西部有一石门")
        plan = self.memory.worker_json(0)
        self.assertEqual(plan["oreEffects"][0]["affectedOre"], "iron")
        self.assertEqual(plan["stockpileOres"], ["iron"])
        self.assertEqual(self.memory.data["officialPlan"]["stockpileOres"], ["iron"])
        self.assertIsNone(self.memory.data.get("treasureHypothesis"))
        self.assertEqual(self.memory.data.get("folkPlan") or {}, {})
        # 同文不重复追加
        self.memory.ingest(state)
        self.assertEqual(len(self.memory.data["legends"]), 1)

    def test_llm_budget_resets_each_day(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="情报1")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        p1 = router.request_prompt(state)
        self.assertIn("民间传闻", p1)
        self.assertEqual(self.memory.data["llmUsed"], 1)
        self.memory.data["needTreasureDecode"] = False
        self.memory.clear_pending()
        self.memory.data["needOreParse"] = True
        p2 = router.request_prompt(state)
        self.assertIn("官方消息", p2)
        self.assertEqual(self.memory.data["llmUsed"], 2)
        self.memory.clear_pending()
        # 耗尽
        self.memory.data["llmUsed"] = 3
        self.memory.data["needOreParse"] = True
        self.assertEqual(router.request_prompt(state), "")
        # 跨天重置
        day2 = self._state(130, official="新消息关于铜矿停产", folk="情报2")
        self.memory.ingest(day2)
        self.assertEqual(self.memory.data["llmUsed"], 0)
        self.assertTrue(self.memory.can_spend())

    def test_heuristic_hit_skips_official_llm_sends_folk(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="西部有一石门")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        prompt = router.request_prompt(state)
        self.assertIn("民间传闻", prompt)
        self.assertNotIn("官方消息解析器", prompt)
        self.assertTrue(self.memory.data["treasurePromptSent"])
        self.assertFalse(self.memory.data["orePromptSent"])

    def test_heuristic_miss_official_llm_before_folk(self):
        state = self._state(0, official="安全委员会发布例行通报，请各队关注后续安排。", folk="西部有一石门")
        self.memory.ingest(state)
        self.assertTrue(self.memory.data["needOreParse"])
        router = PromptRouter(self.memory)
        first = router.request_prompt(state)
        self.assertIn("官方消息", first)
        self.assertTrue(self.memory.data["orePromptSent"])
        self.memory.clear_pending()
        state.round_no = 1
        second = router.request_prompt(state)
        self.assertIn("民间传闻", second)
        self.assertTrue(self.memory.data["treasurePromptSent"])

    def test_official_llm_at_most_once_and_last_slot_saved_for_folk(self):
        state = self._state(0, official="安全委员会发布例行通报。", folk="西部有一石门")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        router.request_prompt(state)
        self.assertEqual(self.memory.data["pendingConsumer"], "ore")
        self.memory.clear_pending()
        self.memory.data["needOreParse"] = True
        state.round_no = 1
        again = router.request_prompt(state)
        self.assertIn("民间传闻", again)
        self.assertEqual(self.memory.data["llmUsed"], 2)

        self.memory.clear_pending()
        self.memory.data["llmUsed"] = 2
        self.memory.data["treasurePromptSent"] = False
        self.memory.data["needTreasureDecode"] = True
        self.memory.data["needOreParse"] = True
        self.memory.data["orePromptSent"] = False
        state.round_no = 2
        last = router.request_prompt(state)
        self.assertIn("民间传闻", last)
        self.assertFalse(self.memory.data["orePromptSent"])

    def test_phase_task_blocks_news_prompt(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="情报")
        self.memory.ingest(state)
        state.phase_task = "做题"
        self.assertEqual(PromptRouter(self.memory).request_prompt(state), "")

    def test_phase_task_does_not_consume_task_json_as_news(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="情报")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        router.request_prompt(state)
        self.assertEqual(self.memory.data["pendingConsumer"], "treasure")
        state.round_no = 1
        state.phase_task = "做题"
        state.llm_resp = '{"action":"submit","taskAnswer":"2"}'
        router.consume_llm_resp(state)
        self.assertIsNone(self.memory.data["pendingConsumer"])
        hyp = self.memory.data.get("treasureHypothesis") or {}
        self.assertFalse(hyp.get("ready"))

    def test_consume_ore_and_treasure_llm(self):
        state = self._state(5, official=IRON_COLLAPSE, folk="石门需三钥")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        router.request_prompt(state)
        self.assertEqual(self.memory.data["pendingConsumer"], "treasure")
        state.round_no = 6
        state.llm_resp = json.dumps({
            "ready": True,
            "altarPos": {"x": 12, "y": 8},
            "items": ["AcientTablet", "StarSand"],
            "openFromRound": 200,
            "openToRound": 260,
            "confidence": 0.9,
        })
        state.decision_events = []
        router.consume_llm_resp(state)
        hyp = self.memory.data["treasureHypothesis"]
        self.assertTrue(hyp["ready"])
        self.assertEqual(hyp["confidence"], 0.9)
        self.assertEqual(hyp["altarPos"], {"x": 12, "y": 8})
        self.assertIsNone(self.memory.data["pendingConsumer"])

        self.memory.data["needOreParse"] = True
        state.llm_resp = ""
        state.round_no = 7
        router.request_prompt(state)
        state.round_no = 8
        state.llm_resp = '{"affectedOre":"iron","mineBannedDays":[2,3],"priceUpDays":[2,3],"notes":"ok"}'
        state.decision_events = []
        router.consume_llm_resp(state)
        self.assertIn("iron", self.memory.banned_ores(2))

    def test_low_confidence_treasure_json_is_not_ready_and_retries(self):
        state = self._state(0, folk="西部有一石门")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        prompt = router.request_prompt(state)
        self.assertIn("置信度", prompt)
        self.assertIn("allowedTaskItems", prompt)
        state.round_no = 1
        state.llm_resp = json.dumps({
            "ready": True,
            "altarPos": {"x": 1, "y": 2},
            "items": ["AcientTablet"],
            "openFromRound": None,
            "openToRound": None,
            "confidence": 0.4,
            "notes": "只有石门，没有坐标原文",
        })
        router.consume_llm_resp(state)
        hyp = self.memory.data["treasureHypothesis"]
        self.assertEqual(hyp["confidence"], 0.4)
        self.assertFalse(hyp["ready"])
        self.assertTrue(self.memory.data["needTreasureDecode"])

    def test_parse_fenced_json(self):
        self.assertEqual(parse_json_object('```json\n{"a":1}\n```'), {"a": 1})

    def test_persistence(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="门需三钥")
        self.memory.ingest(state)
        again = NewsMemory(Path(self.temp.name))
        self.assertEqual(again.data["legends"][-1]["text"], "门需三钥")
        self.assertEqual(again.banned_ores(3), {"iron"})

    def test_context_change_resets(self):
        self.memory.ingest(self._state(0, official=IRON_COLLAPSE, folk="a", team_id="t1"))
        self.memory.ingest(self._state(10, official="x", folk="b", team_id="t2"))
        self.assertEqual(len(self.memory.data["legends"]), 1)
        self.assertEqual(self.memory.data["legends"][0]["text"], "b")

    def test_stderr_news_infer_includes_prompt_and_parsed_json(self):
        import contextlib
        import io
        state = self._state(0, official=IRON_COLLAPSE, folk="西部石门需三钥")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.memory.ingest(state)
            router = PromptRouter(self.memory)
            prompt = router.request_prompt(state)
            self.assertIn("民间传闻", prompt)
            state.round_no = 1
            state.llm_resp = json.dumps({
                "ready": True,
                "altarPos": {"x": 1, "y": 2},
                "items": ["AcientTablet"],
                "openFromRound": 10,
                "openToRound": 20,
                "confidence": 0.8,
            })
            router.consume_llm_resp(state)
        records = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        news = [row for row in records if row.get("marker") == "NEWS_INFER"]
        official = next(row for row in news if row["event"] == "official_ingested")
        self.assertIn("铁矿", official["officialNews"])
        self.assertIn("【新闻】", official["title"])
        official_plan = next(row for row in news if row["event"] == "official_plan")
        self.assertEqual(official_plan["plan"]["oreEffects"][0]["affectedOre"], "iron")
        self.assertEqual(official_plan["plan"]["stockpileOres"], ["iron"])
        folk = next(row for row in news if row["event"] == "folk_ingested")
        self.assertEqual(folk["newLegend"], "西部石门需三钥")
        self.assertTrue(all(
            (row.get("plan") or {}).get("source") == "llm"
            for row in news if row["event"] == "folk_plan"
        ))
        sent = next(row for row in news if row["event"] == "prompt_sent")
        self.assertEqual(sent["consumer"], "treasure")
        self.assertIn("民间传闻", sent["promptText"])
        out = next(row for row in news if row["event"] == "llm_output")
        self.assertTrue(out["parseOk"])
        self.assertEqual(out["parsedJson"]["altarPos"], {"x": 1, "y": 2})
        self.assertIn("民间传闻", out["promptText"])
        folk_plans = [row for row in news if row["event"] == "folk_plan"]
        self.assertEqual(folk_plans[-1]["plan"]["altarPos"], {"x": 1, "y": 2})
        self.assertEqual(folk_plans[-1]["plan"]["source"], "llm")
        self.assertEqual(self.memory.data["folkPlan"]["altarPos"], {"x": 1, "y": 2})


class OrePricingTests(unittest.TestCase):
    def test_sell_highest_price(self):
        state = MatchState()
        state.vendor_shop_list = [
            ShopItem("stone", 1), ShopItem("iron", 9), ShopItem("copper", 5),
        ]
        name, num = best_ore_to_sell(["stone", "stone", "iron", "copper"], state)
        self.assertEqual(name, "iron")
        self.assertEqual(num, 1)

    def test_nearest_mine_skips_banned_prefers_boosted(self):
        state = MatchState()
        state.map_info = MapInfo(width=41, height=32, zones=[
            Zone(pos=Pos(5, 5), neutral_type="iron"),
            Zone(pos=Pos(6, 5), neutral_type="copper"),
            Zone(pos=Pos(20, 20), neutral_type="stone"),
        ])
        state.vendor_shop_list = [
            ShopItem("stone", 1), ShopItem("iron", 3), ShopItem("copper", 5),
        ]
        worker = Role(id=1, pos=Pos(5, 6), role_type="worker", health=220, back_pack_capability=100)
        mine = nearest_mine(state, worker, banned_ores={"iron"}, boosted_ores={"copper"})
        self.assertEqual(mine.neutral_type, "copper")


if __name__ == "__main__":
    unittest.main()
