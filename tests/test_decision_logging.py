import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.agent import GameServer
from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.decision_log import CONSOLE_MARKER, build_report, emit_console_report, snapshot
from test_shop_items import minimal_state, make_role


class DecisionLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = GameServer(self.root)
        self.client = self.server.app.test_client()

    def read_report(self, sequence=1):
        return json.loads((self.root / f'logs/decision_{sequence:06d}.json').read_text(encoding='utf-8'))

    def test_files_are_paired_and_response_protocol_unchanged(self):
        response = self.client.post('/', json={'roundNo': 1})
        self.assertEqual(response.json, {'roleCommandMap': {}, 'prompt': '', 'executeCmd': ''})
        self.assertEqual(self.read_report()['sequence'], 1)
        self.assertEqual(self.read_report()['events'][0]['code'], 'missing_state')
        self.assertIn('回合 1', (self.root / 'logs/decision_000001.txt').read_text(encoding='utf-8'))
        restarted = GameServer(self.root)
        restarted.app.test_client().post('/', json={'roundNo': 2})
        self.assertEqual(self.read_report(2)['round'], 2)

    def test_idle_pioneer_records_actual_budget_reason(self):
        state = minimal_state(gold_num=75)
        state.team_our.roles.append(make_role(1, 1, 1, 'pioneer', back_pack_capability=40))
        before = snapshot(state)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        report = build_report(state, commands, {}, before, None, 1, 0, '白天')
        role = report['roles'][0]
        self.assertEqual(role['status'], 'idle')
        event = next(e for e in role['events'] if e['code'] == 'early_buy_blocked')
        self.assertEqual(event['item'], 'StationUpgradeVoucher1')

    def test_attack_belongs_to_controller_not_idle_role(self):
        from src.agent.protocol import RobotRole, Pos
        state = minimal_state(round_no=80)
        state.team_our.roles += [make_role(1, 12, 12, 'worker'),
                                 make_role(2, 13, 12, 'gatling', attack_range=3, level=1)]
        state.robot.roles = [RobotRole(id=9, pos=Pos(14, 12), role_type='smallRobot', health=40)]
        before = snapshot(state)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        report = build_report(state, commands, {}, before, None, 1, 0, '夜晚')
        self.assertEqual(report['roles'][0]['status'], 'action')
        self.assertEqual(report['roles'][0]['command_key'], 2)

    def test_previous_command_is_captured_before_strategy_overwrites_it(self):
        fixture = Path(__file__).parent / 'fixtures/sample_match_state.json'
        payload = json.loads(fixture.read_text(encoding='utf-8'))
        payload['roundNo'] = 10
        first = self.client.post('/', json=payload).json['roleCommandMap']
        self.assertTrue(first)
        payload['roundNo'] = 11
        payload['lastRoundRoleActionResults'] = {key: False for key in first}
        self.client.post('/', json=payload)
        feedback = self.read_report(2)['previous_feedback']
        self.assertEqual({str(f['command_key']): f['command'] for f in feedback}, first)
        self.assertTrue(all('未提供' in f['message'] for f in feedback))

    def test_logging_failure_does_not_fail_response(self):
        with patch('src.agent.server.write_report', side_effect=OSError('disk unavailable')), patch('src.agent.server.emit_console_report') as console:
            with self.assertLogs(self.server.app.logger, level='ERROR'):
                response = self.client.post('/', json={})
        self.assertEqual(response.status_code, 200)
        console.assert_called_once()

    def test_diagnostics_expose_defense_and_economy_without_mutating_state(self):
        from copy import deepcopy
        from test_defense_priority import defended
        state = defended()
        state.round_no = 200
        state.team_our.roles[1].backpack = ['copper'] * 8
        previous = snapshot(state)
        previous['round'] = 199
        previous['gold'] = 25
        memory = deepcopy(state.policy_memory)
        commands = {1: {'action': 'move', 'targetPos': [{'x': 8, 'y': 9}]}}
        report = build_report(state, {}, commands, snapshot(state), previous, 1, 0, '夜晚')
        diag = report['diagnostics']
        self.assertEqual(diag['gold_delta'], 50)
        self.assertEqual(diag['primary']['planned'], 14)
        self.assertEqual(diag['outer']['planned'], 5)
        self.assertEqual(len(diag['weapons']), 3)
        self.assertTrue(any(a['code'] == 'MOVE_NO_PROGRESS' for a in diag['alerts']))
        self.assertTrue(any(a['code'] == 'NIGHT_UNSTATIONED' for a in diag['alerts']))
        self.assertEqual(state.policy_memory, memory)

    def test_diagnostics_do_not_compare_unrelated_matches(self):
        from test_defense_priority import defended
        state = defended()
        previous = snapshot(state)
        previous['context'] = {'different': 'match'}
        previous['gold'] = 9999
        report = build_report(state, {}, {}, snapshot(state), previous, 1, 0, '白天')
        self.assertIsNone(report['diagnostics']['gold_delta'])
        self.assertIsNotNone(report['diagnostics']['initial_context'])

    def test_invalid_json_does_not_generate_decision(self):
        self.assertEqual(self.client.post('/', data='{broken', content_type='application/json').status_code, 400)
        self.assertFalse(list(self.root.glob('logs/decision_*')))

    def test_trace_is_reset_each_round(self):
        state = minimal_state(gold_num=75)
        state.team_our.roles.append(make_role(1, 1, 1, 'pioneer', back_pack_capability=40))
        strategy = V1Strategy(BasicActionValidator())
        strategy.decide(state)
        count = len(state.decision_events)
        strategy.decide(state)
        self.assertEqual(len(state.decision_events), count)

    def test_console_record_contains_strategy_summary_and_role_reasons(self):
        state = minimal_state(gold_num=75)
        state.team_our.roles.append(make_role(1, 1, 1, 'pioneer', back_pack_capability=40))
        before = snapshot(state)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        report = build_report(state, commands, {}, before, None, 7, 1.2, '白天')
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            emit_console_report(report)
        lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        markers = [row['marker'] for row in lines]
        self.assertIn(CONSOLE_MARKER, markers)
        self.assertIn('BUILD_WEAPON', markers)
        self.assertIn('BUILD_WALL', markers)
        self.assertIn('PIONEER_TASK', markers)
        record = next(row for row in lines if row['marker'] == CONSOLE_MARKER)
        self.assertEqual(record['event'], 'round')
        self.assertEqual(record['gold'], 75)
        self.assertEqual(record['roles'][0]['id'], 1)
        self.assertIn('title', record)
        self.assertNotIn('reasons', record['roles'][0])
        weapon = next(row for row in lines if row['marker'] == 'BUILD_WEAPON')
        self.assertEqual(weapon['event'], 'status')
        self.assertIn('standing', weapon)

    def test_server_emits_console_strategy_record(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.client.post('/', json={'roundNo': 1})
        records = [json.loads(line) for line in output.getvalue().splitlines()
                   if 'STRATEGY_DECISION' in line]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['roundNo'], 1)

    def test_commit_banner_prints_once_per_process(self):
        from src.agent import log_format
        log_format._COMMIT_LOGGED = False
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            log_format.log_commit_banner(0)
            log_format.log_commit_banner(1)
            log_format.log_commit_banner(2)
        lines = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['marker'], 'BUILD_INFO')
        self.assertEqual(lines[0]['event'], 'commit')
        self.assertEqual(lines[0]['roundNo'], 0)
        self.assertTrue(lines[0].get('commit'))
