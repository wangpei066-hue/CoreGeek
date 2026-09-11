import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from src.agent import GameServer
from src.agent.task_solver import extract_md_paths, parse_llm, sandbox_command, READ_SCRIPT


class TaskSolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = GameServer(self.root)
        self.client = self.server.app.test_client()
        self.payload = json.loads((Path(__file__).parent / 'fixtures/sample_match_state.json').read_text())
        self.payload.update(roundNo=10, phaseTask='请计算1+1，仅返回数字', llmResp='', lastCmdResult='')
        self.payload['teamOur']['roles'] = [r for r in self.payload['teamOur']['roles'] if r['roleType'] == 'pioneer']
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def post(self):
        response = self.client.post('/', json=self.payload)
        self.assertEqual(response.status_code, 200)
        return response.json

    def next_round(self, **values):
        self.payload['roundNo'] += 1
        self.payload.update(llmResp='', lastCmdResult='', errors=[], lastRoundRoleActionResults={})
        self.payload.update(values)
        return self.post()

    def sandbox(self, command):
        result = subprocess.run(['sh', '-c', command], cwd=self.root, capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        return '[exitCode:0]\n' + result.stdout

    def test_extract_paths(self):
        self.assertEqual(extract_md_paths('阅读`/app/API Guide.md`，再查看 docs/query.md 和「天气说明.md」。'),
                         ['/app/API Guide.md', 'docs/query.md', '天气说明.md'])
        self.assertEqual(extract_md_paths('请阅读说明.md文件，参考说明.md'), ['说明.md'])

    def test_read_llm_tool_submit_and_restart(self):
        doc = self.root / "API guide.md"
        doc.write_text('运行 python3 -c "print(42)" 获取答案，然后提交结果。')
        self.payload['phaseTask'] = f'阅读 `{doc}`，按说明获取答案'
        first = self.post()
        self.assertEqual(first['prompt'], '')
        feedback = self.sandbox(first['executeCmd'])
        self.assertIn('read_document', feedback)
        # 同回合重试不改变命令。
        self.assertEqual(first, self.post())
        # 重启后恢复等待沙盒结果的状态。
        self.server = GameServer(self.root)
        self.client = self.server.app.test_client()
        second = self.next_round(lastCmdResult=feedback)
        self.assertIn('获取答案，然后提交结果', second['prompt'])
        self.assertIn('phaseTask', second['prompt'])
        third = self.next_round(llmResp=json.dumps({'action': 'execute', 'command': 'python3 -c "print(42)"'}))
        tool_feedback = self.sandbox(third['executeCmd'])
        fourth = self.next_round(lastCmdResult=tool_feedback)
        self.assertIn('42', fourth['prompt'])
        fifth = self.next_round(llmResp='```json\n{"action":"submit","taskAnswer":"42"}\n```')
        self.assertEqual(fifth['roleCommandMap']['10011'], {'action': 'submitAnswer', 'taskAnswer': '42'})
        sixth = self.next_round(lastRoundRoleActionResults={'10011': True})
        self.assertNotIn('10011', sixth['roleCommandMap'])
        self.assertEqual(sixth['prompt'], '')
        final = self.next_round(phaseTask='')
        self.assertEqual(final['executeCmd'], '')
        self.assertEqual(self.server.task_solver.session, {})

    def test_paged_read(self):
        doc = self.root / 'long.md'
        doc.write_text('甲' * 6000 + '末尾答案')
        self.payload['phaseTask'] = f'阅读 `{doc}`'
        first = self.post()
        second = self.next_round(lastCmdResult=self.sandbox(first['executeCmd']))
        self.assertEqual(second['prompt'], '')
        third = self.next_round(lastCmdResult=self.sandbox(second['executeCmd']))
        self.assertIn('末尾答案', third['prompt'])

    def test_missing_file_goes_to_llm_with_error(self):
        self.payload['phaseTask'] = '阅读 `/does-not-exist/task.md`'
        first = self.post()
        second = self.next_round(lastCmdResult=self.sandbox(first['executeCmd']))
        self.assertIn('error', second['prompt'])
        self.assertNotIn('10011', second['roleCommandMap'])

    def test_invalid_llm_and_wrong_answer_retry(self):
        self.assertTrue(self.post()['prompt'])
        bad = self.next_round(llmResp='I think 2')
        self.assertTrue(bad['prompt'])
        self.assertNotIn('10011', bad['roleCommandMap'])
        submit = self.next_round(llmResp='{"action":"submit","taskAnswer":"2"}')
        self.assertEqual(submit['roleCommandMap']['10011']['taskAnswer'], '2')
        retry = self.next_round(errors=[{'errorCode': 2, 'description': '答案不完全正确'}])
        self.assertIn('答案不完全正确', retry['prompt'])

    def test_wrong_sandbox_correlation_does_not_feed_llm(self):
        self.payload['phaseTask'] = '阅读 `/task.md`'
        first = self.post()
        second = self.next_round(lastCmdResult='[exitCode:0]\n{"marker":"PIONEER_TASK","requestId":"wrong","content":"bad"}')
        self.assertEqual(first['executeCmd'], second['executeCmd'])
        self.assertEqual(second['prompt'], '')

    def test_find_relative_file_and_reject_ambiguous_names(self):
        for directory in ('one', 'two'):
            (self.root / directory).mkdir()
            (self.root / directory / 'guide.md').write_text(directory)
        feedback = self.sandbox(sandbox_command(READ_SCRIPT, {'path': 'one/guide.md', 'requestId': 'exact'}))
        self.assertEqual(json.loads(feedback.split('\n', 1)[1])['content'], 'one')
        feedback = self.sandbox(sandbox_command(READ_SCRIPT, {'path': 'guide.md', 'requestId': 'ambiguous'}))
        self.assertEqual(json.loads(feedback.split('\n', 1)[1])['error'], 'ambiguous_path')

    def test_find_nested_basename(self):
        (self.root / 'docs').mkdir()
        (self.root / 'docs' / 'guide.md').write_text('nested content')
        feedback = self.sandbox(sandbox_command(READ_SCRIPT, {'path': 'guide.md', 'requestId': 'nested'}))
        self.assertEqual(json.loads(feedback.split('\n', 1)[1])['content'], 'nested content')

    def test_task_change_discards_old_llm_answer(self):
        self.post()
        response = self.next_round(phaseTask='新的题目', llmResp='{"action":"submit","taskAnswer":"旧答案"}')
        self.assertNotIn('10011', response['roleCommandMap'])
        self.assertIn('新的题目', response['prompt'])

    def test_missing_tool_feedback_does_not_repeat_command(self):
        self.post()
        tool = self.next_round(llmResp='{"action":"execute","command":"echo test"}')
        response = self.next_round(lastCmdResult='[TIMEOUT]')
        self.assertNotEqual(tool['executeCmd'], response['executeCmd'])
        self.assertIn('[TIMEOUT]', response['prompt'])

    def test_shell_quoting(self):
        path = self.root / "a'$(touch INJECTED).md"
        path.write_text('safe')
        result = self.sandbox(sandbox_command(READ_SCRIPT, {'path': str(path), 'requestId': 'test'}))
        self.assertIn('safe', result)
        self.assertFalse((self.root / 'INJECTED').exists())

    def test_non_string_answer_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_llm('{"action":"submit","taskAnswer":{"result":2}}')


if __name__ == '__main__':
    unittest.main()
