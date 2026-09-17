"""入夜前持券兜底：按升级顺序用不上的武器券也不能拿着过夜。"""
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.protocol import Pos
from test_opening import opening_state
from test_shop_items import make_role


def voucher_state(round_no, holder_pos, backpack):
    state = opening_state()
    state.round_no = round_no
    state.team_our.gold_num = 0
    state.team_our.roles = [r for r in state.team_our.roles
                            if r.role_type not in ('rocket', 'gatling', 'railgun')]
    # 升级顺序：火箭A 1→2、火箭B 1→2、火箭A 2→3 ……；这里火箭B已经先到了2级（乱序）
    state.team_our.roles += [
        make_role(20, 9, 9, 'rocket', level=1, attack_range=20, cooldown=0, health=1000),
        make_role(21, 9, 11, 'rocket', level=2, attack_range=20, cooldown=0, health=1500),
        make_role(22, 11, 12, 'railgun', level=1, attack_range=20, cooldown=0, health=1000),
    ]
    holder = next(r for r in state.team_our.roles if r.id == 1)
    holder.pos = holder_pos
    holder.backpack = list(backpack)
    return state, holder


class VoucherDeadlineTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_out_of_order_voucher2_used_on_level2_weapon_before_night(self):
        # 券2按顺序属于"火箭A 2→3"，但火箭A还是1级；入夜前改用到已是2级的火箭B上。
        for round_no in (65, 195):  # 第一天开局状态机 / 之后的白天
            with self.subTest(round_no=round_no):
                state, holder = voucher_state(round_no, Pos(10, 11), ['WeaponUpgradeVoucher2'])
                commands = self.decide(state)
                self.assertEqual(commands[holder.id], {
                    'action': 'use', 'name': 'WeaponUpgradeVoucher2', 'targetPos': [{'x': 9, 'y': 11}]})
                self.assertTrue(any(e['code'] == 'voucher_deadline_use' for e in state.decision_events))

    def test_holder_walks_to_weapon_when_night_is_close(self):
        state, holder = voucher_state(195, Pos(16, 11), ['WeaponUpgradeVoucher2'])
        commands = self.decide(state)
        self.assertEqual(commands[holder.id]['action'], 'move')
        self.assertLess(abs(commands[holder.id]['targetPos'][0]['x'] - 9), abs(holder.pos.x - 9))
        self.assertEqual(state.worker_item_jobs[holder.id]['target'], (9, 11))

    def test_no_takeover_while_there_is_still_time(self):
        state, holder = voucher_state(140, Pos(16, 11), ['WeaponUpgradeVoucher2'])
        self.decide(state)
        self.assertFalse(any(e['code'] == 'voucher_deadline_use' for e in state.decision_events))

    def test_two_holders_do_not_upgrade_same_weapon_twice(self):
        state, holder = voucher_state(195, Pos(10, 11), ['WeaponUpgradeVoucher1'])
        other = next(r for r in state.team_our.roles if r.id == 2)
        other.pos = Pos(10, 10)
        other.backpack = ['WeaponUpgradeVoucher1']
        commands = self.decide(state)
        targets = [tuple(c['targetPos'][0].values()) for rid, c in commands.items()
                   if rid in (1, 2) and c.get('action') == 'use']
        self.assertEqual(len(targets), len(set(targets)))
        uses = [e for e in state.decision_events if e['code'] == 'voucher_deadline_use']
        self.assertEqual(len({e['weapon_id'] for e in uses}), len(uses))

    def test_no_matching_level_weapon_keeps_command(self):
        state, holder = voucher_state(195, Pos(10, 11), ['WeaponUpgradeVoucher2'])
        for r in state.team_our.roles:
            if r.id == 21:
                r.level = 3
        self.decide(state)
        self.assertFalse(any(e['code'] == 'voucher_deadline_use' for e in state.decision_events))


if __name__ == '__main__':
    unittest.main()
