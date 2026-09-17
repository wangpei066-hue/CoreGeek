"""验证工人开局和武器分配不会覆盖先锋任务。"""
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.protocol import PlayerTask, Pos, RobotRole, Zone
from test_opening import opening_state
from test_shop_items import make_role


class WorkerPioneerMergeTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_opening_pioneer_does_not_collect_or_build(self):
        state = opening_state()
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        commands = self.decide(state)
        self.assertIn(3, commands)
        self.assertIn(commands[3]['action'], ('move', 'acceptTask'))
        self.assertNotIn(commands[3]['action'], ('collect', 'build', 'remove'))
        for worker in (1, 2):
            self.assertIn(commands[worker]['action'], ('move', 'build'))
            if commands[worker]['action'] == 'build':
                self.assertEqual(commands[worker]['name'], 'rocket')
        self.assertTrue(any(e['code'] == 'opening_phase' and e['phase'] == '武器' for e in state.decision_events))

    def test_active_task_is_never_abandoned(self):
        """离开任务点任务就失败：即使入夜且工人都阵亡，开拓者也留在任务点。"""
        for round_no in (0, 75):
            with self.subTest(round_no=round_no):
                state = opening_state()
                state.round_no = round_no
                state.phase_task = '请计算1+1'
                state.team_our.roles.append(make_role(20, 9, 10, 'gatling', level=1))
                for worker in state.team_our.roles:
                    if worker.role_type == 'worker':
                        worker.health = 0
                commands = self.decide(state)
                self.assertNotIn(3, commands)

    def test_task_pioneer_keeps_self_healing_during_opening(self):
        state = opening_state()
        state.phase_task = '任务进行中'
        pioneer = state.team_our.roles[-1]
        pioneer.health = 20
        pioneer.backpack = ['Medicine']
        command = self.decide(state)[3]
        self.assertEqual(command['action'], 'use')
        self.assertEqual(command['name'], 'Medicine')

    def test_idle_pioneer_mans_gun_when_workers_cannot_reach(self):
        """没有进行中的任务、工人赶不到时，离得最近的开拓者操炮，不新接任务。"""
        state = opening_state()
        state.round_no = 80
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles.append(make_role(20, 10, 11, 'gatling', level=1, attack_range=20))
        for worker, pos in zip([r for r in state.team_our.roles if r.role_type == 'worker'],
                               (Pos(2, 2), Pos(3, 2))):
            worker.pos = pos
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        commands = self.decide(state)
        allocations = [e for e in state.decision_events if e['code'] == 'weapon_assignment']
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]['role_id'], 3)
        self.assertEqual(commands[20]['controllerId'], '3')
        self.assertFalse(any(c['action'] == 'acceptTask' for c in commands.values()))

    def test_first_two_nights_pioneer_stays_on_guns_even_when_task_is_available(self):
        state = opening_state()
        state.round_no = 80
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles.append(make_role(20, 10, 11, 'gatling', level=1, attack_range=20))
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        commands = self.decide(state)
        self.assertEqual(commands.get(20, {}).get('controllerId'), '3')
        self.assertNotEqual(commands.get(3, {}).get('action'), 'acceptTask')
        self.assertTrue(any(e['code'] == 'early_night_fixed_defense'
                            for e in state.decision_events))

    def test_pioneer_keeps_task_when_worker_can_cover_two_adjacent_rockets(self):
        state = opening_state()
        state.round_no = 80
        state.phase_task = '任务进行中'
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(9, 10)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(11, 13)
        state.team_our.roles = [
            r for r in state.team_our.roles
            if r.role_type not in ('rocket', 'gatling', 'railgun')
        ]
        state.team_our.roles += [
            make_role(20, 9, 9, 'rocket', level=2, attack_range=20, cooldown=0, health=1000),
            make_role(21, 9, 11, 'rocket', level=2, attack_range=20, cooldown=2, health=1000),
        ]
        state.robot.roles = [RobotRole(100, Pos(28, 10), 'smallRobot', 10)]
        commands = self.decide(state)
        self.assertNotIn(pioneer.id, commands)
        self.assertTrue(any(c.get('controllerId') == str(worker.id) for c in commands.values()))

    def test_after_tasks_done_worker_is_released_to_night_economy(self):
        state = opening_state()
        state.round_no = 80
        state.team_our.gold_num = 0
        state.team_our.player_tasks = []
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(9, 10)
        freed = next(r for r in state.team_our.roles if r.id == 2)
        freed.pos = Pos(7, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
        state.team_our.roles = [
            r for r in state.team_our.roles
            if r.role_type not in ('rocket', 'gatling', 'railgun')
        ]
        state.team_our.roles += [
            make_role(20, 9, 9, 'rocket', level=2, attack_range=20, cooldown=0, health=1000),
            make_role(21, 9, 11, 'rocket', level=2, attack_range=20, cooldown=2, health=1000),
            make_role(22, 11, 12, 'railgun', level=2, attack_range=20, cooldown=0, health=1000),
        ]
        state.robot.roles = [RobotRole(100, Pos(28, 10), 'smallRobot', 10)]
        commands = self.decide(state)
        self.assertIn(freed.id, commands)
        self.assertEqual(commands[freed.id]['action'], 'collect')
        self.assertFalse(any(c.get('controllerId') == str(freed.id) for c in commands.values()))

    def _dual_rocket_night(self):
        state = opening_state()
        state.round_no = 80
        state.team_our.gold_num = 0
        state.team_our.player_tasks = []
        state.team_our.roles = [
            r for r in state.team_our.roles
            if r.role_type not in ('rocket', 'gatling', 'railgun')
        ]
        state.team_our.roles += [
            make_role(20, 9, 9, 'rocket', level=2, attack_range=20, cooldown=0, health=1000),
            make_role(21, 9, 11, 'rocket', level=2, attack_range=20, cooldown=2, health=1000),
            make_role(22, 11, 12, 'railgun', level=2, attack_range=20, cooldown=0, health=1000),
        ]
        state.robot.roles = [RobotRole(100, Pos(28, 10), 'smallRobot', 10)]
        return state

    def test_two_fighters_cover_three_guns_via_dual_rockets(self):
        from src.agent.opening import assign_weapons
        for first, second in ((Pos(9, 10), Pos(8, 8)), (Pos(8, 12), Pos(10, 8)), (Pos(10, 10), Pos(8, 11))):
            state = self._dual_rocket_night()
            w1, w2 = [r for r in state.team_our.roles if r.role_type == 'worker']
            w1.pos, w2.pos = first, second
            pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
            assignment = assign_weapons(state, excluded_ids={pioneer.id})
            kinds = sorted(w.role_type for w in assignment.values())
            self.assertEqual(kinds, ['railgun', 'rocket'], (first, second))

    def test_second_night_releases_worker_even_when_defense_is_due(self):
        state = self._dual_rocket_night()
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(9, 10)
        freed = next(r for r in state.team_our.roles if r.id == 2)
        freed.pos = Pos(7, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
        commands = self.decide(state)
        self.assertIn(freed.id, commands)
        self.assertEqual(commands[freed.id]['action'], 'collect')
        self.assertFalse(any(e['code'] == 'night_worker_release_skipped' for e in state.decision_events))

    def test_first_two_nights_release_miner_even_when_task_is_available(self):
        """前两夜开拓者不接任务，任务点可接不应挡住经济工去基地后方采矿。"""
        for round_no in (80, 210):
            with self.subTest(round_no=round_no):
                state = self._dual_rocket_night()
                state.round_no = round_no
                next(r for r in state.team_our.roles if r.id == 1).pos = Pos(9, 10)
                freed = next(r for r in state.team_our.roles if r.id == 2)
                freed.pos = Pos(7, 9)
                pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
                pioneer.pos = Pos(10, 12)
                state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
                state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
                state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
                commands = self.decide(state)
                self.assertEqual(commands[freed.id]['action'], 'collect')
                self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'acceptTask')
                self.assertTrue(any(e['code'] == 'night_worker_released_to_economy'
                                    for e in state.decision_events))

    def test_third_night_worker_stays_on_guns_when_defense_is_due(self):
        state = self._dual_rocket_night()
        state.round_no = 270
        freed = next(r for r in state.team_our.roles if r.id == 1)
        freed.pos = Pos(9, 10)
        economy = next(r for r in state.team_our.roles if r.id == 2)
        economy.pos = Pos(7, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        freed.backpack = ['stone'] * 6
        economy.backpack = ['stone'] * 6
        commands = self.decide(state)
        repairers = [
            rid for rid in (freed.id, economy.id)
            if rid in commands and commands[rid]['action'] in ('move', 'build')
        ]
        self.assertTrue(repairers)
        self.assertFalse(any(c.get('controllerId') == str(rid) for rid in repairers for c in commands.values()))

    def test_ordinary_voucher_does_not_preempt_feasible_task(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 130
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        from src.agent.protocol import Zone
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(8, 9)
        commands = self.decide(state)
        self.assertIn(commands[pioneer.id]['action'], ('move', 'acceptTask'))
        self.assertNotEqual(commands[pioneer.id]['action'], 'buy')

    def test_pioneer_task_beats_ordinary_wall_upgrade_purchase(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 300
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        from src.agent.protocol import Zone
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(8, 9)
        state.team_our.roles.append(make_role(30, 12, 10, 'wall', level=1, health=1000))
        state.worker_item_jobs[pioneer.id] = {'item': 'WallUpgradeVoucher1', 'target': (12, 10), 'kind': 'wall'}
        commands = self.decide(state)
        self.assertIn(commands[pioneer.id]['action'], ('move', 'acceptTask'))
        self.assertNotEqual(commands[pioneer.id].get('name'), 'WallUpgradeVoucher1')

    def test_worker_buys_voucher_when_pioneer_is_next_to_task(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 130
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        from src.agent.protocol import Zone
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(11, 13)
        state.team_our.roles[1].pos = Pos(8, 9)
        commands = self.decide(state)
        self.assertEqual(commands[1]['action'], 'buy')
        self.assertEqual(commands[1]['name'], 'WeaponUpgradeVoucher1')
        self.assertIn(commands[pioneer.id]['action'], ('move', 'acceptTask'))
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'buy')

    def test_busy_pioneer_does_not_leave_task_to_buy_voucher(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 130
        state.phase_task = '任务进行中'
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        from src.agent.protocol import Zone
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.team_our.roles[1].pos = Pos(8, 9)
        commands = self.decide(state)
        self.assertNotEqual(commands.get(3, {}).get('action'), 'buy')
        self.assertEqual(commands[1]['action'], 'buy')
        self.assertEqual(commands[1]['name'], 'WeaponUpgradeVoucher1')

    def test_workers_build_front_walls_early_day(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=2),
            make_role(21, 12, 8, 'rocket', level=2),
            make_role(22, 12, 12, 'rocket', level=2),
        ]
        for worker_id, pos in ((1, Pos(12, 7)), (2, Pos(12, 8))):
            worker = next(r for r in state.team_our.roles if r.id == worker_id)
            worker.backpack = ['stone'] * 8
            worker.pos = pos
        early = self.decide(state)
        self.assertTrue(any(c.get('action') == 'build' and c.get('name') == 'wall' for c in early.values()))
        self.assertFalse(any(e['code'] == 'stones_reserved_for_late_day' for e in state.decision_events))

    def test_workers_hold_stones_when_only_flanks_missing(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=2),
            make_role(21, 12, 8, 'rocket', level=2),
            make_role(22, 12, 12, 'rocket', level=2),
        ]
        for y in range(7, 13):
            state.team_our.roles.append(make_role(40 + y, 13, y, 'wall', health=1000, level=1))
        for worker_id in (1, 2):
            worker = next(r for r in state.team_our.roles if r.id == worker_id)
            worker.backpack = ['stone'] * 8
            worker.pos = Pos(12, 10)
        early = self.decide(state)
        self.assertFalse(any(c.get('action') == 'build' for c in early.values()))
        self.assertTrue(any(e['code'] == 'stones_reserved_for_late_day' for e in state.decision_events))


class PioneerIdleRegressionTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def _day_two_with_tasks(self):
        from test_defense_priority import defended
        state = defended()
        state.round_no = 135
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(14, 14), 0, 10, 10, True),
                                       PlayerTask('自进化类2', Pos(4, 14), 0, 10, 10, True)]
        return state

    def test_teammate_on_pioneer_stand_does_not_block_tasks(self):
        """工人站在开拓者唯一操炮位上时，开拓者不能被判成“回不去、必须回防”而原地打转。"""
        state = self._day_two_with_tasks()
        commands = self.decide(state)
        codes = [e['code'] for e in state.decision_events if e.get('role_id') == 3]
        self.assertNotIn('task_yields_to_defense', codes)
        reservation = state.policy_memory.get('pioneer_task_reservation')
        self.assertIsNotNone(reservation)
        self.assertEqual(commands[3]['action'], 'move')

    def test_unreachable_tasks_do_not_lock_pioneer_out_of_shopping(self):
        from src.agent.brain import self_evolution_work_open
        state = self._day_two_with_tasks()
        state.round_no = 190  # 回防时间不够，任务都会被拒
        self.decide(state)
        self.assertFalse(self_evolution_work_open(state))


class EnRouteMiningTests(unittest.TestCase):
    def _worker_with_voucher(self, round_no):
        from test_defense_priority import defended
        from src.agent.brain import decide_shop_item_job
        from src.agent.grid import build_blocked_set
        state = defended()
        state.round_no = round_no
        state.map_info.zones.append(Zone(Pos(4, 4), 'copper'))
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(4, 5)
        worker.backpack = ['WeaponUpgradeVoucher1']
        rocket = next(r for r in state.team_our.roles if r.role_type == 'rocket')
        state.worker_item_jobs[worker.id] = {
            'item': 'WeaponUpgradeVoucher1', 'target': (rocket.pos.x, rocket.pos.y), 'kind': 'weapon'}
        return decide_shop_item_job(worker, state, build_blocked_set(state), set())

    def test_worker_mines_passing_ore_while_carrying_voucher(self):
        self.assertEqual(self._worker_with_voucher(150),
                         {'action': 'collect', 'targetPos': [{'x': 4, 'y': 4}]})

    def test_worker_goes_home_when_dusk_is_close(self):
        self.assertEqual(self._worker_with_voucher(195)['action'], 'move')


class NightRouteTests(unittest.TestCase):
    def test_night_pioneer_detours_around_robot_to_task(self):
        from src.agent.grid import build_blocked_set, chebyshev
        from src.agent.pioneer_schedule import apply_task_choice
        state = opening_state()
        state.round_no = 80
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        robot = RobotRole(100, Pos(10, 15), 'smallRobot', 10)
        state.robot.roles = [robot]
        row = {'x': 10, 'y': 20, 'taskType': '自进化类1', 'inAcceptRange': False}
        ok, cmd = apply_task_choice(pioneer, state, build_blocked_set(state), set(), row)
        self.assertTrue(ok)
        step = cmd['targetPos'][0]
        self.assertGreater(chebyshev(Pos(step['x'], step['y']), robot.pos), 2)

    def test_night_economist_mines_behind_base_not_in_front(self):
        from src.agent.economy import pick_mine
        from src.agent.grid import build_blocked_set
        from src.agent.opening import attack_direction, defense_bounds
        state = opening_state()
        state.round_no = 210
        base = state.team_our.roles[0]
        left, right, _, _ = defense_bounds(state, base)
        direction = attack_direction(state, base)
        front = right if direction == 1 else left
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(base.pos.x, base.pos.y - 3)  # 离正面矿比离后方矿更近
        front_mine = Zone(Pos(front + 2 * direction, worker.pos.y), 'iron')
        rear_mine = Zone(Pos(left - 4 if direction == 1 else right + 4, worker.pos.y), 'iron')
        state.map_info.zones = [front_mine, rear_mine]
        picked = pick_mine(worker, state, build_blocked_set(state), set(), ('iron',))
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0].pos, rear_mine.pos)

    def test_night_economist_refuses_front_mine_when_no_safe_mine_exists(self):
        from src.agent.economy import pick_mine
        from src.agent.grid import build_blocked_set
        from src.agent.opening import attack_direction, defense_bounds
        state = opening_state()
        state.round_no = 80
        base = state.team_our.roles[0]
        left, right, _, _ = defense_bounds(state, base)
        direction = attack_direction(state, base)
        front = right if direction == 1 else left
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        state.map_info.zones = [Zone(Pos(front + 2 * direction, worker.pos.y), 'iron')]
        self.assertIsNone(pick_mine(worker, state, build_blocked_set(state), set(), ('iron',)))


