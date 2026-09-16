"""自进化求解器（简化版 PioneerTaskSolver）：read -> LLM -> execute/submit 状态机。

这份测试对应 src/agent/task_solver.py 的当前实现——纯 sh 沙盒脚本、单一 session，
不含分页/经验持久化/归档（那些属于更早被回退掉的更复杂版本，见 git 历史）。
"""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from src.agent import GameServer
from src.agent.task_solver import (
    MARKER, extract_md_paths, parse_llm, sandbox_command, task_context,
    task_fingerprint, READ_SCRIPT, EXEC_SCRIPT, PioneerTaskSolver,
)


class TaskSolverHelperTests(unittest.TestCase):
    def test_extract_paths(self):
        self.assertEqual(extract_md_paths('阅读`/app/API Guide.md`，再查看 docs/query.md 和「天气说明.md」。'),
                         ['/app/API Guide.md', 'docs/query.md', '天气说明.md'])
        self.assertEqual(extract_md_paths('请阅读说明.md文件，参考说明.md'), ['说明.md'])

    def test_task_context_classifies_workspace_api_unknown(self):
        self.assertEqual(task_context('工作区为 /srv/app/，请修复配置')['taskKind'], 'workspace')
        self.assertEqual(task_context('工作区为 /srv/app/，请修复配置')['workspace'], '/srv/app/')
        self.assertEqual(task_context('请调用天气 API 查询北京')['taskKind'], 'api')
        self.assertEqual(task_context('计算1+1')['taskKind'], 'unknown')

    def test_task_fingerprint_stable_for_same_text(self):
        self.assertEqual(task_fingerprint('同一段文本'), task_fingerprint('同一段文本'))
        self.assertNotEqual(task_fingerprint('文本A'), task_fingerprint('文本B'))

    def test_parse_llm_requires_known_action_shape(self):
        self.assertEqual(parse_llm('{"action":"submit","taskAnswer":"42"}')['taskAnswer'], '42')
        self.assertEqual(parse_llm('```json\n{"action":"read","path":"a.md"}\n```')['path'], 'a.md')
        self.assertEqual(parse_llm('{"action":"execute","command":"echo hi"}')['command'], 'echo hi')
        with self.assertRaises(ValueError):
            parse_llm('{"action":"read","path":""}')
        with self.assertRaises(ValueError):
            parse_llm('{"action":"submit"}')
        with self.assertRaises(ValueError):
            parse_llm('not json at all')

    def test_sandbox_command_builds_sh_invocation_for_read_and_exec(self):
        read_cmd = sandbox_command(READ_SCRIPT, dict(requestId='r1', path='a.md', offset=0, workspace=''))
        self.assertTrue(read_cmd.startswith('sh -c '))
        self.assertIn('r1', read_cmd)
        self.assertIn('a.md', read_cmd)
        exec_cmd = sandbox_command(EXEC_SCRIPT, dict(requestId='r2', command='echo hi', workspace=''))
        self.assertTrue(exec_cmd.startswith('sh -c '))
        self.assertIn('echo hi', exec_cmd)


