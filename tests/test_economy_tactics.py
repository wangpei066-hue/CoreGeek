"""验证变现、预算、限额与通行不变量，不把模拟收益当作判题器实测。"""
from pathlib import Path
import tempfile
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy, item_cost
from src.agent.economy import liquidate, profitable_mine, sellable_ores
from src.agent.tactics import begin_round, tactical_action
from src.agent.opening import funnel_gap, wall_ring, movement_avoid, safe_wall, assign_weapons
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

    def test_high_value_small_backpack_goes_to_vendor_before_building(self):
        state, role = economy_state(gold=75)
        role.backpack = ['copper'] * 5
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[1]['action'], 'move')
        self.assertTrue(any(e['code'] == 'cashout_priority' for e in state.decision_events))
        self.assertFalse(any(e['code'] == 'build_conditions' for e in state.decision_events))

    def test_sale_commitment_survives_price_drop_and_restart(self):
        state, role = economy_state()
        role.backpack = ['copper'] * 5
        self.assertTrue(liquidate(role, state, build_blocked_set(state), set())[0])
        with tempfile.TemporaryDirectory() as root:
            save_build_memory(state, Path(root))
            restored = MatchState()
            load_build_memory(restored, Path(root))
        state.policy_memory = restored.policy_memory
        state.vendor_shop_list = [ShopItem('copper', 1)]
        self.assertTrue(liquidate(role, state, build_blocked_set(state), set())[0])
        self.assertIn(role.id, state.policy_memory['selling_roles'])

    def test_keeps_only_small_stone_reserve_and_never_sells_items(self):
        state, role = economy_state()
        state.team_our.roles[0].health = 1500
        role.backpack = ['stone']*20 + ['Medicine', 'Bomb', 'WeaponUpgradeVoucher1']
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
        self.assertIsNone(profitable_mine(role, state, build_blocked_set(state), set()))

    def test_multiround_ore_is_converted_to_gold(self):
        state, role = economy_state()
        role.backpack = ['copper']*5 + ['iron']*3
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

    def test_first_day_never_sells_even_when_rich_low_health_or_out_of_gold(self):
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
            self.assertTrue(any(e['code'] == 'opening_defense_only' for e in state.decision_events))


class TacticalTests(unittest.TestCase):
    def test_offense_purchase_preserves_defense_budget(self):
        state, role = defended_state()
        role.pos = Pos(5, 4)
        cmd = tactical_action(role, state, build_blocked_set(state), set())
        self.assertEqual(cmd['name'], 'LargeRobotSummonOrder')
        self.assertGreaterEqual(state.team_our.gold_num-item_cost(cmd['name'], state), 100)

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


class FunnelTests(unittest.TestCase):
    def test_mirrored_outer_gap_is_open_and_not_a_movement_route(self):
        for base_x in (10, 30):
            state = opening_state()
            base = state.team_our.roles[0]
            base.pos = Pos(base_x, 10)
            gap = funnel_gap(state, base)
            walls = set(wall_ring(state, base))
            self.assertNotIn(gap, walls)
            self.assertIn(gap, movement_avoid(state))
            inner_x = base_x+3 if base_x == 10 else base_x-2
            self.assertIn((inner_x, gap[1]), walls)
            self.assertEqual(abs(gap[0]-inner_x), 2)

    def test_map_edge_falls_back_to_one_layer(self):
        state = opening_state()
        state.map_info.width = 6
        state.team_our.roles[0].pos = Pos(1, 10)
        self.assertIsNone(funnel_gap(state, state.team_our.roles[0]))

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