def _slot_layout_state(round_no, levels=(1, 1, 1), station_level=1):
    """按正式炮位编制布三门炮和整圈单层墙。"""
    from src.agent.opening import wall_ring, weapon_slot_plan
    state = opening_state()
    state.round_no = round_no
    state.team_our.gold_num = 0
    state.team_our.player_tasks = []
    base = state.team_our.roles[0]
    base.level = station_level
    state.team_our.roles = [r for r in state.team_our.roles if r.role_type not in ('rocket', 'gatling', 'railgun')]
    for i, ((name, (x, y)), level) in enumerate(zip(weapon_slot_plan(state, base), levels)):
        state.team_our.roles.append(make_role(20 + i, x, y, name, level=level, attack_range=20, health=1000))
    for i, (x, y) in enumerate(wall_ring(state, base)):
        state.team_our.roles.append(make_role(100 + i, x, y, 'wall', level=1, health=1000))
    return state


class ThirdNightRepairTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def _pressure_state(self, wall_pos):
        state = _slot_layout_state(340)
        state.robot.roles = [RobotRole(100 + i, Pos(16, 8 + i), 'smallRobot', 10) for i in range(4)]
        wall = next(r for r in state.team_our.roles if r.role_type == 'wall' and (r.pos.x, r.pos.y) == wall_pos)
        wall.health = 300
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.backpack = ['WallUpgradeVoucher1']
        return state, builder

    def test_builder_repairs_front_wall_from_inside_yard_under_pressure(self):
        state, builder = self._pressure_state((13, 10))
        builder.pos = Pos(12, 10)
        commands = self.decide(state)
        self.assertEqual(commands[builder.id], {'action': 'use', 'name': 'WallUpgradeVoucher1',
                                                'targetPos': [{'x': 13, 'y': 10}]})
        self.assertFalse(any(c.get('controllerId') == str(builder.id) for c in commands.values()))

    def test_builder_mans_gun_when_damaged_wall_is_outside_yard_reach(self):
        state, builder = self._pressure_state((13, 7))  # 角落墙只能从墙外够到
        self.decide(state)
        self.assertIn(str(builder.id), state.policy_memory['weapon_assignment'])
        self.assertFalse(any(e['code'] == 'night_worker_released_to_economy' for e in state.decision_events))

    def test_upgrade_chain_waits_for_station_after_first_level_three_rocket(self):
        from src.agent.brain import maybe_start_shop_item_job
        # 编制顺序：火箭A、电磁炮、火箭B
        state = _slot_layout_state(150, levels=(3, 1, 2), station_level=1)
        state.team_our.gold_num = 1000
        worker = make_role(1, 5, 5, 'worker', back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]['kind'], 'station')


class HeldVoucherAndNightRouteTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_held_level_one_voucher_is_used_even_when_chain_wants_level_three(self):
        from src.agent.brain import maybe_start_shop_item_job
        # 编制顺序：火箭A、电磁炮、火箭B；两门火箭已 2 级，升级链下一步要 2 级券
        state = _slot_layout_state(150, levels=(2, 1, 2), station_level=1)
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.backpack = ['WeaponUpgradeVoucher1']
        maybe_start_shop_item_job(worker, state)
        job = state.worker_item_jobs[1]
        self.assertEqual(job['item'], 'WeaponUpgradeVoucher1')
        railgun = next(r for r in state.team_our.roles if r.role_type == 'railgun')
        self.assertEqual(tuple(job['target']), (railgun.pos.x, railgun.pos.y))

    def test_night_gunner_uses_voucher_when_no_target(self):
        state = _slot_layout_state(80)
        state.robot.roles = [RobotRole(100, Pos(40, 31), 'smallRobot', 10)]
        for weapon in state.team_our.roles:
            if weapon.role_type in ('rocket', 'railgun'):
                weapon.attack_range = 3  # 敌人在射程外
        gunner = next(r for r in state.team_our.roles if r.id == 2)
        gunner.backpack = ['WeaponUpgradeVoucher1']
        commands = self.decide(state)
        self.assertEqual(commands[gunner.id]['action'], 'use')
        self.assertEqual(commands[gunner.id]['name'], 'WeaponUpgradeVoucher1')

    def test_night_worker_returning_to_gun_avoids_robot_route(self):
        from src.agent.opening import night_danger_cells
        state = _slot_layout_state(80)
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 14), 0, 10, 10, True)]
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(16, 4)  # 在墙外，机器人正从右边压过来
        state.robot.roles = [RobotRole(100, Pos(22, 6), 'smallRobot', 10)]
        commands = self.decide(state)
        step = commands[worker.id]['targetPos'][0]
        self.assertNotIn((step['x'], step['y']), night_danger_cells(state, include_front=False))


