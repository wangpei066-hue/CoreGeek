import unittest

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.protocol import Pos, RobotInfo, RobotRole
from src.agent.targeting import DamageLedger, TargetContext, plan_attack
from tests.test_v1_strategy import make_role, minimal_state


def robot(rid, x, y, kind="smallRobot", health=None):
    hp = {"smallRobot": 40, "middleRobot": 60, "largeRobot": 500, "bossRobot": 800}[kind]
    return RobotRole(id=rid, pos=Pos(x, y), role_type=kind, health=hp if health is None else health)


def block(x0, y0, kind="smallRobot", start_id=100, size=3):
    return [robot(start_id + i * size + j, x0 + i, y0 + j, kind) for i in range(size) for j in range(size)]


def night_state(roles, robots, round_no=80):
    state = minimal_state(round_no=round_no)
    state.team_our.roles = [state.team_our.roles[0]] + roles
    state.robot = RobotInfo(roles=robots)
    return state


class RocketTargetingTests(unittest.TestCase):
    def test_rocket_centers_on_dense_block_instead_of_lone_boss(self):
        rocket = make_role(10040, 10, 10, "rocket", attack_range=15, level=1, cooldown=0)
        robots = block(20, 20) + [robot(1, 14, 10, "bossRobot")]
        state = night_state([rocket], robots)
        positions, damage = plan_attack(rocket, robots, state)
        self.assertEqual(positions, [{"x": 21, "y": 21}])
        self.assertEqual(sum(damage.values()), 100)

    def test_level2_rocket_stacks_on_same_block(self):
        rocket = make_role(10040, 10, 10, "rocket", attack_range=15, level=2, cooldown=0)
        robots = block(20, 20)
        state = night_state([rocket], robots)
        positions, damage = plan_attack(rocket, robots, state)
        self.assertEqual(len(positions), 2)
        self.assertEqual(sum(damage.values()), 200)
        self.assertTrue(all(abs(p["x"] - 21) <= 1 and abs(p["y"] - 21) <= 1 for p in positions))

    def test_anchor_on_large_with_middles_around(self):
        # 高级怪在前、低级怪在后：锚点应落在大型怪上，中心伤害不浪费、溅射覆盖一圈中型。
        rocket = make_role(10040, 10, 10, "rocket", attack_range=30, level=3, cooldown=0)
        robots = [robot(1, 20, 20, "largeRobot")]
        robots += [robot(10 + i, 20 + dx, 20 + dy, "middleRobot")
                   for i, (dx, dy) in enumerate((d for d in [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1),
                                                           (1, -1), (1, 0), (1, 1)]))]
        state = night_state([rocket], robots)
        positions, damage = plan_attack(rocket, robots, state)
        self.assertEqual(positions, [{"x": 20, "y": 20}] * 3)
        self.assertEqual(damage[1], 60)

    def test_second_rocket_follows_up_on_damaged_block(self):
        rocket = make_role(10041, 10, 10, "rocket", attack_range=30, level=2, cooldown=0)
        damaged = [robot(r.id, r.pos.x, r.pos.y, health=20) for r in block(20, 20)]
        damaged[4] = robot(damaged[4].id, 21, 21, health=0)  # 中心已死
        fresh = block(26, 20, start_id=200)
        robots = damaged + fresh
        state = night_state([rocket], robots)
        positions, _ = plan_attack(rocket, robots, state)
        self.assertTrue(all(20 <= p["x"] <= 22 for p in positions))

    def test_ledger_prevents_overkill_across_weapons(self):
        rocket = make_role(10040, 10, 10, "rocket", attack_range=30, level=1, cooldown=0)
        weak = [robot(1, 20, 20, health=20)]
        robots = weak + block(30, 20)
        state = night_state([rocket], robots)
        ctx, ledger = TargetContext(state, robots), DamageLedger()
        ledger.add({1: 20})  # 别的武器已经会打死它
        positions, _ = plan_attack(rocket, robots, state, ledger, ctx)
        self.assertEqual(positions, [{"x": 31, "y": 21}])

    def test_late_night_skips_unkillable_boss(self):
        rocket = make_role(10040, 10, 10, "rocket", attack_range=30, level=1, cooldown=0)
        robots = [robot(1, 25, 25, "bossRobot")]
        state = night_state([rocket], robots, round_no=128)
        self.assertIsNone(plan_attack(rocket, robots, state))

    def test_late_night_still_hits_boss_attacking_wall(self):
        rocket = make_role(10040, 10, 10, "rocket", attack_range=30, level=1, cooldown=0)
        wall = make_role(10050, 20, 20, "wall", health=1000)
        robots = [robot(1, 22, 20, "bossRobot")]
        state = night_state([rocket, wall], robots, round_no=128)
        self.assertIsNotNone(plan_attack(rocket, robots, state))


class RailgunTargetingTests(unittest.TestCase):
    def test_railgun_finishes_robot_rocket_already_damaged(self):
        railgun = make_role(10042, 10, 10, "railgun", attack_range=10, level=1)
        robots = [robot(1, 14, 10, health=40), robot(2, 10, 14, health=30)]
        state = night_state([railgun], robots)
        ctx, ledger = TargetContext(state, robots), DamageLedger()
        ledger.add({1: 30})
        positions, damage = plan_attack(railgun, robots, state, ledger, ctx)
        self.assertEqual(positions, [{"x": 14, "y": 10}])
        self.assertEqual(damage, {1: 10})

    def test_railgun_pierces_and_kills_two_weak_robots(self):
        railgun = make_role(10042, 10, 10, "railgun", attack_range=10, level=2)
        robots = [robot(1, 12, 10, health=10), robot(2, 14, 10, health=10), robot(3, 10, 14, health=40)]
        state = night_state([railgun], robots)
        positions, damage = plan_attack(railgun, robots, state)
        self.assertEqual(positions, [{"x": 14, "y": 10}])
        self.assertEqual(damage, {1: 10, 2: 10})


class NightLoopTargetingTests(unittest.TestCase):
    def test_rocket_and_railgun_share_ledger(self):
        rocket = make_role(10040, 9, 10, "rocket", attack_range=15, level=1, cooldown=0)
        railgun = make_role(10042, 12, 10, "railgun", attack_range=10, level=1)
        gunner_a = make_role(10010, 9, 11, "worker", backpack=[], back_pack_capability=100)
        gunner_b = make_role(10011, 12, 11, "worker", backpack=[], back_pack_capability=100)
        # 只有一只20血小怪：火箭会打死它，电磁炮不应再打同一只
        robots = [robot(1, 14, 10, health=20), robot(2, 18, 10, "middleRobot")]
        state = night_state([rocket, railgun, gunner_a, gunner_b], robots, round_no=75)
        state.policy_memory["weapon_assignment"] = {"10010": 10040, "10011": 10042}
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[10040]["action"], "attack")
        self.assertEqual(commands[10042]["targetPos"], [{"x": 18, "y": 10}])


if __name__ == "__main__":
    unittest.main()
