import json
from pathlib import Path
import unittest

from src.agent.protocol import (
    MapInfo,
    MatchState,
    Pos,
    Role,
    RobotInfo,
    RobotRole,
    TeamEnemy,
    TeamOur,
    Zone,
)
from src.agent.brain import (
    V1Strategy,
    BasicActionValidator,
    decide_buy_medicine,
    decide_self_heal,
    is_day_round,
    max_health,
    pick_attack_target,
    tower_sites,
    wall_order,
)
from src.agent.grid import build_blocked_set, chebyshev, move_towards, station_footprint


FIXTURE = Path(__file__).parent / "fixtures/sample_match_state.json"


def make_role(id, x, y, role_type, health=100, backpack=None, level=None, cooldown=None,
              attack_range=0, back_pack_capability=0):
    return Role(
        id=id, pos=Pos(x, y), role_type=role_type, health=health,
        attack_range=attack_range, back_pack_capability=back_pack_capability,
        backpack=list(backpack or []), level=level, cooldown=cooldown,
    )


def minimal_state(**overrides):
    state = MatchState()
    state.round_no = overrides.get("round_no", 10)
    state.map_info = overrides.get("map_info", MapInfo(width=41, height=32, zones=[]))
    state.team_our = overrides.get(
        "team_our",
        TeamOur(type="challenger", team_id="t", team_name="n", gold_num=0, total_score=0,
                player_tasks=[], roles=[make_role(10013, 10, 10, "station")]),
    )
    state.team_enemy = overrides.get("team_enemy", TeamEnemy(roles=[]))
    state.robot = overrides.get("robot", RobotInfo(roles=[]))
    return state


class PathfindingTests(unittest.TestCase):
    def test_chebyshev_distance(self):
        self.assertEqual(chebyshev(Pos(0, 0), Pos(3, 1)), 3)
        self.assertEqual(chebyshev(Pos(2, 2), Pos(2, 2)), 0)

    def test_move_towards_returns_none_when_already_adjacent(self):
        self.assertIsNone(move_towards(Pos(5, 5), Pos(5, 6), set(), 41, 32))

    def test_move_towards_steps_closer_with_no_obstacles(self):
        step = move_towards(Pos(0, 0), Pos(5, 5), set(), 41, 32)
        self.assertIsNotNone(step)
        self.assertLess(chebyshev(step, Pos(5, 5)), chebyshev(Pos(0, 0), Pos(5, 5)))

    def test_move_towards_routes_around_a_wall(self):
        blocked = {(1, 0), (1, 1), (1, 2)}
        step = move_towards(Pos(0, 1), Pos(3, 1), blocked, 41, 32)
        self.assertIsNotNone(step)
        self.assertNotIn((step.x, step.y), blocked)

    def test_move_towards_returns_none_when_unreachable(self):
        blocked = {(x, 1) for x in range(0, 10)}
        step = move_towards(Pos(0, 0), Pos(0, 5), blocked, 10, 10)
        self.assertIsNone(step)

    def test_build_blocked_set_includes_2x2_station_footprint(self):
        state = minimal_state()
        blocked = build_blocked_set(state)
        # station at (10,10) 左上角 → (10,10)/(11,10)/(10,9)/(11,9)
        self.assertIn((10, 10), blocked)
        self.assertIn((11, 9), blocked)
        self.assertNotIn((10, 11), blocked)

    def test_station_footprint_is_top_left_anchored(self):
        cells = station_footprint(Pos(10, 24))
        self.assertEqual(
            {(c.x, c.y) for c in cells},
            {(10, 24), (11, 24), (10, 23), (11, 23)},
        )


class DayNightTests(unittest.TestCase):
    def test_first_day_round_is_day(self):
        self.assertTrue(is_day_round(0))
        self.assertTrue(is_day_round(69))

    def test_night_round_after_day(self):
        self.assertFalse(is_day_round(70))
        self.assertFalse(is_day_round(129))

    def test_second_day_cycle(self):
        self.assertTrue(is_day_round(130))
        self.assertFalse(is_day_round(200))

    def test_none_round_defaults_to_day(self):
        self.assertTrue(is_day_round(None))


class CombatTargetingTests(unittest.TestCase):
    def test_no_target_out_of_range(self):
        weapon = make_role(10020, 0, 0, "gatling", attack_range=3, level=1)
        robots = [RobotRole(id=1, pos=Pos(10, 10), role_type="smallRobot", health=40)]
        self.assertIsNone(pick_attack_target(weapon, robots))

    def test_prioritizes_boss_over_small(self):
        weapon = make_role(10020, 0, 0, "gatling", attack_range=5, level=1)
        robots = [
            RobotRole(id=1, pos=Pos(1, 0), role_type="smallRobot", health=40),
            RobotRole(id=2, pos=Pos(2, 0), role_type="bossRobot", health=800),
        ]
        target = pick_attack_target(weapon, robots)
        self.assertEqual(target.id, 2)

    def test_finishes_lowest_health_within_same_tier(self):
        weapon = make_role(10020, 0, 0, "gatling", attack_range=5, level=1)
        robots = [
            RobotRole(id=1, pos=Pos(1, 0), role_type="smallRobot", health=40),
            RobotRole(id=2, pos=Pos(2, 0), role_type="smallRobot", health=5),
        ]
        target = pick_attack_target(weapon, robots)
        self.assertEqual(target.id, 2)


