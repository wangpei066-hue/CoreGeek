"""全局工作单 + 建造工状态机 + 统一截止时间的验收断言（任务书 第九章）。"""
import unittest

from src.agent import work_orders as wo
from src.agent.grid import build_blocked_set
from src.agent.opening import (
    assign_weapons, movement_avoid, primary_wall_plan, safe_wall, staged_wall_plan,
    survival_wall_missing, weapon_slots,
)
from src.agent.opening_schedule import (
    STAGE_WALL, economist_should_help_wall, opening_wall_work, opening_worker_roles,
    planned_stone_batch,
)
from src.agent.protocol import Pos, ShopItem, Zone
from test_opening import opening_state
from test_opening_fsm import _rockets, _map, apply_opening_commands, run_opening


def scene(round_no=30):
    state = opening_state()
    state.round_no = round_no
    state.team_our.gold_num = 0
    _rockets(state)
    _map(state)
    return state


def blocked_of(state):
    return build_blocked_set(state) | movement_avoid(state)


def builder(state):
    return next(r for r in state.team_our.roles if r.id == 1)


def economist(state):
    return next(r for r in state.team_our.roles if r.id == 2)


class WallTargetTests(unittest.TestCase):
    def test_first_two_weapon_slots_share_a_controller_stand(self):
        state = scene()
        base = next(r for r in state.team_our.roles if r.role_type == 'station')
        first, second = weapon_slots(state, base)[:2]
        common = {
            (x, y)
            for x in range(max(0, min(first[0], second[0]) - 1), min(state.map_info.width, max(first[0], second[0]) + 2))
            for y in range(max(0, min(first[1], second[1]) - 1), min(state.map_info.height, max(first[1], second[1]) + 2))
            if max(abs(x - first[0]), abs(y - first[1])) <= 1
            and max(abs(x - second[0]), abs(y - second[1])) <= 1
            and (x, y) not in {first, second}
        }
        self.assertTrue(common)

    def test_day1_wall_target_is_not_capped_at_eight(self):
        """验收1：三门炮已完成、资源与路径允许时，第一天墙目标可超过 10 段。"""
        state = scene()
        base = next(r for r in state.team_our.roles if r.role_type == 'station')
        plan_slots = len(primary_wall_plan(state, base))
        self.assertGreater(plan_slots, 10, '测试地图本身要能放下 10 段以上，否则断言无意义')
        self.assertGreater(wo.wall_feasible_target(state, blocked_of(state)), 10)
        self.assertGreater(len(staged_wall_plan(state, base)), 10)

    def test_feasible_target_shrinks_near_night_but_never_below_minimum(self):
        """墙目标是动态的：临近入夜会收缩，但不会低于最低生存墙。"""
        late = scene(round_no=66)
        program = wo.compute_work_orders(late, blocked_of(late), force=True)['wall_program']
        self.assertGreaterEqual(program['wall_feasible_target'], program['wall_minimum_target'])
        self.assertLess(program['wall_feasible_target'],
                        wo.wall_feasible_target(scene(round_no=10), blocked_of(scene(10))))


