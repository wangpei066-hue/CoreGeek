import contextlib
import io
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

from src.agent import GameServer
from src.agent.task_solver import extract_md_paths, parse_llm, sandbox_command, READ_SCRIPT, task_context


class TaskSolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = GameServer(self.root)
        self.client = self.server.app.test_client()
        self.payload = json.loads((Path(__file__).parent / 'fixtures/sample_match_state.json').read_text(encoding='utf-8'))
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
        if not shutil.which('sh'):
            self.skipTest('需要 POSIX sh；请在 Linux 比赛运行环境补跑沙盒集成测试')
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
        # 任务结束后，空闲沙盒可继续回传 main 的新闻诊断。
        if final['executeCmd']:
            record = json.loads(self.sandbox(final['executeCmd']).split('\n', 1)[1])
            self.assertEqual(record['marker'], 'NEWS_INFER')
        self.assertEqual(self.server.task_solver.session, {})

    def test_workspace_execution_survives_restart(self):
        workspace = self.root / 'project with spaces'
        workspace.mkdir()
        (workspace / 'task.md').write_text('将 result.txt 写入 done，再检查文件内容。')
        (self.root / 'task.md').write_text('错误目录的任务')
        self.payload['phaseTask'] = f'工作区路径：`{workspace}`，读取 `task.md` 并完成任务'
        first = self.post()
        prompt = self.next_round(lastCmdResult=self.sandbox(first['executeCmd']))['prompt']
        self.assertIn('将 result.txt', prompt)
        self.assertNotIn('错误目录的任务', prompt)
        self.assertEqual(self.server.task_solver.session['taskKind'], 'workspace')
        action = self.next_round(llmResp=json.dumps(dict(action='execute', command='printf done > result.txt')))
        feedback = self.sandbox(action['executeCmd'])
        self.assertEqual((workspace / 'result.txt').read_text(), 'done')
        self.assertFalse((self.root / 'result.txt').exists())
        self.server = GameServer(self.root)
        self.client = self.server.app.test_client()
        self.next_round(lastCmdResult=feedback)
        verify = self.next_round(llmResp=json.dumps(dict(action='execute', command='cat result.txt')))
        result = self.sandbox(verify['executeCmd'])
        self.assertIn('done', result)

    def test_workspace_missing_document_does_not_search_other_projects(self):
        workspace = self.root / 'project'
        workspace.mkdir()
        (self.root / 'task.md').write_text('unrelated')
        feedback = self.sandbox(sandbox_command(READ_SCRIPT, dict(
            requestId='scoped', path='task.md', workspace=str(workspace))))
        result = json.loads(feedback.split('\n', 1)[1])
        self.assertIn('error', result)
        self.assertNotIn('content', result)

    def test_api_context_and_explicit_workspace(self):
        self.assertEqual(task_context('调用API http://service/query'), dict(taskKind='api', workspace=None))
        self.assertEqual(task_context('工作区路径：/app/project，任务描述 task.md')['workspace'], '/app/project')
        self.payload['phaseTask'] = '调用API查询天气，接口说明已在任务中给出'
        self.assertIn('"taskKind": "api"', self.post()['prompt'])
        workspace = self.root / 'api'
        workspace.mkdir()
        action = self.next_round(llmResp=json.dumps(dict(
            action='execute', command='pwd', workspace=str(workspace))))
        self.assertIn(str(workspace), self.sandbox(action['executeCmd']))
        self.assertEqual(self.server.task_solver.session['workspace'], str(workspace))

    def test_gamma_deployment_example(self):
        task = ('修复应用gamma部署，任务背景是：应用gamma的部署环境在本任务文件所在目录的'
                r'ws\_3/ /tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/ws\_3/ 中，'
                '请完成修复。任务要求是进入工作区，阅读spec.md修复所有问题，运行./check验证，'
                '当./check全部通过并输出TOKEN:xxx时，任务完成，提交为{"token":"xxx"}')
        self.assertEqual(task_context(task), dict(taskKind='workspace',
            workspace='/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/ws_3/'))
        self.assertEqual(extract_md_paths(task), ['spec.md'])
        workspace = self.root / 'ws_3'
        workspace.mkdir()
        (workspace / 'spec.md').write_text('修复deployment.txt，使其内容为ready，然后运行./check。')
        checker = workspace / 'check'
        checker.write_text('#!/bin/sh\n[ "$(cat deployment.txt)" = ready ] || exit 1\nprintf "All passed\\nTOKEN:gamma-verified\\n"\n')
        checker.chmod(0o755)
        self.payload['phaseTask'] = task.replace(
            r'/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/ws\_3/', str(workspace) + '/')
        response = self.post()
        prompt = self.next_round(lastCmdResult=self.sandbox(response['executeCmd']))['prompt']
        self.assertIn('修复deployment.txt', prompt)
        action = self.next_round(llmResp=json.dumps(dict(action='execute', command='./check')))
        feedback = self.sandbox(action['executeCmd'])
        self.assertEqual(json.loads(feedback.split('\n', 1)[1])['exitCode'], 1)
        self.next_round(lastCmdResult=feedback)
        action = self.next_round(llmResp=json.dumps(dict(
            action='execute', command='printf ready > deployment.txt && ./check')))
        feedback = self.sandbox(action['executeCmd'])
        self.assertIn('TOKEN:gamma-verified', feedback)
        self.next_round(lastCmdResult=feedback)
        response = self.next_round(llmResp=json.dumps(dict(
            action='submit', taskAnswer=json.dumps({'token': 'gamma-verified'}))))
        answer = response['roleCommandMap']['10011']['taskAnswer']
        self.assertEqual(json.loads(answer), {'token': 'gamma-verified'})

    def test_ambiguous_workspace_and_api_url_are_not_guessed(self):
        self.assertIsNone(task_context('部署环境可能位于 /tmp/one/ 或 /tmp/two/ ，进入工作区阅读spec.md')['workspace'])
        self.assertIsNone(task_context('修复API，地址 https://service/api/')['workspace'])

    def test_discovered_text_instructions_and_custom_submission(self):
        workspace = self.root / 'delta release'
        workspace.mkdir()
        (workspace / 'repair.txt').write_text(
            '写入 deployed 文件，再运行 python3 verify.py，成功后提交 {"receipt":"实际回执"}。' + ' ' * 6000 + '不要提交token。')
        (workspace / 'verify.py').write_text(
            'from pathlib import Path\nassert Path("deployed").read_text() == "yes"\nprint("RECEIPT=delta-ok")\n')
        self.payload['phaseTask'] = f'工作目录：`{workspace}`。根据目录中的说明修复应用。'
        self.assertTrue(self.post()['prompt'])
        action = self.next_round(llmResp=json.dumps(dict(action='execute', command='ls')))
        prompt = self.next_round(lastCmdResult=self.sandbox(action['executeCmd']))['prompt']
        self.assertIn('repair.txt', prompt)
        action = self.next_round(llmResp=json.dumps(dict(action='read', path='repair.txt')))
        page = self.next_round(lastCmdResult=self.sandbox(action['executeCmd']))
        self.assertTrue(page['executeCmd'])
        prompt = self.next_round(lastCmdResult=self.sandbox(page['executeCmd']))['prompt']
        self.assertIn('不要提交token', prompt)
        action = self.next_round(llmResp=json.dumps(dict(action='execute',
            command='printf yes > deployed && python3 verify.py')))
        prompt = self.next_round(lastCmdResult=self.sandbox(action['executeCmd']))['prompt']
        self.assertIn('RECEIPT=delta-ok', prompt)
        result = self.next_round(llmResp=json.dumps(dict(action='submit',
            taskAnswer=json.dumps({'receipt': 'delta-ok'}))))
        self.assertEqual(json.loads(result['roleCommandMap']['10011']['taskAnswer']), {'receipt': 'delta-ok'})

    def test_unresolved_workspace_defers_relative_document_read(self):
        self.payload['phaseTask'] = '在本任务文件所在目录的工程内修复应用，阅读instructions.md'
        first = self.post()
        self.assertTrue(first['prompt'])
        self.assertEqual(self.server.task_solver.session['stage'], 'wait_llm')
        workspace = self.root / 'resolved'
        workspace.mkdir()
        (workspace / 'instructions.md').write_text('真实任务说明')
        action = self.next_round(llmResp=json.dumps(dict(action='read',
            path='instructions.md', workspace=str(workspace))))
        prompt = self.next_round(lastCmdResult=self.sandbox(action['executeCmd']))['prompt']
        self.assertIn('真实任务说明', prompt)
        self.assertEqual(Path(self.server.task_solver.session['workspace']).resolve(), workspace.resolve())

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

    def test_request_logging_preserves_main_behavior(self):
        self.payload['llmResp'] = '原始响应' * 2000
        self.post()
        record = json.loads((self.root / 'logs/request_000001.json').read_text())
        self.assertEqual(record, self.payload)
        self.assertNotIn('PIONEER_TASK_EXCHANGE', self.output.getvalue())

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