class V1StrategyDayTests(unittest.TestCase):
    def setUp(self):
        self.validator = BasicActionValidator()
        self.strategy = V1Strategy(self.validator)

    def test_worker_moves_toward_nearest_mine(self):
        state = minimal_state(round_no=5)
        state.map_info = MapInfo(width=41, height=32, zones=[Zone(pos=Pos(15, 10), neutral_type="stone")])
        worker = make_role(10010, 10, 10, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], worker]
        commands = self.strategy.decide(state)
        self.assertIn(10010, commands)
        self.assertEqual(commands[10010]["action"], "move")

    def test_worker_collects_when_adjacent_to_mine(self):
        state = minimal_state(round_no=5)
        state.map_info = MapInfo(width=41, height=32, zones=[Zone(pos=Pos(11, 10), neutral_type="stone")])
        worker = make_role(10010, 10, 10, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], worker]
        commands = self.strategy.decide(state)
        self.assertEqual(commands[10010], {"action": "collect", "targetPos": [{"x": 11, "y": 10}]})

    def test_worker_sells_when_adjacent_to_vendor_with_backpack(self):
        state = minimal_state(round_no=5)
        state.team_our.roles[0].pos = Pos(10, 24)
        state.map_info = MapInfo(width=41, height=32, zones=[Zone(pos=Pos(11, 24), neutral_type="vendor")])
        # 炮台+围墙蓝图都已建完，才会走到卖矿分支
        sites = tower_sites(state)
        weapons = [
            make_role(10020 + i, site.x, site.y, kind, level=1)
            for i, (site, kind) in enumerate(zip(sites, ("gatling", "railgun", "rocket")))
        ]
        walls = [
            make_role(40000 + i, p.x, p.y, "wall", level=1)
            for i, p in enumerate(wall_order(state))
        ]
        worker = make_role(10010, 10, 24, "worker", backpack=["stone", "stone", "iron"], back_pack_capability=100)
        worker.pos = Pos(10, 24)
        # 邻接 vendor (11,24)
        worker.pos = Pos(10, 24)
        state.team_our.roles = [state.team_our.roles[0], *weapons, *walls, worker]
        commands = self.strategy.decide(state)
        self.assertEqual(commands[10010], {"action": "sell", "name": "stone", "num": 2})

    def test_worker_builds_demo_tower_site_when_gold_available(self):
        state = minimal_state(round_no=5)
        state.team_our.gold_num = 75
        state.team_our.roles[0].pos = Pos(10, 24)
        sites = tower_sites(state)
        self.assertEqual(len(sites), 3)
        worker = make_role(10010, sites[0].x, sites[0].y - 1, "worker", backpack=[], back_pack_capability=100)
        # 站在第一个炮台位旁边
        if chebyshev(worker.pos, sites[0]) > 1:
            worker.pos = Pos(sites[0].x, sites[0].y)
            # 不能站在建造格上建自己脚下；改成邻格
            worker.pos = Pos(sites[0].x - 1, sites[0].y) if sites[0].x > 0 else Pos(sites[0].x + 1, sites[0].y)
        state.team_our.roles = [state.team_our.roles[0], worker]
        commands = self.strategy.decide(state)
        self.assertIn(10010, commands)
        self.assertIn(commands[10010]["action"], ("build", "move"))
        if commands[10010]["action"] == "build":
            self.assertIn(commands[10010]["name"], ("gatling", "railgun", "rocket"))

    def test_wall_order_forms_ring_around_station(self):
        state = minimal_state()
        state.team_our.roles[0].pos = Pos(10, 24)
        order = wall_order(state)
        self.assertGreater(len(order), 4)
        # 入口 xmax+2, ymin-1 不应出现
        entrance = (12, 22)  # xmax=11, ymin=23 → entrance (13, 22)? xmax+2=13, ymin-1=22
        self.assertNotIn(Pos(13, 22), order)

    def test_worker_collects_stone_before_walls_when_towers_done(self):
        state = minimal_state(round_no=5)
        state.team_our.gold_num = 0
        state.team_our.roles[0].pos = Pos(10, 24)
        # 三座炮已在蓝图位点上
        sites = tower_sites(state)
        weapons = [
            make_role(10020, sites[0].x, sites[0].y, "gatling", level=1),
            make_role(10030, sites[1].x, sites[1].y, "railgun", level=1),
            make_role(10040, sites[2].x, sites[2].y, "rocket", level=1),
        ]
        state.map_info = MapInfo(
            width=41, height=32,
            zones=[Zone(pos=Pos(15, 20), neutral_type="stone")],
        )
        worker = make_role(10010, 14, 20, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], *weapons, worker]
        commands = self.strategy.decide(state)
        self.assertEqual(commands[10010]["action"], "collect")

    def test_no_actions_at_night_for_economy(self):
        state = minimal_state(round_no=75)  # night
        state.map_info = MapInfo(width=41, height=32, zones=[Zone(pos=Pos(11, 10), neutral_type="stone")])
        worker = make_role(10010, 10, 10, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], worker]
        commands = self.strategy.decide(state)
        # 夜晚没有武器可操控、附近也没有机器人，工人应待机靠近基地或不动，不应产生 collect/move-to-mine
        self.assertNotIn("collect", [c.get("action") for c in commands.values()])