class StoneBatchTests(unittest.TestCase):
    def test_safe_wall_keeps_stone_route_until_remaining_walls_are_funded(self):
        """建造工没囤够剩余墙石时，不能把唯一出院采石通路封死。"""
        from test_shop_items import make_role
        state = scene()
        state.map_info.zones = [Zone(Pos(16, 10), 'stone')]
        target = (13, 10)
        for i, y in enumerate(y for y in range(state.map_info.height) if y != target[1]):
            state.team_our.roles.append(make_role(500 + i, target[0], y, 'wall', level=1, health=1000))
        self.assertFalse(safe_wall(state, target, blocked_of(state), assign_weapons(state)))

    def test_batch_covers_many_walls_not_one(self):
        """验收2：一趟石矿要备够连续修多段墙的量，不是采一块修一段。"""
        state = scene()
        role = builder(state)
        b = blocked_of(state)
        slots = wo.remaining_wall_slots(state)
        batch = planned_stone_batch(state, role, b, slots, survival_wall_missing(state))
        self.assertGreaterEqual(batch, 4)

    def test_one_stone_early_keeps_mining_instead_of_building(self):
        """验收2：无紧急事件时不得出现 采1块 -> 修1段 -> 再采 的循环。"""
        state = scene()
        role = builder(state)
        role.backpack = ['stone']
        state.decision_events = []
        opening_wall_work(role, state, blocked_of(state), set(), set(), {})
        tick = next(e for e in state.decision_events if e['code'] == 'opening_worker_tick')
        self.assertEqual(tick.get('goal_type'), 'stone')
        self.assertEqual(tick.get('switch_reason'), 'batch_not_ready')

    def test_batch_never_exceeds_free_backpack_slots(self):
        """背包容量只能来自快照；批量不得超过剩余格数。"""
        state = scene()
        role = builder(state)
        role.back_pack_capability = 5
        role.backpack = ['iron', 'iron']
        self.assertEqual(wo.inventory_capacity(role), 5)
        self.assertEqual(wo.free_slots(role), 3)
        batch = planned_stone_batch(state, role, blocked_of(state),
                                    wo.remaining_wall_slots(state), survival_wall_missing(state))
        self.assertLessEqual(batch, 3)

    def test_unknown_capacity_is_not_guessed(self):
        state = scene()
        role = builder(state)
        role.back_pack_capability = 0
        self.assertIsNone(wo.inventory_capacity(role))
        self.assertIsNone(wo.free_slots(role))


class NightValueTests(unittest.TestCase):
    def test_keeps_mining_stone_when_no_wall_slot_is_left(self):
        """验收3：没有墙可修但还能安全采石时，必须继续采石留作第二天材料，不提前回防空转。"""
        state = scene()
        base = next(r for r in state.team_our.roles if r.role_type == 'station')
        # 把计划内墙位全部建好，只剩「无墙可修」的情形。
        from test_shop_items import make_role
        for i, (x, y) in enumerate(primary_wall_plan(state, base)):
            state.team_our.roles.append(make_role(300 + i, x, y, 'wall', level=1, health=1000))
        role = builder(state)
        role.backpack = []
        state.decision_events = []
        cmd = opening_wall_work(role, state, blocked_of(state), set(), set(), {})
        self.assertIsNotNone(cmd)
        self.assertIn(cmd['action'], ('move', 'collect'))
        tick = next(e for e in state.decision_events if e['code'] == 'opening_worker_tick')
        self.assertEqual(tick.get('switch_reason'), 'stock_for_day2')

    def test_mixed_plan_is_available_and_scored(self):
        """验收4：可行时能给出「采石 + 修部分额外墙 + 保留石头」的混合计划并打分。"""
        state = scene()
        role = builder(state)
        role.backpack = ['stone'] * 4
        candidates, choice = wo.plan_candidates(state, role, blocked_of(state))
        names = {c['candidate'] for c in candidates}
        self.assertIn('GATHER_ONLY', names)
        self.assertIn('BUILD_FROM_INVENTORY', names)
        self.assertIsNotNone(choice)
        for row in candidates:
            self.assertIn('extra_walls_built', row)
            self.assertIn('stone_carried_to_day2', row)
            self.assertIn('can_return_safely', row)
        mixed = next(c for c in candidates if c['candidate'] == 'BUILD_FROM_INVENTORY')
        self.assertGreater(mixed['extra_walls_built'], 0)

    def test_unsafe_candidates_are_excluded(self):
        late = scene(round_no=69)
        role = builder(late)
        role.backpack = ['stone'] * 4
        _candidates, choice = wo.plan_candidates(late, role, blocked_of(late))
        if choice is not None:
            self.assertTrue(choice['can_return_safely'])


