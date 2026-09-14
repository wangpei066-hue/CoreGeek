"""首日阶段与夜间分工；合成地图不代表官方建造区已确认。"""
import unittest

from src.agent.brain import V1Strategy, BasicActionValidator, pick_weapon_name
from src.agent.opening import wall_ring, primary_wall_plan, assign_weapons, safe_wall
from src.agent.grid import build_blocked_set
from src.agent.protocol import Pos, Zone, RobotRole
from test_shop_items import minimal_state, make_role


def opening_state():
    state = minimal_state(round_no=0, gold_num=75)
    state.team_our.roles = [make_role(10, 10, 10, 'station', health=1500, level=1),
                            make_role(1, 9, 9, 'worker', health=220, back_pack_capability=100),
                            make_role(2, 12, 9, 'worker', health=220, back_pack_capability=100),
                            make_role(3, 10, 12, 'pioneer', health=200, back_pack_capability=40)]
    state.map_info.zones = [Zone(Pos(6, 9), 'stone')]
    return state


class OpeningTests(unittest.TestCase):
    def test_wanted_loadout_is_rocket_railgun_gatling(self):
        state = opening_state()
        self.assertEqual(pick_weapon_name(state), 'rocket')
        state.team_our.roles.append(make_role(20, 12, 10, 'rocket', level=1))
        self.assertEqual(pick_weapon_name(state), 'railgun')
        state.team_our.roles.append(make_role(21, 11, 10, 'railgun', level=1))
        self.assertEqual(pick_weapon_name(state), 'gatling')
        self.assertEqual(pick_weapon_name(state, ('gatling',)), 'rocket')

    def test_initial_workers_gather_wall_material_before_weapons(self):
        state = opening_state()
        commands = V1Strategy(BasicActionValidator()).decide(state)
        for role_id in (1, 2):
            self.assertEqual(commands[role_id]['action'], 'move')
        self.assertTrue(any(e['code'] == 'opening_phase' and e['phase'] == '围墙' for e in state.decision_events))
        self.assertNotEqual(commands[1]['targetPos'], commands[2]['targetPos'])
        self.assertEqual(state.team_our.gold_num, 75)

    def test_wall_plan_faces_right_and_leaves_rear_open(self):
        state = opening_state()
        ring = wall_ring(state, state.team_our.roles[0])
        self.assertEqual(len(ring), 17)
        # 后方竖边保持开放；侧墙延伸到最靠后的短射程武器列。
        self.assertFalse(any(x == 9 and 7 < y < 12 for x, y in ring))
        self.assertTrue(all(x == 13 for x, y in ring[:6]))
        self.assertIn((13, 12), ring)

    def test_failed_wall_position_is_not_counted_as_completed(self):
        state = opening_state()
        state.team_our.roles[2].backpack = ['stone'] * 4
        strategy = V1Strategy(BasicActionValidator())
        first = strategy.decide(state)
        state.round_no = 1
        state.last_round_role_action_results = {1: False, 2: False}
        second = strategy.decide(state)
        failed = {(c['targetPos'][0]['x'], c['targetPos'][0]['y'])
                  for c in first.values() if c['action'] == 'build'}
        self.assertTrue(all((c['targetPos'][0]['x'], c['targetPos'][0]['y']) not in failed
                            for c in second.values() if c['action'] == 'build'))
        phase = next(e for e in state.decision_events if e['code'] == 'opening_phase')
        self.assertEqual(phase['weapons'], 0)
        self.assertEqual(phase['phase'], '围墙')
        self.assertEqual(phase['walls_completed'], 0)
        self.assertTrue(failed)

    def test_right_base_faces_left_after_switching_sides(self):
        state = opening_state()
        base = state.team_our.roles[0]
        base.pos = Pos(30, 8)
        line = wall_ring(state, base)
        self.assertEqual(len(line), 17)
        self.assertFalse(any(x == 32 and 5 < y < 10 for x, y in line))
        self.assertTrue(all(x == 28 for x, y in line[:6]))

    def test_cooldown_does_not_assign_same_weapon_twice(self):
        state = opening_state()
        state.round_no = 75
        state.team_our.roles += [make_role(20, 9, 10, 'rocket', cooldown=2, level=1),
                                 make_role(21, 12, 10, 'gatling', level=1),
                                 make_role(22, 10, 11, 'railgun', level=1)]
        V1Strategy(BasicActionValidator()).decide(state)
        allocations = [e['weapon_id'] for e in state.decision_events if e['code'] == 'weapon_assignment']
        self.assertEqual(len(allocations), 3)
        self.assertEqual(len(set(allocations)), 3)

    def test_pioneer_leaves_future_wall(self):
        state = opening_state()
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(commands[3]['action'], 'move')
        target = commands[3]['targetPos'][0]
        self.assertNotIn((target['x'], target['y']), wall_ring(state, state.team_our.roles[0]))

    def test_night_three_different_weapons_and_high_tier_target(self):
        state = opening_state()
        state.round_no = 75
        for i, (kind, x) in enumerate([('gatling', 9), ('railgun', 12), ('rocket', 10)]):
            state.team_our.roles.append(make_role(20+i, x, 10 if i < 2 else 11, kind, level=1, attack_range=20))
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 1),
                             RobotRole(101, Pos(16, 10), 'bossRobot', 800)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        attacks = [c for c in commands.values() if c['action'] == 'attack']
        self.assertEqual(len(attacks), 3)
        self.assertEqual(len({c['controllerId'] for c in attacks}), 3)
        self.assertTrue(all(c['targetPos'] == [{'x': 16, 'y': 10}] for c in attacks))

    def test_complete_opening_on_synthetic_buildable_map(self):
        state = opening_state()
        strategy = V1Strategy(BasicActionValidator())
        wall_built = False
        for turn in range(70):
            state.round_no = turn
            commands = strategy.decide(state)
            results = {}
            for key, cmd in commands.items():
                role = next(r for r in state.team_our.roles if r.id == key)
                results[key] = True
                if cmd['action'] == 'move':
                    role.pos = Pos(**cmd['targetPos'][0])
                elif cmd['action'] == 'collect':
                    role.backpack.append('stone')
                elif cmd['action'] == 'build':
                    if cmd['name'] == 'wall':
                        self.assertEqual(sum(r.role_type in ('gatling', 'railgun', 'rocket') for r in state.team_our.roles), 0)
                        role.backpack.remove('stone')
                        wall_built = True
                    else:
                        self.assertTrue(wall_built)
                        self.assertEqual({(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall'},
                                         set(primary_wall_plan(state, state.team_our.roles[0])))
                        state.team_our.gold_num -= 25
                    pos = cmd['targetPos'][0]
                    state.team_our.roles.append(make_role(100+len(state.team_our.roles), pos['x'], pos['y'], cmd['name'], level=1, attack_range=10))
            state.last_round_role_action_results = results
        self.assertTrue(wall_built)
        assignments = assign_weapons(state)
        self.assertEqual(len(assignments), 3)
        for role in state.team_our.roles:
            if role.id in assignments:
                weapon = assignments[role.id]
                self.assertLessEqual(max(abs(role.pos.x-weapon.pos.x), abs(role.pos.y-weapon.pos.y)), 1)
        walls = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'wall'}
        self.assertEqual(walls, set(primary_wall_plan(state, state.team_our.roles[0])))
        kinds = [r.role_type for r in state.team_our.roles if r.role_type in ('gatling', 'railgun', 'rocket')]
        self.assertEqual(sorted(kinds), ['gatling', 'railgun', 'rocket'])
        rocket_x = next(r.pos.x for r in state.team_our.roles if r.role_type == 'rocket')
        railgun_x = next(r.pos.x for r in state.team_our.roles if r.role_type == 'railgun')
        gatling_x = next(r.pos.x for r in state.team_our.roles if r.role_type == 'gatling')
        self.assertGreaterEqual(rocket_x, railgun_x)
        self.assertGreaterEqual(railgun_x, gatling_x)
