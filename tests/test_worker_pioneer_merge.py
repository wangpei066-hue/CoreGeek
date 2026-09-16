"""验证工人开局和武器分配不会覆盖先锋任务。"""
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.protocol import PlayerTask, Pos, RobotRole
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
                commands = self.decide(state)
                if round_no == 0:
                    self.assertNotIn(3, commands)
                else:
                    self.assertEqual(commands[3]['action'], 'move')
                    self.assertTrue(any(e['code'] in ('income_muster', 'no_free_weapon') and e['role_id'] == 3
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
                # 先锋距离炮台最近，夜间也应参与操控。
                state.team_our.roles.append(make_role(20, 10, 11, 'gatling', level=1, attack_range=20))
                state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
                commands = self.decide(state)
                allocations = [e for e in state.decision_events if e['code'] == 'weapon_assignment']
                self.assertEqual(len(allocations), 1)
                self.assertEqual(allocations[0]['role_id'], 3)
                self.assertEqual(commands[20]['controllerId'], '3')
                self.assertFalse(any(c['action'] == 'acceptTask' for c in commands.values()))

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
        self.assertTrue(any(c.get('action') == 'buy' and c.get('name') == 'WeaponUpgradeVoucher1'
                            for rid, c in commands.items() if rid != pioneer.id))

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