class V1StrategyNightTests(unittest.TestCase):
    def setUp(self):
        self.validator = BasicActionValidator()
        self.strategy = V1Strategy(self.validator)

    def test_fighter_operates_adjacent_weapon_and_attacks(self):
        state = minimal_state(round_no=75)  # night
        gatling = make_role(10020, 9, 10, "gatling", attack_range=4, level=1)
        worker = make_role(10010, 9, 11, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], gatling, worker]
        state.robot = RobotInfo(roles=[RobotRole(id=30001, pos=Pos(9, 9), role_type="smallRobot", health=40)])
        commands = self.strategy.decide(state)
        self.assertIn(10020, commands)
        self.assertEqual(commands[10020]["action"], "attack")
        self.assertEqual(commands[10020]["controllerId"], "10010")
        self.assertNotIn(10010, commands)

    def test_fighter_moves_toward_weapon_when_not_adjacent(self):
        state = minimal_state(round_no=75)
        gatling = make_role(10020, 20, 20, "gatling", attack_range=4, level=1)
        worker = make_role(10010, 10, 10, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], gatling, worker]
        state.robot = RobotInfo(roles=[])
        commands = self.strategy.decide(state)
        self.assertEqual(commands[10010]["action"], "move")

    def test_rocket_on_cooldown_is_not_fired(self):
        state = minimal_state(round_no=75)
        rocket = make_role(10040, 9, 10, "rocket", attack_range=10, level=1, cooldown=2)
        worker = make_role(10010, 9, 11, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], rocket, worker]
        state.robot = RobotInfo(roles=[RobotRole(id=30001, pos=Pos(9, 9), role_type="smallRobot", health=40)])
        commands = self.strategy.decide(state)
        self.assertNotIn(10040, commands)

    def test_multi_target_count_matches_weapon_level(self):
        state = minimal_state(round_no=75)
        gatling = make_role(10020, 9, 10, "gatling", attack_range=5, level=2)
        worker = make_role(10010, 9, 11, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], gatling, worker]
        state.robot = RobotInfo(roles=[RobotRole(id=30001, pos=Pos(9, 9), role_type="smallRobot", health=40)])
        commands = self.strategy.decide(state)
        self.assertEqual(len(commands[10020]["targetPos"]), 2)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validator = BasicActionValidator()

    def test_move_without_target_is_invalid(self):
        with self.assertRaises(ValueError):
            self.validator.validate({"action": "move"}, None)

    def test_attack_without_controller_is_invalid(self):
        with self.assertRaises(ValueError):
            self.validator.validate({"action": "attack", "targetPos": [{"x": 1, "y": 1}]}, None)

    def test_valid_collect_passes(self):
        self.validator.validate({"action": "collect", "targetPos": [{"x": 1, "y": 1}]}, None)

    def test_v1strategy_never_emits_invalid_commands(self):
        # 用真实样本状态跑一整轮日间决策，确认所有产出指令都能通过本地校验。
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        state = MatchState()
        state.update(payload)
        strategy = V1Strategy(BasicActionValidator())
        commands = strategy.decide(state)
        validator = BasicActionValidator()
        for command in commands.values():
            validator.validate(command, state)  # 不抛异常即视为通过


