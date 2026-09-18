import unittest

from src.agent.brain import (
    BasicActionValidator, V1Strategy, FIXER_STOCK_TARGET, BOMB_STOCK_TARGET,
    economist_wall_watch, maybe_start_shop_item_job, night_wall_watcher, _batch_buy_quantity,
)
from src.agent.grid import build_blocked_set, chebyshev
from src.agent.opening import defense_bounds, attack_direction
from src.agent.protocol import Pos, RobotRole
from tests.test_worker_pioneer_merge import _slot_layout_state

DAY5_NIGHT = 4 * 130 + 75
DAY4_NIGHT = 3 * 130 + 75
DAY5_DAY = 4 * 130 + 20
DAY6_DAY = 5 * 130 + 20


def _economist(state):
    return next(r for r in state.team_our.roles if r.id == 2)


def _front_walls(state):
    base = state.team_our.roles[0]
    left, right, _, _ = defense_bounds(state, base)
    front = right if attack_direction(state, base) == 1 else left
    return base, front, [r for r in state.team_our.roles if r.role_type == 'wall' and r.pos.x == front]


def _robots_facing(state, wall, n=4, kind='smallRobot'):
    base, front, _ = _front_walls(state)
    step = attack_direction(state, base)
    return [RobotRole(900 + i, Pos(wall.pos.x + step * 2, wall.pos.y - 1 + i % 3), kind, 40) for i in range(n)]


