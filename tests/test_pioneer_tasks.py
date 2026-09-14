import contextlib
import io
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

from src.agent import GameServer
from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.protocol import MatchState


class PioneerTaskTests(unittest.TestCase):
    def payload(self, round_no=10):
        data = json.loads((Path(__file__).parent / 'fixtures/sample_match_state.json').read_text(encoding='utf-8'))
        data['roundNo'] = round_no
        data['phaseTask'] = ''
        data['teamOur']['roles'] = [r for r in data['teamOur']['roles'] if r['roleType'] == 'pioneer']
        data['teamOur']['roles'][0]['pos'] = {'x': 13, 'y': 13}
        return data

    def decide(self, data):
        state = MatchState()
        state.update(data)
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_accept_only_during_day(self):
        self.assertEqual(self.decide(self.payload(10))[10011], {'action': 'acceptTask'})
        self.assertNotEqual(self.decide(self.payload(80)).get(10011, {}).get('action'), 'acceptTask')

    def test_moves_to_task(self):
        data = self.payload()
        data['teamOur']['roles'][0]['pos'] = {'x': 9, 'y': 9}
        self.assertEqual(self.decide(data)[10011]['action'], 'move')

    def test_unavailable_tasks_are_not_accepted(self):
        for invalid in ({'isValid': False}, {'coldDownRounds': 2}):
            data = self.payload()
            for task in data['teamOur']['playerTasks']:
                task.update(invalid)
            self.assertNotEqual(self.decide(data).get(10011, {}).get('action'), 'acceptTask')

    def test_active_task_stays_day_and_night_even_after_restart(self):
        for round_no in (10, 80):
            data = self.payload(round_no)
            data['phaseTask'] = '任务原文'
            self.assertNotIn(10011, self.decide(data))

    def test_dead_pioneer_does_not_accept(self):
        data = self.payload()
        data['teamOur']['roles'][0]['health'] = 0
        self.assertNotIn(10011, self.decide(data))

    @unittest.skipUnless(shutil.which('sh'), '需要 POSIX sh 执行平台沙盒命令')
    def test_platform_command_output_and_feedback(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stderr(io.StringIO()) as stderr:
            server = GameServer(Path(root))
            client = server.app.test_client()
            data = self.payload()
            response = client.post('/', json=data)
            self.assertEqual(response.json['roleCommandMap']['10011'], {'action': 'acceptTask'})
            self.assertEqual(response.json['executeCmd'], '')
            data.update(roundNo=11, phaseTask="查询天气：'；$(exit 9) `exit 8`\n下一行",
                        lastRoundRoleActionResults={'10011': True})
            response = client.post('/', json=data)
            result = subprocess.run(['sh', '-c', response.json['executeCmd']], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            record = json.loads(result.stdout)
            self.assertEqual(record['marker'], 'PIONEER_TASK')
            self.assertEqual(record['phaseTaskChunk'], data['phaseTask'])
            data.update(roundNo=12, lastCmdResult='[exitCode:0]\n' + result.stdout)
            self.assertEqual(client.post('/', json=data).status_code, 200)
            records = [record for line in stderr.getvalue().splitlines()
                       if (record := json.loads(line)).get('marker') == 'PIONEER_TASK']
            self.assertEqual(records[1]['pioneers'][0]['previousCommand'], {'action': 'acceptTask'})
            self.assertTrue(records[1]['pioneers'][0]['lastActionLegal'])
            self.assertIn('PIONEER_TASK', records[2]['lastCmdResult'])
            data.update(roundNo=13, phaseTask='')
            for task in data['teamOur']['playerTasks']:
                task.update(isValid=False, coldDownRounds=30)
            self.assertEqual(client.post('/', json=data).json['executeCmd'], '')


if __name__ == '__main__':
    unittest.main()
