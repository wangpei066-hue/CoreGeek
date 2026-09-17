"""新闻记忆、LLM 日额度仲裁与矿价启发式。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.news_memory import (
    NewsMemory, game_day, heuristic_ore_effect, heuristic_ore_effects, vendor_prices,
    legend_mentions_open_time, merge_ore_effect, is_resume_official,
)
from src.agent.prompt_router import PromptRouter, parse_json_object, make_treasure_prompt
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

    def test_merge_ore_effect_keeps_prior_window_on_status_update(self):
        previous = {
            "affectedOre": "iron",
            "mineBannedDays": [3, 4],
            "priceUpDays": [3, 4],
            "source": "llm",
            "publishedDay": 2,
        }
        incoming = {
            "affectedOre": "iron",
            "mineBannedDays": [3],
            "priceUpDays": [3],
            "notes": "仍在修复",
            "source": "llm",
            "publishedDay": 3,
        }
        merged = merge_ore_effect(previous, incoming, resume=False)
        self.assertEqual(merged["mineBannedDays"], [3, 4])
        self.assertEqual(merged["priceUpDays"], [3, 4])
        cleared = merge_ore_effect(previous, {
            "affectedOre": "iron", "mineBannedDays": [], "priceUpDays": [],
            "notes": "恢复", "source": "llm", "publishedDay": 5,
        }, resume=True)
        self.assertEqual(cleared["mineBannedDays"], [])
        self.assertTrue(is_resume_official("铁矿区修复工程完成，即日起恢复开采。"))
        self.assertFalse(is_resume_official(IRON_COLLAPSE))
        self.assertFalse(is_resume_official(
            "修复工程通常需要2天左右才能完成并恢复开采。"
        ))

        # 误判复工会把 LLM 已填的 [3,4] 清成空（见 issue #647）
        self.memory.data["officialHash"] = IRON_COLLAPSE
        self.memory.apply_ore_llm({
            "affectedOre": "iron",
            "mineBannedDays": [3, 4],
            "priceUpDays": [3, 4],
            "notes": "明日停工2天",
        }, published_day=2)
        self.assertEqual(self.memory.data["oreEffects"][0]["mineBannedDays"], [3, 4])
        self.assertEqual(self.memory.banned_ores(3), {"iron"})
        self.assertEqual(self.memory.banned_ores(4), {"iron"})

    def test_empty_llm_arrays_filled_from_notes_or_heuristic(self):
        # #651：Day2 LLM notes 写「禁采日为3和4」但数组为空 → 禁采/涨价/抢收全空
        self.memory.data["officialHash"] = IRON_COLLAPSE
        self.memory.apply_ore_llm({
            "affectedOre": "iron",
            "mineBannedDays": [],
            "priceUpDays": [],
            "notes": "铁矿区塌方，明日（第3天）起停工修复，预计工期2天，禁采日为3和4",
        }, published_day=2)
        effect = self.memory.data["oreEffects"][0]
        self.assertEqual(effect["mineBannedDays"], [3, 4])
        self.assertEqual(effect["priceUpDays"], [3, 4])
        plan = self.memory.store_official_plan(137)
        self.assertEqual(plan["today"], 2)
        self.assertEqual(plan["bannedOres"], [])
        self.assertEqual(plan["stockpileOres"], ["iron"])
        self.assertEqual(plan["priceUpOres"], [])

        # notes 没写具体日时，回退启发式
        other = NewsMemory(Path(self.temp.name) / "other")
        other.data["officialHash"] = IRON_COLLAPSE
        other.apply_ore_llm({
            "affectedOre": "iron",
            "mineBannedDays": [],
            "priceUpDays": [],
            "notes": "塌方停工，工期约两天",
        }, published_day=2)
        self.assertEqual(other.data["oreEffects"][0]["mineBannedDays"], [3, 4])

    def test_status_update_llm_merges_with_memory(self):
        state = self._state(131, official=IRON_COLLAPSE)
        self.memory.ingest(state)
        self.memory.apply_ore_llm({
            "affectedOre": "iron",
            "mineBannedDays": [3, 4],
            "priceUpDays": [3, 4],
            "notes": "明日停工2天",
        }, published_day=2)
        self.assertEqual(self.memory.banned_ores(3), {"iron"})
        self.assertEqual(self.memory.banned_ores(4), {"iron"})

        state = self._state(261, official="铁矿区修复工程仍在进行中，修复过程中无法采集铁矿")
        self.memory.ingest(state)
        self.assertEqual(len(self.memory.data["officialHistory"]), 2)
        # pending plan 仍应带着旧窗，不能清空记忆
        plan = self.memory.store_official_plan(261)
        self.assertEqual(plan["oreEffects"][0]["mineBannedDays"], [3, 4])
        self.assertEqual(plan["bannedOres"], ["iron"])

        self.memory.apply_ore_llm({
            "affectedOre": "iron",
            "mineBannedDays": [3],
            "priceUpDays": [3],
            "notes": "只写了今天",
        }, published_day=3)
        effect = self.memory.data["oreEffects"][0]
        self.assertEqual(effect["mineBannedDays"], [3, 4])
        self.assertEqual(self.memory.banned_ores(4), {"iron"})

    def test_ingest_official_and_legend(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="西部有一石门")
        self.memory.ingest(state)
        self.assertTrue(self.memory.data["needOreParse"])
        self.assertTrue(self.memory.data["needTreasureDecode"])
        self.assertEqual(self.memory.banned_ores(2), set())
        self.assertEqual(self.memory.data["legends"][-1]["text"], "西部有一石门")
        plan = self.memory.worker_json(0)
        self.assertEqual(plan["oreEffects"], [])
        self.assertEqual(plan["stockpileOres"], [])
        self.assertEqual(self.memory.data["officialPlan"]["oreEffects"], [])
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
        self.assertIn("官方消息", p1)
        self.assertEqual(self.memory.data["llmUsed"], 1)
        self.memory.clear_pending()
        self.memory.data["needTreasureDecode"] = True
        p2 = router.request_prompt(state)
        self.assertIn("民间传闻", p2)
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

    def test_official_change_always_sends_ore_llm_before_folk(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="西部有一石门")
        self.memory.ingest(state)
        self.assertTrue(self.memory.data["needOreParse"])
        router = PromptRouter(self.memory)
        prompt = router.request_prompt(state)
        self.assertIn("官方消息解析器", prompt)
        self.assertIn("previousOreEffects", prompt)
        self.assertTrue(self.memory.data["orePromptSent"])
        self.assertFalse(self.memory.data["treasurePromptSent"])

    def test_official_llm_before_folk_on_any_meaningful_news(self):
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

    def test_official_llm_at_most_once_per_day_folk_gets_remaining(self):
        state = self._state(0, official="安全委员会发布例行通报。", folk="西部有一石门")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        router.request_prompt(state)
        self.assertEqual(self.memory.data["pendingConsumer"], "ore")
        self.memory.clear_pending()
        # 同天再设 needOreParse，但 orePromptSent 已 true，应改送传闻
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
        # 还剩 1 次额度且官方仍需：默认官方优先占用这 1 次
        last = router.request_prompt(state)
        self.assertIn("官方消息", last)
        self.assertTrue(self.memory.data["orePromptSent"])

    def test_folk_priority_when_last_confidence_above_half(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="西部有一石门需三钥")
        self.memory.ingest(state)
        self.memory.data["treasureHypothesis"] = {
            "ready": False, "altarPos": {"x": 1, "y": 2}, "items": ["Key"],
            "confidence": 0.51, "notes": "partial",
        }
        self.memory.data["needTreasureDecode"] = True
        router = PromptRouter(self.memory)
        first = router.request_prompt(state)
        self.assertIn("民间传闻", first)
        self.assertEqual(self.memory.data["pendingConsumer"], "treasure")
        self.assertFalse(self.memory.data["orePromptSent"])
        self.memory.clear_pending()
        state.round_no = 1
        second = router.request_prompt(state)
        self.assertIn("官方消息", second)
        self.assertTrue(self.memory.data["orePromptSent"])
        self.memory.clear_pending()

        # 置信度 ≤0.5 时仍官方优先
        low = self._state(130, official="铜矿区明日停工两天。", folk="新情报补充祭坛坐标")
        self.memory.ingest(low)
        self.memory.data["treasureHypothesis"] = {
            "ready": False, "confidence": 0.5, "notes": "edge",
        }
        self.memory.data["needTreasureDecode"] = True
        router2 = PromptRouter(self.memory)
        self.assertIn("官方消息", router2.request_prompt(low))

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
        self.assertEqual(self.memory.data["pendingConsumer"], "ore")
        state.round_no = 1
        state.phase_task = "做题"
        state.llm_resp = '{"action":"submit","taskAnswer":"2"}'
        router.consume_llm_resp(state)
        self.assertIsNone(self.memory.data["pendingConsumer"])
        self.assertEqual(self.memory.data.get("oreEffects") or [], [])

    def test_consume_ore_and_treasure_llm(self):
        state = self._state(5, official=IRON_COLLAPSE, folk="石门需三钥")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        router.request_prompt(state)
        self.assertEqual(self.memory.data["pendingConsumer"], "ore")
        state.round_no = 6
        state.llm_resp = json.dumps({
            "affectedOre": "iron",
            "mineBannedDays": [2, 3],
            "priceUpDays": [2, 3],
            "notes": "ok",
        })
        state.decision_events = []
        router.consume_llm_resp(state)
        self.assertIn("iron", self.memory.banned_ores(2))
        self.assertIsNone(self.memory.data["pendingConsumer"])

        self.memory.data["needTreasureDecode"] = True
        state.llm_resp = ""
        state.round_no = 7
        router.request_prompt(state)
        self.assertEqual(self.memory.data["pendingConsumer"], "treasure")
        state.round_no = 8
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
    def test_low_confidence_treasure_json_is_not_ready_and_waits_for_new_legend(self):
        state = self._state(0, folk="西部有一石门")
        self.memory.ingest(state)
        router = PromptRouter(self.memory)
        prompt = router.request_prompt(state)
        self.assertIn("置信度", prompt)
        self.assertIn("allowedTaskItems", prompt)
        self.assertIn("heardOnDay", prompt)
        self.assertIn("禁止用 now、heardOnDay", prompt)
        state.round_no = 1
        state.llm_resp = json.dumps({
            "ready": True,
            "altarPos": {"x": 1, "y": 2},
            "items": ["AcientTablet"],
            "openFromRound": 260,
            "openToRound": 389,
            "confidence": 0.4,
            "notes": "只有石门，没有坐标原文",
        })
        router.consume_llm_resp(state)
        hyp = self.memory.data["treasureHypothesis"]
        self.assertEqual(hyp["confidence"], 0.4)
        self.assertFalse(hyp["ready"])
        self.assertIsNone(hyp["openFromRound"])
        self.assertIsNone(hyp["openToRound"])
        self.assertFalse(self.memory.data["needTreasureDecode"])
        state.round_no = 131
        state.world_news.folk_legends = "武器店有铭文石板"
        self.memory.ingest(state)
        self.assertTrue(self.memory.data["needTreasureDecode"])

    def test_open_window_kept_when_legend_names_day(self):
        state = self._state(0, folk="祭坛第4天开启，坐标(12,8)，需铭文石板")
        self.memory.ingest(state)
        self.assertTrue(legend_mentions_open_time(state.world_news.folk_legends))
        self.memory.apply_treasure_llm({
            "ready": True,
            "altarPos": {"x": 12, "y": 8},
            "items": ["AcientTablet"],
            "openFromRound": 390,
            "openToRound": 519,
            "confidence": 0.9,
        })
        hyp = self.memory.data["treasureHypothesis"]
        self.assertEqual(hyp["openFromRound"], 390)
        self.assertEqual(hyp["openToRound"], 519)

    def test_treasure_prompt_strips_guessed_window_from_previous(self):
        state = self._state(262, folk="西部有一石门，门需三钥")
        self.memory.ingest(state)
        self.memory.data["treasureHypothesis"] = {
            "ready": False, "altarPos": None,
            "items": ["AcientTablet"],
            "openFromRound": 260, "openToRound": 389,
            "confidence": 0.6, "source": "llm",
        }
        prompt = make_treasure_prompt(state, self.memory)
        payload = json.loads(prompt.split("输入：", 1)[1])
        self.assertEqual(payload["now"]["gameDay"], 3)
        self.assertEqual(payload["legends"][0]["heardOnDay"], 3)
        self.assertNotIn("day", payload["legends"][0])
        self.assertIsNone(payload["previousHypothesis"]["openFromRound"])
        self.assertIsNone(payload["previousHypothesis"]["openToRound"])

    def test_parse_fenced_json(self):
        self.assertEqual(parse_json_object('```json\n{"a":1}\n```'), {"a": 1})

    def test_persistence(self):
        state = self._state(0, official=IRON_COLLAPSE, folk="门需三钥")
        self.memory.ingest(state)
        again = NewsMemory(Path(self.temp.name))
        self.assertEqual(again.data["legends"][-1]["text"], "门需三钥")
        self.assertTrue(again.data["needOreParse"])
        self.assertEqual(again.banned_ores(3), set())

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
            self.assertIn("官方消息", prompt)
            self.assertIn("previousOreEffects", prompt)
            self.assertIn("officialHistory", prompt)
            state.round_no = 1
            state.llm_resp = json.dumps({
                "affectedOre": "iron",
                "mineBannedDays": [2, 3],
                "priceUpDays": [2, 3],
                "notes": "ok",
            })
            router.consume_llm_resp(state)
            self.memory.clear_pending()
            self.memory.data["needTreasureDecode"] = True
            state.round_no = 2
            state.llm_resp = ""
            treasure_prompt = router.request_prompt(state)
            self.assertIn("民间传闻", treasure_prompt)
            state.round_no = 3
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
        pending_plan = next(
            row for row in news
            if row["event"] == "official_plan" and (row.get("plan") or {}).get("oreEffects") == []
        )
        self.assertEqual(pending_plan["plan"]["stockpileOres"], [])
        ore_out = next(row for row in news if row["event"] == "llm_output" and row.get("consumer") == "ore")
        self.assertTrue(ore_out["parseOk"])
        self.assertEqual(ore_out["parsedJson"]["affectedOre"], "iron")
        folk = next(row for row in news if row["event"] == "folk_ingested")
        self.assertEqual(folk["newLegend"], "西部石门需三钥")
        sent_ore = next(row for row in news if row["event"] == "prompt_sent" and row.get("consumer") == "ore")
        self.assertIn("官方消息", sent_ore["promptText"])
        folk_plans = [row for row in news if row["event"] == "folk_plan"]
        self.assertEqual(folk_plans[-1]["plan"]["altarPos"], {"x": 1, "y": 2})
        self.assertEqual(folk_plans[-1]["plan"]["source"], "llm")
        self.assertEqual(self.memory.data["folkPlan"]["altarPos"], {"x": 1, "y": 2})
        self.assertEqual(self.memory.banned_ores(2), {"iron"})

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
