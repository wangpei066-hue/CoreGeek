import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.agent import GameServer, GameState, MatchState

FIXTURE = Path(__file__).parent / "fixtures/sample_match_state.json"


class StateParserTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.state = MatchState()

    def test_is_a_game_state(self):
        self.assertIsInstance(self.state, GameState)

    def test_top_level_scalars(self):
        self.state.update(self.payload)
        self.assertEqual(self.state.round_no, 85)
        self.assertEqual(self.state.phase_task, "")
        self.assertEqual(self.state.last_summon_treasure_result, 0)
        self.assertEqual(self.state.llm_resp, "")
        self.assertEqual(self.state.last_cmd_result, "")

    def test_map_info_and_zones(self):
        self.state.update(self.payload)
        self.assertEqual((self.state.map_info.width, self.state.map_info.height), (41, 32))
        self.assertEqual(len(self.state.map_info.zones), 14)
        stone_zones = [z for z in self.state.map_info.zones if z.neutral_type == "stone"]
        self.assertEqual(len(stone_zones), 2)
        self.assertEqual((stone_zones[0].pos.x, stone_zones[0].pos.y), (4, 24))

    def test_team_our_roles_and_tasks(self):
        self.state.update(self.payload)
        team = self.state.team_our
        self.assertEqual(team.type, "challenger")
        self.assertEqual(team.gold_num, 20)
        self.assertEqual(len(team.roles), 9)
        self.assertEqual(len(team.player_tasks), 2)
        station = next(r for r in team.roles if r.role_type == "station")
        self.assertEqual(station.health, 1500)
        self.assertEqual(station.level, 1)
        worker = next(r for r in team.roles if r.id == 10010)
        self.assertEqual(worker.backpack, ["stone", "iron", "copper"])
        self.assertIsNone(worker.level)
        rocket = next(r for r in team.roles if r.role_type == "rocket")
        self.assertEqual(rocket.cooldown, 0)

    def test_team_enemy_roles(self):
        self.state.update(self.payload)
        self.assertEqual(len(self.state.team_enemy.roles), 2)
        self.assertEqual(self.state.team_enemy.roles[0].role_type, "station")

    def test_robot_roles(self):
        self.state.update(self.payload)
        boss = next(r for r in self.state.robot.roles if r.role_type == "bossRobot")
        self.assertEqual(boss.abnormal_state, "dizzy")
        self.assertEqual(boss.health, 800)

    def test_world_news_and_shops(self):
        self.state.update(self.payload)
        self.assertIn("无重大新闻", self.state.world_news.official_news)
        self.assertEqual(len(self.state.vendor_shop_list), 3)
        self.assertEqual(self.state.vendor_shop_list[1].name, "iron")
        self.assertEqual(self.state.weapon_shop_list[0].price, 100)

    def test_errors_and_last_round_results(self):
        self.state.update(self.payload)
        self.assertEqual(self.state.errors[0].error_code, 2)
        self.assertEqual(self.state.last_round_role_action_results[10010], False)
        self.assertEqual(self.state.last_round_role_action_results[10011], True)

    def test_update_is_full_rebuild_not_incremental(self):
        self.state.update(self.payload)
        self.assertEqual(len(self.state.team_our.roles), 9)
        self.state.update({"roundNo": 1})
        self.assertIsNone(self.state.team_our)
        self.assertEqual(self.state.round_no, 1)

    def test_minimal_payload_does_not_crash(self):
        self.state.update({})
        self.assertIsNone(self.state.round_no)
        self.assertIsNone(self.state.map_info)
        self.assertIsNone(self.state.team_our)
        self.assertEqual(self.state.errors, [])

    def test_connectivity_fixture_does_not_crash(self):
        payload = json.loads((Path(__file__).parent / "fixtures/valid_request.json").read_text(encoding="utf-8"))
        self.state.update(payload)
        self.assertIsNone(self.state.team_our)

    def test_callback_processes_game_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 在临时目录中创建GameServer，避免测试把学习记忆写进项目真实的state/目录
            server = GameServer(Path(tmp))
            server.match_state.update(self.payload)
            role_command_map = server.strategy.decide(server.match_state)
            self.assertIsInstance(role_command_map, dict)
            self.assertEqual(server.match_state.round_no, 85)
            self.assertEqual(len(server.match_state.team_our.roles), 9)


if __name__ == "__main__":
    unittest.main()
