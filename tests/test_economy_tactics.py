"""验证变现、预算、限额与通行不变量，不把模拟收益当作判题器实测。"""
from pathlib import Path
import tempfile
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy, item_cost
from src.agent.economy import liquidate, pick_mine, profitable_mine, sellable_ores
from src.agent.tactics import begin_round, tactical_action
from src.agent.opening import primary_wall_plan, wall_ring, safe_wall, assign_weapons
from src.agent.grid import build_blocked_set
from src.agent.protocol import MatchState, Pos, Zone, RobotRole, ShopItem
from src.agent.server import load_build_memory, save_build_memory
from test_shop_items import minimal_state, make_role
from test_opening import opening_state


def economy_state(gold=0):
    state = minimal_state(round_no=140, gold_num=gold)
    state.map_info.zones = [Zone(Pos(5, 5), 'vendor'), Zone(Pos(6, 5), 'weaponShop'), Zone(Pos(2, 1), 'copper')]
    state.vendor_shop_list = [ShopItem('stone', 1), ShopItem('iron', 3), ShopItem('copper', 5)]
    worker = make_role(1, 1, 1, 'worker', health=220, back_pack_capability=100)
    state.team_our.roles.append(worker)
    return state, worker


def defended_state(gold=400):
    state, role = economy_state(gold)
    state.team_our.roles += [make_role(20+i, 9+i, 8, kind, level=1, health=1000, attack_range=10)
                             for i, kind in enumerate(('gatling', 'railgun', 'rocket'))]
    state.team_our.roles += [make_role(30+i, 13, 7+i, 'wall', level=1, health=1000) for i in range(6)]
    state.team_our.roles[0].health = 1500
    begin_round(state)
    return state, role


