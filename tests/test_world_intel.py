"""官方消息停工窗口与民间传闻祭坛线索；不把启发式当成官方规则。"""
import json
import tempfile
from pathlib import Path
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.economy import profitable_mine, sellable_ores
from src.agent.grid import build_blocked_set
from src.agent.news_memory import NewsMemory
from src.agent.prompt_router import PromptRouter
from src.agent.protocol import Pos, ShopItem, WorldNews, Zone
from src.agent.world_intel import (
    decide_treasure,
    ingest_news,
    maybe_prompt,
    ore_blocked,
    ores_in_spike,
    ores_to_stockpile,
    parse_intel_llm,
    parse_legend_clues,
    parse_official_forecast,
)
from test_economy_tactics import defended_state, economy_state
from test_shop_items import make_role, minimal_state


IRON_COLLAPSE = (
    "矿业管理局紧急通报：北部铁矿区昨夜发生严重矿井塌方事故。"
    "矿区将于明日全面停工，进行巷道加固。修复工程通常需要2天左右才能完成并恢复开采。"
)


class OfficialNewsTests(unittest.TestCase):
    def test_collapse_example_blocks_iron_for_two_following_days(self):
        forecast = parse_official_forecast(IRON_COLLAPSE, 0)
        self.assertEqual(forecast["iron"]["block_from"], 1)
        self.assertEqual(forecast["iron"]["block_to"], 2)

    def test_blank_or_all_clear_news_does_not_invent_forecast(self):
        self.assertEqual(parse_official_forecast("今日无重大新闻", 0), {})
        self.assertEqual(parse_official_forecast("", 0), {})

    def test_stockpile_json_does_not_skip_blocked_iron_mine(self):
        state, role = economy_state()
        state.round_no = 10
        state.world_news = WorldNews(official_news=IRON_COLLAPSE, folk_legends="")
        ingest_news(state)
        self.assertEqual(ores_to_stockpile(state), {"iron"})
        self.assertFalse(ore_blocked(state, "iron"))
        state.map_info.zones = [z for z in state.map_info.zones if z.neutral_type != "copper"]
        state.map_info.zones.append(Zone(Pos(3, 1), "iron"))
        cmd = profitable_mine(role, state, build_blocked_set(state), set())
        self.assertEqual(cmd["action"], "move")

        state.round_no = 140
        self.assertTrue(ore_blocked(state, "iron"))
        self.assertEqual(ores_in_spike(state), {"iron"})
        cmd = profitable_mine(role, state, build_blocked_set(state), set())
        self.assertIsNotNone(cmd)
        self.assertIn(cmd["action"], ("move", "collect"))

    def test_does_not_hold_stockpiled_iron_for_news(self):
        state, role = economy_state()
        state.round_no = 10
        state.world_news = WorldNews(official_news=IRON_COLLAPSE, folk_legends="")
        ingest_news(state)
        role.backpack = ["iron"] * 8
        self.assertIn("iron", sellable_ores(role, state))


