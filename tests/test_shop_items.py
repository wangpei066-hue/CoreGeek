import unittest

from src.agent.protocol import (
    MapInfo,
    MatchState,
    Pos,
    Role,
    RobotInfo,
    TeamEnemy,
    TeamOur,
    Zone,
)
from src.agent.brain import (
    V1Strategy,
    BasicActionValidator,
    decide_shop_item_job,
    maybe_start_shop_item_job,
    voucher_for,
)


def make_role(id, x, y, role_type, health=100, backpack=None, level=None, cooldown=None,
              attack_range=0, back_pack_capability=0):
    return Role(
        id=id, pos=Pos(x, y), role_type=role_type, health=health,
        attack_range=attack_range, back_pack_capability=back_pack_capability,
        backpack=list(backpack or []), level=level, cooldown=cooldown,
    )


def minimal_state(**overrides):
    state = MatchState()
    state.round_no = overrides.get("round_no", 140)
    state.map_info = overrides.get(
        "map_info",
        MapInfo(width=41, height=32, zones=[Zone(pos=Pos(20, 20), neutral_type="weaponShop")]),
    )
    state.team_our = overrides.get(
        "team_our",
        TeamOur(type="challenger", team_id="t", team_name="n", gold_num=overrides.get("gold_num", 0),
                total_score=0, player_tasks=[], roles=[make_role(10013, 10, 10, "station", level=1)]),
    )
    state.team_enemy = overrides.get("team_enemy", TeamEnemy(roles=[]))
    state.robot = overrides.get("robot", RobotInfo(roles=[]))
    return state


class VoucherCostTests(unittest.TestCase):
    def test_weapon_and_station_share_price_table(self):
        self.assertEqual(voucher_for("weapon", 1), ("WeaponUpgradeVoucher1", 100))
        self.assertEqual(voucher_for("weapon", 2), ("WeaponUpgradeVoucher2", 150))
        self.assertEqual(voucher_for("station", 1), ("StationUpgradeVoucher1", 100))
        self.assertEqual(voucher_for("station", 2), ("StationUpgradeVoucher2", 150))

    def test_wall_has_its_own_cheaper_price_table(self):
        self.assertEqual(voucher_for("wall", 1), ("WallUpgradeVoucher1", 20))
        self.assertEqual(voucher_for("wall", 2), ("WallUpgradeVoucher2", 30))