class ChainVoucherTests(unittest.TestCase):
    def test_batch_follows_plan_and_counts_team_held_vouchers(self):
        from src.agent.brain import next_upgrade_step, upgrade_batch_size
        state = _slot_layout_state(150, levels=(1, 1, 1))  # 火箭A、电磁炮、火箭B
        self.assertEqual(upgrade_batch_size(state, 'WeaponUpgradeVoucher1'), 2)  # 电磁炮的券等轮到再买
        state = _slot_layout_state(150, levels=(2, 1, 1))
        self.assertEqual(upgrade_batch_size(state, 'WeaponUpgradeVoucher1'), 1)
        # 有人已买了火箭A升3级的券还没用：下一步直接买基地券
        state = _slot_layout_state(150, levels=(2, 1, 2))
        next(r for r in state.team_our.roles if r.id == 2).backpack = ['WeaponUpgradeVoucher2']
        step = next_upgrade_step(state)
        self.assertEqual(step['name'], 'StationUpgradeVoucher1')
        # 基地升好后：火箭B升3级，接着电磁炮 1→2
        state = _slot_layout_state(150, levels=(3, 1, 2), station_level=2)
        self.assertEqual(next_upgrade_step(state)['name'], 'WeaponUpgradeVoucher2')
        state = _slot_layout_state(150, levels=(3, 1, 3), station_level=2)
        self.assertEqual(upgrade_batch_size(state, 'WeaponUpgradeVoucher1'), 1)

    def test_held_voucher_follows_chain_order(self):
        from src.agent.brain import maybe_start_shop_item_job
        state = _slot_layout_state(150, levels=(1, 1, 1))
        railgun = next(r for r in state.team_our.roles if r.role_type == 'railgun')
        worker = make_role(1, railgun.pos.x - 1, railgun.pos.y, 'worker',
                           back_pack_capability=100, backpack=['WeaponUpgradeVoucher1'])
        maybe_start_shop_item_job(worker, state)
        target = tuple(state.worker_item_jobs[1]['target'])
        kind = next(r.role_type for r in state.team_our.roles if (r.pos.x, r.pos.y) == target)
        self.assertEqual(kind, 'rocket')  # 就站在电磁炮旁也先按顺序升火箭