class FolkLegendTests(unittest.TestCase):
    def test_ingest_folk_does_not_heuristic_fill_treasure(self):
        state = minimal_state(round_no=140)
        state.weapon_shop_list = [ShopItem("AcientTablet", 15)]
        state.world_news = WorldNews(
            official_news="",
            folk_legends="带上AcientTablet，第3天去祭坛(18, 7)开启宝藏。",
        )
        ingest_news(state)
        mem = state.policy_memory["world_intel"]
        self.assertEqual(mem["legends"][-1]["text"], "带上AcientTablet，第3天去祭坛(18, 7)开启宝藏。")
        self.assertFalse(mem.get("treasure"))

    def test_parse_legend_clues_extracts_altar_items_and_open_day(self):
        state = minimal_state(round_no=140)
        state.weapon_shop_list = [ShopItem("AcientTablet", 15)]
        clues = parse_legend_clues({
            "legends": [{"text": "带上AcientTablet，第3天去祭坛(18, 7)开启宝藏。"}],
        }, state)
        self.assertEqual(clues["altar"], {"x": 18, "y": 7})
        self.assertEqual(clues["items"], ["AcientTablet"])
        self.assertEqual(clues["openDay"], 2)

    def test_pioneer_buys_then_summons_when_adjacent(self):
        state, _ = defended_state(gold=40)
        pioneer = make_role(3, 5, 5, "pioneer", health=200, back_pack_capability=40)
        state.team_our.roles.append(pioneer)
        state.round_no = 400
        state.weapon_shop_list = [ShopItem("AcientTablet", 15)]
        state.world_news = WorldNews(
            official_news="",
            folk_legends="携带AcientTablet在(6, 5)召唤。第3天。",
        )
        ingest_news(state)
        state.policy_memory["world_intel"]["treasure"] = {
            "altar": {"x": 6, "y": 5}, "items": ["AcientTablet"], "openDay": 2,
        }
        handled, cmd = decide_treasure(pioneer, state, build_blocked_set(state), set())
        self.assertTrue(handled)
        self.assertEqual(cmd["action"], "buy")
        self.assertEqual(cmd["name"], "AcientTablet")
        pioneer.backpack = ["AcientTablet"]
        pioneer.pos = Pos(6, 6)
        handled, cmd = decide_treasure(pioneer, state, build_blocked_set(state), set())
        self.assertEqual(cmd["action"], "summonTreasure")
        self.assertEqual(cmd["item"], ["AcientTablet"])

    def test_does_not_buy_treasure_items_before_day_four(self):
        state, _ = defended_state(gold=400)
        pioneer = make_role(3, 5, 5, "pioneer", health=200, back_pack_capability=40)
        state.team_our.roles.append(pioneer)
        state.round_no = 140
        state.weapon_shop_list = [ShopItem("AcientTablet", 15)]
        state.world_news = WorldNews(
            official_news="",
            folk_legends="携带AcientTablet在(6, 5)召唤。第3天。",
        )
        ingest_news(state)
        state.policy_memory["world_intel"]["treasure"] = {
            "altar": {"x": 6, "y": 5}, "items": ["AcientTablet"], "openDay": 2,
        }
        handled, cmd = decide_treasure(pioneer, state, build_blocked_set(state), set())
        self.assertFalse(handled)
        self.assertIsNone(cmd)
        self.assertTrue(any(e.get("code") == "treasure_buy_deferred" for e in state.decision_events))

    def test_maybe_prompt_uses_daily_quota_and_parses_llm(self):
        state = minimal_state(round_no=140)
        state.world_news = WorldNews(official_news="", folk_legends="明日再听详情。")
        ingest_news(state)
        prompt, cmd = maybe_prompt(state)
        self.assertEqual(prompt, "")
        self.assertEqual(cmd, "")
        with tempfile.TemporaryDirectory() as root:
            memory = NewsMemory(Path(root))
            memory.ingest(state)
            router = PromptRouter(memory)
            prompt = router.request_prompt(state)
            self.assertIn("民间传闻", prompt)
            state.round_no = 141
            state.llm_resp = json.dumps({
                "ready": True,
                "altarPos": {"x": 9, "y": 10},
                "items": ["StarSand"],
                "openFromRound": 520,
                "openToRound": 649,
                "confidence": 0.8,
            })
            router.consume_llm_resp(state)
            hyp = memory.data["treasureHypothesis"]
            self.assertEqual(hyp["altarPos"], {"x": 9, "y": 10})
            self.assertEqual(hyp["items"], ["StarSand"])
            self.assertEqual(hyp["source"], "llm")

    def test_parse_intel_llm_accepts_fenced_json(self):
        value = parse_intel_llm('```json\n{"openDay": 1}\n```')
        self.assertEqual(value["openDay"], 1)

    def test_self_evolution_still_beats_treasure(self):
        state, _ = defended_state(gold=20)
        state.round_no = 140
        state.phase_task = ""
        pioneer = state.team_our.roles[3] if len(state.team_our.roles) > 3 else None
        if pioneer is None or pioneer.role_type != "pioneer":
            pioneer = make_role(3, 8, 8, "pioneer", health=200, back_pack_capability=40)
            state.team_our.roles.append(pioneer)
        from src.agent.protocol import PlayerTask
        state.team_our.player_tasks = [PlayerTask("自进化类1", Pos(9, 8), 0, 10, 10, True)]
        pioneer.pos = Pos(8, 8)
        state.world_news = WorldNews(folk_legends="AcientTablet 去(30, 30) 第9天")
        state.weapon_shop_list = [ShopItem("AcientTablet", 15)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[pioneer.id], {"action": "acceptTask"})
