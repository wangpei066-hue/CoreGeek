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

    def test_opening_active_task_yields_at_muster_time(self):
        for round_no in (0, 75):
            with self.subTest(round_no=round_no):
                state = opening_state()
                state.round_no = round_no
                state.phase_task = '请计算1+1'
                state.team_our.roles.append(make_role(20, 9, 10, 'gatling', level=1))
                # 工人被困在围栏里赶不到炮位，入夜后开拓者必须放下任务回炮。
                for worker in state.team_our.roles:
                    if worker.role_type == 'worker':
                        worker.health = 0
                commands = self.decide(state)
                if round_no == 0:
                    self.assertNotIn(3, commands)
                else:
                    self.assertEqual(commands[3]['action'], 'move')
                    self.assertTrue(any(e['code'] in ('income_muster', 'no_free_weapon', 'weapon_assignment') and e['role_id'] == 3
                                        for e in state.decision_events))

    def test_task_pioneer_keeps_self_healing_during_opening(self):
        state = opening_state()
        state.phase_task = '任务进行中'
        pioneer = state.team_our.roles[-1]
        pioneer.health = 20
        pioneer.backpack = ['Medicine']
        command = self.decide(state)[3]
        self.assertEqual(command['action'], 'use')
        self.assertEqual(command['name'], 'Medicine')

    def test_night_task_yields_weapon_to_nearest_fighter_including_pioneer(self):
        for active in (False, True):
            with self.subTest(active=active):
                state = opening_state()
                state.round_no = 80
                state.phase_task = '任务进行中' if active else ''
                state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
                # 先锋距离炮台最近，工人赶不及，夜间先锋应参与操控。
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

    def test_night_pioneer_takes_task_when_workers_cover_all_guns(self):
        for active in (False, True):
            with self.subTest(active=active):
                state = opening_state()
                state.round_no = 80
                state.phase_task = '任务进行中' if active else ''
                state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
                state.team_our.roles.append(make_role(20, 10, 11, 'gatling', level=1, attack_range=20))
                state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
                commands = self.decide(state)
                self.assertNotEqual(commands.get(20, {}).get('controllerId'), '3')
                if not active:
                    self.assertEqual(commands[3]['action'], 'acceptTask')

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

    def test_released_worker_stays_on_guns_when_defense_is_due(self):
        state = self._dual_rocket_night()
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(9, 10)
        freed = next(r for r in state.team_our.roles if r.id == 2)
        freed.pos = Pos(7, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        self.decide(state)
        self.assertIn(str(freed.id), state.policy_memory['weapon_assignment'])
        self.assertTrue(any(e['code'] == 'night_worker_release_skipped' for e in state.decision_events))

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
