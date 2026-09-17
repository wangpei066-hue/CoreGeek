"""持券：开拓者到手就用；工人身边有对应武器当场用，否则干完活回防时顺路用。"""
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy, upgrade_plan
from src.agent.protocol import Pos
from test_opening import opening_state
from test_shop_items import make_role


def voucher_state(round_no, holder_pos, backpack, levels=(1, 2)):
    state = opening_state()
    state.round_no = round_no
    state.team_our.gold_num = 0
    state.team_our.roles = [r for r in state.team_our.roles
                            if r.role_type not in ('rocket', 'gatling', 'railgun')]
    state.team_our.roles += [
        make_role(20, 9, 9, 'rocket', level=levels[0], attack_range=20, cooldown=0, health=1000),
        make_role(21, 9, 11, 'rocket', level=levels[1], attack_range=20, cooldown=0, health=1000),
        make_role(22, 11, 12, 'railgun', level=1, attack_range=20, cooldown=0, health=1000),
    ]
    holder = next(r for r in state.team_our.roles if r.id == 1)
    holder.pos = holder_pos
    holder.backpack = list(backpack)
    return state, holder


class HeldVoucherTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_voucher2_goes_to_the_level2_rocket_when_ordered_rocket_is_still_level1(self):
        # 券2按顺序属于"火箭A 2→3"，但火箭A还是1级：直接用在已是2级的火箭B上，不等。
        for round_no in (20, 65, 140, 195):
            with self.subTest(round_no=round_no):
                state, holder = voucher_state(round_no, Pos(10, 11), ['WeaponUpgradeVoucher2'])
                commands = self.decide(state)
                self.assertEqual(commands[holder.id], {
                    'action': 'use', 'name': 'WeaponUpgradeVoucher2', 'targetPos': [{'x': 9, 'y': 11}]})

    def test_voucher1_follows_upgrade_order(self):
        state, holder = voucher_state(140, Pos(10, 10), ['WeaponUpgradeVoucher1'], levels=(1, 1))
        first_rocket = upgrade_plan(state)[0][0]['role']
        commands = self.decide(state)
        self.assertEqual(commands[holder.id]['action'], 'use')
        self.assertEqual(commands[holder.id]['targetPos'], [{'x': first_rocket.pos.x, 'y': first_rocket.pos.y}])

    def test_far_worker_keeps_working_early_and_uses_voucher_on_the_way_home(self):
        from src.agent.protocol import Zone
        state, holder = voucher_state(140, Pos(16, 11), ['WeaponUpgradeVoucher2'])
        state.map_info.zones.append(Zone(Pos(17, 11), 'copper'))
        self.decide(state)
        codes = [e['code'] for e in state.decision_events if e.get('role_id') == holder.id]
        self.assertIn('weapon_voucher_deferred', codes)  # 背包有空，先干活
        self.assertNotIn('held_voucher_use', codes)
        state, holder = voucher_state(195, Pos(16, 11), ['WeaponUpgradeVoucher2'])  # 该回防了
        commands = self.decide(state)
        self.assertEqual(commands[holder.id]['action'], 'move')
        self.assertLess(abs(commands[holder.id]['targetPos'][0]['x'] - 9), abs(holder.pos.x - 9))
        self.assertEqual(state.worker_item_jobs[holder.id]['target'], (9, 11))

    def test_worker_with_full_backpack_goes_to_use_voucher(self):
        state, holder = voucher_state(140, Pos(16, 11), ['WeaponUpgradeVoucher2'])
        holder.backpack += ['copper'] * (holder.back_pack_capability - 1)
        commands = self.decide(state)
        self.assertEqual(state.worker_item_jobs[holder.id]['target'], (9, 11))
        self.assertEqual(commands[holder.id]['action'], 'move')

    def test_pioneer_uses_voucher_immediately(self):
        state, _holder = voucher_state(140, Pos(4, 4), [])
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(16, 11)
        pioneer.backpack = ['WeaponUpgradeVoucher2']
        commands = self.decide(state)
        self.assertEqual(commands[pioneer.id]['action'], 'move')
        self.assertEqual(state.worker_item_jobs[pioneer.id]['target'], (9, 11))

    def test_two_holders_do_not_upgrade_same_weapon_twice(self):
        state, holder = voucher_state(195, Pos(10, 11), ['WeaponUpgradeVoucher1'], levels=(1, 1))
        other = next(r for r in state.team_our.roles if r.id == 2)
        other.pos = Pos(10, 10)
        other.backpack = ['WeaponUpgradeVoucher1']
        commands = self.decide(state)
        targets = [tuple(c['targetPos'][0].values()) for rid, c in commands.items()
                   if rid in (1, 2) and c.get('action') == 'use']
        self.assertEqual(len(targets), len(set(targets)))

    def test_no_matching_level_weapon_leaves_holder_alone(self):
        state, holder = voucher_state(195, Pos(10, 11), ['WeaponUpgradeVoucher2'], levels=(1, 3))
        self.decide(state)
        self.assertFalse(any(e['code'] == 'held_voucher_use' for e in state.decision_events))


if __name__ == '__main__':
    unittest.main()