class FixtureNightMinerTests(unittest.TestCase):
    """用真实请求样例：前两夜带石头的经济工不去正面缺口（夜里不能建造），而是去基地后方采矿。"""

    def _state(self, round_no):
        import json
        from pathlib import Path
        from src.agent.protocol import MatchState
        payload = json.loads((Path(__file__).parent / 'fixtures/sample_match_state.json').read_text(encoding='utf-8'))
        payload['roundNo'] = round_no
        for r in payload['teamOur']['roles']:
            if r['roleType'] == 'gatling':
                r.update(roleType='rocket', level=1, cooldown=0, attackRange=10)
            if r['id'] == 10010:
                r['pos'] = {'x': 8, 'y': 24}
            if r['id'] == 10012:
                r['pos'] = {'x': 11, 'y': 26}
        payload['teamOur']['playerTasks'] = [{
            'taskType': '自进化类1', 'taskPosition': {'x': 14, 'y': 14}, 'coldDownRounds': 0,
            'scoreReward': 10, 'goldReward': 10, 'isValid': True,
        }]
        payload['robot'] = {'roles': [{'id': 1, 'pos': {'x': 30, 'y': 10}, 'roleType': 'smallRobot',
                                       'health': 40, 'targetTeam': ''}]}
        state = MatchState()
        state.update(payload)
        return state

    def test_released_worker_heads_to_rear_mine(self):
        for round_no in (85, 215):
            with self.subTest(round_no=round_no):
                state = self._state(round_no)
                V1Strategy(BasicActionValidator()).decide(state)
                codes = [e['code'] for e in state.decision_events if e['role_id'] == 10012]
                self.assertIn('night_worker_released_to_economy', codes)
                self.assertIn('income_mine', codes)
                self.assertNotIn('emergency_front_seal', codes)