class DeadlineTests(unittest.TestCase):
    def test_deadlines_are_computed_per_role(self):
        state = scene()
        b = blocked_of(state)
        role = builder(state)
        self.assertIsNotNone(wo.return_deadline(state, role, b))
        self.assertEqual(wo.wall_deadline(state, role, b), wo.return_deadline(state, role, b))
        # 升级链路要多算卖矿 + 买券的往返，因此不晚于单纯回防的截止点。
        self.assertLessEqual(wo.upgrade_deadline(state, role, b), wo.return_deadline(state, role, b))

    def test_fits_before_deadline_rejects_overlong_actions(self):
        state = scene()
        b = blocked_of(state)
        role = builder(state)
        self.assertTrue(wo.fits_before_deadline(state, role, b, 1))
        self.assertFalse(wo.fits_before_deadline(state, role, b, 999))


class HandoverTests(unittest.TestCase):
    def test_wall_program_survives_builder_death(self):
        """验收5：建造工阵亡后，最低墙线工作单交给经济工，不随角色消失。"""
        state = scene()
        b = blocked_of(state)
        wo.compute_work_orders(state, b, force=True)
        original = opening_worker_roles(state)['builder']
        self.assertEqual(original, 1)
        builder(state).health = 0
        state.policy_memory.pop('opening_worker_roles', None)
        program = wo.compute_work_orders(state, b, force=True)['wall_program']
        self.assertEqual(program['assigned_builder'], 2)
        self.assertEqual(program['takeover'], 'builder_unavailable')
        self.assertTrue(program['target_wall_slots'])
        self.assertTrue(economist_should_help_wall(state, 40, economist(state)))

    def test_revived_builder_gets_work_not_idle(self):
        """验收5：建造工复活后仍被派活，不因旧任务丢失而白天空转。"""
        state = scene()
        b = blocked_of(state)
        builder(state).health = 0
        state.policy_memory.pop('opening_worker_roles', None)
        wo.compute_work_orders(state, b, force=True)
        revived = builder(state)
        revived.health = 220
        cmd = opening_wall_work(revived, state, blocked_of(state), set(), set(), {})
        self.assertIsNotNone(cmd)


class EconomistTests(unittest.TestCase):
    def test_ordinary_wall_backlog_does_not_conscript_economist(self):
        """验收8：普通扩墙不能把经济工长期变成石工。"""
        state = scene(round_no=10)
        self.assertFalse(economist_should_help_wall(state, 60, economist(state)))

    def test_emergency_defense_preempts_ordinary_economy(self):
        """验收8：真实危险才抢占；普通缺墙不是紧急。"""
        state = scene()
        self.assertFalse(wo.emergency_defense(state)['active'])
        station = next(r for r in state.team_our.roles if r.role_type == 'station')
        station.health = 100
        orders = wo.emergency_defense(state)
        self.assertTrue(orders['active'])
        self.assertIn('station_low_hp', orders['reasons'])
        self.assertTrue(economist_should_help_wall(state, 60, economist(state)))


class FirstUpgradeTests(unittest.TestCase):
    def test_stage_walks_fund_sell_buy_apply_complete(self):
        """验收6：第一张券的闭环阶段必须显式推进，不能停在「升级优先」却不动作。"""
        state = scene()
        b = blocked_of(state)
        state.team_our.gold_num = 0
        self.assertEqual(wo.first_upgrade(state, b)['upgrade_stage'], wo.STAGE_FUND)

        economist(state).backpack = ['copper'] * 30
        state.vendor_shop_list = [ShopItem('copper', 5), ShopItem('iron', 3), ShopItem('stone', 1)]
        self.assertEqual(wo.first_upgrade(state, b)['upgrade_stage'], wo.STAGE_SELL)

        economist(state).backpack = []
        state.team_our.gold_num = 100
        order = wo.first_upgrade(state, b)
        self.assertEqual(order['upgrade_stage'], wo.STAGE_BUY)
        self.assertEqual(order['funding_gap'], 0)
        self.assertIsNotNone(order['upgrade_owner'])
        self.assertIsNotNone(order['upgrade_target_weapon'])

        economist(state).backpack = ['WeaponUpgradeVoucher1']
        order = wo.first_upgrade(state, b)
        self.assertEqual(order['upgrade_stage'], wo.STAGE_APPLY)
        self.assertEqual(order['upgrade_owner'], 2)

        economist(state).backpack = []
        next(r for r in state.team_our.roles if r.role_type == 'rocket').level = 2
        self.assertEqual(wo.first_upgrade(state, b)['upgrade_stage'], wo.STAGE_COMPLETE)

    def test_funding_gap_is_recorded_when_short(self):
        state = scene()
        state.team_our.gold_num = 40
        order = wo.first_upgrade(state, blocked_of(state))
        self.assertEqual(order['funding_gap'], order['cost'] - 40)

    def test_only_one_owner_while_voucher_held(self):
        """已有人持券时，其他角色不得为同一次升级重复买券。"""
        state = scene()
        state.team_our.gold_num = 500
        builder(state).backpack = ['WeaponUpgradeVoucher1']
        order = wo.first_upgrade(state, blocked_of(state))
        self.assertEqual(order['upgrade_stage'], wo.STAGE_APPLY)
        self.assertEqual(order['upgrade_owner'], 1)