class MaxHealthTests(unittest.TestCase):
    def test_worker_and_pioneer_use_fixed_hp_regardless_of_level(self):
        worker = make_role(10010, 0, 0, "worker", level=None)
        pioneer = make_role(10011, 0, 0, "pioneer", level=None)
        self.assertEqual(max_health(worker), 220)
        self.assertEqual(max_health(pioneer), 200)

    def test_building_hp_scales_with_level(self):
        gatling1 = make_role(10020, 0, 0, "gatling", level=1)
        gatling3 = make_role(10020, 0, 0, "gatling", level=3)
        self.assertEqual(max_health(gatling1), 1000)
        self.assertEqual(max_health(gatling3), 2000)

    def test_unknown_role_type_falls_back_to_current_health(self):
        mystery = make_role(99999, 0, 0, "mysteryUnit", health=777)
        self.assertEqual(max_health(mystery), 777)


class SelfHealTests(unittest.TestCase):
    def test_no_heal_without_medicine_in_backpack(self):
        worker = make_role(10010, 0, 0, "worker", health=10, backpack=[])
        self.assertIsNone(decide_self_heal(worker))

    def test_no_heal_when_above_threshold(self):
        worker = make_role(10010, 0, 0, "worker", health=220, backpack=["Medicine"])
        self.assertIsNone(decide_self_heal(worker))

    def test_heals_when_below_half_hp_and_carrying_medicine(self):
        worker = make_role(10010, 0, 0, "worker", health=50, backpack=["Medicine"])
        self.assertEqual(decide_self_heal(worker), {"action": "use", "name": "Medicine"})

    def test_self_heal_preempts_night_combat(self):
        state = minimal_state(round_no=75)
        gatling = make_role(10020, 9, 10, "gatling", attack_range=4, level=1)
        hurt_worker = make_role(10010, 9, 11, "worker", health=50, backpack=["Medicine"], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], gatling, hurt_worker]
        state.robot = RobotInfo(roles=[RobotRole(id=30001, pos=Pos(9, 9), role_type="smallRobot", health=40)])
        strategy = V1Strategy(BasicActionValidator())
        commands = strategy.decide(state)
        self.assertEqual(commands[10010], {"action": "use", "name": "Medicine"})
        self.assertNotIn(10020, commands)  # 治疗优先，这一回合没有人操控武器

    def test_self_heal_preempts_day_economy(self):
        state = minimal_state(round_no=5)
        state.map_info = MapInfo(width=41, height=32, zones=[Zone(pos=Pos(11, 10), neutral_type="stone")])
        hurt_worker = make_role(10010, 10, 10, "worker", health=50, backpack=["Medicine"], back_pack_capability=100)
        state.team_our.roles = [state.team_our.roles[0], hurt_worker]
        strategy = V1Strategy(BasicActionValidator())
        commands = strategy.decide(state)
        self.assertEqual(commands[10010], {"action": "use", "name": "Medicine"})


class BuyMedicineTests(unittest.TestCase):
    def _state_with_shop(self, gold=50, shop_pos=(11, 10)):
        state = minimal_state(round_no=5)
        state.map_info = MapInfo(
            width=41, height=32, zones=[Zone(pos=Pos(*shop_pos), neutral_type="weaponShop")]
        )
        state.team_our.gold_num = gold
        return state

    def test_no_buy_when_not_adjacent_to_shop(self):
        state = self._state_with_shop(shop_pos=(30, 30))
        worker = make_role(10010, 10, 10, "worker", backpack=[], back_pack_capability=100)
        self.assertIsNone(decide_buy_medicine(worker, state))

    def test_no_buy_when_already_carrying_medicine(self):
        state = self._state_with_shop()
        worker = make_role(10010, 10, 10, "worker", backpack=["Medicine"], back_pack_capability=100)
        self.assertIsNone(decide_buy_medicine(worker, state))

    def test_no_buy_when_gold_insufficient(self):
        state = self._state_with_shop(gold=5)
        worker = make_role(10010, 10, 10, "worker", backpack=[], back_pack_capability=100)
        self.assertIsNone(decide_buy_medicine(worker, state))

    def test_no_buy_when_backpack_full(self):
        state = self._state_with_shop()
        worker = make_role(10010, 10, 10, "worker", backpack=["stone"], back_pack_capability=1)
        self.assertIsNone(decide_buy_medicine(worker, state))

    def test_buys_when_adjacent_and_affordable(self):
        state = self._state_with_shop()
        worker = make_role(10010, 10, 10, "worker", backpack=[], back_pack_capability=100)
        self.assertEqual(decide_buy_medicine(worker, state), {"action": "buy", "name": "Medicine", "num": 1})

    def test_pioneer_buys_medicine_while_passing_shop_on_day(self):
        state = self._state_with_shop()
        pioneer = make_role(10011, 10, 10, "pioneer", backpack=[], back_pack_capability=40)
        state.team_our.roles = [state.team_our.roles[0], pioneer]
        strategy = V1Strategy(BasicActionValidator())
        commands = strategy.decide(state)
        self.assertEqual(commands[10011], {"action": "buy", "name": "Medicine", "num": 1})


if __name__ == "__main__":
    unittest.main()
