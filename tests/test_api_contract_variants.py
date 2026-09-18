"""回归测试：求解器不再对任何业务题型预置契约。"""
import unittest

from src.agent.task_solver import normalize_skill


class ContentAgnosticSkillTests(unittest.TestCase):
    def test_arbitrary_mechanisms_use_the_same_skill_schema(self):
        for name, signal in (
                ('cipher-transform', '输入包含编码规则'),
                ('dataset-reconcile', '提供两份需核对的数据'),
                ('build-repair', '提供可执行验收条件')):
            item = normalize_skill({
                'name': name,
                'applicability': {
                    'summary': '机制和验收契约一致的后续实例',
                    'requiredSignals': [signal],
                    'incompatibleSignals': ['验收契约变更'],
                },
                'invariants': ['只使用已验证机制'],
                'parameters': [{'name': 'input', 'source': '当前题面',
                                'validation': '必须存在'}],
                'procedure': ['绑定{{input}}', '执行稳定机制'],
                'verification': ['核对当前验收条件'],
                'failureRecovery': ['根据真实错误局部修订'],
                'answerContract': '以当前任务为准',
            })
            self.assertTrue(item['skillId'].startswith('skill-'))


if __name__ == '__main__':
    unittest.main()
