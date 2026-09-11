"""验证工人开局和武器分配不会覆盖先锋任务。"""
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.protocol import PlayerTask, Pos, RobotRole
from test_opening import opening_state
from test_shop_items import make_role


class WorkerPioneerMergeTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_opening_workers_build_while_pioneer_accepts(self):
        state = opening_state()
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        commands = self.decide(state)
        self.assertEqual(commands[3], {'action': 'acceptTask'})
        for worker in (1, 2):
            self.assertEqual(commands[worker]['action'], 'build')

    def test_opening_active_task_stays_even_at_muster_time(self):
        for round_no in (0, 69):
            with self.subTest(round_no=round_no):
                state = opening_state()
                state.round_no = round_no
                state.phase_task = '请计算1+1'
                state.team_our.roles.append(make_role(20, 9, 10, 'gatling', level=1))
                commands = self.decide(state)
                self.assertNotIn(3, commands)
                self.assertFalse(any(e['code'] == 'weapon_assignment' and e['role_id'] == 3
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

    def test_night_task_does_not_reserve_workers_weapon(self):
        for active in (False, True):
            with self.subTest(active=active):
                state = opening_state()
                state.round_no = 80
                state.phase_task = '任务进行中' if active else ''
                state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
                # 先锋距离炮台最近，但应由工人使用。
                state.team_our.roles.append(make_role(20, 10, 11, 'gatling', level=1, attack_range=20))
                state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
                commands = self.decide(state)
                allocations = [e for e in state.decision_events if e['code'] == 'weapon_assignment']
                self.assertEqual(len(allocations), 1)
                self.assertIn(allocations[0]['role_id'], (1, 2))
                if active:
                    self.assertNotIn(3, commands)
                else:
                    self.assertEqual(commands[3], {'action': 'acceptTask'})
