"""宝藏执行状态机与开拓者优先级。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent import GameServer
from src.agent.news_memory import NewsMemory
from src.agent.protocol import (
    MatchState, MapInfo, TeamOur, Zone, Pos, Role, WorldNews, ShopItem, PlayerTask,
)
from src.agent.treasure import (
    decide_treasure_action, treasure_should_claim_pioneer, handle_summon_result,
)
from src.agent.brain import decide_pioneer_task, V1Strategy, BasicActionValidator
from src.agent.grid import build_blocked_set


def make_state(round_no=200, gold=100, pioneer_pos=(10, 10), backpack=None, tasks_valid=False):
    state = MatchState()
    state.round_no = round_no
    state.decision_events = []
    state.map_info = MapInfo(width=41, height=32, zones=[
        Zone(pos=Pos(15, 15), neutral_type="weaponShop"),
        Zone(pos=Pos(14, 14), neutral_type="challengerTaskPoint1"),
    ])
    pioneer = Role(
        id=10011, pos=Pos(*pioneer_pos), role_type="pioneer", health=200,
        back_pack_capability=40, backpack=list(backpack or []),
    )
    tasks = []
    if tasks_valid:
        tasks = [PlayerTask(
            task_type="自进化类1", task_position=Pos(14, 14),
            cold_down_rounds=0, score_reward=50, gold_reward=30, is_valid=True,
        )]
    state.team_our = TeamOur(
        type="challenger", team_id="1", team_name="A", gold_num=gold, total_score=0,
        player_tasks=tasks,
        roles=[
            Role(id=10013, pos=Pos(10, 24), role_type="station", health=1500, level=1),
            pioneer,
        ],
    )
    state.weapon_shop_list = [
        ShopItem("AcientTablet", 15), ShopItem("StarSand", 15), ShopItem("FlameBreath", 15),
    ]
    state.world_news = WorldNews()
    state.last_sent_command = {}
    state.last_summon_treasure_result = 0
    return state, pioneer


class TreasureUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = NewsMemory(Path(self.temp.name))
        self.memory.data["treasureHypothesis"] = {
            "ready": True,
            "altarPos": {"x": 12, "y": 12},
            "items": ["AcientTablet", "StarSand"],
            "openFromRound": 200,
            "openToRound": 260,
            "confidence": 0.9,
        }
        self.memory.data["treasureStage"] = "gather"

    def test_buy_then_summon(self):
        state, pioneer = make_state(round_no=210, pioneer_pos=(20, 20), backpack=[])
        blocked, reserved = build_blocked_set(state), set()
        cmd = decide_treasure_action(pioneer, state, self.memory, blocked, reserved)
        self.assertEqual(cmd["action"], "move")
        # 站在商店旁购买
        pioneer.pos = Pos(14, 15)
        cmd = decide_treasure_action(pioneer, state, self.memory, blocked, reserved)
        self.assertEqual(cmd, {"action": "buy", "name": "AcientTablet", "num": 1})
        pioneer.backpack = ["AcientTablet", "StarSand"]
        pioneer.pos = Pos(12, 13)
        cmd = decide_treasure_action(pioneer, state, self.memory, blocked, reserved)
        self.assertEqual(cmd["action"], "summonTreasure")
        self.assertEqual(cmd["item"], ["AcientTablet", "StarSand"])
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 12}])

    def test_wait_outside_window(self):
        state, pioneer = make_state(round_no=100, backpack=["AcientTablet", "StarSand"])
        blocked, reserved = set(), set()
        cmd = decide_treasure_action(pioneer, state, self.memory, blocked, reserved)
        self.assertIsNone(cmd)
        self.assertEqual(self.memory.data["treasureStage"], "wait_window")

    def test_summon_result_codes(self):
        state, _ = make_state()
        state.last_sent_command = {10011: {"action": "summonTreasure"}}
        state.last_summon_treasure_result = 1
        handle_summon_result(state, self.memory)
        self.assertTrue(self.memory.data["treasureEmpty"])

        self.memory.data["treasureEmpty"] = False
        self.memory.data["treasureHypothesis"] = {"ready": True, "items": ["AcientTablet"], "altarPos": {"x": 1, "y": 1}}
        state.last_summon_treasure_result = 3
        handle_summon_result(state, self.memory)
        self.assertIsNone(self.memory.data["treasureHypothesis"])
        self.assertTrue(self.memory.data["needTreasureDecode"])

    def test_treasure_preempts_accept_task(self):
        state, pioneer = make_state(
            round_no=210, pioneer_pos=(12, 13),
            backpack=["AcientTablet", "StarSand"], tasks_valid=True,
        )
        state.news_memory = self.memory
        blocked, reserved = build_blocked_set(state), set()
        self.assertTrue(treasure_should_claim_pioneer(state, pioneer, self.memory))
        handled, cmd = decide_pioneer_task(pioneer, state, blocked, reserved)
        self.assertTrue(handled)
        self.assertEqual(cmd["action"], "summonTreasure")


class TreasureHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = GameServer(self.root)
        self.client = self.server.app.test_client()
        self.payload = json.loads(
            (Path(__file__).parent / "fixtures/sample_match_state.json").read_text(encoding="utf-8")
        )
        # 白天、无自进化任务占用
        self.payload["roundNo"] = 200
        self.payload["phaseTask"] = ""
        self.payload["llmResp"] = ""
        self.payload["lastCmdResult"] = ""
        self.payload["worldNews"] = {
            "officialNews": "今日无重大新闻",
            "folkLegends": "西部石门需古符石板与星辰之沙，祭坛在(12,12)，第200到260回合可开",
        }
        for role in self.payload["teamOur"]["roles"]:
            if role["roleType"] == "pioneer":
                role["pos"] = {"x": 12, "y": 13}
                role["backpack"] = ["AcientTablet", "StarSand"]
        for task in self.payload["teamOur"]["playerTasks"]:
            task["isValid"] = False

    def test_decode_then_summon_via_http(self):
        first = self.client.post("/", json=self.payload)
        self.assertEqual(first.status_code, 200)
        body = first.get_json()
        self.assertIn("民间传闻", body["prompt"])
        # 下一回合注入 LLM 结果
        self.payload["roundNo"] = 201
        self.payload["llmResp"] = json.dumps({
            "ready": True,
            "altarPos": {"x": 12, "y": 12},
            "items": ["AcientTablet", "StarSand"],
            "openFromRound": 200,
            "openToRound": 260,
            "confidence": 0.95,
        })
        second = self.client.post("/", json=self.payload)
        self.assertEqual(second.status_code, 200)
        cmds = second.get_json()["roleCommandMap"]
        self.assertIn("10011", cmds)
        self.assertEqual(cmds["10011"]["action"], "summonTreasure")
        # 解码结果应写入本回合决策报告，不被 decision_events 清空冲掉
        reports = sorted((self.root / "logs").glob("decision_*.json"))
        self.assertTrue(reports)
        decoded = json.loads(reports[-1].read_text(encoding="utf-8"))
        self.assertIn("treasure_decoded", json.dumps(decoded, ensure_ascii=False))
        event_codes = [e.get("code") for e in decoded.get("events", [])]
        self.assertIn("treasure_decoded", event_codes)

    def test_heuristic_appears_in_http_decision_log(self):
        self.payload["roundNo"] = 5
        self.payload["worldNews"] = {
            "officialNews": (
                "矿业管理局紧急通报：北部铁矿区塌方，矿区将于明日全面停工，修复约需2天。"
            ),
            "folkLegends": "",
        }
        self.payload["phaseTask"] = "占住prompt的假任务"  # 避免本回合再发宝藏/矿价 LLM
        resp = self.client.post("/", json=self.payload)
        self.assertEqual(resp.status_code, 200)
        reports = sorted((self.root / "logs").glob("decision_*.json"))
        self.assertTrue(reports)
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        codes = [e.get("code") for e in report.get("events", [])]
        self.assertIn("ore_heuristic", codes)
        effects = self.server.news_memory.banned_ores(2)
        self.assertIn("iron", effects)

    def test_news_diagnostics_via_execute_cmd_when_idle(self):
        """无自进化任务时，新闻诊断应像 PIONEER_TASK 一样经 executeCmd 回传。"""
        self.payload["roundNo"] = 200
        self.payload["phaseTask"] = ""
        self.payload["worldNews"] = {
            "officialNews": "今日无重大新闻",
            "folkLegends": "西部石门需三钥",
        }
        for task in self.payload["teamOur"]["playerTasks"]:
            task["isValid"] = False
        resp = self.client.post("/", json=self.payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("NEWS_INFER", body["executeCmd"])
        self.assertIn("folkLegends", body["executeCmd"])

    def test_news_diagnostics_yields_sandbox_during_phase_task(self):
        self.payload["phaseTask"] = "请计算1+1"
        self.payload["roundNo"] = 10
        resp = self.client.post("/", json=self.payload)
        cmd = resp.get_json().get("executeCmd") or ""
        # 任务期间沙盒归自进化诊断/解题，新闻不抢 executeCmd
        self.assertNotIn("NEWS_INFER", cmd)

    def test_task_solver_owns_prompt_during_phase_task(self):
        self.payload["phaseTask"] = "请计算1+1"
        self.payload["roundNo"] = 10
        self.client.post("/", json=self.payload)
        mem = self.server.news_memory
        self.assertNotEqual(mem.data.get("pendingConsumer"), "treasure")
        self.assertNotEqual(mem.data.get("pendingConsumer"), "ore")


if __name__ == "__main__":
    unittest.main()