class MaybeStartJobPriorityTests(unittest.TestCase):
    def test_weapon_then_wall_then_station(self):
        state = minimal_state(gold_num=1000)
        weapon = make_role(20, 12, 10, "gatling", health=1000, level=1)
        wall = make_role(30, 13, 10, "wall", health=1000, level=1)
        state.team_our.roles += [weapon, wall]
        worker = make_role(1, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["kind"], "weapon")
        state.worker_item_jobs.clear()
        weapon.level = 3
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["kind"], "wall")
        state.worker_item_jobs.clear()
        wall.level = 3
        wall.health = 2000
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["kind"], "station")

    def test_damaged_wall_prefers_upgrade_because_it_heals(self):
        state = minimal_state(gold_num=1000)
        wall = make_role(40000, 11, 10, "wall", health=100, level=1)
        state.team_our.roles.append(wall)
        worker = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        job = state.worker_item_jobs[10010]
        self.assertEqual(job["item"], "WallUpgradeVoucher1")
        self.assertEqual(job["kind"], "wall")

    def test_moderate_damage_does_not_buy_wall_fixer(self):
        state = minimal_state(gold_num=1000)
        state.team_our.roles[0].level = 3
        wall = make_role(40000, 11, 10, "wall", health=500, level=1)
        state.team_our.roles.append(wall)
        worker = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        job = state.worker_item_jobs[10010]
        self.assertEqual(job["item"], "WallUpgradeVoucher1")
        self.assertNotEqual(job["item"], "WallFixer")

    def test_critical_max_level_wall_repairs_only_with_fixer_in_bag(self):
        state = minimal_state(gold_num=1000)
        state.team_our.roles[0].level = 3
        wall = make_role(40000, 11, 10, "wall", health=50, level=3)
        state.team_our.roles.append(wall)
        worker = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        self.assertNotIn(10010, state.worker_item_jobs)
        worker.backpack = ["WallFixer"]
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[10010]["item"], "WallFixer")

    def test_station_upgrade_when_no_damaged_wall(self):
        state = minimal_state(gold_num=1000)
        worker = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        job = state.worker_item_jobs[10010]
        self.assertEqual(job["item"], "StationUpgradeVoucher1")
        self.assertEqual(job["kind"], "station")

    def test_weapon_upgrade_when_station_already_max_level(self):
        state = minimal_state(gold_num=1000)
        state.team_our.roles[0].level = 3  # 基地已满级
        gatling = make_role(10020, 12, 10, "gatling", level=1)
        state.team_our.roles.append(gatling)
        worker = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        job = state.worker_item_jobs[10010]
        self.assertEqual(job["item"], "WeaponUpgradeVoucher1")
        self.assertEqual(job["kind"], "weapon")

    def test_weapon_upgrade_picks_frontmost_lowest_level(self):
        state = minimal_state(gold_num=1000)
        state.team_our.roles[0].level = 3
        rear = make_role(21, 10, 8, "rocket", level=1)
        mid = make_role(22, 11, 8, "rocket", level=1)
        front = make_role(23, 12, 8, "rocket", level=2)
        state.team_our.roles += [rear, mid, front]
        worker = make_role(1, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["target"], (11, 8))
        self.assertEqual(state.worker_item_jobs[1]["item"], "WeaponUpgradeVoucher1")
        state.worker_item_jobs.clear()
        mid.level = 2
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["target"], (10, 8))
        state.worker_item_jobs.clear()
        rear.level = 2
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["target"], (12, 8))
        self.assertEqual(state.worker_item_jobs[1]["item"], "WeaponUpgradeVoucher2")

    def test_unbought_rear_upgrade_yields_to_front_rocket(self):
        state = minimal_state(gold_num=1000)
        rear = make_role(21, 10, 8, "rocket", level=1)
        front = make_role(23, 12, 8, "rocket", level=1)
        state.team_our.roles += [rear, front]
        worker = make_role(1, 5, 5, "worker", back_pack_capability=100)
        state.worker_item_jobs[1] = {"item": "WeaponUpgradeVoucher1", "target": (10, 8), "kind": "weapon"}
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["target"], (12, 8))

    def test_weapon_upgrade_beats_repair(self):
        state = minimal_state(gold_num=1000)
        wall = make_role(30, 13, 10, "wall", health=100, level=1)
        rocket = make_role(23, 12, 8, "rocket", level=1)
        state.team_our.roles += [wall, rocket]
        worker = make_role(1, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["kind"], "weapon")
        self.assertEqual(state.worker_item_jobs[1]["item"], "WeaponUpgradeVoucher1")

    def test_unbought_repair_yields_to_weapon_upgrade(self):
        state = minimal_state(gold_num=1000)
        wall = make_role(30, 13, 10, "wall", health=100, level=1)
        rocket = make_role(23, 12, 8, "rocket", level=1)
        state.team_our.roles += [wall, rocket]
        worker = make_role(1, 5, 5, "worker", back_pack_capability=100)
        state.worker_item_jobs[1] = {"item": "WallFixer", "target": (13, 10), "kind": "wall"}
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["kind"], "weapon")

    def test_unbought_gatling_upgrade_yields_to_rocket(self):
        state = minimal_state(gold_num=1000)
        gatling = make_role(21, 10, 8, "gatling", level=1)
        rocket = make_role(23, 12, 8, "rocket", level=1)
        state.team_our.roles += [gatling, rocket]
        worker = make_role(1, 5, 5, "worker", back_pack_capability=100)
        state.worker_item_jobs[1] = {"item": "WeaponUpgradeVoucher1", "target": (10, 8), "kind": "weapon"}
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]["target"], (12, 8))

    def test_level_two_weapon_starts_third_tier_upgrade(self):
        state = minimal_state(gold_num=200)
        state.team_our.roles[0].level = 3
        gatling = make_role(10020, 12, 10, "gatling", level=2)
        state.team_our.roles.append(gatling)
        worker = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        job = state.worker_item_jobs[10010]
        self.assertEqual(job["item"], "WeaponUpgradeVoucher2")
        self.assertEqual(job["kind"], "weapon")

    def test_wall_upgrade_as_lowest_priority(self):
        state = minimal_state(gold_num=1000)
        state.team_our.roles[0].level = 3
        wall = make_role(40000, 11, 10, "wall", health=1000, level=1)  # 满血，不需要维修
        state.team_our.roles.append(wall)
        worker = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        job = state.worker_item_jobs[10010]
        self.assertEqual(job["item"], "WallUpgradeVoucher1")
        self.assertEqual(job["kind"], "wall")

    def test_no_job_started_without_enough_gold(self):
        state = minimal_state(gold_num=5)
        worker = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        self.assertNotIn(10010, state.worker_item_jobs)

    def test_does_not_assign_same_target_to_two_workers(self):
        state = minimal_state(gold_num=1000)
        state.team_our.roles[0].level = 3  # 基地满级，逼到武器分支，方便断言唯一武器不会被抢
        gatling = make_role(10020, 12, 10, "gatling", level=1)
        state.team_our.roles.append(gatling)
        worker_a = make_role(10010, 5, 5, "worker", back_pack_capability=100)
        worker_b = make_role(10012, 6, 6, "worker", back_pack_capability=100)
        maybe_start_shop_item_job(worker_a, state)
        maybe_start_shop_item_job(worker_b, state)
        self.assertIn(10010, state.worker_item_jobs)
        self.assertNotIn(10012, state.worker_item_jobs)  # 唯一的可升级武器已被 worker_a 占用


