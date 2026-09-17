"""开拓者接任务调度：预约、领取当轮、采购不抢占、过期会话。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.economy import defense_due, pick_weapon_voucher_buyer, solver_ready_to_submit
from src.agent.grid import build_blocked_set
from src.agent.opening import station_return_detail, station_return_steps
from src.agent.pioneer_schedule import (
    ESTIMATED_SOLVE_ROUNDS, SCHEDULER_VERSION, SHOP_PROGRESS_KEY,
    SHOP_STALL_ROUNDS, defense_snapshot, estimated_solve_rounds, evaluate_task_candidates,
    reservation_of, scheduler_task_session,
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
        self.assertEqual((reservation_of(state) or {}).get('stage'), 'accept_pending')
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
        self.assertEqual((reservation_of(state) or {}).get('stage'), 'approaching')
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
        self.assertTrue(job and job.get('kind') == 'weapon')
        event = next(e for e in state.decision_events if e['code'] == 'shop_stall_reassess')
        self.assertEqual(event.get('stallReason'), 'gold_insufficient')
        self.assertFalse(event.get('transferred'))

    def test_opening_task_takeover_not_overwritten_by_shop(self):
        state = opening_state()
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        commands = self.decide(state)
        self.assertIn(3, commands)
        self.assertIn(commands[3]['action'], ('move', 'acceptTask'))
        self.assertNotIn(commands[3]['action'], ('collect', 'build', 'buy'))

    def test_invalid_task_clears_reservation_and_picks_other(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(8, 9), task_pos=Pos(11, 13))
        other = PlayerTask('自进化类2', Pos(10, 13), 0, 10, 10, True, 15)
        state.team_our.player_tasks.append(other)
        self.decide(state)
        reserved = reservation_of(state)
        self.assertEqual((reserved or {}).get('stage'), 'approaching')
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
        # 服务器上任务仍在进行（phaseTask 非空）：过期会话不影响，开拓者照样留在任务点。
        self.assertNotIn('3', {c['controllerId'] for c in commands.values() if c.get('action') == 'attack'})

    def test_stale_task_session_does_not_leak_into_new_task(self):
        """phaseTask 换了新任务时，求解器必须重新开会话，不能把旧任务的"已可提交"状态带过来。"""
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
            state.round_no = 140
            state.llm_resp = ''
            state.last_cmd_result = ''
            state.errors = []
            state.last_round_role_action_results = {}
            solver.step(state, {})
            self.assertNotEqual(state.task_session.get('answer'), '旧答案')
            self.assertNotEqual(state.task_session.get('fingerprint'), task_fingerprint('旧任务正文'))
            self.assertFalse(solver_ready_to_submit(state))

    def test_timeout_is_not_used_as_solve_duration(self):
        state, pioneer = self.armed_day(round_no=185, pioneer_pos=Pos(11, 12), timeout=15)
        rows = evaluate_task_candidates(pioneer, state, set())
        feasible = [row for row in rows if not row.get('rejected')]
        self.assertTrue(feasible)
        self.assertEqual(feasible[0]['solveEstimate'], ESTIMATED_SOLVE_ROUNDS)
        self.assertGreaterEqual(feasible[0]['solveEstimate'], 9)
        self.assertNotEqual(feasible[0]['solveEstimate'], 15)
        self.assertTrue((feasible[0].get('taskConstraint') or {}).get('ok'))
        old_needed = ((feasible[0]['outbound'] or 0) + 15
                      + (feasible[0]['returnSteps'] or 0) + 3)
        self.assertGreaterEqual(old_needed, feasible[0]['available'])
        self.assertLess(feasible[0]['needed'], feasible[0]['available'])
        if feasible[0]['returnSteps'] == 0:
            self.assertTrue(feasible[0]['alreadyAtPost'])
        commands = self.decide(state)
        self.assertEqual(commands[pioneer.id], {'action': 'acceptTask'})

    def test_timeout_below_solve_estimate_is_not_accepted(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(11, 13), timeout=5)
        rows = evaluate_task_candidates(pioneer, state, set())
        self.assertEqual(rows[0]['rejected'], 'solve_exceeds_platform_timeout')
        commands = self.decide(state)
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'acceptTask')

    def test_accept_pending_not_cleared_by_is_valid_false(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(11, 13))
        self.decide(state)
        self.assertEqual((reservation_of(state) or {}).get('stage'), 'accept_pending')
        for task in state.team_our.player_tasks:
            task.is_valid = False
        state.last_sent_command = {pioneer.id: {'action': 'acceptTask'}}
        commands = self.decide(state)
        self.assertEqual((reservation_of(state) or {}).get('stage'), 'accept_pending')
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'buy')

    def test_active_phase_task_not_sent_shopping_when_point_invalid(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(11, 13), gold=130)
        state.phase_task = '部署修复任务'
        for task in state.team_our.player_tasks:
            task.is_valid = False
        commands = self.decide(state)
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'buy')

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

    def test_return_zero_only_when_already_at_post(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(11, 12))
        blocked = build_blocked_set(state)
        detail = station_return_detail(pioneer, state, blocked)
        snap = defense_snapshot(pioneer, state, blocked)
        self.assertEqual(snap['travel'], detail['steps'])
        self.assertEqual(snap['alreadyAtPost'], detail['alreadyAtPost'])
        self.assertEqual(defense_due(pioneer, state, blocked), snap['defenseDue'])
        if detail['steps'] == 0:
            self.assertTrue(detail['alreadyAtPost'])
            self.assertIn(detail['reason'], ('already_at_weapon', 'already_at_station'))
        rows = evaluate_task_candidates(pioneer, state, blocked)
        self.assertTrue(rows)
        if rows[0].get('returnSteps') == 0:
            self.assertTrue(rows[0]['alreadyAtPost'])
        stand = rows[0].get('acceptStand')
        if stand:
            from_stand = station_return_detail(
                pioneer, state, blocked, from_pos=Pos(stand['x'], stand['y']))
            self.assertEqual(rows[0]['returnSteps'], from_stand['steps'])
            self.assertEqual(rows[0]['alreadyAtPost'], from_stand['alreadyAtPost'])

    def test_missing_station_or_path_is_unknown_not_zero(self):
        state, pioneer = self.armed_day(pioneer_pos=Pos(5, 5), task_pos=Pos(8, 8))
        state.team_our.roles = [r for r in state.team_our.roles
                                if r.role_type not in ('rocket', 'gatling', 'railgun', 'station')]
        blocked = build_blocked_set(state)
        detail = station_return_detail(pioneer, state, blocked)
        self.assertIsNone(detail['steps'])
        self.assertEqual(detail['reason'], 'no_station_or_weapon')
        self.assertFalse(detail['alreadyAtPost'])
        self.assertIsNone(station_return_steps(pioneer, state, blocked))
        rows = evaluate_task_candidates(pioneer, state, blocked)
        self.assertEqual(rows[0]['rejected'], 'no_station_or_weapon')
        self.assertIsNone(rows[0]['returnSteps'])
        blocked_all = {(x, y) for x in range(state.map_info.width) for y in range(state.map_info.height)}
        blocked_all.discard((pioneer.pos.x, pioneer.pos.y))
        state.team_our.roles.append(
            next(r for r in opening_state().team_our.roles if r.role_type == 'station'))
        state.team_our.roles.append(
            next(r for r in self.armed_day()[0].team_our.roles if r.role_type == 'rocket'))
        lost = station_return_detail(pioneer, state, blocked_all)
        self.assertIsNone(lost['steps'])
        self.assertIn(lost['reason'], ('no_path_to_weapon', 'no_path_to_station'))

    def test_failed_duration_samples_raise_solve_estimate(self):
        state, _pioneer = self.armed_day()
        state.phase_task = '工作区路径：`/tmp/ws`，修复部署环境'
        state.task_experience = {
            'durations': {
                'workspace': [
                    {'duration': 4, 'outcome': 'answer_ready'},
                    {'duration': 5, 'outcome': 'answer_ready'},
                    {'duration': 12, 'outcome': 'timeout'},
                    {'duration': 12, 'outcome': 'timeout'},
                ],
            },
        }
        estimate, source = estimated_solve_rounds(state)
        self.assertGreaterEqual(estimate, 12)
        self.assertIn('p75', source)
        self.assertIn('failures', source)

    def _post_task_home_state(self, round_no=30, gold=130, stale_active=True):
        state, pioneer = self.armed_day(round_no=round_no, gold=gold, pioneer_pos=Pos(11, 10))
        state.team_our.player_tasks = []
        state.phase_task = ''
        if stale_active:
            state.policy_memory['pioneer_task_reservation'] = {
                'pioneerId': pioneer.id, 'taskType': '自进化类1', 'x': 20, 'y': 20,
                'stage': 'active', 'sinceRound': 18,
            }
        workers = [r for r in state.team_our.roles if r.role_type == 'worker']
        workers[0].pos = Pos(2, 1)
        workers[1].pos = Pos(3, 1)
        state.map_info.zones += [Zone(Pos(2, 1), 'copper'), Zone(Pos(3, 1), 'iron')]
        return state, pioneer

    def _evidence_row(self, state, pioneer, commands, stderr_text):
        events = list(state.decision_events or [])
        sched = [json.loads(line) for line in stderr_text.splitlines() if line.startswith('{')]
        sched = [r for r in sched if r.get('event') == 'scheduler']
        last = sched[-1] if sched else {}
        defense = last.get('defense') or {}
        job = None
        for rid, item in (state.worker_item_jobs or {}).items():
            if item.get('kind') == 'weapon':
                job = {'role': rid, 'item': item.get('item')}
                break
        holders = [r.id for r in state.team_our.roles
                   if any(isinstance(i, str) and 'WeaponUpgradeVoucher' in i for i in r.backpack)]
        weapons = {r.id: r.level or 1 for r in state.team_our.roles
                   if r.role_type in ('rocket', 'gatling', 'railgun') and r.health > 0}
        phase = next((e.get('phase') for e in events if e.get('code') == 'opening_phase'), None)
        return {
            'round': state.round_no,
            'pos': {'x': pioneer.pos.x, 'y': pioneer.pos.y},
            'phaseTask': bool(state.phase_task),
            'openingPhase': phase,
            'finalAction': (commands.get(pioneer.id) or {}).get('action'),
            'actionSource': last.get('actionSource') or last.get('returnedBranch'),
            'occupancy': last.get('occupancy') or defense.get('occupancy'),
            'defenseDue': defense.get('defenseDue'),
            'defenseReasons': defense.get('defenseDueReasons'),
            'travel': defense.get('travel'),
            'threatEta': defense.get('threatEta'),
            'gold': state.team_our.gold_num,
            'reservation': None if not reservation_of(state) else reservation_of(state).get('stage'),
            'voucherHolders': holders,
            'weaponJob': job,
            'weaponLevels': weapons,
            'codes': [e.get('code') for e in events if e.get('role_id') in (None, pioneer.id)],
        }

    def test_stale_active_reservation_cleared_and_upgrade_starts(self):
        state, pioneer = self._post_task_home_state()
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            commands = self.decide(state)
        self.assertTrue(any(e['code'] == 'task_reservation_cleared'
                            and e.get('reason') == 'phase_task_cleared'
                            for e in state.decision_events))
        self.assertIsNone(reservation_of(state))
        cmd = commands.get(pioneer.id) or {}
        self.assertIn(cmd.get('action'), ('move', 'buy', 'use'))
        self.assertTrue(any(e['code'] in ('pioneer_buys_voucher', 'pioneer_voucher_job', 'voucher_buyer_pick')
                            for e in state.decision_events))
        self.assertFalse(any(e['code'] == 'income_muster' and e.get('role_id') == pioneer.id
                             for e in state.decision_events))
        row = self._evidence_row(state, pioneer, commands, stderr.getvalue())
        self.assertEqual(row['occupancy'], 'free')
        self.assertNotEqual(row['finalAction'], None)

    def test_at_gun_without_pressure_does_not_muster_idle(self):
        from src.agent.economy import defense_occupancy, muster_for_night
        state, pioneer = self._post_task_home_state(stale_active=False)
        blocked = build_blocked_set(state)
        occupancy, snap = defense_occupancy(pioneer, state, blocked)
        self.assertEqual(occupancy, 'free')
        self.assertTrue(snap.get('atGun') or snap.get('alreadyAtPost'))
        handled, cmd = muster_for_night(pioneer, state, blocked, set())
        self.assertFalse(handled)
        self.assertIsNone(cmd)
        self.assertTrue(any(e['code'] == 'at_post_no_mandatory_hold' for e in state.decision_events))

    def test_real_pressure_still_holds_at_gun(self):
        state, pioneer = self._post_task_home_state()
        state.robot.roles = [
            RobotRole(200 + i, Pos(10, 10), 'largeRobot', 100) for i in range(4)
        ]
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            commands = self.decide(state)
        self.assertNotEqual((commands.get(pioneer.id) or {}).get('action'), 'buy')
        row = self._evidence_row(state, pioneer, commands, stderr.getvalue())
        self.assertEqual(row['occupancy'], 'must_hold')
        self.assertTrue(any(e['code'] in ('income_muster', 'mandatory_hold_no_action')
                            for e in state.decision_events if e.get('role_id') == pioneer.id)
                        or row['occupancy'] == 'must_hold')

    def test_existing_worker_weapon_job_is_not_duplicated(self):
        state, pioneer = self._post_task_home_state()
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(8, 9)
        state.worker_item_jobs[worker.id] = {
            'kind': 'weapon', 'item': 'WeaponUpgradeVoucher1', 'target': (12, 10),
        }
        commands = self.decide(state)
        self.assertEqual(state.worker_item_jobs.get(worker.id, {}).get('kind'), 'weapon')
        self.assertNotEqual((commands.get(pioneer.id) or {}).get('action'), 'buy')
        self.assertTrue(any(e['code'] == 'voucher_skip_not_selected' and e.get('buyer_id') == worker.id
                            for e in state.decision_events if e.get('role_id') == pioneer.id)
                        or (commands.get(worker.id) or {}).get('action') in ('buy', 'move', 'use'))

    def test_gold_short_is_not_reported_as_defense(self):
        state, pioneer = self._post_task_home_state(gold=0)
        self.decide(state)
        self.assertFalse(any(e['code'] == 'income_muster' and e.get('role_id') == pioneer.id
                             for e in state.decision_events))
        self.assertTrue(any(e['code'] in (
            'pioneer_voucher_wait_gold', 'voucher_skip_not_selected', 'voucher_no_buyer',
            'weapon_upgrade_funding_gap', 'voucher_skip_not_due',
        ) for e in state.decision_events))

    def test_round_evidence_r25_task_then_r30_home_upgrade(self):
        rows = []
        state, pioneer = self.armed_day(round_no=25, gold=40, pioneer_pos=Pos(20, 20),
                                        task_pos=Pos(20, 20), timeout=15)
        state.phase_task = '部署修复任务'
        state.policy_memory['pioneer_task_reservation'] = {
            'pioneerId': pioneer.id, 'taskType': '自进化类1', 'x': 20, 'y': 20,
            'stage': 'active', 'sinceRound': 18,
        }
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            commands = self.decide(state)
        rows.append(self._evidence_row(state, pioneer, commands, stderr.getvalue()))
        self.assertTrue(rows[-1]['phaseTask'])
        self.assertNotEqual(rows[-1]['finalAction'], 'buy')

        state, pioneer = self._post_task_home_state(round_no=30, gold=130)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            commands = self.decide(state)
        rows.append(self._evidence_row(state, pioneer, commands, stderr.getvalue()))
        self.assertFalse(rows[-1]['phaseTask'])
        self.assertIsNone(rows[-1]['reservation'])
        self.assertEqual(rows[-1]['occupancy'], 'free')
        self.assertIn(rows[-1]['finalAction'], ('move', 'buy', 'use'))
        self.assertEqual(rows[-1]['gold'], 130)
        self.assertIsNotNone(rows[-1]['weaponJob'] or rows[-1]['finalAction'])
        state._opening_idle_evidence = rows
        self.assertEqual(len(rows), 2)


if __name__ == '__main__':
    unittest.main()