class EconomyTests(unittest.TestCase):
    def test_owned_upgrade_is_used_even_without_gold(self):
        from src.agent.brain import maybe_start_shop_item_job, decide_shop_item_job
        state, role = defended_state(gold=0)
        role.backpack = ['WeaponUpgradeVoucher1']
        role.pos = Pos(9, 7)
        maybe_start_shop_item_job(role, state)
        command = decide_shop_item_job(role, state, build_blocked_set(state), set())
        self.assertEqual(command['action'], 'use')
        self.assertEqual(command['name'], 'WeaponUpgradeVoucher1')

    def test_later_day_returns_to_weapon_instead_of_collecting_at_dusk(self):
        state, role = defended_state()
        state.round_no = 198
        role.backpack = ['copper'] * 20
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[role.id]['action'], 'move')
        self.assertTrue(any(e['code'] == 'income_muster' and e['role_id'] == role.id
                            for e in state.decision_events))

    def test_mid_day_near_gun_keeps_working(self):
        """去掉第50回合一刀切后，离炮很近的工人白天中段仍可继续干活。"""
        state, role = defended_state()
        state.round_no = 185
        role.pos = Pos(9, 10)
        role.backpack = ['copper'] * 3
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertFalse(any(e['code'] == 'income_muster' and e.get('role_id') == role.id
                             for e in state.decision_events))
        if role.id in commands:
            self.assertNotEqual(commands[role.id].get('action'), 'attack')

    def test_dusk_distant_enemy_does_not_delay_night_defense(self):
        state, role = defended_state()
        state.round_no = 198
        role.pos = Pos(1, 1)
        role.backpack = ['copper'] * 3
        state.robot.roles = [RobotRole(id=30001, pos=Pos(30, 10), role_type='smallRobot', health=40)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertTrue(any(e['code'] == 'income_muster' and e.get('role_id') == role.id
                            for e in state.decision_events))
        self.assertEqual(commands[role.id]['action'], 'move')

    def test_pre_night_small_ore_goes_to_vendor_before_third_night(self):
        state, role = defended_state(gold=400)
        state.round_no = 310  # 第三天白天，入夜前约 20 回合。
        role.backpack = ['copper'] * 8
        handled, cmd = liquidate(role, state, build_blocked_set(state), set())
        self.assertTrue(handled)
        self.assertIsNotNone(cmd)
        self.assertTrue(any(e['code'] == 'cashout_priority' and '入夜前清空背包' in ' '.join(e.get('triggers') or [])
                            for e in state.decision_events))

    def test_pre_night_skips_mining_and_buys_weapon_voucher(self):
        state, role = defended_state(gold=150)
        state.round_no = 310
        role.backpack = []
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertNotEqual((commands.get(role.id) or {}).get('action'), 'collect')
        self.assertTrue(any(e['code'] == 'cashout_skip_mine' for e in state.decision_events)
                        or (commands.get(role.id) or {}).get('action') in ('move', 'buy', 'use'))
        self.assertTrue(
            (commands.get(role.id) or {}).get('action') in ('move', 'buy', 'use')
            or any(job.get('kind') == 'weapon' for job in state.worker_item_jobs.values())
            or any(e['code'] in ('voucher_buyer_pick', 'shop_job_check', 'cashout_skip_mine')
                   for e in state.decision_events)
        )

    def test_day2_worker_buys_weapon_voucher_before_muster_when_gold_is_ready(self):
        state, role = defended_state(gold=150)
        state.round_no = 150
        role.pos = Pos(6, 6)
        state.map_info.zones = [
            Zone(Pos(5, 5), 'vendor'),
            Zone(Pos(6, 5), 'weaponShop'),
        ]
        state.team_our.roles = [
            state.team_our.roles[0],
            role,
            make_role(21, 12, 10, 'rocket', level=2, health=1000, attack_range=10),
            make_role(22, 12, 8, 'rocket', level=1, health=1000, attack_range=10),
            make_role(23, 12, 12, 'railgun', level=1, health=1000, attack_range=10),
        ]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[role.id]['action'], 'buy')
        self.assertEqual(commands[role.id]['name'], 'WeaponUpgradeVoucher1')

    def test_held_weapon_voucher_is_used_before_muster(self):
        state, role = defended_state(gold=0)
        state.round_no = 190
        role.pos = Pos(12, 9)
        role.backpack = ['WeaponUpgradeVoucher1']
        state.team_our.roles = [
            state.team_our.roles[0],
            role,
            make_role(21, 12, 10, 'rocket', level=2, health=1000, attack_range=10),
            make_role(22, 12, 8, 'rocket', level=1, health=1000, attack_range=10),
            make_role(23, 12, 12, 'railgun', level=1, health=1000, attack_range=10),
        ]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[role.id]['action'], 'use')
        self.assertEqual(commands[role.id]['name'], 'WeaponUpgradeVoucher1')
        self.assertTrue(any(e['code'] == 'pre_muster_weapon_voucher' for e in state.decision_events))

    def test_pre_night_too_late_still_must_return_to_gun(self):
        state, role = defended_state()
        state.round_no = 198
        role.backpack = ['copper'] * 8
        handled, cmd = liquidate(role, state, build_blocked_set(state), set())
        self.assertFalse(handled)
        self.assertTrue(any(e['code'] == 'sale_too_late' for e in state.decision_events))

    def test_mid_day_small_ore_still_does_not_dump_without_voucher_gap(self):
        state, role = defended_state(gold=400)
        state.round_no = 145
        role.backpack = ['copper'] * 8
        handled, cmd = liquidate(role, state, build_blocked_set(state), set())
        self.assertFalse(handled)

    def test_small_ore_pile_does_not_run_to_vendor(self):
        state, role = economy_state(gold=75)
        role.backpack = ['copper'] * 5
        handled, cmd = liquidate(role, state, build_blocked_set(state), set())
        self.assertFalse(handled)
        self.assertFalse(any(e['code'] == 'cashout_priority' for e in state.decision_events))

    def test_voucher_gap_sends_backpack_to_vendor(self):
        state, role = defended_state(gold=75)
        role.backpack = ['copper'] * 5
        handled, cmd = liquidate(role, state, build_blocked_set(state), set())
        self.assertTrue(handled)
        self.assertTrue(any(e['code'] == 'cashout_priority' for e in state.decision_events))

    def test_day1_split_metal_still_sends_a_worker_to_vendor(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 50
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1, health=1000),
            make_role(21, 12, 8, 'rocket', level=1, health=1000),
            make_role(22, 12, 12, 'rocket', level=1, health=1000),
        ]
        state.map_info.zones = [
            Zone(Pos(6, 9), 'stone'),
            Zone(Pos(1, 11), 'vendor'),
            Zone(Pos(1, 9), 'weaponShop'),
        ]
        state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 3)]
        a = next(r for r in state.team_our.roles if r.id == 1)
        b = next(r for r in state.team_our.roles if r.id == 2)
        a.backpack = ['copper'] * 5
        b.backpack = ['copper'] * 5
        blocked = build_blocked_set(state)
        handled_a, cmd_a = liquidate(a, state, blocked, set())
        handled_b, cmd_b = liquidate(b, state, blocked, set())
        self.assertTrue(handled_a or handled_b)
        self.assertTrue(cmd_a or cmd_b)
        self.assertTrue(any(e['code'] == 'cashout_priority' for e in state.decision_events))

    def test_day1_sells_metal_without_vendor_quote(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1, health=1000),
            make_role(21, 12, 8, 'rocket', level=1, health=1000),
            make_role(22, 12, 12, 'rocket', level=1, health=1000),
        ]
        state.map_info.zones = [Zone(Pos(1, 11), 'vendor'), Zone(Pos(1, 9), 'weaponShop')]
        state.vendor_shop_list = []
        role = next(r for r in state.team_our.roles if r.id == 1)
        role.backpack = ['copper'] * 6
        handled, cmd = liquidate(role, state, build_blocked_set(state), set())
        self.assertTrue(handled)
        self.assertIsNotNone(cmd)
        self.assertTrue(any(e['code'] == 'sale_value_unknown' for e in state.decision_events))
        self.assertFalse(any('无价值' in (e.get('message') or '') for e in state.decision_events))

    def test_batch_fill_goes_to_vendor(self):
        state, role = economy_state()
        role.back_pack_capability = 10
        role.backpack = ['copper'] * 6
        self.assertTrue(liquidate(role, state, build_blocked_set(state), set())[0])

    def test_sale_commitment_survives_price_drop_and_restart(self):
        state, role = economy_state()
        role.back_pack_capability = 10
        role.backpack = ['copper'] * 6
        self.assertTrue(liquidate(role, state, build_blocked_set(state), set())[0])
        with tempfile.TemporaryDirectory() as root:
            save_build_memory(state, Path(root))
            restored = MatchState()
            load_build_memory(restored, Path(root))
        state.policy_memory = restored.policy_memory
        state.vendor_shop_list = [ShopItem('copper', 1)]
        self.assertTrue(liquidate(role, state, build_blocked_set(state), set())[0])
        self.assertIn(role.id, state.policy_memory['selling_roles'])

    def test_keeps_wall_stones_after_first_night_and_never_sells_items(self):
        state, role = economy_state()
        state.team_our.roles[0].health = 1500
        role.backpack = ['stone']*20 + ['Medicine', 'Bomb', 'WeaponUpgradeVoucher1']
        # 墙目标是动态的，断言不变式：缺口所需的石头全部留着，只卖超出部分，道具永不出售。
        from src.agent.brain import own_station
        from src.agent.opening import staged_wall_plan
        walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles
                 if r.role_type == 'wall' and r.health > 0}
        missing = len(set(staged_wall_plan(state, own_station(state))) - walls)
        self.assertEqual(sellable_ores(role, state), {'stone': 20 - missing})

    def test_day1_still_uses_small_stone_reserve(self):
        state, role = economy_state()
        state.round_no = 20
        state.team_our.roles[0].health = 1500
        role.backpack = ['stone'] * 20
        self.assertEqual(sellable_ores(role, state), {'stone': 16})

    def test_low_health_triggers_sale_before_normal_threshold(self):
        state, role = economy_state()
        role.health = 80
        role.backpack = ['iron']
        self.assertTrue(liquidate(role, state, build_blocked_set(state), set())[0])

    def test_does_not_depart_for_sale_too_late_at_night(self):
        state, role = economy_state()
        role.backpack = ['copper']*20
        state.round_no = 198  # 第二天白天仅余2回合。
        self.assertFalse(liquidate(role, state, build_blocked_set(state), set())[0])
        self.assertTrue(any(e['code'] == 'sale_too_late' for e in state.decision_events))

    def test_full_backpack_does_not_keep_collecting(self):
        state, role = economy_state()
        role.back_pack_capability = 1
        role.backpack = ['Medicine']
        state.policy_memory['mine_targets'] = {str(role.id): {'x': 2, 'y': 1, 'ore': 'copper'}}
        self.assertIsNone(profitable_mine(role, state, build_blocked_set(state), set()))
        self.assertNotIn(str(role.id), state.policy_memory.get('mine_targets') or {})

    def test_two_workers_claim_different_mines(self):
        state, a = economy_state()
        b = make_role(2, 1, 2, 'worker', health=220, back_pack_capability=100)
        state.team_our.roles.append(b)
        state.map_info.zones = [
            Zone(Pos(5, 5), 'vendor'),
            Zone(Pos(6, 5), 'weaponShop'),
            Zone(Pos(2, 1), 'copper'),
            Zone(Pos(2, 10), 'copper'),
        ]
        blocked = build_blocked_set(state)
        self.assertIsNotNone(profitable_mine(a, state, blocked, set()))
        self.assertIsNotNone(profitable_mine(b, state, blocked, set()))
        ta = state.policy_memory['mine_targets'][str(a.id)]
        tb = state.policy_memory['mine_targets'][str(b.id)]
        self.assertNotEqual((ta['x'], ta['y']), (tb['x'], tb['y']))

    def test_two_workers_can_share_the_only_stone_mine(self):
        state, a = economy_state()
        b = make_role(2, 1, 2, 'worker', health=220, back_pack_capability=100)
        state.team_our.roles.append(b)
        state.map_info.zones = [
            Zone(Pos(5, 5), 'vendor'),
            Zone(Pos(2, 1), 'stone'),
        ]
        blocked = build_blocked_set(state)
        first = pick_mine(a, state, blocked, set(), want_ores=('stone',), purpose='stone')
        second = pick_mine(b, state, blocked, set(), want_ores=('stone',), purpose='stone')
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual((first[0].pos.x, first[0].pos.y), (second[0].pos.x, second[0].pos.y))

    def test_sticky_mine_survives_a_better_score_appearing(self):
        state, role = economy_state()
        state.map_info.zones = [
            Zone(Pos(5, 5), 'vendor'),
            Zone(Pos(8, 1), 'copper'),
        ]
        blocked = build_blocked_set(state)
        self.assertIsNotNone(profitable_mine(role, state, blocked, set()))
        sticky = dict(state.policy_memory['mine_targets'][str(role.id)])
        state.map_info.zones.append(Zone(Pos(2, 1), 'copper'))
        self.assertIsNotNone(profitable_mine(role, state, blocked, set()))
        self.assertEqual(state.policy_memory['mine_targets'][str(role.id)], sticky)

    def test_soft_cap_rotates_away_from_ore_above_eighty_percent(self):
        state, role = economy_state()
        role.back_pack_capability = 10
        role.backpack = ['copper'] * 8
        state.map_info.zones = [
            Zone(Pos(5, 5), 'vendor'),
            Zone(Pos(2, 1), 'copper'),
            Zone(Pos(3, 1), 'iron'),
        ]
        picked = pick_mine(role, state, build_blocked_set(state), set(),
                           want_ores=('iron', 'copper'), purpose='income')
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0].neutral_type, 'iron')

    def test_trip_cap_prefers_near_iron_over_far_copper(self):
        state, role = economy_state()
        role.back_pack_capability = 100
        role.backpack = []
        state.map_info.zones = [
            Zone(Pos(5, 5), 'vendor'),
            Zone(Pos(2, 1), 'iron'),
            Zone(Pos(25, 25), 'copper'),
        ]
        blocked = build_blocked_set(state)
        self.assertIsNotNone(profitable_mine(role, state, blocked, set()))
        target = state.policy_memory['mine_targets'][str(role.id)]
        self.assertEqual(target['ore'], 'iron')
        self.assertEqual((target['x'], target['y']), (2, 1))

    def test_voucher_mine_prefers_near_high_price_iron(self):
        state, role = economy_state()
        role.backpack = []
        state.map_info.zones = [
            Zone(Pos(5, 5), 'vendor'),
            Zone(Pos(2, 1), 'iron'),
            Zone(Pos(25, 25), 'copper'),
        ]
        state.vendor_shop_list = [ShopItem('iron', 10), ShopItem('copper', 5)]
        picked = pick_mine(role, state, build_blocked_set(state), set(),
                           want_ores=('iron', 'copper'), purpose='voucher')
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0].neutral_type, 'iron')

    def test_voucher_mine_prefers_near_copper_when_far_iron_takes_more_rounds(self):
        state, role = economy_state()
        role.backpack = []
        state.map_info.zones = [
            Zone(Pos(5, 5), 'vendor'),
            Zone(Pos(2, 1), 'copper'),
            Zone(Pos(25, 25), 'iron'),
        ]
        state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 10)]
        picked = pick_mine(role, state, build_blocked_set(state), set(),
                           want_ores=('iron', 'copper'), purpose='voucher')
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0].neutral_type, 'copper')

    def test_voucher_mine_without_vendor_prices_does_not_invent_them(self):
        state, role = economy_state()
        role.backpack = []
        state.vendor_shop_list = []
        self.assertIsNone(pick_mine(role, state, build_blocked_set(state), set(),
                                    want_ores=('iron', 'copper'), purpose='voucher'))

    def test_cashout_window_clears_mine_target(self):
        state, role = defended_state()
        state.round_no = 310
        role.backpack = ['copper']
        state.policy_memory['mine_targets'] = {str(role.id): {'x': 2, 'y': 1, 'ore': 'copper'}}
        self.assertIsNone(profitable_mine(role, state, build_blocked_set(state), set()))
        self.assertNotIn(str(role.id), state.policy_memory.get('mine_targets') or {})

    def test_multiround_ore_is_converted_to_gold(self):
        state, role = economy_state()
        role.backpack = ['copper']*5 + ['iron']*3
        role.back_pack_capability = 10
        sold_value = 0
        for turn in range(140, 160):
            state.round_no = turn
            handled, cmd = liquidate(role, state, build_blocked_set(state), set())
            if not handled:
                break
            self.assertIsNotNone(cmd)
            if cmd['action'] == 'move':
                role.pos = Pos(**cmd['targetPos'][0])
            else:
                price = next(i.price for i in state.vendor_shop_list if i.name == cmd['name'])
                sold_value += cmd['num'] * price
                for _ in range(cmd['num']):
                    role.backpack.remove(cmd['name'])
        self.assertEqual(sold_value, 34)
        self.assertEqual(role.backpack, [])

    def test_first_day_does_not_sell_before_three_rockets(self):
        for gold in (0, 75):
            state = opening_state()
            state.round_no = 45
            state.team_our.gold_num = gold
            role = state.team_our.roles[1]
            role.backpack = ['copper']*40 + ['stone']*4
            role.health = 50
            state.map_info.zones.append(Zone(Pos(8, 9), 'vendor'))
            commands = V1Strategy(BasicActionValidator()).decide(state)
            self.assertFalse(any(c['action'] == 'sell' for c in commands.values()))
            self.assertFalse(any(e['code'] == 'cashout_priority' for e in state.decision_events))
            self.assertTrue(any(e['code'] == 'opening_rockets_first' for e in state.decision_events))

    def test_first_day_sells_after_rockets_when_ore_worth_about_130(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        role = state.team_our.roles[1]
        role.pos = Pos(8, 9)
        role.backpack = ['copper'] * 26
        state.map_info.zones.append(Zone(Pos(8, 9), 'vendor'))
        state.map_info.zones.append(Zone(Pos(7, 9), 'weaponShop'))
        from src.agent.protocol import ShopItem
        state.vendor_shop_list = [ShopItem('copper', 5)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertTrue(any(c['action'] == 'sell' for c in commands.values())
                        or any(e['code'] == 'cashout_priority' for e in state.decision_events))

    def test_first_day_sells_when_cash_plus_ore_covers_actual_voucher(self):
        state = opening_state()
        state.round_no = 20
        state.team_our.gold_num = 90
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        role = state.team_our.roles[1]
        role.pos = Pos(8, 9)
        role.backpack = ['copper'] * 3
        state.map_info.zones.append(Zone(Pos(8, 9), 'vendor'))
        state.map_info.zones.append(Zone(Pos(7, 9), 'weaponShop'))
        state.vendor_shop_list = [ShopItem('copper', 5)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertTrue(any(c['action'] == 'sell' for c in commands.values())
                        or any(e['code'] == 'cashout_priority' for e in state.decision_events))


class TacticalTests(unittest.TestCase):
    def test_does_not_buy_summon_before_day_four(self):
        state, role = defended_state()
        for building in state.team_our.roles:
            if building.role_type in ('gatling', 'railgun', 'rocket'):
                building.level = 2
        role.pos = Pos(5, 4)
        self.assertIsNone(tactical_action(role, state, build_blocked_set(state), set()))

    def test_offense_purchase_preserves_defense_budget(self):
        state, role = defended_state()
        state.round_no = 400
        for building in state.team_our.roles:
            if building.role_type in ('gatling', 'railgun', 'rocket'):
                building.level = 2
        role.pos = Pos(5, 4)
        cmd = tactical_action(role, state, build_blocked_set(state), set())
        self.assertEqual(cmd['name'], 'LargeRobotSummonOrder')
        self.assertGreaterEqual(state.team_our.gold_num-item_cost(cmd['name'], state), 100)

    def test_decide_does_not_buy_luxury_items_before_day_four(self):
        state, role = defended_state(gold=400)
        for building in state.team_our.roles:
            if building.role_type in ('gatling', 'railgun', 'rocket'):
                building.level = 2
        role.pos = Pos(5, 4)
        pioneer = make_role(3, 5, 5, 'pioneer', health=200, back_pack_capability=40)
        state.team_our.roles.append(pioneer)
        from src.agent.protocol import ShopItem, WorldNews
        state.weapon_shop_list = [
            ShopItem('AcientTablet', 15), ShopItem('LargeRobotSummonOrder', 100),
            ShopItem('StationUpgradeVoucher1', 100), ShopItem('WeaponUpgradeVoucher1', 100),
        ]
        state.world_news = WorldNews(folk_legends='携带AcientTablet在(6, 5)召唤。第3天。')
        commands = V1Strategy(BasicActionValidator()).decide(state)
        buys = [c.get('name') for c in commands.values() if c.get('action') == 'buy']
        self.assertNotIn('AcientTablet', buys)
        self.assertFalse(any(isinstance(name, str) and name.endswith('SummonOrder') for name in buys))

    def test_no_offense_when_base_is_in_danger_or_budget_low(self):
        for health, gold in ((500, 400), (1500, 110)):
            state, role = defended_state(gold)
            state.team_our.roles[0].health = health
            role.pos = Pos(5, 4)
            self.assertIsNone(tactical_action(role, state, build_blocked_set(state), set()))

    def test_summon_limit_survives_restart_and_resets_next_day(self):
        state, role = defended_state()
        role.backpack = ['SmallRobotSummonOrder']
        for turn in range(140, 150):
            state.round_no = turn
            begin_round(state)
            cmd = tactical_action(role, state, build_blocked_set(state), set())
            self.assertEqual(cmd['action'], 'use')
        with tempfile.TemporaryDirectory() as root:
            save_build_memory(state, Path(root))
            restored = MatchState()
            load_build_memory(restored, Path(root))
        state.policy_memory = restored.policy_memory
        state.round_no = 150
        begin_round(state)
        self.assertIsNone(tactical_action(role, state, build_blocked_set(state), set()))
        state.round_no = 260
        begin_round(state)
        self.assertEqual(tactical_action(role, state, build_blocked_set(state), set())['action'], 'use')

    def test_dizzy_used_only_under_pressure(self):
        state, role = defended_state()
        role.backpack = ['DizzyWeapon']
        state.round_no = 140
        self.assertIsNone(tactical_action(role, state, build_blocked_set(state), set(), allow_travel=False))
        state.round_no = 200
        state.robot.roles = [RobotRole(i, Pos(15+i%2, 10+i//2), 'smallRobot', 40, target_team=state.team_our.type) for i in range(4)]
        cmd = tactical_action(role, state, build_blocked_set(state), set(), allow_travel=False)
        self.assertEqual(cmd['action'], 'use')
        self.assertEqual(cmd['name'], 'DizzyWeapon')
        BasicActionValidator().validate(cmd, state)

    def test_pressure_bomb_hits_cluster_and_avoids_duplicate_bomb(self):
        state, role = defended_state()
        state.round_no = 200
        role.backpack = ['Bomb']
        state.robot.roles = [RobotRole(i, Pos(15+i%2, 10+i//2), 'smallRobot', 40, target_team=state.team_our.type) for i in range(4)]
        cmd = tactical_action(role, state, build_blocked_set(state), set(), allow_travel=False)
        self.assertEqual(cmd['action'], 'use')
        self.assertEqual(cmd['name'], 'Bomb')
        BasicActionValidator().validate(cmd, state)
        role2 = make_role(2, 2, 2, 'worker', backpack=['Bomb'], back_pack_capability=100)
        self.assertIsNone(tactical_action(role2, state, build_blocked_set(state), set(), allow_travel=False))

    def test_two_roles_do_not_buy_two_emergency_bombs(self):
        state, role = defended_state(gold=100)
        state.round_no = 200
        role.pos = Pos(5, 4)
        state.robot.roles = [RobotRole(i, Pos(15+i%2, 10+i//2), 'smallRobot', 40, target_team=state.team_our.type) for i in range(4)]
        role2 = make_role(2, 7, 4, 'worker', health=220, back_pack_capability=100)
        state.team_our.roles.append(role2)
        commands = V1Strategy(BasicActionValidator()).decide(state)
        buys = [c for c in commands.values() if c['action'] == 'buy']
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]['name'], 'Bomb')
        self.assertEqual(state.team_our.gold_num, 100)

    def test_enemy_targeted_robots_do_not_trigger_our_emergency(self):
        state, role = defended_state()
        role.backpack = ['Bomb']
        state.round_no = 200
        enemy_type = 'defender' if state.team_our.type == 'challenger' else 'challenger'
        state.robot.roles = [RobotRole(i, Pos(11+i%2, 12+i//2), 'smallRobot', 40, target_team=enemy_type) for i in range(4)]
        self.assertIsNone(tactical_action(role, state, build_blocked_set(state), set(), allow_travel=False))


class WallLayoutTests(unittest.TestCase):
    def test_single_layer_front_on_both_sides(self):
        """规则不允许双层墙：迎敌方向只有一整列墙。"""
        from src.agent.opening import attack_direction, defense_bounds
        for base_x in (10, 30):
            state = opening_state()
            base = state.team_our.roles[0]
            base.pos = Pos(base_x, 10)
            left, right, bottom, top = defense_bounds(state, base)
            direction = attack_direction(state, base)
            front = right if direction == 1 else left
            walls = set(wall_ring(state, base))
            self.assertTrue(all((front, y) in walls for y in range(bottom, top + 1)))
            beyond = {p for p in walls if (p[0] - front) * direction > 0}
            self.assertEqual(beyond, set())
            self.assertEqual(primary_wall_plan(state, base), wall_ring(state, base))

    def test_wall_cannot_cut_off_only_vendor_route(self):
        state = opening_state()
        role = state.team_our.roles[1]
        role.pos = Pos(2, 2)
        state.team_our.roles = [state.team_our.roles[0], role]
        state.map_info.width = 7
        state.map_info.height = 7
        state.map_info.zones = [Zone(Pos(5, 2), 'vendor')]
        blocked = {(3, y) for y in range(7) if y != 2} | {(5, 2)}
        self.assertFalse(safe_wall(state, (3, 2), blocked, {}))


def _spike_state(gold, copper):
    from test_defense_priority import defended
    state = defended()
    state.round_no = 150
    state.team_our.gold_num = gold
    state.map_info.zones += [Zone(Pos(8, 9), 'weaponShop'), Zone(Pos(4, 12), 'vendor'),
                             Zone(Pos(2, 14), 'copper')]
    state.vendor_shop_list = [ShopItem('stone', 1), ShopItem('iron', 3), ShopItem('copper', 5)]
    economist = next(r for r in state.team_our.roles if r.id == 2)
    economist.pos = Pos(12, 9)
    economist.back_pack_capability = 100
    economist.backpack = ['copper'] * copper
    return state, economist


class SpikeAndDuskSaleTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_spike_day_economist_clears_pack_first(self):
        from unittest import mock
        state, economist = _spike_state(0, 20)
        with mock.patch('src.agent.economy.price_spike_today', return_value={'copper'}):
            self.decide(state)
        codes = [e['code'] for e in state.decision_events if e.get('role_id') == economist.id]
        self.assertIn('spike_day_cashout', codes)
        self.assertIn(economist.id, state.policy_memory['selling_roles'])

    def test_spike_day_economist_buys_upgrade_after_pack_is_empty(self):
        from unittest import mock
        state, economist = _spike_state(300, 0)
        with mock.patch('src.agent.economy.price_spike_today', return_value={'copper'}):
            self.decide(state)
        self.assertEqual(state.policy_memory['spike_cashout'], {'1': 'upgrade'})
        self.assertEqual(state.worker_item_jobs[economist.id]['kind'], 'weapon')

    def test_spike_day_returns_to_mining_when_gold_is_spent(self):
        from unittest import mock
        state, economist = _spike_state(0, 0)
        state.policy_memory['spike_cashout'] = {'1': 'upgrade'}
        with mock.patch('src.agent.economy.price_spike_today', return_value={'copper'}):
            self.decide(state)
        self.assertEqual(state.policy_memory['spike_cashout'], {'1': 'done'})

    def _dusk_sale(self, contacts):
        from collections import Counter
        from src.agent.economy import _sale_return_rounds, _vendor_choices
        state, role = defended_state(gold=0)
        role.backpack = ['copper'] * 30
        blocked = build_blocked_set(state)
        path = min(_vendor_choices(role, state, blocked, set()), key=lambda c: c[:3])[4]
        sale_rounds = _sale_return_rounds(role, state, blocked, Counter({'copper': 30}), path)[0]
        state.round_no = 130 * 2 + 70 - (sale_rounds - 2)  # 天黑前卖得完，但回炮会晚2回合
        if contacts:
            state.policy_memory['night_contact'] = contacts
        return liquidate(role, state, blocked, set())

    def test_dusk_sale_blocked_without_observed_night_contact(self):
        self.assertEqual(self._dusk_sale(None), (False, None))

    def test_dusk_sale_uses_observed_after_dark_grace(self):
        handled, cmd = self._dusk_sale({'1': 16})
        self.assertTrue(handled)
        self.assertEqual(cmd['action'], 'move')

    def test_night_contact_records_first_close_robot_round(self):
        from src.agent.economy import after_dark_grace, note_night_contact
        state, _role = defended_state(gold=0)
        state.round_no = 130 + 70 + 9
        base = state.team_our.roles[0]
        state.robot.roles = [RobotRole(100, Pos(base.pos.x + 5, base.pos.y), 'smallRobot', 10)]
        note_night_contact(state)
        self.assertEqual(state.policy_memory['night_contact'], {'1': 9})
        self.assertEqual(after_dark_grace(state), 5)

    def test_night_worker_can_sell_after_wave_is_gone(self):
        state, role = defended_state(gold=0)
        state.round_no = 130 + 100
        role.backpack = ['copper'] * 60
        state.policy_memory['night_saw_threat'] = True
        state.policy_memory['night_empty_streak'] = 1
        handled, cmd = liquidate(role, state, build_blocked_set(state), set())
        self.assertTrue(handled)
        self.assertIsNotNone(cmd)

    def test_ore_rising_soon_is_held_even_when_weapon_voucher_is_short(self):
        from unittest import mock
        state, role = defended_state(gold=0)
        role.backpack = ['copper'] * 30 + ['iron'] * 10
        with mock.patch('src.agent.economy.ores_held_for_price_rise', return_value={'iron'}):
            ores = sellable_ores(role, state)
        self.assertEqual(ores['copper'], 30)
        self.assertNotIn('iron', ores)

    def test_opening_does_not_sell_ore_rising_soon(self):
        from unittest import mock
        from src.agent.opening_schedule import opening_sell_metal
        state, role = defended_state(gold=0)
        state.round_no = 30
        role.backpack = ['iron'] * 10
        with mock.patch('src.agent.economy.ores_held_for_price_rise', return_value={'iron'}):
            cmd = opening_sell_metal(role, state, build_blocked_set(state), set(), 'fund')
        self.assertIsNone(cmd)