class ShopItemJobExecutionTests(unittest.TestCase):
    def test_moves_toward_shop_when_item_not_yet_bought(self):
        state = minimal_state(gold_num=10)
        worker = make_role(10010, 0, 0, "worker", backpack=[], back_pack_capability=100)
        state.worker_item_jobs[10010] = {"item": "WallFixer", "target": (11, 10), "kind": "wall"}
        wall = make_role(40000, 11, 10, "wall")
        state.team_our.roles.append(wall)
        cmd = decide_shop_item_job(worker, state, set(), set())
        self.assertEqual(cmd["action"], "move")

    def test_buys_when_adjacent_to_shop_without_item(self):
        state = minimal_state(gold_num=10)
        worker = make_role(10010, 20, 21, "worker", backpack=[], back_pack_capability=100)  # 邻接 (20,20) 商店
        state.worker_item_jobs[10010] = {"item": "WallFixer", "target": (11, 10), "kind": "wall"}
        wall = make_role(40000, 11, 10, "wall")
        state.team_our.roles.append(wall)
        cmd = decide_shop_item_job(worker, state, set(), set())
        self.assertEqual(cmd, {"action": "buy", "name": "WallFixer", "num": 1})

    def test_moves_toward_target_once_item_is_bought(self):
        state = minimal_state()
        worker = make_role(10010, 20, 21, "worker", backpack=["WallFixer"], back_pack_capability=100)
        state.worker_item_jobs[10010] = {"item": "WallFixer", "target": (11, 10), "kind": "wall"}
        wall = make_role(40000, 11, 10, "wall")
        state.team_our.roles.append(wall)
        cmd = decide_shop_item_job(worker, state, set(), set())
        self.assertEqual(cmd["action"], "move")

    def test_uses_item_when_adjacent_to_target_with_item_in_backpack(self):
        state = minimal_state()
        worker = make_role(10010, 11, 11, "worker", backpack=["WallFixer"], back_pack_capability=100)
        state.worker_item_jobs[10010] = {"item": "WallFixer", "target": (11, 10), "kind": "wall"}
        wall = make_role(40000, 11, 10, "wall")
        state.team_our.roles.append(wall)
        cmd = decide_shop_item_job(worker, state, set(), set())
        self.assertEqual(cmd, {"action": "use", "name": "WallFixer", "targetPos": [{"x": 11, "y": 10}]})
        self.assertTrue(state.worker_item_jobs[10010]["awaiting_use"])  # 等待下一回合确认

    def test_job_abandoned_when_target_building_no_longer_exists(self):
        state = minimal_state()
        worker = make_role(10010, 11, 11, "worker", backpack=["WallFixer"], back_pack_capability=100)
        state.worker_item_jobs[10010] = {"item": "WallFixer", "target": (11, 10), "kind": "wall"}
        # 故意不把 wall 加进 team_our.roles，模拟围墙已被摧毁
        cmd = decide_shop_item_job(worker, state, set(), set())
        self.assertIsNone(cmd)
        self.assertNotIn(10010, state.worker_item_jobs)

    def test_no_job_no_command(self):
        state = minimal_state()
        worker = make_role(10010, 11, 11, "worker", back_pack_capability=100)
        self.assertIsNone(decide_shop_item_job(worker, state, set(), set()))


