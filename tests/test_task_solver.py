import contextlib
import io
import json
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

from src.agent import GameServer
from src.agent.economy import solver_ready_to_submit
from src.agent.task_solver import (
    extract_md_paths, parse_llm, sandbox_command, READ_SCRIPT, PROBE_SCRIPT,
    harvest_api_call, matching_api_experience, build_api_answer, is_plain_int,
    PROMPT_HASH, PROMPT_VERSION, PioneerTaskSolver, BASE_PROMPT, DEPLOYMENT_SOP, API_SOP,
    MARKER, task_fingerprint, relevant_md_paths, task_context,
    curl_api_command, ingest_api_page, parse_curl_output, extract_city,
)
from test_opening import opening_state


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
        self.assertEqual(extract_city('/tmp/task_2_nanjing.md'), '南京')

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
        self.assertIn('deploy_probe', first['executeCmd'])
        prompt = self.next_round(lastCmdResult=self.sandbox(first['executeCmd']))['prompt']
        self.assertIn('将 result.txt', prompt)
        self.assertNotIn('错误目录的任务', prompt)
        self.assertEqual(self.server.task_solver.session['taskKind'], 'workspace')
        self.assertIn('部署任务首次探查', prompt)
        self.assertIn(PROMPT_VERSION, prompt)
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
        prompt = self.post()['prompt']
        self.assertIn('"taskKind": "api"', prompt)
        self.assertIn('优先复用', prompt)
        self.assertNotIn('部署任务首次探查', prompt)
        self.assertNotIn('部署修复 SOP', prompt)
        self.assertIn(PROMPT_HASH, prompt)
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
        self.assertIn('deploy_probe', response['executeCmd'])
        prompt = self.next_round(lastCmdResult=self.sandbox(response['executeCmd']))['prompt']
        self.assertIn('修复deployment.txt', prompt)
        self.assertIn('部署任务首次探查', prompt)
        action = self.next_round(llmResp=json.dumps(dict(action='execute', command='./check')))
        feedback = self.sandbox(action['executeCmd'])
        self.assertEqual(json.loads(feedback.split('\n', 1)[1])['exitCode'], 1)
        self.next_round(lastCmdResult=feedback)
        action = self.next_round(llmResp=json.dumps(dict(
            action='execute', command='printf ready > deployment.txt && ./check')))
        feedback = self.sandbox(action['executeCmd'])
        self.assertIn('TOKEN:gamma-verified', feedback)
        response = self.next_round(lastCmdResult=feedback)
        answer = response['roleCommandMap']['10011']['taskAnswer']
        self.assertEqual(json.loads(answer), {'token': 'gamma-verified'})
        self.assertEqual(self.server.task_solver.session.get('submitStatus'), 'sent')

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
        first = self.post()
        self.assertTrue(first['executeCmd'])
        prompt = self.next_round(lastCmdResult=self.sandbox(first['executeCmd']))['prompt']
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
        record = json.loads((self.root / 'logs/request_000001.json').read_text(encoding='utf-8'))
        expected = {key: value for key, value in self.payload.items() if key != 'llmResp'}
        self.assertEqual(record, expected)
        self.assertIn('PIONEER_TASK_EXCHANGE', self.output.getvalue())

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

    def test_task_switch_archives_and_keeps_experience(self):
        self.server.task_solver.experience['api'] = [{
            'baseUrl': 'http://127.0.0.1:8080', 'path': '/api/v1/heritage/search',
            'method': 'GET', 'authStyle': 'Authorization: Bearer', 'cityParam': 'location',
            'recordsPath': 'data.records', 'serviceHint': 'heritage',
        }]
        self.post()
        response = self.next_round(phaseTask='新的题目', llmResp='{"action":"submit","taskAnswer":"旧答案"}')
        self.assertNotIn('10011', response['roleCommandMap'])
        self.assertTrue(self.server.task_solver.archives)
        self.assertEqual(self.server.task_solver.experience['api'][0]['path'], '/api/v1/heritage/search')
        restored = self.next_round(phaseTask='请计算1+1，仅返回数字')
        self.assertTrue(self.server.task_solver.session.get('restored'))
        self.assertIn('请计算1+1', restored['prompt'])

    def test_empty_sandbox_waits_before_reasking_llm(self):
        self.post()
        tool = self.next_round(llmResp='{"action":"execute","command":"echo test"}')
        self.assertTrue(tool['executeCmd'])
        first = self.next_round(lastCmdResult='')
        self.assertEqual(first['prompt'], '')
        self.assertEqual(first['executeCmd'] or '', '')
        self.assertEqual(self.server.task_solver.session['stage'], 'wait_tool')
        self.next_round(lastCmdResult='')
        self.assertEqual(self.server.task_solver.session['stage'], 'wait_tool')
        recovered = self.next_round(lastCmdResult='')
        self.assertIn('等待沙盒结果超时', recovered['prompt'])

    def test_read_retry_does_not_bypass_holding(self):
        from src.agent.protocol import MatchState
        state = MatchState()
        self.payload['phaseTask'] = '阅读 `guide.md`'
        state.update(self.payload)
        solver = self.server.task_solver
        solver.session = dict(
            key=[state.team_our.team_id, state.team_our.type, state.phase_task],
            stage='wait_read', requestId='rid', pendingCommand='OLD_READ',
            paths=['guide.md'], documents=[], history=[], index=0, offset=0,
            calls=0, retries=0, round=9, taskKind='unknown', workspace=None,
            metrics={}, promptVersion=PROMPT_VERSION, promptHash=PROMPT_HASH,
        )
        state.round_no = 10
        state.last_cmd_result = '[TIMEOUT]'
        prompt, execute = solver.step(state, {10011: {'action': 'move', 'targetPos': [{'x': 1, 'y': 1}]}})
        self.assertEqual((prompt, execute), ('', ''))
        self.assertEqual(solver.session['stage'], 'wait_read')
        self.assertTrue(solver.session.get('resendPending'))
        state.round_no = 11
        prompt, execute = solver.step(state, {})
        self.assertEqual(execute, 'OLD_READ')

    def test_accepted_submit_is_not_confirmed_until_task_clears(self):
        self.post()
        submitted = self.next_round(llmResp='{"action":"submit","taskAnswer":"2"}')
        self.assertEqual(submitted['roleCommandMap']['10011']['taskAnswer'], '2')
        self.assertEqual(self.server.task_solver.session.get('submitStatus'), 'sent')
        waiting = self.next_round(lastRoundRoleActionResults={'10011': True})
        self.assertNotIn('10011', waiting['roleCommandMap'])
        self.assertEqual(self.server.task_solver.session.get('submitStatus'), 'accepted')
        self.assertEqual(waiting['prompt'], '')
        still = self.next_round(lastRoundRoleActionResults={'10011': True},
                                errors=[{'errorCode': 4, 'description': '建造失败'}])
        self.assertEqual(self.server.task_solver.session.get('submitStatus'), 'accepted')
        self.assertEqual(still['prompt'], '')
        self.next_round(phaseTask='')
        self.assertEqual(self.server.task_solver.session, {})

    def test_heritage_experience_is_reused_for_nanjing(self):
        self.server.task_solver.experience['api'] = [{
            'baseUrl': 'http://127.0.0.1:8080', 'path': '/api/v1/heritage/search',
            'method': 'GET', 'authStyle': 'Authorization: Bearer', 'cityParam': 'location',
            'extraParams': {'limit': '1000'}, 'recordsPath': 'data.records',
            'pagination': None, 'serviceHint': 'heritage',
        }]
        self.payload['phaseTask'] = (
            '调用API查询南京遗产。密钥：tok-1。'
            '提交{"city":"南京","total_count":0,"type_count":0,"oldest_era":"名称"}'
        )
        first = self.post()
        self.assertIn('/api/v1/heritage/search', first['executeCmd'])
        self.assertTrue(first['executeCmd'].lstrip().startswith('curl '))
        self.assertNotIn('python3', first['executeCmd'])
        self.assertIn('location', first['executeCmd'])
        self.assertTrue(self.server.task_solver.session.get('experienceHit'))
        rid = self.server.task_solver.session['requestId']
        result = dict(
            marker='PIONEER_TASK', requestId=rid, event='api_fetch', ok=True,
            recordsComplete=True, completenessEvidence='total=2 records=2',
            totalCount=2, typeCount=1, oldestEraName='明孝陵', city='南京',
            status=200, path='/api/v1/heritage/search',
        )
        response = self.next_round(lastCmdResult='[exitCode:0]\n' + json.dumps(result, ensure_ascii=False))
        answer = json.loads(response['roleCommandMap']['10011']['taskAnswer'])
        self.assertEqual(answer, {'city': '南京', 'total_count': 2, 'type_count': 1, 'oldest_era': '明孝陵'})
        self.assertIsInstance(answer['total_count'], int)

    def test_crlf_experience_is_injected_into_next_deploy_prompt(self):
        self.server.task_solver.experience['deploy'] = [{
            'kind': 'crlf', 'method': 'python_newline', 'paths': ['start.sh'],
            'sourceTask': 'alpha', 'environment': '/tmp/alpha', 'evidence': 'probe_crlf',
        }]
        self.payload['phaseTask'] = '修复应用gamma部署，工作区路径：`/tmp/gamma/`，阅读spec.md'
        first = self.post()
        self.assertIn('deploy_probe', first['executeCmd'])
        rid = self.server.task_solver.session['requestId']
        probe = dict(
            marker='PIONEER_TASK', requestId=rid, event='deploy_probe',
            workspace='/tmp/gamma', listing=['spec.md', 'start.sh'],
            files=[
                {'path': 'spec.md', 'exists': True, 'crlf': False, 'contentHead': '修复后运行./check，输出TOKEN'},
                {'path': 'start.sh', 'exists': True, 'crlf': True, 'shebang': '#!/bin/bash'},
            ],
        )
        prompt = self.next_round(lastCmdResult='[exitCode:0]\n' + json.dumps(probe, ensure_ascii=False))['prompt']
        self.assertIn('python_newline', prompt)
        self.assertIn('CRLF', prompt)
        self.assertIn('deployPhase', prompt)

    def test_harvest_api_call_strips_secrets_and_keeps_city_param(self):
        item = harvest_api_call(
            'curl -H "Authorization: Bearer SECRET" '
            '"http://svc/api/v1/heritage/search?location=北京&limit=1000"',
            json.dumps({'code': 200, 'data': {'records': [{'name': '天坛'}], 'pagination': {'total': 1, 'page': 1}}}, ensure_ascii=False),
            '查询北京遗产')
        self.assertEqual(item['path'], '/api/v1/heritage/search')
        self.assertEqual(item['cityParam'], 'location')
        self.assertEqual(item['authStyle'], 'Authorization: Bearer')
        self.assertNotIn('SECRET', json.dumps(item))
        self.assertNotIn('limit', item.get('extraParams') or {})
        self.assertEqual(
            matching_api_experience({'api': [item]}, '调用API查询南京遗产')['path'],
            '/api/v1/heritage/search')

    def test_prompt_hash_and_kind_specific_sop(self):
        self.assertEqual(len(PROMPT_HASH), 16)
        self.assertIn('优先复用', API_SOP)
        self.assertIn('部署任务首次探查', DEPLOYMENT_SOP)
        self.assertNotIn(DEPLOYMENT_SOP, BASE_PROMPT)
        self.assertNotIn('部署任务首次探查', self.post()['prompt'])

    def test_long_check_output_token_is_taken_from_tail(self):
        solver = PioneerTaskSolver(self.root / 'state')
        session = dict(taskKind='workspace', metrics={})
        result = dict(exitCode=0, output='noise' * 100, outputTail='All passed\nTOKEN:tail-ok\n')
        self.assertTrue(solver._finish_from_tool(session, result, '修复应用', None))
        self.assertEqual(json.loads(session['answer']), {'token': 'tail-ok'})
        self.assertEqual(session['stage'], 'submit')

    def test_duplicate_failed_read_is_blocked(self):
        self.payload['phaseTask'] = '阅读 `/does-not-exist/task.md` 并完成任务'
        first = self.post()
        rid = self.server.task_solver.session['requestId']
        feedback = '[exitCode:0]\n' + json.dumps(dict(
            marker='PIONEER_TASK', requestId=rid, event='read_document', error='not_found',
            path='/does-not-exist/task.md'))
        prompt = self.next_round(lastCmdResult=feedback)['prompt']
        self.assertIn('error', prompt)
        blocked = self.next_round(llmResp=json.dumps(dict(action='read', path='/does-not-exist/task.md')))
        self.assertFalse(blocked.get('executeCmd'))
        self.assertTrue(blocked['prompt'])
        self.assertGreaterEqual(self.server.task_solver.session['metrics']['duplicateBlocked'], 1)
        self.assertTrue(any('拦截' in item or '失败' in item for item in self.server.task_solver.session.get('facts') or []))

    def test_failed_read_switches_to_verified_api_experience(self):
        self.server.task_solver.experience['api'] = [{
            'baseUrl': 'http://127.0.0.1:8080', 'path': '/api/v1/heritage/search',
            'method': 'GET', 'authStyle': 'Authorization: Bearer', 'cityParam': 'location',
            'recordsPath': 'data.records', 'serviceHint': 'heritage', 'callVerified': True,
        }]
        self.payload['phaseTask'] = (
            '调用API查询南京遗产。阅读 `/does-not-exist/beijing.md`。密钥：tok-n。'
            '提交{"city":"南京","total_count":0,"type_count":0,"oldest_era":"名称"}'
        )
        first = self.post()
        self.assertIn('/api/v1/heritage/search', first['executeCmd'])
        self.assertNotIn('beijing.md', first['executeCmd'])

    def test_harvest_requires_business_code_200(self):
        self.assertIsNone(harvest_api_call(
            'curl "http://svc/api/v1/heritage/search?location=北京"',
            json.dumps({'data': {'records': [{'name': '天坛'}]}}, ensure_ascii=False),
            '查询北京遗产'))
        item = harvest_api_call(
            'curl "http://svc/api/v1/heritage/search?location=北京"',
            json.dumps({'code': 200, 'data': {'records': [{'name': '天坛'}],
                                             'pagination': {'total': 1}}}, ensure_ascii=False),
            '查询北京遗产')
        self.assertTrue(item['callVerified'])
        self.assertFalse(item['recordsComplete'])
        self.assertEqual(item['recordsPath'], 'data.records')

    def test_bool_is_not_accepted_as_int_in_api_answer(self):
        self.assertFalse(is_plain_int(True))
        self.assertIsNone(build_api_answer(
            '提交{"city":"南京","total_count":0,"type_count":0,"oldest_era":"名称"}',
            dict(recordsComplete=True, totalCount=True, typeCount=1,
                 oldestEraName='明孝陵', city='南京')))

    def test_unrelated_role_error_does_not_recompute_answer(self):
        self.post()
        self.next_round(llmResp='{"action":"submit","taskAnswer":"2"}')
        self.next_round(lastRoundRoleActionResults={'10011': True})
        again = self.next_round(lastRoundRoleActionResults={'10011': True},
                                errors=[{'errorCode': 4, 'description': '工人指令错误'}])
        self.assertEqual(self.server.task_solver.session.get('submitStatus'), 'accepted')
        self.assertEqual(again['prompt'], '')
        self.assertNotIn('10011', again['roleCommandMap'])

    def test_answer_error_rejects_after_legal_submit(self):
        self.post()
        self.next_round(llmResp='{"action":"submit","taskAnswer":"2"}')
        retry = self.next_round(lastRoundRoleActionResults={'10011': True},
                                errors=[{'errorCode': 2, 'description': '答案错误'}])
        self.assertEqual(self.server.task_solver.session.get('submitStatus'), 'rejected')
        self.assertIn('答案错误', retry['prompt'])

    def test_unrelated_diagnostic_does_not_fail_waiting_tool(self):
        self.post()
        tool = self.next_round(llmResp='{"action":"execute","command":"echo test"}')
        diagnostic = json.dumps({'marker': 'PIONEER_TASK', 'event': 'task_active', 'requestId': 'diag'})
        waiting = self.next_round(lastCmdResult='[exitCode:0]\n' + diagnostic)
        self.assertEqual(waiting['prompt'], '')
        self.assertEqual(waiting.get('executeCmd') or '', '')
        self.assertEqual(self.server.task_solver.session['stage'], 'wait_tool')

    def test_probe_converts_crlf_and_does_not_mark_task_success(self):
        workspace = self.root / 'alpha'
        workspace.mkdir()
        (workspace / 'start.sh').write_bytes(b'#!/bin/sh\r\necho keep\r\n')
        (workspace / 'spec.md').write_text('修复后运行./check\n')
        result = subprocess.run(
            [__import__('sys').executable, '-c', PROBE_SCRIPT,
             json.dumps(dict(requestId='probe', workspace=str(workspace)))],
            capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        payload = json.loads(result.stdout.decode('utf-8'))
        self.assertIn('start.sh', payload.get('convertedCrlf') or [])
        self.assertTrue(payload.get('precheckOnly'))
        self.assertNotIn(b'\r\n', (workspace / 'start.sh').read_bytes())
        self.assertIn(b'echo keep', (workspace / 'start.sh').read_bytes())

    def test_auth_failed_fetch_does_not_keep_paginating(self):
        self.server.task_solver.experience['api'] = [{
            'baseUrl': 'http://127.0.0.1:8080', 'path': '/api/v1/heritage/search',
            'method': 'GET', 'authStyle': 'Authorization: Bearer', 'cityParam': 'location',
            'recordsPath': 'data.records', 'serviceHint': 'heritage', 'callVerified': True,
        }]
        self.payload['phaseTask'] = (
            '调用API查询南京遗产。密钥：bad。'
            '提交{"city":"南京","total_count":0,"type_count":0,"oldest_era":"名称"}'
        )
        first = self.post()
        rid = self.server.task_solver.session['requestId']
        result = dict(marker='PIONEER_TASK', requestId=rid, event='api_fetch',
                      error='auth_failed', httpStatus=401, businessCode=401,
                      callVerified=False, recordsComplete=False, httpRequestCount=1,
                      path='/api/v1/heritage/search')
        prompt = self.next_round(lastCmdResult='[exitCode:0]\n' + json.dumps(result))['prompt']
        self.assertIn('auth_failed', prompt)
        self.assertEqual(self.server.task_solver.session['stage'], 'wait_llm')
        self.assertFalse(self.server.task_solver.session.get('metrics', {}).get('dataComplete'))

    def test_code_200_fetch_submits_without_status_success_field(self):
        self.server.task_solver.experience['api'] = [{
            'baseUrl': 'http://127.0.0.1:8080', 'path': '/api/v1/heritage/search',
            'method': 'GET', 'authStyle': 'Authorization: Bearer', 'cityParam': 'location',
            'recordsPath': 'data.records', 'serviceHint': 'heritage', 'callVerified': True,
        }]
        self.payload['phaseTask'] = (
            '调用API查询南京遗产。密钥：tok-1。'
            '提交{"city":"南京","total_count":0,"world_heritage_count":0,"types":[],"oldest_era":"名称"}'
        )
        self.post()
        rid = self.server.task_solver.session['requestId']
        result = dict(
            marker='PIONEER_TASK', requestId=rid, event='api_fetch', ok=True,
            callVerified=True, recordsComplete=True,
            completenessEvidence='pagination.total=2 records=2',
            totalCount=2, typeCount=1, types=['陵墓'], worldHeritageCount=1,
            oldestEraName='明孝陵', oldestEraEvidence='year=1381', city='南京',
            businessCode=200, httpStatus=200, path='/api/v1/heritage/search',
        )
        response = self.next_round(lastCmdResult='[exitCode:0]\n' + json.dumps(result, ensure_ascii=False))
        answer = json.loads(response['roleCommandMap']['10011']['taskAnswer'])
        self.assertEqual(answer['city'], '南京')
        self.assertEqual(answer['total_count'], 2)
        self.assertEqual(answer['world_heritage_count'], 1)
        self.assertEqual(answer['types'], ['陵墓'])
        self.assertEqual(answer['oldest_era'], '明孝陵')
        self.assertIsInstance(answer['world_heritage_count'], int)

    def test_same_round_retry_consumes_empty_feedback_once(self):
        doc = self.root / 'guide.md'
        doc.write_text('运行 python3 -c "print(1)"')
        self.payload['phaseTask'] = f'阅读 `{doc}`，按说明获取答案'
        first = self.post()
        self.assertTrue(first['executeCmd'])
        waiting = self.next_round(lastCmdResult='')
        self.assertEqual(waiting.get('prompt'), '')
        waits = self.server.task_solver.session.get('emptyWaits')
        self.assertEqual(waits, 1)
        retry = self.post()
        self.assertEqual(self.server.task_solver.session.get('emptyWaits'), 1)
        self.assertEqual(retry.get('executeCmd') or '', waiting.get('executeCmd') or '')

    def test_restart_after_submit_ack_does_not_resubmit(self):
        doc = self.root / 'guide.md'
        doc.write_text('答案是42')
        self.payload['phaseTask'] = f'阅读 `{doc}`，提交答案'
        first = self.post()
        read_back = self.sandbox(first['executeCmd'])
        asked = self.next_round(lastCmdResult=read_back)
        self.assertTrue(asked['prompt'])
        submitted = self.next_round(llmResp=json.dumps({'action': 'submit', 'taskAnswer': '42'}))
        self.assertEqual(submitted['roleCommandMap']['10011'],
                         {'action': 'submitAnswer', 'taskAnswer': '42'})
        acked = self.next_round(lastRoundRoleActionResults={'10011': True})
        self.assertNotIn('10011', acked['roleCommandMap'])
        self.assertEqual(self.server.task_solver.session.get('submitStatus'), 'accepted')
        self.server = GameServer(self.root)
        self.client = self.server.app.test_client()
        restored = self.post()
        self.assertNotIn('10011', restored['roleCommandMap'])
        self.assertEqual(self.server.task_solver.session.get('submitStatus'), 'accepted')

    def test_other_city_md_is_not_read_for_current_city(self):
        paths = relevant_md_paths('查询南京遗产。阅读 `task_1_beijing.md` 和 `API_DOCS.md`。')
        self.assertIn('API_DOCS.md', paths)
        self.assertFalse(any('beijing' in path.lower() for path in paths))

    def test_chengdu_first_query_reuses_verified_beijing_api(self):
        self.server.task_solver.experience['api'] = [{
            'baseUrl': 'http://127.0.0.1:8080', 'path': '/api/v1/heritage/search',
            'method': 'GET', 'authStyle': 'Authorization: Bearer', 'cityParam': 'location',
            'recordsPath': 'data.records', 'serviceHint': 'heritage', 'callVerified': True,
            'recordsComplete': False,
        }]
        self.payload['phaseTask'] = (
            '查询成都文化遗产。密钥：tok-cd。'
            '提交{"city":"成都","total_count":0,"world_heritage_count":0,"types":[],"oldest_era":"名称"}'
        )
        first = self.post()
        self.assertIn('/api/v1/heritage/search', first['executeCmd'])
        self.assertTrue(first['executeCmd'].lstrip().startswith('curl '))
        self.assertIn('成都', first['executeCmd'])
        self.assertNotIn('python3', first['executeCmd'])
        self.assertNotIn('cultural-heritage', first['executeCmd'])
        self.assertTrue(self.server.task_solver.session.get('experienceHit'))

    def test_duplicate_failed_read_blocks_same_basename(self):
        self.payload['phaseTask'] = '阅读 `/tmp/old/task_1_beijing.md` 并完成任务'
        first = self.post()
        rid = self.server.task_solver.session['requestId']
        feedback = '[exitCode:0]\n' + json.dumps(dict(
            marker='PIONEER_TASK', requestId=rid, event='read_document', error='not_found',
            path='/tmp/old/task_1_beijing.md'))
        self.next_round(lastCmdResult=feedback)
        blocked = self.next_round(llmResp=json.dumps(dict(action='read', path='task_1_beijing.md')))
        self.assertFalse(blocked.get('executeCmd'))
        self.assertGreaterEqual(self.server.task_solver.session['metrics']['duplicateBlocked'], 1)

    def test_incomplete_api_fetch_does_not_submit(self):
        self.server.task_solver.experience['api'] = [{
            'baseUrl': 'http://127.0.0.1:8080', 'path': '/api/v1/heritage/search',
            'method': 'GET', 'authStyle': 'Authorization: Bearer', 'cityParam': 'location',
            'recordsPath': 'data.records', 'serviceHint': 'heritage', 'callVerified': True,
        }]
        self.payload['phaseTask'] = (
            '调用API查询北京遗产。密钥：tok-1。'
            '提交{"city":"北京","total_count":0,"world_heritage_count":0,"types":[],"oldest_era":"名称"}'
        )
        self.post()
        rid = self.server.task_solver.session['requestId']
        result = dict(
            marker='PIONEER_TASK', requestId=rid, event='api_fetch', ok=False,
            callVerified=True, recordsComplete=False, error='total_mismatch',
            completenessEvidence='pagination.total=15 records=10',
            recordsCollected=10, expectedTotal=15, totalCount=10,
            typeCount=1, types=['坛庙'], worldHeritageCount=1,
            oldestEraName='天坛', oldestEraEvidence='year=1420', city='北京',
            businessCode=200, httpStatus=200, path='/api/v1/heritage/search',
        )
        response = self.next_round(lastCmdResult='[exitCode:0]\n' + json.dumps(result, ensure_ascii=False))
        self.assertNotIn('10011', response.get('roleCommandMap') or {})
        self.assertFalse(self.server.task_solver.session.get('metrics', {}).get('dataComplete'))
        self.assertIn('offset', response.get('executeCmd') or '')
        guessed = self.next_round(llmResp=json.dumps({
            'action': 'submit',
            'taskAnswer': json.dumps({'city': '北京', 'total_count': 10}, ensure_ascii=False),
        }))
        self.assertNotIn('10011', guessed.get('roleCommandMap') or {})
        self.assertTrue(any('未查全' in item for item in self.server.task_solver.session.get('facts') or []))

    def test_curl_command_urlencodes_city_without_python(self):
        cmd = curl_api_command(dict(
            baseUrl='http://127.0.0.1:8080', path='/api/v1/heritage/search',
            method='GET', authStyle='Authorization: Bearer', token='tok-1',
            cityParam='location', city='北京', extraParams={'size': '100'},
        ))
        self.assertTrue(cmd.startswith('curl '))
        self.assertIn('--data-urlencode', cmd)
        self.assertIn('北京', cmd)
        self.assertIn('Bearer tok-1', cmd)
        self.assertNotIn('python3', cmd)
        self.assertNotIn('size=100', cmd)
        self.assertNotIn('urllib', cmd)

    def test_host_pagination_completes_offset_total_count_pages(self):
        records = [
            {'id': i, 'name': 'n%s' % i, 'type': '陵墓' if i == 14 else '坛庙',
             'protected_level': '世界遗产' if i == 0 else '市级', 'era': 1000 + i}
            for i in range(15)
        ]
        collected = []
        first = ingest_api_page(collected, {
            'code': 200,
            'data': {
                'records': records[:10],
                'pagination': {'total_count': 15, 'offset': 0, 'limit': 10},
            },
        }, 200)
        self.assertFalse(first.get('recordsComplete'))
        self.assertEqual(first.get('recordsCollected'), 10)
        self.assertEqual(first.get('expectedTotal'), 15)
        self.assertEqual(first.get('nextOffset'), 10)
        self.assertEqual(first.get('nextLimit'), 10)
        self.assertFalse(first.get('ok'))
        second = ingest_api_page(collected, {
            'code': 200,
            'data': {
                'records': records[10:],
                'pagination': {'total_count': 15, 'offset': 10, 'limit': 10},
            },
        }, 200)
        self.assertTrue(second.get('recordsComplete'))
        self.assertEqual(second.get('recordsCollected'), 15)
        self.assertEqual(second.get('expectedTotal'), 15)
        self.assertEqual(second.get('totalCount'), 15)
        self.assertEqual(second.get('worldHeritageCount'), 1)
        self.assertTrue(second.get('ok'))
        status, payload, _ = parse_curl_output(
            '[exitCode:0]\n{"code":200,"data":{"records":[]}}\nHTTPSTATUS:200')
        self.assertEqual(status, 200)
        self.assertEqual(payload['code'], 200)

    def test_probe_token_submits_without_llm(self):
        self.payload['phaseTask'] = '工作区路径：`/tmp/alpha/`，修复部署环境'
        first = self.post()
        self.assertIn('deploy_probe', first['executeCmd'])
        rid = self.server.task_solver.session['requestId']
        probe = dict(
            marker='PIONEER_TASK', requestId=rid, event='deploy_probe', precheckOnly=True,
            workspace='/tmp/alpha', convertedCrlf=['start.sh'],
            checkExitCode=0, checkTail='6/6 passed\nTOKEN: alpha-ok\n',
            files=[{'path': 'start.sh', 'exists': True, 'crlf': False, 'convertedCrlf': True}],
        )
        response = self.next_round(lastCmdResult='[exitCode:0]\n' + json.dumps(probe, ensure_ascii=False))
        self.assertEqual(response.get('prompt') or '', '')
        self.assertEqual(response['roleCommandMap']['10011']['action'], 'submitAnswer')
        self.assertIn('alpha-ok', response['roleCommandMap']['10011']['taskAnswer'])


class IngestFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.solver = PioneerTaskSolver(Path(self.temp.name))
        self.state = opening_state()
        self.state.round_no = 20
        self.state.phase_task = '工作区路径：`/tmp/ws`，修复部署环境'
        self.pioneer = next(r for r in self.state.team_our.roles if r.role_type == 'pioneer')

    def waiting(self, stage, request_id='rid-1', **extra):
        from src.agent.task_solver import empty_metrics, task_context
        ctx = task_context(self.state.phase_task)
        session = dict(
            key=[self.state.team_our.team_id, self.state.team_our.type, self.state.phase_task],
            stage=stage, paths=[], documents=[], history=[], facts=[], failedActions=[],
            index=0, offset=0, calls=0, retries=0, emptyWaits=0, emptyLlmWaits=0,
            fingerprint=task_fingerprint(self.state.phase_task), requestId=request_id,
            metrics=empty_metrics(self.state.round_no), round=self.state.round_no - 1,
            llmPending=stage == 'wait_llm', submitStatus=None, pioneer=self.pioneer.id,
        )
        session.update(ctx)
        session.update(extra)
        return session

    def test_duplicate_ingest_same_round_consumes_once(self):
        self.solver.session = self.waiting('wait_tool')
        self.state.last_cmd_result = ''
        self.solver.ingest_feedback(self.state)
        self.assertEqual(self.solver.session['emptyWaits'], 1)
        self.solver.ingest_feedback(self.state)
        self.assertEqual(self.solver.session['emptyWaits'], 1)

    def test_step_does_not_reprocess_ingested_feedback(self):
        self.solver.session = self.waiting('wait_llm', llmPending=True)
        self.state.llm_resp = json.dumps({'action': 'submit', 'taskAnswer': 'TOKEN: ready'})
        self.solver.ingest_feedback(self.state)
        self.assertEqual(self.solver.session['stage'], 'submit')
        self.assertEqual(self.solver.session.get('answer'), 'TOKEN: ready')
        history_len = len(self.solver.session.get('history') or [])
        commands = {}
        prompt, execute = self.solver.step(self.state, commands)
        self.assertEqual(len(self.solver.session.get('history') or []), history_len)
        self.assertEqual(self.solver.session.get('answer'), 'TOKEN: ready')
        self.assertEqual(commands.get(self.pioneer.id, {}).get('action'), 'submitAnswer')
        self.assertEqual(prompt, '')
        self.assertEqual(execute, '')

    def test_token_this_round_is_visible_to_scheduler(self):
        rid = 'probe-1'
        self.solver.session = self.waiting('wait_probe', request_id=rid, taskKind='workspace')
        payload = dict(
            marker=MARKER, requestId=rid, event='deploy_probe',
            exitCode=0, checkExitCode=0, output='TOKEN: abcdef', checkTail='TOKEN: abcdef',
        )
        self.state.last_cmd_result = json.dumps(payload, ensure_ascii=False)
        self.solver.ingest_feedback(self.state)
        self.assertEqual(self.state.task_session.get('stage'), 'submit')
        self.assertIn('abcdef', self.state.task_session.get('answer') or '')
        self.assertTrue(solver_ready_to_submit(self.state))

    def test_task_switch_does_not_apply_old_sandbox_result(self):
        old_task = '旧任务正文请计算1+1'
        self.state.phase_task = old_task
        self.solver.session = self.waiting(
            'wait_tool', request_id='old-rid', history=[],
            key=[self.state.team_our.team_id, self.state.team_our.type, old_task],
            fingerprint=task_fingerprint(old_task),
            taskKind='unknown',
        )
        self.state.last_cmd_result = json.dumps(dict(
            marker=MARKER, requestId='old-rid', event='execute_tool',
            exitCode=0, output='OLD_SANDBOX_SECRET',
        ), ensure_ascii=False)
        self.state.phase_task = '新任务正文请阅读说明.md'
        self.solver.ingest_feedback(self.state)
        dumped = json.dumps(self.solver.session, ensure_ascii=False)
        self.assertNotIn('OLD_SANDBOX_SECRET', dumped)
        self.assertNotEqual(self.solver.session.get('requestId'), 'old-rid')
        self.assertEqual(self.solver.session.get('fingerprint'),
                         task_fingerprint('新任务正文请阅读说明.md'))
        self.assertFalse(solver_ready_to_submit(self.state))

    def test_ingest_does_not_emit_tool_llm_or_role_action(self):
        self.solver.session = self.waiting('ask', calls=0)
        commands = {self.pioneer.id: {'action': 'move', 'targetPos': [{'x': 9, 'y': 9}]}}
        snapshot = json.dumps(commands, sort_keys=True)
        self.solver.ingest_feedback(self.state)
        self.assertEqual(json.dumps(commands, sort_keys=True), snapshot)
        self.assertNotIn('response', self.solver.session)
        self.assertEqual(self.solver.session.get('stage'), 'ask')
        self.assertFalse(self.solver.session.get('llmPending'))
        self.assertEqual(self.solver.session.get('calls'), 0)

    def test_restart_same_ack_does_not_submit_twice(self):
        self.solver.session = self.waiting(
            'wait_submit', answer='42', submitStatus='sent',
            pioneer=self.pioneer.id)
        self.state.last_round_role_action_results = {self.pioneer.id: True}
        self.solver.ingest_feedback(self.state)
        self.assertEqual(self.solver.session.get('submitStatus'), 'accepted')
        commands = {}
        self.solver.step(self.state, commands)
        self.assertNotIn(self.pioneer.id, commands)
        restored = PioneerTaskSolver(Path(self.temp.name))
        again = {}
        restored.ingest_feedback(self.state)
        restored.step(self.state, again)
        self.assertNotIn(self.pioneer.id, again)
        self.assertEqual(restored.session.get('submitStatus'), 'accepted')


if __name__ == '__main__':
    unittest.main()
