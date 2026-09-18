import unittest

from src.agent.brain import V1Strategy, BasicActionValidator, maintain_front_wall_health
from src.agent.economy import (
    BUILDER_HOME_RADIUS, BUILDER_SELL_VALUE, builder_home_economy, builder_home_mine,
    builder_stone_need, profitable_mine,
)
from src.agent.grid import build_blocked_set, chebyshev
from src.agent.opening_schedule import opening_worker_mode
from src.agent.protocol import Pos, Zone, ShopItem
from tests.test_worker_pioneer_merge import _slot_layout_state

DAY4 = 3 * 130 + 5          # 第四天白天早段
DAY4_DUSK = 3 * 130 + 64    # 第四天入夜前


def _state(round_no=DAY4, zones=()):
    st = _slot_layout_state(round_no, levels=(3, 3, 3), station_level=2)  # 整圈墙已齐，基地 (10,10)
    st.vendor_shop_list = [ShopItem('stone', 1), ShopItem('iron', 3), ShopItem('copper', 5)]
    st.map_info.zones = [Zone(Pos(20, 16), 'vendor'), Zone(Pos(25, 20), 'weaponShop')] + list(zones)
    builder = next(r for r in st.team_our.roles if r.role_type == 'worker'
                   and opening_worker_mode(st, r) == 'builder')
    builder.backpack = []
    return st, builder


def _target(cmd):
    return Pos(cmd['targetPos'][0]['x'], cmd['targetPos'][0]['y'])


class BuilderHomeEconomyTests(unittest.TestCase):
    def test_no_stone_mining_once_walls_complete(self):
        st, builder = _state(zones=[Zone(Pos(5, 9), 'stone')])
        builder.backpack = ['stone'] * 3
        self.assertEqual(builder_stone_need(st), 0)
        self.assertIsNone(profitable_mine(builder, st, build_blocked_set(st), set()))
        self.assertIsNone(builder_home_mine(builder, st, build_blocked_set(st), set()))

    def test_stone_wanted_when_wall_missing(self):
        st, builder = _state(zones=[Zone(Pos(5, 9), 'stone')])
        wall = next(r for r in st.team_our.roles if r.role_type == 'wall')
        st.team_our.roles.remove(wall)
        self.assertGreater(builder_stone_need(st), 0)
        self.assertIsNotNone(builder_home_mine(builder, st, build_blocked_set(st), set()))

    def test_prefers_near_copper_over_far(self):
        near, far = Pos(5, 4), Pos(30, 28)
        st, builder = _state(zones=[Zone(far, 'copper'), Zone(near, 'copper')])
        cmd = builder_home_mine(builder, st, build_blocked_set(st), set())
        self.assertIsNotNone(cmd)
        self.assertLess(chebyshev(_target(cmd), near), chebyshev(_target(cmd), far))

    def test_far_mine_ignored_and_builder_holds_at_home(self):
        st, builder = _state(zones=[Zone(Pos(35, 28), 'copper')])  # 离基地 25 格
        self.assertIsNone(builder_home_mine(builder, st, build_blocked_set(st), set()))
        cmd = V1Strategy(BasicActionValidator()).decide(st).get(builder.id)
        base = st.team_our.roles[0].pos
        if cmd is not None and cmd['action'] == 'move':
            self.assertLessEqual(chebyshev(_target(cmd), base), 3)

    def test_wide_radius_only_when_time_allows(self):
        mine = Pos(10, 25)  # 离基地 15 格：超出近圈、在放宽圈内
        st, builder = _state(zones=[Zone(mine, 'copper')])
        self.assertGreater(chebyshev(mine, st.team_our.roles[0].pos), BUILDER_HOME_RADIUS)
        self.assertIsNotNone(builder_home_mine(builder, st, build_blocked_set(st), set()))
        st, builder = _state(round_no=DAY4_DUSK - 20, zones=[Zone(mine, 'copper')])
        self.assertIsNone(builder_home_mine(builder, st, build_blocked_set(st), set()))

    def test_sells_after_full_batch(self):
        st, builder = _state(zones=[Zone(Pos(5, 4), 'copper')])
        builder.backpack = ['copper'] * (BUILDER_SELL_VALUE // 5)
        cmd = builder_home_economy(builder, st, build_blocked_set(st), set())
        self.assertEqual(cmd['action'], 'move')
        # 走的是去小贩的变现路线（第一步可能先从后门出院子）
        self.assertTrue(any(e['code'] == 'cashout_priority' for e in st.decision_events))

    def test_small_batch_keeps_mining(self):
        st, builder = _state(zones=[Zone(Pos(5, 4), 'copper')])
        builder.backpack = ['copper'] * 3
        cmd = builder_home_economy(builder, st, build_blocked_set(st), set())
        vendor = Pos(20, 16)
        self.assertFalse(cmd['action'] == 'move' and chebyshev(_target(cmd), vendor) < chebyshev(builder.pos, vendor)
                         and chebyshev(_target(cmd), Pos(5, 4)) > chebyshev(builder.pos, Pos(5, 4)))

    def test_no_shop_trip_for_wall_item_at_dusk(self):
        st, builder = _state(round_no=DAY4_DUSK)
        wall = next(r for r in st.team_our.roles if r.role_type == 'wall')
        st.team_our.gold_num = 100
        st.worker_item_jobs[builder.id] = {'item': 'WallUpgradeVoucher1', 'target': (wall.pos.x, wall.pos.y),
                                           'kind': 'wall'}
        self.assertIsNone(maintain_front_wall_health(builder, st, build_blocked_set(st), set()))
        self.assertTrue(any(e['code'] == 'wall_job_shop_too_late' for e in st.decision_events))


if __name__ == '__main__':
    unittest.main()