class NightWallWatchTests(unittest.TestCase):
    def test_day5_economist_with_fixers_stays_home_not_released(self):
        state = _slot_layout_state(DAY5_NIGHT, levels=(3, 3, 3), station_level=3)
        econ = _economist(state)
        econ.backpack = ['WallFixer'] * 6
        _, _, front = _front_walls(state)
        far = front[len(front) // 2]
        base, _, _ = _front_walls(state)
        state.robot.roles = [RobotRole(900, Pos(far.pos.x + 12 * attack_direction(state, base), far.pos.y),
                                       'smallRobot', 40)]
        self.assertEqual(night_wall_watcher(state, build_blocked_set(state)).id, econ.id)
        cmd = V1Strategy(BasicActionValidator()).decide(state).get(econ.id)
        # 不外出采矿：要么原地墙边待命（无指令），要么在院里走向正面墙
        self.assertTrue(cmd is None or cmd['action'] == 'move', cmd)
        if cmd is not None:
            step = Pos(cmd['targetPos'][0]['x'], cmd['targetPos'][0]['y'])
            self.assertLess(min(chebyshev(step, w.pos) for w in front), chebyshev(econ.pos, far.pos) + 1)

    def test_day4_economist_not_watching(self):
        state = _slot_layout_state(DAY4_NIGHT, levels=(3, 3, 3), station_level=3)
        _economist(state).backpack = ['WallFixer'] * 6
        self.assertIsNone(night_wall_watcher(state, build_blocked_set(state)))

    def test_no_items_no_watch(self):
        state = _slot_layout_state(DAY5_NIGHT, levels=(3, 3, 3), station_level=3)
        _economist(state).backpack = []
        self.assertIsNone(night_wall_watcher(state, build_blocked_set(state)))

    def test_uses_fixer_on_low_wall_under_fire(self):
        state = _slot_layout_state(DAY5_NIGHT, levels=(3, 3, 3), station_level=3)
        econ = _economist(state)
        econ.backpack = ['WallFixer'] * 3
        _, _, front = _front_walls(state)
        wall = min(front, key=lambda w: chebyshev(w.pos, econ.pos))
        wall.health = 200
        state.robot.roles = _robots_facing(state, wall)
        handled, cmd = economist_wall_watch(econ, state, build_blocked_set(state), set())
        self.assertTrue(handled)
        if chebyshev(econ.pos, wall.pos) <= 1:
            self.assertEqual(cmd['action'], 'use')
            self.assertEqual(cmd['name'], 'WallFixer')
            self.assertEqual(cmd['targetPos'], [{'x': wall.pos.x, 'y': wall.pos.y}])
        else:
            self.assertEqual(cmd['action'], 'move')

    def test_adjacent_watcher_fixes_immediately(self):
        state = _slot_layout_state(DAY5_NIGHT, levels=(3, 3, 3), station_level=3)
        econ = _economist(state)
        econ.backpack = ['WallFixer']
        base, front_x, front = _front_walls(state)
        wall = front[len(front) // 2]
        occupied = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.id != econ.id}
        stand = next((front_x - attack_direction(state, base), wall.pos.y + dy) for dy in (0, -1, 1)
                     if (front_x - attack_direction(state, base), wall.pos.y + dy) not in occupied)
        econ.pos = Pos(*stand)
        wall.health = 300
        state.robot.roles = _robots_facing(state, wall)
        handled, cmd = economist_wall_watch(econ, state, build_blocked_set(state), set())
        self.assertTrue(handled)
        self.assertEqual(cmd['action'], 'use')
        self.assertEqual(cmd['name'], 'WallFixer')

    def test_healthy_wall_not_fixed(self):
        state = _slot_layout_state(DAY5_NIGHT, levels=(3, 3, 3), station_level=3)
        econ = _economist(state)
        econ.backpack = ['WallFixer']
        _, _, front = _front_walls(state)
        wall = front[len(front) // 2]
        state.robot.roles = _robots_facing(state, wall)
        handled, cmd = economist_wall_watch(econ, state, build_blocked_set(state), set())
        self.assertTrue(handled)
        self.assertNotEqual((cmd or {}).get('action'), 'use')

    def test_first_contact_bomb_then_hold_rest(self):
        state = _slot_layout_state(DAY5_NIGHT + 130, levels=(3, 3, 3), station_level=3)
        state.team_our.roles[0].health = 4500  # 基地满血，排除“基地低血=高压”
        econ = _economist(state)
        econ.backpack = ['Bomb', 'Bomb']
        _, _, front = _front_walls(state)
        wall = front[len(front) // 2]
        state.robot.roles = _robots_facing(state, wall, n=6)
        handled, cmd = economist_wall_watch(econ, state, build_blocked_set(state), set())
        self.assertEqual(cmd['action'], 'use')
        self.assertEqual(cmd['name'], 'Bomb')
        # 同一夜再次接敌、没有高压：第二颗留着
        state.bombed_robots = set()
        state.robot.roles = _robots_facing(state, wall, n=2)
        handled, cmd = economist_wall_watch(econ, state, build_blocked_set(state), set())
        self.assertNotEqual((cmd or {}).get('name'), 'Bomb')


class NightStockTests(unittest.TestCase):
    def test_day5_stocks_fixers_to_target(self):
        state = _slot_layout_state(DAY5_DAY, levels=(3, 3, 3), station_level=3)
        state.team_our.gold_num = 200
        econ = _economist(state)
        econ.backpack = []
        state.worker_item_jobs.pop(econ.id, None)
        maybe_start_shop_item_job(econ, state)
        job = state.worker_item_jobs.get(econ.id)
        self.assertIsNotNone(job)
        self.assertEqual(job['item'], 'WallFixer')
        self.assertTrue(job['stock_for_night'])
        self.assertEqual(_batch_buy_quantity(econ, state, job), FIXER_STOCK_TARGET)

    def test_day6_stocks_bombs_after_fixers(self):
        state = _slot_layout_state(DAY6_DAY, levels=(3, 3, 3), station_level=3)
        state.team_our.gold_num = 300
        econ = _economist(state)
        econ.backpack = ['WallFixer'] * FIXER_STOCK_TARGET
        state.worker_item_jobs.pop(econ.id, None)
        maybe_start_shop_item_job(econ, state)
        job = state.worker_item_jobs.get(econ.id)
        self.assertIsNotNone(job)
        self.assertEqual(job['item'], 'Bomb')
        self.assertEqual(_batch_buy_quantity(econ, state, job), BOMB_STOCK_TARGET)

    def test_day5_no_bombs_yet(self):
        state = _slot_layout_state(DAY5_DAY, levels=(3, 3, 3), station_level=3)
        state.team_our.gold_num = 300
        econ = _economist(state)
        econ.backpack = ['WallFixer'] * FIXER_STOCK_TARGET
        state.worker_item_jobs.pop(econ.id, None)
        maybe_start_shop_item_job(econ, state)
        job = state.worker_item_jobs.get(econ.id) or {}
        self.assertNotEqual(job.get('item'), 'Bomb')


if __name__ == '__main__':
    unittest.main()