class LoggingTests(unittest.TestCase):
    def test_work_order_log_carries_required_fields(self):
        """验收9：每回合 decision log 输出工作单、截止时间和背包容量。"""
        state = scene()
        state.decision_events = []
        from src.agent.opening import plan_opening
        plan_opening(state)
        row = next(e for e in state.decision_events if e['code'] == 'work_orders')
        for field in ('emergency_defense', 'wall_minimum_target', 'wall_feasible_target',
                      'walls_built', 'walls_missing', 'wall_deadline', 'wall_slack',
                      'wall_program_owner', 'wall_program_helper', 'required_stone',
                      'stone_in_inventory_by_role', 'expected_finish_round',
                      'first_upgrade_stage', 'upgrade_owner', 'upgrade_target_weapon',
                      'funding_gap'):
            self.assertIn(field, row, field)
        deadlines = next(e for e in state.decision_events if e['code'] == 'role_deadlines')
        for field in ('return_deadline', 'wall_deadline', 'upgrade_deadline',
                      'inventory_capacity', 'inventory_used', 'free_slots', 'stone_carried'):
            self.assertIn(field, deadlines, field)

    def test_builder_wait_always_has_reason(self):
        """验收9：每个 WAIT 都要带原因。"""
        state = scene()
        role = builder(state)
        role.back_pack_capability = 1
        role.backpack = ['Medicine']       # 背包满、没有石头、也卖不掉道具
        state.map_info.zones = [z for z in state.map_info.zones if z.neutral_type != 'stone']
        state.decision_events = []
        opening_wall_work(role, state, blocked_of(state), set(), set(), {})
        wait = next(e for e in state.decision_events if e['code'] == 'builder_wait')
        self.assertTrue(wait.get('wait_reason'))
        self.assertIn('builder_state', wait)


class ContinuousBuildTests(unittest.TestCase):
    def test_one_mine_trip_builds_multiple_walls(self):
        """验收2：一次石矿往返能连续建多段墙。"""
        state = scene(round_no=20)
        trail = run_opening(state, 50)
        builds = [r for r in trail
                  if r['worker_id'] == 1 and r['action'] == 'build' and r['goal_type'] == 'wall']
        walls = sum(1 for r in state.team_our.roles if r.role_type == 'wall' and r.health > 0)
        self.assertGreaterEqual(walls, 4)
        # 连续两段墙之间不应插入采矿动作的次数占多数
        rows = [r for r in trail if r['worker_id'] == 1 and r['stage'] == STAGE_WALL]
        pairs = [(a, b) for a, b in zip(rows, rows[1:])
                 if a['action'] == 'build' and a['goal_type'] == 'wall']
        consecutive = sum(1 for _a, b in pairs if b['goal_type'] == 'wall')
        self.assertTrue(builds)
        self.assertGreaterEqual(consecutive, 1)


if __name__ == '__main__':
    unittest.main()
