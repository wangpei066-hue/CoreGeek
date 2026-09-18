import tempfile
import unittest
from pathlib import Path

from src.agent.brain import V1Strategy, BasicActionValidator
from src.agent.opening import weapon_candidates, wall_ring, primary_wall_plan
from src.agent.protocol import Pos, RobotRole, Zone
from src.agent.task_solver import PioneerTaskSolver
from test_opening import opening_state
from test_shop_items import make_role


def defended():
    state = opening_state()
    state.round_no = 140
    for i, kind in enumerate(('gatling', 'railgun', 'rocket')):
        state.team_our.roles.append(make_role(20+i, 9+i, 8, kind, health=1000, level=1, attack_range=20))
    return state


class DefensePriorityTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_task_pioneer_holds_active_task_with_nearby_threat(self):
        """离开任务点任务就失败：近敌且答案未就绪也留在任务点，由工人守炮。"""
        state = defended()
        state.round_no = 200
        state.phase_task = '仍在解题'
        for weapon in state.team_our.roles[-3:]:
            weapon.level = 2
        for role, pos in zip(state.team_our.roles[1:4], (Pos(8, 8), Pos(10, 7), Pos(12, 8))):
            role.pos = pos
        state.robot.roles = [RobotRole(100, Pos(16, 8), 'largeRobot', 100)]
        commands = self.decide(state)
        controllers = {c.get('controllerId') for c in commands.values() if c.get('action') == 'attack'}
        self.assertNotIn('3', controllers)
        self.assertNotEqual(commands.get(3, {}).get('action'), 'move')
        self.assertFalse(any(e['code'] == 'task_yields_to_defense' and e.get('role_id') == 3
                             for e in state.decision_events))

    def test_day_three_worker_builds_instead_of_wall_voucher_when_gaps_exist(self):
        state = defended()
        state.team_our.gold_num = 100
        state.round_no = 312
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(8, 9)
        front = primary_wall_plan(state, state.team_our.roles[0])[0]
        state.team_our.roles.append(make_role(200, front[0], front[1], 'wall', health=400, level=1))
        commands = self.decide(state)
        self.assertIn(commands[worker.id].get('action'), ('build', 'move', 'collect'))
        self.assertNotEqual(commands[worker.id].get('name'), 'WallUpgradeVoucher1')

    def _day_three_low_front_walls(self, count, gold=300, round_no=312):
        state = defended()
        state.team_our.gold_num = gold
        state.round_no = round_no
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        workers = [r for r in state.team_our.roles if r.role_type == 'worker']
        for worker in workers:
            worker.pos = Pos(8, 9)
        for i, p in enumerate(primary_wall_plan(state, state.team_our.roles[0])[:count]):
            state.team_our.roles.append(make_role(200 + i, p[0], p[1], 'wall', health=400, level=1))
        return state, workers

    def test_day_three_only_wall_keeper_builds_when_gaps_remain(self):
        state, workers = self._day_three_low_front_walls(2)
        commands = self.decide(state)
        keeper, economist = workers[0], workers[1]
        self.assertIn(commands[keeper.id].get('action'), ('build', 'move', 'collect'))
        self.assertNotEqual(commands.get(keeper.id, {}).get('name'), 'WallUpgradeVoucher1')
        self.assertNotEqual(state.worker_item_jobs.get(economist.id, {}).get('kind'), 'wall')

    def test_day3_keeper_upgrades_healthy_front_wall_when_u_complete(self):
        """三面墙没有缺口后，施工工白天买墙券，优先升迎敌正面，满血也升。"""
        state = defended()
        state.team_our.gold_num = 200
        state.round_no = 270
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        keeper = next(r for r in state.team_our.roles if r.id == 1)
        economist = next(r for r in state.team_our.roles if r.id == 2)
        keeper.pos = Pos(8, 9)
        economist.pos = Pos(4, 11)
        for i, p in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(200 + i, p[0], p[1], 'wall', health=1000, level=1))
        commands = self.decide(state)
        self.assertEqual(commands[keeper.id].get('action'), 'buy')
        self.assertEqual(commands[keeper.id].get('name'), 'WallUpgradeVoucher1')
        job = state.worker_item_jobs[keeper.id]
        self.assertEqual(job['kind'], 'wall')
        self.assertEqual(job['target'][0], 13)

    def test_wall_gaps_preempt_unbought_wall_upgrade_job(self):
        from src.agent.brain import maybe_start_shop_item_job
        state = defended()
        state.round_no = 270
        keeper = next(r for r in state.team_our.roles if r.id == 1)
        for i, p in enumerate(primary_wall_plan(state, state.team_our.roles[0])[:-2]):
            state.team_our.roles.append(make_role(200 + i, p[0], p[1], 'wall', health=1000, level=1))
        state.worker_item_jobs[keeper.id] = {
            'item': 'WallUpgradeVoucher1', 'target': (13, 10), 'kind': 'wall'}
        commands = self.decide(state)
        self.assertIn(commands[keeper.id].get('action'), ('build', 'move', 'collect'))
        self.assertNotEqual(commands[keeper.id].get('name'), 'WallUpgradeVoucher1')

    def test_wall_upgrade_picks_center_front_before_wings(self):
        from src.agent.brain import _pick_spawn_facing_wall_to_upgrade
        state = defended()
        state.round_no = 270
        base = state.team_our.roles[0]
        ring = primary_wall_plan(state, base)
        for i, p in enumerate(ring):
            state.team_our.roles.append(make_role(200 + i, p[0], p[1], 'wall', health=1000, level=1))
        from src.agent.opening import defense_mid_y
        mid_y = defense_mid_y(state, base)
        wing = next(p for p in ring if p[0] != 13)
        center = min((p for p in ring if p[0] == 13), key=lambda p: abs(p[1] - mid_y))
        for wall in state.team_our.roles:
            if wall.role_type == 'wall' and (wall.pos.x, wall.pos.y) == wing:
                wall.level = 2
        pick = _pick_spawn_facing_wall_to_upgrade(state, set())
        self.assertEqual(pick.pos.x, 13)
        self.assertLessEqual(abs(pick.pos.y - mid_y), 1)
        self.assertLess(abs(pick.pos.y - mid_y), abs(wing[1] - mid_y))

    def test_wall_upgrade_phase3_prefers_center_three_front(self):
        from src.agent.brain import _pick_spawn_facing_wall_to_upgrade
        from src.agent.opening import spawn_ring_wall_points, defense_mid_y
        state = defended()
        state.round_no = 270
        base = state.team_our.roles[0]
        ring = primary_wall_plan(state, base)
        mid_y = defense_mid_y(state, base)
        for i, p in enumerate(ring):
            lvl = 2 if p in spawn_ring_wall_points(state, base) else 1
            state.team_our.roles.append(make_role(200 + i, p[0], p[1], 'wall', health=1000, level=lvl))
        pick = _pick_spawn_facing_wall_to_upgrade(state, set())
        self.assertEqual(pick.level, 2)
        self.assertEqual(pick.pos.x, 13)
        self.assertLessEqual(abs(pick.pos.y - mid_y), 1)

    def test_day5_keeper_wall_upgrade_precedes_station(self):
        """第五天三面墙已齐：施工工买墙券升迎敌面，不把回合让给升基地而空转。"""
        from src.agent.brain import maybe_start_shop_item_job
        state = defended()
        state.team_our.gold_num = 426
        state.round_no = 530
        for building in state.team_our.roles:
            if building.role_type in ('gatling', 'railgun', 'rocket'):
                building.level = 2
        keeper = next(r for r in state.team_our.roles if r.id == 1)
        for i, p in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(200 + i, p[0], p[1], 'wall', health=1000, level=1))
        maybe_start_shop_item_job(keeper, state)
        job = state.worker_item_jobs[keeper.id]
        self.assertEqual(job['kind'], 'wall')
        self.assertEqual(job['item'], 'WallUpgradeVoucher1')
        self.assertEqual(job['target'][0], 13)

    def test_keeper_builds_new_walls_before_half_health_upgrades_early_day(self):
        """白天还早、侧翼没齐：施工工先补新墙，不跑商店升半血墙。"""
        state, workers = self._day_three_low_front_walls(2, round_no=270)
        commands = self.decide(state)
        keeper = workers[0]
        self.assertNotEqual(commands[keeper.id].get('name'), 'WallUpgradeVoucher1')
        self.assertIn(commands[keeper.id].get('action'), ('build', 'move', 'collect'))

    def test_front_wall_does_not_override_held_weapon_voucher(self):
        state, workers = self._day_three_low_front_walls(1, gold=100)
        keeper = workers[0]
        keeper.backpack = ['WeaponUpgradeVoucher1']
        rocket = next(r for r in state.team_our.roles if r.role_type == 'rocket')
        state.worker_item_jobs[keeper.id] = {
            'item': 'WeaponUpgradeVoucher1', 'target': (rocket.pos.x, rocket.pos.y), 'kind': 'weapon'}
        commands = self.decide(state)
        self.assertEqual(state.worker_item_jobs[keeper.id]['kind'], 'weapon')
        self.assertNotEqual(commands[keeper.id].get('name'), 'WallUpgradeVoucher1')

    def _day_three_wall_gap(self, gold, pack):
        from src.agent.protocol import ShopItem
        state = defended()
        state.round_no = 280
        state.team_our.gold_num = gold
        state.map_info.zones += [Zone(Pos(8, 9), 'weaponShop'), Zone(Pos(4, 12), 'vendor'),
                                 Zone(Pos(2, 14), 'copper')]
        state.vendor_shop_list = [ShopItem('stone', 1), ShopItem('iron', 3), ShopItem('copper', 5)]
        for i, y in enumerate(range(6, 12)):
            state.team_our.roles.append(make_role(40 + i, 13, y, 'wall', level=1, health=1000))
        keeper, economist = [r for r in state.team_our.roles if r.role_type == 'worker']
        keeper.pos, economist.pos = Pos(12, 9), Pos(5, 12)
        economist.back_pack_capability = 100
        economist.backpack = pack
        return state, keeper, economist

    def test_day_three_economist_clears_pack_before_judging_gold(self):
        state, keeper, economist = self._day_three_wall_gap(50, ['copper'] * 40)
        commands = self.decide(state)
        self.assertEqual(commands[economist.id], {'action': 'sell', 'name': 'copper', 'num': 40})
        self.assertNotEqual(commands[keeper.id]['action'], 'sell')

    def test_day_three_economist_keeps_wall_stone_when_clearing(self):
        state, _keeper, economist = self._day_three_wall_gap(50, ['stone'] * 5)
        commands = self.decide(state)
        self.assertNotEqual(commands[economist.id]['action'], 'sell')

    def test_day_three_economist_spends_gold_while_keeper_fills_wall_gap(self):
        from src.agent.opening import critical_wall_missing
        from src.agent.protocol import ShopItem
        state = defended()
        state.round_no = 280
        state.team_our.gold_num = 400
        state.map_info.zones += [Zone(Pos(8, 9), 'weaponShop'), Zone(Pos(4, 12), 'vendor'),
                                 Zone(Pos(2, 14), 'copper')]
        state.vendor_shop_list = [ShopItem('stone', 1), ShopItem('iron', 3), ShopItem('copper', 5)]
        for i, y in enumerate(range(6, 12)):
            state.team_our.roles.append(make_role(40 + i, 13, y, 'wall', level=1, health=1000))
        keeper, economist = [r for r in state.team_our.roles if r.role_type == 'worker']
        keeper.pos, economist.pos = Pos(12, 9), Pos(9, 9)
        self.assertTrue(critical_wall_missing(state))
        commands = self.decide(state)
        self.assertEqual(commands[economist.id]['action'], 'buy')
        self.assertNotEqual(commands[keeper.id]['action'], 'buy')
        self.assertTrue(any(e['code'] == 'economist_upgrade_during_wall_gap' for e in state.decision_events))

    def test_task_pioneer_stays_to_submit_when_answer_ready_before_threat(self):
        """答案已就绪且敌人还来不及打到基地时，留在任务点提交。"""
        state = defended()
        state.round_no = 200
        state.phase_task = '仍在解题'
        state.task_session = {'stage': 'submit', 'answer': '42'}
        for weapon in state.team_our.roles[-3:]:
            weapon.level = 2
        for role, pos in zip(state.team_our.roles[1:4], (Pos(8, 8), Pos(10, 7), Pos(12, 8))):
            role.pos = pos
        state.robot.roles = [RobotRole(100, Pos(18, 8), 'smallRobot', 40)]
        commands = self.decide(state)
        self.assertNotIn('3', {c.get('controllerId') for c in commands.values() if c.get('action') == 'attack'})
        with tempfile.TemporaryDirectory() as root:
            solver = PioneerTaskSolver(Path(root))
            solver.session = {
                'key': [state.team_our.team_id, state.team_our.type, state.phase_task],
                'stage': 'submit', 'answer': '42', 'paths': [], 'documents': [],
                'history': [], 'index': 0, 'offset': 0, 'calls': 0, 'retries': 0, 'round': 199,
            }
            prompt, execute = solver.step(state, commands)
            self.assertEqual((prompt, execute), ('', ''))
            self.assertEqual(commands[3], {'action': 'submitAnswer', 'taskAnswer': '42'})
            self.assertEqual(solver.session.get('answer'), '42')

    def test_task_pioneer_stays_when_answer_ready_and_threat_near(self):
        """答案已就绪、敌人靠近：开拓者照样留在任务点，不去操炮。"""
        state = defended()
        state.round_no = 200
        state.phase_task = '仍在解题'
        for weapon in state.team_our.roles[-3:]:
            weapon.level = 2
        for role, pos in zip(state.team_our.roles[1:4], (Pos(8, 8), Pos(10, 7), Pos(12, 8))):
            role.pos = pos
        state.robot.roles = [RobotRole(100, Pos(16, 8), 'largeRobot', 100)]
        commands = self.decide(state)
        controllers = {c.get('controllerId') for c in commands.values() if c.get('action') == 'attack'}
        self.assertNotIn('3', controllers)
        self.assertNotEqual(commands.get(3, {}).get('action'), 'move')
        self.assertFalse(any(e['code'] == 'task_yields_to_defense' and e.get('role_id') == 3
                             for e in state.decision_events))

    def test_dusk_task_pioneer_stays_when_answer_not_ready(self):
        """白天回防窗口里答案还没出：开拓者也不离开任务点。"""
        state = defended()
        state.round_no = 198
        state.phase_task = '仍在解题'
        for weapon in state.team_our.roles[-3:]:
            weapon.level = 2
        state.team_our.roles[3].pos = Pos(16, 10)
        commands = self.decide(state)
        controllers = {c.get('controllerId') for c in commands.values() if c.get('action') == 'attack'}
        self.assertNotIn('3', controllers)
        self.assertNotEqual(commands.get(3, {}).get('action'), 'move')
        self.assertFalse(any(e['code'] == 'task_yields_to_defense' and e.get('role_id') == 3
                             for e in state.decision_events))

    def test_task_pioneer_holds_even_when_weapons_not_yet_upgraded(self):
        """武器还没升级也不放下进行中的任务。"""
        state = defended()
        state.round_no = 200
        state.phase_task = '仍在解题'
        for role, pos in zip(state.team_our.roles[1:4], (Pos(8, 8), Pos(10, 7), Pos(12, 8))):
            role.pos = pos
        state.robot.roles = [RobotRole(100, Pos(16, 8), 'largeRobot', 100)]
        commands = self.decide(state)
        controllers = {c.get('controllerId') for c in commands.values() if c.get('action') == 'attack'}
        self.assertNotIn('3', controllers)
        self.assertNotEqual(commands.get(3, {}).get('action'), 'move')
        self.assertFalse(any(e['code'] == 'task_yields_to_defense' and e.get('role_id') == 3
                             for e in state.decision_events))

    def test_task_pioneer_holds_on_third_night(self):
        """第三夜起也不放下进行中的任务。"""
        state = defended()
        state.round_no = 340
        state.phase_task = '仍在解题'
        for weapon in state.team_our.roles[-3:]:
            weapon.level = 2
        for role, pos in zip(state.team_our.roles[1:4], (Pos(8, 8), Pos(10, 7), Pos(12, 8))):
            role.pos = pos
        state.robot.roles = [RobotRole(100, Pos(16, 8), 'largeRobot', 100)]
        commands = self.decide(state)
        controllers = {c.get('controllerId') for c in commands.values() if c.get('action') == 'attack'}
        self.assertNotIn('3', controllers)
        self.assertNotEqual(commands.get(3, {}).get('action'), 'move')
        self.assertFalse(any(e['code'] == 'task_yields_to_defense' and e.get('role_id') == 3
                             for e in state.decision_events))

    def test_second_day_missing_outer_walls_generates_stone_task(self):
        state = defended()
        base = state.team_our.roles[0]
        # 已有内层，但外层尚未建好。
        for i, (x, y) in enumerate(wall_ring(state, base)[:6]):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1000, level=1))
        commands = self.decide(state)
        self.assertTrue(any(e['code'] == 'persistent_wall_plan' for e in state.decision_events))
        self.assertTrue(all(commands[i]['action'] in ('move', 'collect') for i in (1, 2)))

    def test_small_inventory_sold_early_before_dusk(self):
        state = defended()
        for i, (x, y) in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1000, level=1))
        state.round_no = 152
        worker = state.team_our.roles[1]
        worker.backpack = ['iron']
        state.map_info.zones.append(Zone(Pos(8, 9), 'vendor'))
        self.assertEqual(self.decide(state)[1], {'action': 'sell', 'name': 'iron', 'num': 1})

    def test_weapon_layout_rockets_behind_station_railgun_front(self):
        from src.agent.opening import attack_direction, weapon_slot_plan
        state = opening_state()
        base = state.team_our.roles[0]
        for x, direction in ((10, 1), (30, -1)):
            base.pos = Pos(x, 10)
            (_, rocket_a), (_, railgun_slot), (_, rocket_b) = weapon_slot_plan(state, base)
            first = weapon_candidates(state, base, 'rocket')[0]
            state.team_our.roles.append(make_role(50, first[0], first[1], 'rocket'))
            second = weapon_candidates(state, base, 'rocket')[0]
            railgun = weapon_candidates(state, base, 'railgun')[0]
            self.assertEqual({first, second}, {rocket_a, rocket_b})
            self.assertEqual(first[0], second[0])
            self.assertEqual(first[0], base.pos.x - 1 if direction == 1 else base.pos.x + 2)
            self.assertEqual(railgun, railgun_slot)
            self.assertGreater((railgun[0] - first[0]) * attack_direction(state, base), 0)
            state.team_our.roles.pop()

    def test_rebuild_missing_wall_beats_third_tier_upgrade(self):
        state = defended()
        state.team_our.gold_num = 200
        weapons = [r for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
        weapons[0].level = 2
        weapons[1].level = 2
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.round_no = 185
        worker = state.team_our.roles[1]
        worker.backpack = ['stone'] * 4
        commands = self.decide(state)
        self.assertNotEqual(commands[1].get('name'), 'WeaponUpgradeVoucher2')
        self.assertIn(commands[1]['action'], ('build', 'move', 'collect'))
        if commands[1]['action'] == 'build':
            self.assertEqual(commands[1]['name'], 'wall')

    def test_unbought_fixer_does_not_block_new_walls(self):
        state = defended()
        state.round_no = 140
        state.team_our.gold_num = 50
        worker = state.team_our.roles[1]
        worker.pos = Pos(12, 7)
        worker.backpack = ['stone'] * 4
        wall = make_role(80, 13, 10, 'wall', health=600, level=1)
        state.team_our.roles.append(wall)
        state.worker_item_jobs[worker.id] = {'item': 'WallFixer', 'target': (13, 10), 'kind': 'wall'}
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        commands = self.decide(state)
        self.assertNotEqual(commands.get(1, {}).get('name'), 'WallFixer')
        self.assertIn(commands[1]['action'], ('build', 'move', 'collect'))
        if commands[1]['action'] == 'build':
            self.assertEqual(commands[1]['name'], 'wall')

    def test_primary_side_walls_reach_short_range_weapon_column(self):
        state = defended()
        base = state.team_our.roles[0]
        for x, direction in ((10, 1), (30, -1)):
            base.pos = Pos(x, 10)
            gatling = weapon_candidates(state, base, 'gatling')[0]
            primary = set(primary_wall_plan(state, base))
            side_y = min(y for _, y in primary)
            self.assertIn((gatling[0], side_y), primary)

    def test_outer_construction_precedes_healthy_wall_upgrades(self):
        state = defended()
        for building in state.team_our.roles:
            if building.role_type in ('gatling', 'railgun', 'rocket'):
                building.level = 3
        for i, (x, y) in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1000, level=1))
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.team_our.roles[1].backpack = ['stone'] * 4
        commands = self.decide(state)
        self.assertNotEqual(commands[1].get('name'), 'WallUpgradeVoucher1')

    def test_level_two_weapons_upgrade_before_walls(self):
        state = defended()
        state.team_our.gold_num = 200
        for building in state.team_our.roles:
            if building.role_type in ('gatling', 'railgun', 'rocket'):
                building.level = 2
        for i, (x, y) in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1000, level=1))
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(8, 9)
        commands = self.decide(state)
        buys = [c for c in commands.values() if c.get('action') == 'buy']
        self.assertEqual([c['name'] for c in buys if 'WeaponUpgradeVoucher2' in c.get('name', '')],
                         ['WeaponUpgradeVoucher2'])
        self.assertFalse(any('StationUpgrade' in c.get('name', '') for c in buys))
        self.assertFalse(any(c.get('name', '').endswith('SummonOrder') for c in buys))

    def test_unbought_wall_upgrade_job_yields_to_level_one_weapon(self):
        from src.agent.brain import maybe_start_shop_item_job
        state = defended()
        worker = state.team_our.roles[1]
        wall = make_role(100, 13, 10, 'wall', health=1000, level=1)
        state.team_our.roles.append(wall)
        state.worker_item_jobs[worker.id] = {'item': 'WallUpgradeVoucher1', 'target': (13, 10), 'kind': 'wall'}
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[worker.id]['kind'], 'weapon')
        self.assertTrue(any(e['code'] == 'weapon_upgrade_funding_gap' for e in state.decision_events))

    def test_offense_purchase_waits_until_all_weapons_level_two(self):
        from src.agent.tactics import tactical_action, begin_round
        from src.agent.grid import build_blocked_set
        state = defended()
        state.team_our.gold_num = 400
        role = state.team_our.roles[1]
        role.pos = Pos(5, 4)
        state.map_info.zones.append(Zone(Pos(5, 5), 'weaponShop'))
        for i, (x, y) in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1500, level=2))
        begin_round(state)
        self.assertIsNone(tactical_action(role, state, build_blocked_set(state), set()))

    def test_breach_uses_bomb_against_single_robot_before_healing(self):
        state = defended()
        wall = make_role(100, 13, 10, 'wall', health=1000, level=1)
        state.team_our.roles.append(wall)
        self.decide(state)
        state.team_our.roles.remove(wall)
        state.round_no = 200
        state.robot.roles = [RobotRole(900, Pos(13, 10), 'smallRobot', 40)]
        state.team_our.roles[1].backpack = ['Bomb', 'Medicine']
        state.team_our.roles[1].health = 30
        commands = self.decide(state)
        self.assertEqual(commands[1]['name'], 'Bomb')
        self.assertTrue(any(e['code'] == 'front_breached' for e in state.decision_events))

    def test_breach_spends_gold_on_bomb_at_adjacent_shop(self):
        state = defended()
        wall = make_role(100, 13, 10, 'wall', health=1000, level=1)
        state.team_our.roles.append(wall)
        self.decide(state)
        state.team_our.roles.remove(wall)
        state.round_no = 200
        state.team_our.gold_num = 100
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.robot.roles = [RobotRole(900, Pos(13, 10), 'smallRobot', 40)]
        self.assertEqual(self.decide(state)[1], {'action': 'buy', 'name': 'Bomb', 'num': 1})
