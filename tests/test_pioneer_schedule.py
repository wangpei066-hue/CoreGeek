"""开拓者接任务调度：预约、领取当轮、采购不抢占、过期会话。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.economy import pick_weapon_voucher_buyer, solver_ready_to_submit
from src.agent.pioneer_schedule import (
    ESTIMATED_SOLVE_ROUNDS, SCHEDULER_VERSION, SHOP_PROGRESS_KEY,
    SHOP_STALL_ROUNDS, evaluate_task_candidates, reservation_of, scheduler_task_session,
)
from src.agent.protocol import PlayerTask, Pos, RobotRole, Zone
from src.agent.task_solver import PioneerTaskSolver, task_fingerprint
from test_defense_priority import defended
from test_opening import opening_state
from test_shop_items import make_role


class PioneerScheduleTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def armed_day(self, round_no=140, gold=130, pioneer_pos=None, task_pos=None, timeout=15):
        state = opening_state()
        state.round_no = round_no
        state.team_our.gold_num = gold
        task_pos = task_pos or Pos(11, 13)
        state.team_our.player_tasks = [
            PlayerTask('自进化类1', task_pos, 0, 10, 10, True, timeout)]
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        if pioneer_pos is not None:
            pioneer.pos = pioneer_pos
        return state, pioneer

    def test_accept_this_round_when_in_range_and_unblocked(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(11, 13))
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            commands = self.decide(state)
        self.assertEqual(commands[pioneer.id], {'action': 'acceptTask'})
        self.assertTrue(reservation_of(state))
        records = [json.loads(line) for line in stderr.getvalue().splitlines()
                   if line.startswith('{')]
        sched = [r for r in records if r.get('event') == 'scheduler']
        self.assertTrue(sched)
        self.assertEqual(sched[-1]['finalAction'], 'acceptTask')
        self.assertEqual(sched[-1]['codeVersion'], SCHEDULER_VERSION)
        self.assertTrue(sched[-1]['inAcceptRange'])
        self.assertFalse(sched[-1].get('acceptOverwritten'))

    def test_en_route_reservation_not_interrupted_by_ordinary_voucher(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(8, 9))
        commands = self.decide(state)
        self.assertEqual(commands[pioneer.id]['action'], 'move')
        self.assertNotEqual(commands[pioneer.id]['action'], 'buy')
        self.assertTrue(reservation_of(state))
        buyer = pick_weapon_voucher_buyer(state)
        self.assertIsNotNone(buyer)
        self.assertNotEqual(buyer.id, pioneer.id)
        self.assertTrue(any(c.get('action') == 'buy' for rid, c in commands.items() if rid != pioneer.id))

    def test_real_defense_can_interrupt_reservation_with_reason(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(11, 12))
        first = self.decide(state)
        self.assertIn(first[pioneer.id]['action'], ('move', 'acceptTask'))
        self.assertTrue(reservation_of(state))
        state.robot.roles = [RobotRole(100, Pos(10, 10), 'largeRobot', 100)]
        commands = self.decide(state)
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'acceptTask')
        self.assertTrue(any(e['code'] in ('task_reservation_interrupted', 'task_yields_to_defense',
                                          'income_muster') and e.get('role_id') == pioneer.id
                            for e in state.decision_events))

    def test_defense_clear_reevaluates_task_without_stale_shop_job(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(11, 12))
        self.decide(state)
        state.robot.roles = [RobotRole(100, Pos(10, 10), 'largeRobot', 100)]
        self.decide(state)
        state.robot.roles = []
        commands = self.decide(state)
        self.assertIn(commands[pioneer.id]['action'], ('move', 'acceptTask'))
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'buy')

    def test_shop_stall_reassesses_instead_of_holding_forever(self):
        state, pioneer = self.armed_day(gold=0)
        state.team_our.player_tasks = []
        state.worker_item_jobs[pioneer.id] = {
            'kind': 'weapon', 'item': 'WeaponUpgradeVoucher1', 'target': (12, 10),
        }
        signature = dict(
            jobKind='weapon', item='WeaponUpgradeVoucher1',
            pos=[pioneer.pos.x, pioneer.pos.y],
            backpack=[], phaseTask=False, reservation=None,
        )
        state.policy_memory[SHOP_PROGRESS_KEY] = dict(
            signature=signature, stallRounds=SHOP_STALL_ROUNDS, round=state.round_no)
        self.decide(state)
        job = state.worker_item_jobs.get(pioneer.id)
        self.assertFalse(job and job.get('kind') == 'weapon')
        self.assertTrue(any(e['code'] == 'shop_stall_reassess' for e in state.decision_events))

    def test_opening_task_takeover_not_overwritten_by_shop(self):
        state = opening_state()
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        commands = self.decide(state)
        self.assertIn(3, commands)
        self.assertIn(commands[3]['action'], ('move', 'acceptTask'))
        self.assertNotIn(commands[3]['action'], ('collect', 'build', 'buy'))

    def test_invalid_task_clears_reservation_and_picks_other(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(10, 12), task_pos=Pos(11, 13))
        other = PlayerTask('自进化类2', Pos(10, 13), 0, 10, 10, True, 15)
        state.team_our.player_tasks.append(other)
        self.decide(state)
        reserved = reservation_of(state)
        self.assertTrue(reserved)
        for task in state.team_our.player_tasks:
            if (task.task_type == reserved['taskType']
                    and task.task_position.x == reserved['x']
                    and task.task_position.y == reserved['y']):
                task.is_valid = False
        commands = self.decide(state)
        self.assertTrue(any(e['code'] == 'task_reservation_cleared' for e in state.decision_events))
        new_res = reservation_of(state)
        self.assertTrue(new_res)
        self.assertNotEqual((new_res['taskType'], new_res['x'], new_res['y']),
                            (reserved['taskType'], reserved['x'], reserved['y']))
        self.assertIn(commands[pioneer.id]['action'], ('move', 'acceptTask'))

    def test_stale_solver_session_does_not_hold_or_block_accept(self):
        state = defended()
        state.round_no = 200
        state.phase_task = '当前任务'
        state.task_session = {
            'stage': 'submit', 'answer': '旧答案',
            'fingerprint': 'deadbeefdeadbeefdeadbeef',
            'key': [state.team_our.team_id, state.team_our.type, '旧任务'],
        }
        for weapon in state.team_our.roles[-3:]:
            weapon.level = 2
        for role, pos in zip(state.team_our.roles[1:4], (Pos(8, 8), Pos(10, 7), Pos(12, 8))):
            role.pos = pos
        state.robot.roles = [RobotRole(100, Pos(16, 8), 'largeRobot', 100)]
        self.assertFalse(scheduler_task_session(state))
        self.assertFalse(solver_ready_to_submit(state))
        commands = self.decide(state)
        self.assertIn('3', {c['controllerId'] for c in commands.values() if c.get('action') == 'attack'})

    def test_ingest_rebinding_clears_old_task_session_before_decide(self):
        state, pioneer = self.armed_day()
        state.phase_task = '新任务正文'
        with tempfile.TemporaryDirectory() as root:
            solver = PioneerTaskSolver(Path(root))
            solver.session = {
                'key': [state.team_our.team_id, state.team_our.type, '旧任务正文'],
                'stage': 'submit', 'answer': '旧答案', 'paths': [], 'documents': [],
                'history': [], 'index': 0, 'offset': 0, 'calls': 0, 'retries': 0,
                'round': 139, 'fingerprint': task_fingerprint('旧任务正文'),
            }
            solver.ingest_feedback(state)
            self.assertNotEqual(state.task_session.get('answer'), '旧答案')
            self.assertEqual(state.task_session.get('fingerprint'), task_fingerprint('新任务正文'))
            self.assertFalse(solver_ready_to_submit(state))

    def test_timeout_is_not_used_as_solve_duration(self):
        state, pioneer = self.armed_day(round_no=185, pioneer_pos=Pos(11, 12), timeout=15)
        rows = evaluate_task_candidates(pioneer, state, set())
        feasible = [row for row in rows if not row.get('rejected')]
        self.assertTrue(feasible)
        self.assertEqual(feasible[0]['solveEstimate'], ESTIMATED_SOLVE_ROUNDS)
        self.assertNotEqual(feasible[0]['solveEstimate'], 15)
        old_needed = ((feasible[0]['outbound'] or 0) + 15
                      + (feasible[0]['returnSteps'] or 0) + 3)
        self.assertGreaterEqual(old_needed, feasible[0]['available'])
        self.assertLess(feasible[0]['needed'], feasible[0]['available'])
        commands = self.decide(state)
        self.assertEqual(commands[pioneer.id], {'action': 'acceptTask'})

    def test_short_platform_timeout_still_rejected(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(11, 13), timeout=2)
        commands = self.decide(state)
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'acceptTask')

    def test_scheduler_log_emitted_without_phase_task(self):
        state, _pioneer = self.armed_day()
        state.phase_task = ''
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.decide(state)
        records = [json.loads(line) for line in stderr.getvalue().splitlines()
                   if line.startswith('{')]
        sched = [r for r in records if r.get('marker') == 'PIONEER_TASK' and r.get('event') == 'scheduler']
        self.assertEqual(len(sched), 1)
        self.assertFalse(sched[0]['phaseTaskPresent'])
        self.assertIn('defense', sched[0])
        self.assertIn('candidates', sched[0])


if __name__ == '__main__':
    unittest.main()