class SellDoesNotDumpNonOreItemsTests(unittest.TestCase):
    def test_worker_with_only_voucher_in_backpack_does_not_sell_it(self):
        state = minimal_state()
        state.map_info = MapInfo(
            width=41, height=32,
            zones=[Zone(pos=Pos(11, 10), neutral_type="vendor"), Zone(pos=Pos(20, 20), neutral_type="weaponShop")],
        )
        worker = make_role(10010, 10, 10, "worker", backpack=["WallFixer"], back_pack_capability=100)
        state.team_our.roles.append(worker)
        strategy = V1Strategy(BasicActionValidator())
        commands = strategy.decide(state)
        # 没矿也没金币时合法地什么都不做；关键断言是"不会把 WallFixer 当矿石卖掉"。
        cmd = commands.get(10010)
        self.assertNotEqual((cmd or {}).get("action"), "sell")

    def test_worker_sells_only_ore_when_ore_and_voucher_both_present(self):
        state = minimal_state()
        state.map_info = MapInfo(width=41, height=32, zones=[Zone(pos=Pos(11, 10), neutral_type="vendor")])
        worker = make_role(10010, 10, 10, "worker", backpack=["copper", "WallFixer"], back_pack_capability=100)
        state.team_our.roles.append(worker)
        strategy = V1Strategy(BasicActionValidator())
        commands = strategy.decide(state)
        self.assertEqual(commands[10010], {"action": "sell", "name": "copper", "num": 1})


class PioneerParticipatesInJobsTests(unittest.TestCase):
    def test_pioneer_starts_and_executes_upgrade_job(self):
        state = minimal_state(gold_num=1000)
        state.team_our.roles[0].level = 1  # 基地 level1，会先选中它升级
        pioneer = make_role(10011, 10, 11, "pioneer", back_pack_capability=40)  # 邻接基地 (10,10)
        state.team_our.roles.append(pioneer)
        strategy = V1Strategy(BasicActionValidator())
        commands = strategy.decide(state)
        self.assertIn(10011, commands)
        self.assertIn(commands[10011]["action"], ("move", "buy", "use"))


class MultiRoundRepairIntegrationTest(unittest.TestCase):
    def test_worker_upgrades_damaged_wall_across_several_rounds(self):
        """阶段墙已齐后，受损一级墙走升级券：买券、走到墙边、use。升级回满血。"""
        from src.agent.opening import staged_wall_plan
        from src.agent.brain import own_station
        state = minimal_state(gold_num=1000)
        state.map_info = MapInfo(width=41, height=32, zones=[Zone(pos=Pos(0, 0), neutral_type="weaponShop")])
        base = own_station(state)
        for i, (x, y) in enumerate(staged_wall_plan(state, base)):
            state.team_our.roles.append(make_role(500 + i, x, y, "wall", health=400, level=1))
        worker = make_role(10010, 15, 15, "worker", backpack=[], back_pack_capability=100)
        state.team_our.roles.append(worker)

        strategy = V1Strategy(BasicActionValidator())
        validator = BasicActionValidator()
        actions_seen = []
        bought = None
        for _ in range(200):
            commands = strategy.decide(state)
            cmd = commands.get(10010)
            if cmd is None:
                break
            validator.validate(cmd, state)
            actions_seen.append(cmd["action"])
            worker = next(r for r in state.team_our.roles if r.id == 10010)
            if cmd["action"] == "move":
                worker.pos = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            elif cmd["action"] == "buy":
                bought = cmd["name"]
                worker.backpack.append(cmd["name"])
            elif cmd["action"] == "use":
                break
        self.assertIn("buy", actions_seen)
        self.assertIn("use", actions_seen)
        self.assertEqual(actions_seen[-1], "use")
        self.assertIn("WallUpgradeVoucher", bought or "")


if __name__ == "__main__":
    unittest.main()