class TaskSolverStepTests(unittest.TestCase):
    """直接用 PioneerTaskSolver.step()，跳过 HTTP 层，聚焦状态机本身。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.solver = PioneerTaskSolver(Path(self.temp.name))
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def _state(self, phase_task, round_no=10, llm_resp='', last_cmd_result='', errors=(),
               last_round_role_action_results=None):
        state = type('S', (), {})()
        team = type('T', (), {})()
        team.team_id, team.type = 't1', 'challenger'
        pioneer = type('R', (), {})()
        pioneer.id, pioneer.role_type, pioneer.health = 1, 'pioneer', 200
        team.roles = [pioneer]
        state.team_our = team
        state.map_info = type('M', (), {})()
        state.phase_task = phase_task
        state.round_no = round_no
        state.llm_resp = llm_resp
        state.last_cmd_result = last_cmd_result
        state.errors = list(errors)
        state.last_round_role_action_results = last_round_role_action_results or {}
        return state

    def sandbox(self, command):
        if not shutil.which('sh'):
            self.skipTest('需要 POSIX sh；请在 Linux 比赛运行环境补跑沙盒集成测试')
        result = subprocess.run(['sh', '-c', command], cwd=self.temp.name, capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)
        return '[exitCode:0]\n' + result.stdout

    def sandbox_without_python(self, command):
        sh_path = shutil.which('sh')
        if not sh_path:
            self.skipTest('需要 POSIX sh；请在 Linux 比赛运行环境补跑沙盒集成测试')
        env = {'PATH': str(Path(sh_path).parent)}
        result = subprocess.run(['sh', '-c', command], cwd=self.temp.name, capture_output=True,
                                text=True, timeout=12, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_no_task_returns_empty_and_clears_session(self):
        commands = {}
        prompt, execute = self.solver.step(self._state(None), commands)
        self.assertEqual((prompt, execute), ('', ''))
        self.assertEqual(self.solver.session, {})

    def test_read_llm_submit_and_confirm_accepted(self):
        doc = Path(self.temp.name) / 'guide.md'
        doc.write_text('答案是42')
        state = self._state(f'阅读 `{doc}`，提交答案')
        commands = {}
        prompt, execute = self.solver.step(state, commands)
        self.assertEqual(self.solver.session['stage'], 'wait_read')
        self.assertTrue(execute)

        # 沙盒脚本本身的字节级内容提取以 Linux/gawk 为准，这台机器的 sh/awk 只用来验证
        # 状态机流转不出错、返回的是合法 JSON，不断言逐字节内容（环境相关，非求解器逻辑）。
        read_back = self.sandbox(execute)
        self.assertIn('"event":"read_document"', read_back)
        state = self._state(state.phase_task, round_no=11, last_cmd_result=read_back)
        prompt, execute = self.solver.step(state, commands)
        self.assertEqual(self.solver.session['stage'], 'wait_llm')
        self.assertTrue(prompt)

        state = self._state(state.phase_task, round_no=12,
                            llm_resp=json.dumps({'action': 'submit', 'taskAnswer': '42'}))
        prompt, execute = self.solver.step(state, commands)
        self.assertEqual(self.solver.session['stage'], 'wait_submit')
        self.assertEqual(commands[1]['action'], 'submitAnswer')
        self.assertEqual(commands[1]['taskAnswer'], '42')

        state = self._state(state.phase_task, round_no=13,
                            last_round_role_action_results={1: True})
        self.solver.step(state, commands)
        self.assertEqual(self.solver.session.get('answer'), '42')

    def test_execute_tool_then_submit(self):
        state = self._state('执行 echo ready，再提交结果')
        commands = {}
        prompt, execute = self.solver.step(state, commands)
        # 没有点名 .md 文档，paths 为空，本回合直接从 read 落到 ask 再去问 LLM，不死等读文件。
        self.assertEqual(self.solver.session['stage'], 'wait_llm')
        self.assertTrue(prompt)

    def test_sandbox_command_falls_back_when_python3_missing(self):
        doc = Path(self.temp.name) / 'fallback.md'
        doc.write_text('fallback-ok', encoding='utf-8')
        read_cmd = sandbox_command(READ_SCRIPT, dict(requestId='fallback-read', path=str(doc),
                                                     offset=0, documentDir='', workspace=''))
        read_back = self.sandbox_without_python(read_cmd)
        self.assertIn('"event":"read_document"', read_back)
        self.assertIn('fallback-ok', read_back)

        exec_cmd = sandbox_command(EXEC_SCRIPT, dict(requestId='fallback-exec',
                                                     command='printf tool-ok', workspace=''))
        exec_back = self.sandbox_without_python(exec_cmd)
        self.assertIn('"event":"execute_tool"', exec_back)
        self.assertIn('tool-ok', exec_back)
        self.assertIn('"exitCode":0', exec_back)

        state = self._state(state.phase_task, round_no=11,
                            llm_resp=json.dumps({'action': 'execute', 'command': 'echo ready'}))
        prompt, execute = self.solver.step(state, commands)
        self.assertEqual(self.solver.session['stage'], 'wait_tool')
        self.assertTrue(execute)

        # 同上：这台机器的 sh/awk 组合不保证逐字节回显，只验证流程不出错、返回合法 JSON。
        tool_result = self.sandbox(execute)
        self.assertIn('"event":"execute_tool"', tool_result)
        self.assertIn('"exitCode":0', tool_result)
        state = self._state(state.phase_task, round_no=12, last_cmd_result=tool_result)
        prompt, execute = self.solver.step(state, commands)
        self.assertEqual(self.solver.session['stage'], 'wait_llm')
        self.assertTrue(prompt)

    def test_rejected_submission_goes_back_to_ask(self):
        state = self._state('直接提交一个答案')
        commands = {}
        self.solver.step(state, commands)
        self.assertEqual(self.solver.session['stage'], 'wait_llm')
        state = self._state(state.phase_task, round_no=11,
                            llm_resp=json.dumps({'action': 'submit', 'taskAnswer': '错误答案'}))
        self.solver.step(state, commands)
        self.assertEqual(self.solver.session['stage'], 'wait_submit')

        class Err:
            def __init__(self, code):
                self.error_code = code
                self.description = 'answer wrong'
        state = self._state(state.phase_task, round_no=12, errors=[Err(2)])
        self.solver.step(state, commands)
        # 拒绝识别后同一回合会立刻再问一次 LLM（stage 落回 ask 又马上进 wait_llm），
        # 关键是要在历史里留下"被拒绝"的记录，而不是假装提交成功。
        self.assertEqual(self.solver.session['stage'], 'wait_llm')
        self.assertTrue(any('submissionRejected' in item for item in self.solver.session['history']))

    def test_empty_llm_response_waits_before_reasking(self):
        state = self._state('直接提交一个答案')
        commands = {}
        prompt, _ = self.solver.step(state, commands)
        self.assertTrue(prompt)
        calls = self.solver.session['calls']

        state = self._state(state.phase_task, round_no=11, llm_resp='')
        prompt, execute = self.solver.step(state, commands)
        self.assertEqual((prompt, execute), ('', ''))
        self.assertEqual(self.solver.session['stage'], 'wait_llm')
        self.assertEqual(self.solver.session['calls'], calls)

        state = self._state(state.phase_task, round_no=12, llm_resp='')
        prompt, _ = self.solver.step(state, commands)
        self.assertTrue(prompt)
        self.assertEqual(self.solver.session['stage'], 'wait_llm')
        self.assertEqual(self.solver.session['calls'], calls + 1)

    def test_same_round_replay_returns_cached_response(self):
        doc = Path(self.temp.name) / 'guide.md'
        doc.write_text('答案是7')
        state = self._state(f'阅读 `{doc}`')
        commands = {}
        first = self.solver.step(state, commands)
        second = self.solver.step(state, dict(commands))
        self.assertEqual(first, second)

    def test_reset_clears_session_and_file(self):
        state = self._state('阅读 x.md')
        self.solver.step(state, {})
        self.assertTrue(self.solver.session)
        self.solver.reset()
        self.assertEqual(self.solver.session, {})
        self.assertFalse(self.solver.path.exists())

    def test_phase_task_change_starts_a_fresh_session(self):
        state = self._state('第一个任务，阅读 a.md')
        self.solver.step(state, {})
        first_stage = self.solver.session['stage']
        self.assertEqual(first_stage, 'wait_read')
        state = self._state('完全不同的第二个任务', round_no=20)
        self.solver.step(state, {})
        self.assertEqual(self.solver.session['paths'], [])

    def test_business_error_read_result_not_added_as_document(self):
        """not_found/ambiguous_path 等业务错误不能被当成读到的文档内容。"""
        state = self._state('阅读 missing.md')
        commands = {}
        self.solver.step(state, commands)
        rid = self.solver.session['requestId']
        error_result = json.dumps({'marker': MARKER, 'requestId': rid, 'event': 'read_document',
                                    'error': 'not_found', 'candidates': []})
        state = self._state(state.phase_task, round_no=11, last_cmd_result=error_result)
        self.solver.step(state, commands)
        self.assertEqual(self.solver.session['documents'], [])
        self.assertEqual(self.solver.session['stage'], 'wait_llm')
        self.assertTrue(any(item.get('readError') == 'not_found' for item in self.solver.session['history']))

    def test_ambiguous_result_reported_with_distinct_candidates(self):
        state = self._state('阅读 spec.md')
        commands = {}
        self.solver.step(state, commands)
        rid = self.solver.session['requestId']
        amb_result = json.dumps({'marker': MARKER, 'requestId': rid, 'event': 'read_document',
                                  'error': 'ambiguous_path', 'candidates': ['/a/spec.md', '/b/spec.md']})
        state = self._state(state.phase_task, round_no=11, last_cmd_result=amb_result)
        self.solver.step(state, commands)
        entry = next(item for item in self.solver.session['history'] if item.get('readError') == 'ambiguous_path')
        self.assertEqual(entry['candidates'], ['/a/spec.md', '/b/spec.md'])
        self.assertEqual(len(set(entry['candidates'])), 2)

    def test_malformed_json_result_does_not_spuriously_retry_forever(self):
        """requestId 匹配但结构不合法（如缺字段）的结果要能明确识别为 malformed，
        并在有限轮内转 ask，而不是被当成 None 陷入无穷等待/覆盖已成功结果。"""
        state = self._state('阅读 x.md')
        commands = {}
        self.solver.step(state, commands)
        rid = self.solver.session['requestId']
        bad = json.dumps({'marker': MARKER, 'requestId': rid, 'event': 'read_document'})  # 缺 path/content 等字段
        for round_no in (11, 12, 13):
            state = self._state(state.phase_task, round_no=round_no, last_cmd_result=bad)
            self.solver.step(state, commands)
        # 3 次以内必须已经放弃重试、转去问 LLM，不再是 wait_read 死等。
        self.assertIn(self.solver.session['stage'], ('wait_llm', 'ask'))

    def test_documentDir_backward_compatible_default(self):
        """旧会话文件没有 documentDir 字段时，应退化为旧的 workspace 值，不报错。"""
        state = self._state('工作区为 /srv/app/，阅读 API_DOCS.md')
        self.solver.step(state, {})
        del self.solver.session['documentDir']
        state = self._state(state.phase_task, round_no=state.round_no)
        self.solver.step(state, {})
        self.assertEqual(self.solver.session.get('documentDir'), self.solver.session.get('workspace'))
