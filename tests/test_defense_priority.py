import tempfile
import unittest
from pathlib import Path

from src.agent.brain import V1Strategy, BasicActionValidator
from src.agent.opening import weapon_candidates, wall_ring, primary_wall_plan, active_wall_plan, outer_wall_ready
from src.agent.protocol import Pos, RobotRole, Zone
from src.agent.task_solver import PioneerTaskSolver
from test_opening import opening_state
from test_shop_items import make_role


def defended():
    state = opening_state()
    state.round_no = 140
    for i, kind in enumerate(('gatling', 'railgun', 'rocket')):
        state.team_our.roles.append(make_role(20+i, 9+i, 8, kind, health=1000, level=1, attack_range=20))
    return state


class DefensePriorityTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_three_controllers_at_night_despite_active_task(self):
        state = defended()
        state.round_no = 200
        state.phase_task = '仍在解题'
        for role, pos in zip(state.team_our.roles[1:4], (Pos(8, 8), Pos(10, 7), Pos(12, 8))):
            role.pos = pos
        state.robot.roles = [RobotRole(100, Pos(16, 8), 'largeRobot', 100)]
        commands = self.decide(state)
        self.assertEqual({c['controllerId'] for c in commands.values() if c['action'] == 'attack'}, {'1', '2', '3'})
        with tempfile.TemporaryDirectory() as root:
            solver = PioneerTaskSolver(Path(root))
            before = dict(commands)
            self.assertEqual(solver.step(state, commands), ('', ''))
            self.assertEqual(commands, before)

    def test_dusk_task_pioneer_returns_before_night(self):
        state = defended()
        state.round_no = 180
        state.phase_task = '仍在解题'
        state.team_our.roles[3].pos = Pos(25, 10)
        commands = self.decide(state)
        self.assertEqual(commands[3]['action'], 'move')
        self.assertLess(commands[3]['targetPos'][0]['x'], 25)

    def test_second_day_missing_outer_walls_generates_stone_task(self):
        state = defended()
        base = state.team_our.roles[0]
        # 已有内层，但外层尚未建好。
        for i, (x, y) in enumerate(wall_ring(state, base)[:6]):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1000, level=1))
        commands = self.decide(state)
        self.assertTrue(any(e['code'] == 'persistent_wall_plan' for e in state.decision_events))
        self.assertTrue(all(commands[i]['action'] in ('move', 'collect') for i in (1, 2)))

    def test_small_inventory_sold_early_before_dusk(self):
        state = defended()
        for i, (x, y) in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1000, level=1))
        state.round_no = 152
        worker = state.team_our.roles[1]
        worker.backpack = ['iron']
        state.map_info.zones.append(Zone(Pos(8, 9), 'vendor'))
        self.assertEqual(self.decide(state)[1], {'action': 'sell', 'name': 'iron', 'num': 1})

    def test_weapon_positions_are_range_ordered_on_both_sides(self):
        state = defended()
        base = state.team_our.roles[0]
        for x, direction in ((10, 1), (30, -1)):
            base.pos = Pos(x, 10)
            positions = [weapon_candidates(state, base, kind)[0][0]*direction for kind in ('gatling', 'railgun', 'rocket')]
            self.assertLess(positions[0], positions[1])
            self.assertLess(positions[1], positions[2])

    def test_primary_upgrades_precede_outer_construction(self):
        state = defended()
        for i, (x, y) in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1000, level=1))
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.team_our.roles[1].backpack = ['stone'] * 4
        commands = self.decide(state)
        self.assertFalse(outer_wall_ready(state))
        self.assertEqual(commands[1], {'action': 'buy', 'name': 'WallUpgradeVoucher1', 'num': 1})
        self.assertEqual(active_wall_plan(state, state.team_our.roles[0]), primary_wall_plan(state, state.team_our.roles[0]))

    def test_outer_unlock_requires_complete_upgraded_healthy_primary_and_weapons(self):
        state = defended()
        for r in state.team_our.roles:
            if r.role_type in ('gatling', 'railgun', 'rocket'):
                r.level = 2
        for i, (x, y) in enumerate(primary_wall_plan(state, state.team_our.roles[0])):
            state.team_our.roles.append(make_role(100+i, x, y, 'wall', health=1500, level=2))
        self.assertTrue(outer_wall_ready(state))
        self.assertEqual(active_wall_plan(state, state.team_our.roles[0]), wall_ring(state, state.team_our.roles[0]))
        state.team_our.roles[-1].health = 100
        self.assertFalse(outer_wall_ready(state))

    def test_breach_uses_bomb_against_single_robot_before_healing(self):
        state = defended()
        wall = make_role(100, 13, 10, 'wall', health=1000, level=1)
        state.team_our.roles.append(wall)
        self.decide(state)
        state.team_our.roles.remove(wall)
        state.round_no = 200
        state.robot.roles = [RobotRole(900, Pos(13, 10), 'smallRobot', 40)]
        state.team_our.roles[1].backpack = ['Bomb', 'Medicine']
        state.team_our.roles[1].health = 30
        commands = self.decide(state)
        self.assertEqual(commands[1]['name'], 'Bomb')
        self.assertTrue(any(e['code'] == 'front_breached' for e in state.decision_events))

    def test_breach_spends_gold_on_bomb_at_adjacent_shop(self):
        state = defended()
        wall = make_role(100, 13, 10, 'wall', health=1000, level=1)
        state.team_our.roles.append(wall)
        self.decide(state)
        state.team_our.roles.remove(wall)
        state.round_no = 200
        state.team_our.gold_num = 100
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.robot.roles = [RobotRole(900, Pos(13, 10), 'smallRobot', 40)]
        self.assertEqual(self.decide(state)[1], {'action': 'buy', 'name': 'Bomb', 'num': 1})
