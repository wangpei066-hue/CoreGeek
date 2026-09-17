"""验证工人开局和武器分配不会覆盖先锋任务。"""
import unittest

from src.agent.brain import BasicActionValidator, V1Strategy
from src.agent.protocol import PlayerTask, Pos, RobotRole, Zone
from test_opening import opening_state
from test_shop_items import make_role


class WorkerPioneerMergeTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_opening_pioneer_does_not_collect_or_build(self):
        state = opening_state()
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        commands = self.decide(state)
        self.assertIn(3, commands)
        self.assertIn(commands[3]['action'], ('move', 'acceptTask'))
        self.assertNotIn(commands[3]['action'], ('collect', 'build', 'remove'))
        for worker in (1, 2):
            self.assertIn(commands[worker]['action'], ('move', 'build'))
            if commands[worker]['action'] == 'build':
                self.assertEqual(commands[worker]['name'], 'rocket')
        self.assertTrue(any(e['code'] == 'opening_phase' and e['phase'] == '武器' for e in state.decision_events))

    def test_active_task_is_never_abandoned(self):
        """离开任务点任务就失败：即使入夜且工人都阵亡，开拓者也留在任务点。"""
        for round_no in (0, 75):
            with self.subTest(round_no=round_no):
                state = opening_state()
                state.round_no = round_no
                state.phase_task = '请计算1+1'
                state.team_our.roles.append(make_role(20, 9, 10, 'gatling', level=1))
                for worker in state.team_our.roles:
                    if worker.role_type == 'worker':
                        worker.health = 0
                commands = self.decide(state)
                self.assertNotIn(3, commands)

    def test_task_pioneer_keeps_self_healing_during_opening(self):
        state = opening_state()
        state.phase_task = '任务进行中'
        pioneer = state.team_our.roles[-1]
        pioneer.health = 20
        pioneer.backpack = ['Medicine']
        command = self.decide(state)[3]
        self.assertEqual(command['action'], 'use')
        self.assertEqual(command['name'], 'Medicine')

    def test_idle_pioneer_mans_gun_when_workers_cannot_reach(self):
        """没有进行中的任务、工人赶不到时，离得最近的开拓者操炮，不新接任务。"""
        state = opening_state()
        state.round_no = 80
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles.append(make_role(20, 10, 11, 'gatling', level=1, attack_range=20))
        for worker, pos in zip([r for r in state.team_our.roles if r.role_type == 'worker'],
                               (Pos(2, 2), Pos(3, 2))):
            worker.pos = pos
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        commands = self.decide(state)
        allocations = [e for e in state.decision_events if e['code'] == 'weapon_assignment']
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]['role_id'], 3)
        self.assertEqual(commands[20]['controllerId'], '3')
        self.assertFalse(any(c['action'] == 'acceptTask' for c in commands.values()))

    def test_first_two_nights_pioneer_stays_on_guns_even_when_task_is_available(self):
        state = opening_state()
        state.round_no = 80
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles.append(make_role(20, 10, 11, 'gatling', level=1, attack_range=20))
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        commands = self.decide(state)
        self.assertEqual(commands.get(20, {}).get('controllerId'), '3')
        self.assertNotEqual(commands.get(3, {}).get('action'), 'acceptTask')
        self.assertTrue(any(e['code'] == 'night_fixed_defense'
                            for e in state.decision_events))

    def test_pioneer_keeps_task_when_worker_can_cover_two_adjacent_rockets(self):
        state = opening_state()
        state.round_no = 80
        state.phase_task = '任务进行中'
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(9, 10)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(11, 13)
        state.team_our.roles = [
            r for r in state.team_our.roles
            if r.role_type not in ('rocket', 'gatling', 'railgun')
        ]
        state.team_our.roles += [
            make_role(20, 9, 9, 'rocket', level=2, attack_range=20, cooldown=0, health=1000),
            make_role(21, 9, 11, 'rocket', level=2, attack_range=20, cooldown=2, health=1000),
        ]
        state.robot.roles = [RobotRole(100, Pos(28, 10), 'smallRobot', 10)]
        commands = self.decide(state)
        self.assertNotIn(pioneer.id, commands)
        self.assertTrue(any(c.get('controllerId') == str(worker.id) for c in commands.values()))

    def test_after_tasks_done_worker_is_released_to_night_economy(self):
        state = opening_state()
        state.round_no = 80
        state.team_our.gold_num = 0
        state.team_our.player_tasks = []
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(9, 10)
        freed = next(r for r in state.team_our.roles if r.id == 2)
        freed.pos = Pos(7, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
        state.team_our.roles = [
            r for r in state.team_our.roles
            if r.role_type not in ('rocket', 'gatling', 'railgun')
        ]
        state.team_our.roles += [
            make_role(20, 9, 9, 'rocket', level=2, attack_range=20, cooldown=0, health=1000),
            make_role(21, 9, 11, 'rocket', level=2, attack_range=20, cooldown=2, health=1000),
            make_role(22, 11, 12, 'railgun', level=2, attack_range=20, cooldown=0, health=1000),
        ]
        state.robot.roles = [RobotRole(100, Pos(28, 10), 'smallRobot', 10)]
        commands = self.decide(state)
        self.assertIn(freed.id, commands)
        self.assertEqual(commands[freed.id]['action'], 'collect')
        self.assertFalse(any(c.get('controllerId') == str(freed.id) for c in commands.values()))

    def _dual_rocket_night(self):
        state = opening_state()
        state.round_no = 80
        state.team_our.gold_num = 0
        state.team_our.player_tasks = []
        state.team_our.roles = [
            r for r in state.team_our.roles
            if r.role_type not in ('rocket', 'gatling', 'railgun')
        ]
        state.team_our.roles += [
            make_role(20, 9, 9, 'rocket', level=2, attack_range=20, cooldown=0, health=1000),
            make_role(21, 9, 11, 'rocket', level=2, attack_range=20, cooldown=2, health=1000),
            make_role(22, 11, 12, 'railgun', level=2, attack_range=20, cooldown=0, health=1000),
        ]
        state.robot.roles = [RobotRole(100, Pos(28, 10), 'smallRobot', 10)]
        return state

    def test_two_fighters_cover_three_guns_via_dual_rockets(self):
        from src.agent.opening import assign_weapons
        for first, second in ((Pos(9, 10), Pos(8, 8)), (Pos(8, 12), Pos(10, 8)), (Pos(10, 10), Pos(8, 11))):
            state = self._dual_rocket_night()
            w1, w2 = [r for r in state.team_our.roles if r.role_type == 'worker']
            w1.pos, w2.pos = first, second
            pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
            assignment = assign_weapons(state, excluded_ids={pioneer.id})
            kinds = sorted(w.role_type for w in assignment.values())
            self.assertEqual(kinds, ['railgun', 'rocket'], (first, second))

    def test_second_night_releases_worker_even_when_defense_is_due(self):
        state = self._dual_rocket_night()
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(9, 10)
        freed = next(r for r in state.team_our.roles if r.id == 2)
        freed.pos = Pos(7, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
        commands = self.decide(state)
        self.assertIn(freed.id, commands)
        self.assertEqual(commands[freed.id]['action'], 'collect')
        self.assertFalse(any(e['code'] == 'night_worker_release_skipped' for e in state.decision_events))

    def test_first_two_nights_builder_mans_dual_rockets(self):
        """前两夜施工工留守开双火箭，经济工外出；施工工分到的必须是火箭。"""
        for round_no in (80, 210):
            with self.subTest(round_no=round_no):
                state = self._dual_rocket_night()
                state.round_no = round_no
                builder = next(r for r in state.team_our.roles if r.id == 1)
                economist = next(r for r in state.team_our.roles if r.id == 2)
                builder.pos = Pos(9, 10)
                economist.pos = Pos(7, 9)
                pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
                pioneer.pos = Pos(10, 12)
                state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
                commands = self.decide(state)
                released = [e for e in state.decision_events if e['code'] == 'night_worker_released_to_economy']
                self.assertEqual(len(released), 1)
                self.assertEqual(released[0]['role_id'], economist.id)
                assignment = state.policy_memory['weapon_assignment']
                self.assertIn(str(builder.id), assignment)
                self.assertNotIn(str(economist.id), assignment)
                weapon = next(r for r in state.team_our.roles if r.id == int(assignment[str(builder.id)]))
                self.assertEqual(weapon.role_type, 'rocket')
                firing = any(c.get('controllerId') == str(builder.id) for c in commands.values())
                walking = commands.get(builder.id, {}).get('action') == 'move'
                self.assertTrue(firing or walking)

    def test_builder_moves_to_shared_stand_instead_of_idling_on_cooling_rocket(self):
        """贴着冷却火箭但不在共用位时，要迈到共用位去切另一门，不能原地空转。"""
        state = self._dual_rocket_night()
        builder = next(r for r in state.team_our.roles if r.id == 1)
        economist = next(r for r in state.team_our.roles if r.id == 2)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        builder.pos = Pos(8, 9)
        economist.pos = Pos(6, 9)
        pioneer.pos = Pos(10, 12)
        cooling = next(r for r in state.team_our.roles if r.id == 20)
        ready = next(r for r in state.team_our.roles if r.id == 21)
        cooling.cooldown = 2
        ready.cooldown = 0
        state.policy_memory['weapon_assignment'] = {'1': 20, '3': 22}
        state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
        state.robot.roles = [RobotRole(100, Pos(20, 10), 'smallRobot', 40)]
        commands = self.decide(state)
        self.assertNotIn(20, commands)
        fired = commands.get(21, {})
        moved = commands.get(1, {})
        if fired.get('action') == 'attack' and fired.get('controllerId') == '1':
            return
        self.assertEqual(moved.get('action'), 'move', commands)
        dest = moved['targetPos'][0]
        self.assertLessEqual(max(abs(dest['x'] - 9), abs(dest['y'] - 10)), 2)

    def test_day2_builder_does_not_idle_with_stones_and_gaps(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=2),
            make_role(21, 12, 8, 'rocket', level=2),
            make_role(22, 12, 12, 'rocket', level=2),
        ]
        for y in range(7, 13):
            state.team_our.roles.append(make_role(40 + y, 13, y, 'wall', health=1000, level=1))
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = Pos(12, 10)
        builder.backpack = ['stone'] * 8
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(6, 10)
        economist.backpack = []
        state.last_sent_command[1] = {'action': 'move', 'targetPos': [{'x': 11, 'y': 10}]}
        state.last_round_role_action_results[1] = False
        state.worker_build_targets[1] = (8, 7, 'wall')
        commands = self.decide(state)
        cmd = commands.get(1, {})
        self.assertIn(cmd.get('action'), ('build', 'move'))
        if cmd.get('action') == 'build':
            self.assertEqual(cmd.get('name'), 'wall')

    def test_day_builder_with_stone_south_of_base_still_moves_or_builds(self):
        """有石、墙没齐、人不在缺口旁时不能空转（#973 R179 类问题）。"""
        state = opening_state()
        state.round_no = 180
        state.team_our.gold_num = 90
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=2),
            make_role(21, 12, 8, 'rocket', level=2),
            make_role(22, 12, 12, 'railgun', level=2),
        ]
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = Pos(9, 20)
        builder.backpack = ['stone']
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(6, 10)
        economist.backpack = []
        commands = self.decide(state)
        cmd = commands.get(1, {})
        self.assertIn(cmd.get('action'), ('move', 'collect'), cmd)
        self.assertNotEqual(cmd.get('action'), 'build')

    def _day2_guns(self, state):
        state.round_no = 140
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 8, 'rocket', level=2),
            make_role(21, 11, 8, 'rocket', level=2),
            make_role(22, 12, 10, 'railgun', level=2),
        ]
        state.map_info.zones = [Zone(Pos(6, 9), 'stone'), Zone(Pos(4, 11), 'iron'), Zone(Pos(4, 8), 'copper')]
        return state

    def test_day2_builder_on_wall_cell_steps_into_yard(self):
        """站在墙格上要迈进院子，不能迈到墙外再绕回同一格。"""
        state = self._day2_guns(opening_state())
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = Pos(13, 12)
        builder.backpack = ['stone'] * 6
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(4, 11)
        economist.backpack = []
        cmd = self.decide(state).get(1, {})
        self.assertEqual(cmd.get('action'), 'move')
        dest = cmd['targetPos'][0]
        self.assertTrue(9 <= dest['x'] <= 12 and 8 <= dest['y'] <= 11, dest)

    def test_day2_builder_does_not_loop_on_wing_cell(self):
        """#781：施工工不能连续多回合对着同一墙格空转。"""
        state = self._day2_guns(opening_state())
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = Pos(13, 12)
        builder.backpack = ['stone'] * 8
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(4, 11)
        economist.backpack = []
        targets = []
        built = 0
        for turn in range(8):
            state.round_no = 140 + turn
            commands = self.decide(state)
            cmd = commands.get(1, {})
            if cmd.get('action') == 'move':
                dest = (cmd['targetPos'][0]['x'], cmd['targetPos'][0]['y'])
                targets.append(dest)
                builder.pos = Pos(*dest)
            elif cmd.get('action') == 'build' and cmd.get('name') == 'wall':
                pos = cmd['targetPos'][0]
                state.team_our.roles.append(make_role(80 + turn, pos['x'], pos['y'], 'wall', health=1000, level=1))
                builder.backpack.remove('stone')
                built += 1
                targets.append(('build', pos['x'], pos['y']))
            else:
                targets.append(cmd.get('action'))
        self.assertTrue(built >= 1 or len(set(t for t in targets if isinstance(t, tuple) and t[0] != 'build')) >= 2, targets)
        self.assertFalse(all(t == (13, 12) for t in targets if isinstance(t, tuple) and t[0] != 'build'), targets)

    def test_day2_builder_without_stone_goes_to_mine(self):
        state = self._day2_guns(opening_state())
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = Pos(12, 10)
        builder.backpack = []
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(4, 8)
        economist.backpack = []
        cmd = self.decide(state).get(1, {})
        self.assertIn(cmd.get('action'), ('move', 'collect'))
        if cmd.get('action') == 'collect':
            self.assertEqual(cmd['targetPos'][0], {'x': 6, 'y': 9})

    def test_day2_economist_mines_instead_of_wandering_to_walls(self):
        """经济工第一晚后不去跟施工工抢墙/双火箭，空背包就去采矿。"""
        state = self._day2_guns(opening_state())
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = Pos(12, 10)
        builder.backpack = ['stone'] * 6
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(4, 11)
        economist.backpack = []
        cmd = self.decide(state).get(2, {})
        self.assertIn(cmd.get('action'), ('move', 'collect'))
        if cmd.get('action') == 'collect':
            ore = (cmd['targetPos'][0]['x'], cmd['targetPos'][0]['y'])
            self.assertIn(ore, ((4, 11), (4, 8), (6, 9)))

    def test_day2_builder_with_stones_does_not_walk_to_dual_rockets(self):
        """白天还早、手里有石、墙有缺口：施工工必须建或接近缺口，不能去双火箭空转。"""
        state = self._day2_guns(opening_state())
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = Pos(12, 10)
        builder.backpack = ['stone'] * 6
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(4, 11)
        economist.backpack = []
        cmd = self.decide(state).get(1, {})
        self.assertIn(cmd.get('action'), ('build', 'move'))
        if cmd.get('action') == 'build':
            self.assertEqual(cmd.get('name'), 'wall')
        else:
            dest = (cmd['targetPos'][0]['x'], cmd['targetPos'][0]['y'])
            rockets = {(r.pos.x, r.pos.y) for r in state.team_our.roles if r.role_type == 'rocket'}
            self.assertNotIn(dest, rockets)

    def test_builder_extends_extra_walls_when_day_has_spare_rounds(self):
        """生存墙齐了、白天还早：施工工可以补其余 16 段，不能原地空转。"""
        from src.agent.opening import (
            courtyard_cells, extra_wall_missing, survival_wall_plan, worker_should_build_walls,
        )
        state = self._day2_guns(opening_state())
        base = next(r for r in state.team_our.roles if r.role_type == 'station')
        for i, p in enumerate(survival_wall_plan(state, base)):
            state.team_our.roles.append(make_role(200 + i, p[0], p[1], 'wall', health=1000, level=1))
        extra = extra_wall_missing(state)
        self.assertTrue(extra)
        gap = extra[0]
        yard = courtyard_cells(state, base)
        stand = next((Pos(x, y) for x, y in (
            (gap[0] + dx, gap[1] + dy)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1)
            if dx or dy
        ) if (x, y) in yard), Pos(12, 10))
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = stand
        builder.backpack = ['stone'] * 4
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(4, 11)
        economist.backpack = []
        self.assertTrue(worker_should_build_walls(state, builder), (stand, gap, extra))
        cmd = self.decide(state).get(1, {})
        self.assertIn(cmd.get('action'), ('build', 'move', 'collect'), cmd)

    def test_builder_does_not_chase_extra_walls_at_dusk(self):
        """生存墙齐了、入夜窗口：不再追后沿，去回炮或采矿。"""
        from src.agent.opening import extra_wall_missing, survival_wall_plan, worker_should_build_walls
        state = self._day2_guns(opening_state())
        state.round_no = 198
        base = next(r for r in state.team_our.roles if r.role_type == 'station')
        for i, p in enumerate(survival_wall_plan(state, base)):
            state.team_our.roles.append(make_role(200 + i, p[0], p[1], 'wall', health=1000, level=1))
        self.assertTrue(extra_wall_missing(state))
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.pos = Pos(11, 10)
        builder.backpack = ['stone'] * 4
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(4, 11)
        economist.backpack = []
        self.assertFalse(worker_should_build_walls(state, builder))
        cmd = self.decide(state).get(1, {})
        if cmd.get('action') == 'build':
            self.fail('入夜窗口不该去砌侧翼/后沿: %s' % cmd)

    def test_builder_still_builds_after_staged_plan_is_done(self):
        self.test_builder_extends_extra_walls_when_day_has_spare_rounds()

    def test_night_pioneer_never_takes_new_task_and_one_worker_goes_out(self):
        """未清波的夜里开拓者不接新任务、留在守炮名单；是否放工人与任务点可不可接无关。"""
        for round_no in (80, 210, 340):
            with self.subTest(round_no=round_no):
                state = self._dual_rocket_night()
                state.round_no = round_no
                next(r for r in state.team_our.roles if r.id == 1).pos = Pos(9, 10)
                next(r for r in state.team_our.roles if r.id == 2).pos = Pos(7, 9)
                pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
                pioneer.pos = Pos(10, 12)
                state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
                state.robot.roles = [RobotRole(100, Pos(28, 10), 'smallRobot', 10)]
                state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
                commands = self.decide(state)
                self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'acceptTask')
                self.assertIn(str(pioneer.id), state.policy_memory['weapon_assignment'])
                released = [e for e in state.decision_events if e['code'] == 'night_worker_released_to_economy']
                self.assertEqual(len(released), 1)
                self.assertIn(released[0]['role_id'], (1, 2))
                self.assertFalse(any(e['code'] == 'pioneer_task' for e in state.decision_events))

    def test_night_active_task_holds_pioneer_and_keeps_both_workers_on_guns(self):
        state = self._dual_rocket_night()
        state.phase_task = '天黑前已开始的任务'
        next(r for r in state.team_our.roles if r.id == 1).pos = Pos(9, 10)
        next(r for r in state.team_our.roles if r.id == 2).pos = Pos(7, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(11, 14)
        state.map_info.zones = [Zone(Pos(6, 9), 'iron')]
        self.decide(state)
        self.assertTrue(any(e['code'] == 'night_hold_active_task' for e in state.decision_events))
        self.assertFalse(any(e['code'] == 'night_worker_released_to_economy' for e in state.decision_events))
        self.assertNotIn(str(pioneer.id), state.policy_memory.get('weapon_assignment', {}))

    def test_third_night_worker_stays_on_guns_when_defense_is_due(self):
        state = self._dual_rocket_night()
        state.round_no = 270
        freed = next(r for r in state.team_our.roles if r.id == 1)
        freed.pos = Pos(9, 10)
        economy = next(r for r in state.team_our.roles if r.id == 2)
        economy.pos = Pos(7, 9)
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        state.robot.roles = [RobotRole(100, Pos(15, 10), 'smallRobot', 10)]
        freed.backpack = ['stone'] * 6
        economy.backpack = ['stone'] * 6
        commands = self.decide(state)
        repairers = [
            rid for rid in (freed.id, economy.id)
            if rid in commands and commands[rid]['action'] in ('move', 'build')
        ]
        self.assertTrue(repairers)
        self.assertFalse(any(c.get('controllerId') == str(rid) for rid in repairers for c in commands.values()))

    def test_ordinary_voucher_does_not_preempt_feasible_task(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 130
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        from src.agent.protocol import Zone
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(8, 9)
        commands = self.decide(state)
        self.assertIn(commands[pioneer.id]['action'], ('move', 'acceptTask'))
        self.assertNotEqual(commands[pioneer.id]['action'], 'buy')

    def test_pioneer_task_beats_ordinary_wall_upgrade_purchase(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 300
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        from src.agent.protocol import Zone
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(8, 9)
        state.team_our.roles.append(make_role(30, 12, 10, 'wall', level=1, health=1000))
        state.worker_item_jobs[pioneer.id] = {'item': 'WallUpgradeVoucher1', 'target': (12, 10), 'kind': 'wall'}
        commands = self.decide(state)
        self.assertIn(commands[pioneer.id]['action'], ('move', 'acceptTask'))
        self.assertNotEqual(commands[pioneer.id].get('name'), 'WallUpgradeVoucher1')

    def test_worker_buys_voucher_when_pioneer_is_next_to_task(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 130
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 13), 0, 10, 10, True)]
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        from src.agent.protocol import Zone
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(11, 13)
        state.team_our.roles[1].pos = Pos(8, 9)
        commands = self.decide(state)
        self.assertEqual(commands[1]['action'], 'buy')
        self.assertEqual(commands[1]['name'], 'WeaponUpgradeVoucher1')
        self.assertIn(commands[pioneer.id]['action'], ('move', 'acceptTask'))
        self.assertNotEqual(commands.get(pioneer.id, {}).get('action'), 'buy')

    def test_busy_pioneer_does_not_leave_task_to_buy_voucher(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 130
        state.phase_task = '任务进行中'
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=1),
            make_role(21, 12, 8, 'rocket', level=1),
            make_role(22, 12, 12, 'rocket', level=1),
        ]
        from src.agent.protocol import Zone
        state.map_info.zones.append(Zone(Pos(8, 9), 'weaponShop'))
        state.team_our.roles[1].pos = Pos(8, 9)
        commands = self.decide(state)
        self.assertNotEqual(commands.get(3, {}).get('action'), 'buy')
        self.assertEqual(commands[1]['action'], 'buy')
        self.assertEqual(commands[1]['name'], 'WeaponUpgradeVoucher1')

    def test_workers_build_front_walls_early_day(self):
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=2),
            make_role(21, 12, 8, 'rocket', level=2),
            make_role(22, 12, 12, 'rocket', level=2),
        ]
        for worker_id, pos in ((1, Pos(12, 7)), (2, Pos(12, 8))):
            worker = next(r for r in state.team_our.roles if r.id == worker_id)
            worker.backpack = ['stone'] * 8
            worker.pos = pos
        early = self.decide(state)
        self.assertTrue(any(c.get('action') == 'build' and c.get('name') == 'wall' for c in early.values()))
        self.assertFalse(any(e['code'] == 'stones_reserved_for_late_day' for e in state.decision_events))

    def test_workers_build_flanks_when_front_is_sealed(self):
        """正面已齐、白天还早：施工工带着石头应立刻补侧翼，不能空转留石。"""
        state = opening_state()
        state.round_no = 140
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=2),
            make_role(21, 12, 8, 'rocket', level=2),
            make_role(22, 12, 12, 'rocket', level=2),
        ]
        for y in range(7, 13):
            state.team_our.roles.append(make_role(40 + y, 13, y, 'wall', health=1000, level=1))
        for worker_id in (1, 2):
            worker = next(r for r in state.team_our.roles if r.id == worker_id)
            worker.backpack = ['stone'] * 8
            worker.pos = Pos(12, 10)
        early = self.decide(state)
        builder_cmd = early.get(1) or {}
        self.assertIn(builder_cmd.get('action'), ('build', 'move'))
        if builder_cmd.get('action') == 'build':
            self.assertEqual(builder_cmd.get('name'), 'wall')
        self.assertFalse(any(e['code'] == 'stones_reserved_for_late_day' for e in state.decision_events))

    def test_day3_builder_keeps_building_flanks(self):
        state = opening_state()
        state.round_no = 270
        state.team_our.gold_num = 0
        state.team_our.roles += [
            make_role(20, 12, 10, 'rocket', level=2),
            make_role(21, 12, 8, 'rocket', level=2),
            make_role(22, 12, 12, 'rocket', level=2),
        ]
        for y in range(7, 13):
            state.team_our.roles.append(make_role(40 + y, 13, y, 'wall', health=1000, level=1))
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.backpack = ['stone'] * 8
        builder.pos = Pos(12, 8)
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.backpack = []
        economist.pos = Pos(6, 10)
        commands = self.decide(state)
        cmd = commands.get(1, {})
        self.assertIn(cmd.get('action'), ('build', 'move'))
        if cmd.get('action') == 'build':
            self.assertEqual(cmd.get('name'), 'wall')


class PioneerIdleRegressionTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def _day_two_with_tasks(self):
        from test_defense_priority import defended
        state = defended()
        state.round_no = 135
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(14, 14), 0, 10, 10, True),
                                       PlayerTask('自进化类2', Pos(4, 14), 0, 10, 10, True)]
        return state

    def test_teammate_on_pioneer_stand_does_not_block_tasks(self):
        """工人站在开拓者唯一操炮位上时，开拓者不能被判成“回不去、必须回防”而原地打转。"""
        state = self._day_two_with_tasks()
        commands = self.decide(state)
        codes = [e['code'] for e in state.decision_events if e.get('role_id') == 3]
        self.assertNotIn('task_yields_to_defense', codes)
        reservation = state.policy_memory.get('pioneer_task_reservation')
        self.assertIsNotNone(reservation)
        self.assertEqual(commands[3]['action'], 'move')

    def test_unreachable_tasks_do_not_lock_pioneer_out_of_shopping(self):
        from src.agent.brain import self_evolution_work_open
        state = self._day_two_with_tasks()
        state.round_no = 190  # 回防时间不够，任务都会被拒
        self.decide(state)
        self.assertFalse(self_evolution_work_open(state))


class EnRouteMiningTests(unittest.TestCase):
    def _worker_with_voucher(self, round_no):
        from test_defense_priority import defended
        from src.agent.brain import decide_shop_item_job
        from src.agent.grid import build_blocked_set
        state = defended()
        state.round_no = round_no
        state.map_info.zones.append(Zone(Pos(4, 4), 'copper'))
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(4, 5)
        worker.backpack = ['WeaponUpgradeVoucher1']
        rocket = next(r for r in state.team_our.roles if r.role_type == 'rocket')
        state.worker_item_jobs[worker.id] = {
            'item': 'WeaponUpgradeVoucher1', 'target': (rocket.pos.x, rocket.pos.y), 'kind': 'weapon'}
        return decide_shop_item_job(worker, state, build_blocked_set(state), set())

    def test_worker_holds_voucher_and_keeps_working_early_in_the_day(self):
        self.assertIsNone(self._worker_with_voucher(150))  # 不专程回去用，交给后续分支继续干活

    def test_passing_ore_is_collected_on_the_way_to_use_a_voucher(self):
        from src.agent.economy import en_route_collect
        from test_defense_priority import defended
        state = defended()
        state.round_no = 150
        state.map_info.zones.append(Zone(Pos(4, 4), 'copper'))
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(4, 5)
        self.assertEqual(en_route_collect(worker, state, 8, '顺路采矿'),
                         {'action': 'collect', 'targetPos': [{'x': 4, 'y': 4}]})

    def test_worker_goes_home_when_dusk_is_close(self):
        self.assertEqual(self._worker_with_voucher(195)['action'], 'move')


class NightRouteTests(unittest.TestCase):
    def test_night_pioneer_detours_around_robot_to_task(self):
        from src.agent.grid import build_blocked_set, chebyshev
        from src.agent.pioneer_schedule import apply_task_choice
        state = opening_state()
        state.round_no = 80
        pioneer = next(r for r in state.team_our.roles if r.role_type == 'pioneer')
        pioneer.pos = Pos(10, 12)
        robot = RobotRole(100, Pos(10, 15), 'smallRobot', 10)
        state.robot.roles = [robot]
        row = {'x': 10, 'y': 20, 'taskType': '自进化类1', 'inAcceptRange': False}
        ok, cmd = apply_task_choice(pioneer, state, build_blocked_set(state), set(), row)
        self.assertTrue(ok)
        step = cmd['targetPos'][0]
        self.assertGreater(chebyshev(Pos(step['x'], step['y']), robot.pos), 2)

    def test_night_economist_mines_behind_base_not_in_front(self):
        from src.agent.economy import pick_mine
        from src.agent.grid import build_blocked_set
        from src.agent.opening import attack_direction, defense_bounds
        state = opening_state()
        state.round_no = 210
        base = state.team_our.roles[0]
        left, right, _, _ = defense_bounds(state, base)
        direction = attack_direction(state, base)
        front = right if direction == 1 else left
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        worker.pos = Pos(base.pos.x, base.pos.y - 3)  # 离正面矿比离后方矿更近
        front_mine = Zone(Pos(front + 2 * direction, worker.pos.y), 'iron')
        rear_mine = Zone(Pos(left - 4 if direction == 1 else right + 4, worker.pos.y), 'iron')
        state.map_info.zones = [front_mine, rear_mine]
        picked = pick_mine(worker, state, build_blocked_set(state), set(), ('iron',))
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0].pos, rear_mine.pos)

    def test_night_economist_refuses_front_mine_when_no_safe_mine_exists(self):
        from src.agent.economy import pick_mine
        from src.agent.grid import build_blocked_set
        from src.agent.opening import attack_direction, defense_bounds
        state = opening_state()
        state.round_no = 80
        base = state.team_our.roles[0]
        left, right, _, _ = defense_bounds(state, base)
        direction = attack_direction(state, base)
        front = right if direction == 1 else left
        worker = next(r for r in state.team_our.roles if r.role_type == 'worker')
        state.map_info.zones = [Zone(Pos(front + 2 * direction, worker.pos.y), 'iron')]
        self.assertIsNone(pick_mine(worker, state, build_blocked_set(state), set(), ('iron',)))


def _slot_layout_state(round_no, levels=(1, 1, 1), station_level=1):
    """按正式炮位编制布三门炮和整圈单层墙。"""
    from src.agent.opening import wall_ring, weapon_slot_plan
    state = opening_state()
    state.round_no = round_no
    state.team_our.gold_num = 0
    state.team_our.player_tasks = []
    base = state.team_our.roles[0]
    base.level = station_level
    state.team_our.roles = [r for r in state.team_our.roles if r.role_type not in ('rocket', 'gatling', 'railgun')]
    for i, ((name, (x, y)), level) in enumerate(zip(weapon_slot_plan(state, base), levels)):
        state.team_our.roles.append(make_role(20 + i, x, y, name, level=level, attack_range=20, health=1000))
    for i, (x, y) in enumerate(wall_ring(state, base)):
        state.team_our.roles.append(make_role(100 + i, x, y, 'wall', level=1, health=1000))
    return state


class ThirdNightRepairTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def _pressure_state(self, wall_pos):
        state = _slot_layout_state(340)
        state.robot.roles = [RobotRole(100 + i, Pos(16, 8 + i), 'smallRobot', 10) for i in range(4)]
        wall = next(r for r in state.team_our.roles if r.role_type == 'wall' and (r.pos.x, r.pos.y) == wall_pos)
        wall.health = 300
        builder = next(r for r in state.team_our.roles if r.id == 1)
        builder.backpack = ['WallUpgradeVoucher1']
        return state, builder

    def test_builder_repairs_front_wall_from_inside_yard_under_pressure(self):
        state, builder = self._pressure_state((13, 10))
        builder.pos = Pos(12, 10)
        commands = self.decide(state)
        self.assertEqual(commands[builder.id], {'action': 'use', 'name': 'WallUpgradeVoucher1',
                                                'targetPos': [{'x': 13, 'y': 10}]})
        self.assertFalse(any(c.get('controllerId') == str(builder.id) for c in commands.values()))

    def test_builder_goes_mining_when_damaged_wall_is_outside_yard_reach(self):
        """院里够不着残墙时不回去当第三个炮手：两人已守住三炮，这名工人去后院采矿。"""
        state, builder = self._pressure_state((13, 7))  # 角落墙只能从墙外够到
        from src.agent.protocol import Zone
        state.map_info.zones = [Zone(Pos(3, 10), 'iron')]
        commands = self.decide(state)
        self.assertNotIn(str(builder.id), state.policy_memory['weapon_assignment'])
        released = [e['role_id'] for e in state.decision_events if e['code'] == 'night_worker_released_to_economy']
        self.assertEqual(released, [builder.id])
        self.assertNotEqual(commands.get(builder.id, {}).get('action'), 'use')

    def test_upgrade_chain_waits_for_station_after_first_level_three_rocket(self):
        from src.agent.brain import maybe_start_shop_item_job
        # 编制顺序：火箭A、电磁炮、火箭B
        state = _slot_layout_state(150, levels=(3, 1, 2), station_level=1)
        state.team_our.gold_num = 1000
        worker = make_role(1, 5, 5, 'worker', back_pack_capability=100)
        maybe_start_shop_item_job(worker, state)
        self.assertEqual(state.worker_item_jobs[1]['kind'], 'station')


class HeldVoucherAndNightRouteTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def test_held_level_one_voucher_is_used_even_when_chain_wants_level_three(self):
        from src.agent.brain import maybe_start_shop_item_job
        # 编制顺序：火箭A、电磁炮、火箭B；两门火箭已 2 级，升级链下一步要 2 级券
        state = _slot_layout_state(150, levels=(2, 1, 2), station_level=1)
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.backpack = ['WeaponUpgradeVoucher1']
        maybe_start_shop_item_job(worker, state)
        job = state.worker_item_jobs[1]
        self.assertEqual(job['item'], 'WeaponUpgradeVoucher1')
        railgun = next(r for r in state.team_our.roles if r.role_type == 'railgun')
        self.assertEqual(tuple(job['target']), (railgun.pos.x, railgun.pos.y))

    def test_night_gunner_uses_voucher_when_no_target(self):
        state = _slot_layout_state(80)
        state.robot.roles = [RobotRole(100, Pos(40, 31), 'smallRobot', 10)]
        for weapon in state.team_our.roles:
            if weapon.role_type in ('rocket', 'railgun'):
                weapon.attack_range = 3  # 敌人在射程外
        gunner = next(r for r in state.team_our.roles if r.id == 2)
        gunner.backpack = ['WeaponUpgradeVoucher1']
        commands = self.decide(state)
        self.assertEqual(commands[gunner.id]['action'], 'use')
        self.assertEqual(commands[gunner.id]['name'], 'WeaponUpgradeVoucher1')

    def test_night_worker_returning_to_gun_avoids_robot_route(self):
        from src.agent.opening import night_danger_cells
        state = _slot_layout_state(80)
        state.team_our.player_tasks = [PlayerTask('自进化类1', Pos(11, 14), 0, 10, 10, True)]
        worker = next(r for r in state.team_our.roles if r.id == 1)
        worker.pos = Pos(16, 4)  # 在墙外，机器人正从右边压过来
        state.robot.roles = [RobotRole(100, Pos(22, 6), 'smallRobot', 10)]
        commands = self.decide(state)
        step = commands[worker.id]['targetPos'][0]
        self.assertNotIn((step['x'], step['y']), night_danger_cells(state, include_front=False))


class ChainVoucherTests(unittest.TestCase):
    def test_batch_follows_plan_and_counts_team_held_vouchers(self):
        from src.agent.brain import next_upgrade_step, upgrade_batch_size
        state = _slot_layout_state(150, levels=(1, 1, 1))  # 火箭A、电磁炮、火箭B
        self.assertEqual(upgrade_batch_size(state, 'WeaponUpgradeVoucher1'), 2)  # 电磁炮的券等轮到再买
        state = _slot_layout_state(150, levels=(2, 1, 1))
        self.assertEqual(upgrade_batch_size(state, 'WeaponUpgradeVoucher1'), 1)
        # 有人已买了火箭A升3级的券还没用：下一步直接买基地券
        state = _slot_layout_state(150, levels=(2, 1, 2))
        next(r for r in state.team_our.roles if r.id == 2).backpack = ['WeaponUpgradeVoucher2']
        step = next_upgrade_step(state)
        self.assertEqual(step['name'], 'StationUpgradeVoucher1')
        # 基地升好后：火箭B升3级，接着电磁炮 1→2
        state = _slot_layout_state(150, levels=(3, 1, 2), station_level=2)
        self.assertEqual(next_upgrade_step(state)['name'], 'WeaponUpgradeVoucher2')
        state = _slot_layout_state(150, levels=(3, 1, 3), station_level=2)
        self.assertEqual(upgrade_batch_size(state, 'WeaponUpgradeVoucher1'), 1)

    def test_held_voucher_follows_chain_order(self):
        from src.agent.brain import maybe_start_shop_item_job
        state = _slot_layout_state(150, levels=(1, 1, 1))
        railgun = next(r for r in state.team_our.roles if r.role_type == 'railgun')
        worker = make_role(1, railgun.pos.x - 1, railgun.pos.y, 'worker',
                           back_pack_capability=100, backpack=['WeaponUpgradeVoucher1'])
        maybe_start_shop_item_job(worker, state)
        target = tuple(state.worker_item_jobs[1]['target'])
        kind = next(r.role_type for r in state.team_our.roles if (r.pos.x, r.pos.y) == target)
        self.assertEqual(kind, 'rocket')  # 就站在电磁炮旁也先按顺序升火箭


class FixtureNightMinerTests(unittest.TestCase):
    """用真实请求样例：任务点可接也不影响放人；带石头的经济工夜里不去正面缺口（不能建造），而是去基地后方采矿。"""

    def _state(self, round_no):
        import json
        from pathlib import Path
        from src.agent.protocol import MatchState
        payload = json.loads((Path(__file__).parent / 'fixtures/sample_match_state.json').read_text(encoding='utf-8'))
        payload['roundNo'] = round_no
        for r in payload['teamOur']['roles']:
            if r['roleType'] == 'gatling':
                r.update(roleType='rocket', level=1, cooldown=0, attackRange=10)
            if r['id'] == 10010:
                r['pos'] = {'x': 8, 'y': 24}
            if r['id'] == 10012:
                r['pos'] = {'x': 11, 'y': 26}
        payload['teamOur']['playerTasks'] = [{
            'taskType': '自进化类1', 'taskPosition': {'x': 14, 'y': 14}, 'coldDownRounds': 0,
            'scoreReward': 10, 'goldReward': 10, 'isValid': True,
        }]
        payload['robot'] = {'roles': [{'id': 1, 'pos': {'x': 30, 'y': 10}, 'roleType': 'smallRobot',
                                       'health': 40, 'targetTeam': ''}]}
        state = MatchState()
        state.update(payload)
        return state

    def test_released_worker_heads_to_rear_mine(self):
        for round_no in (85, 215):
            with self.subTest(round_no=round_no):
                state = self._state(round_no)
                V1Strategy(BasicActionValidator()).decide(state)
                codes = [e['code'] for e in state.decision_events if e['role_id'] == 10012]
                self.assertIn('night_worker_released_to_economy', codes)
                self.assertIn('income_mine', codes)
                self.assertNotIn('emergency_front_seal', codes)


class NightPressureRecallTests(unittest.TestCase):
    """敌人逼近时叫外出工人回防：前两夜不叫；第三夜起只有带着能在家用上的道具才叫。"""

    def _state(self, round_no, backpack):
        state = _slot_layout_state(round_no)
        state.map_info.zones = [Zone(Pos(3, 10), 'iron'), Zone(Pos(4, 6), 'stone')]
        eco = next(r for r in state.team_our.roles if r.id == 2)
        eco.pos = Pos(4, 10)  # 已在后院矿旁
        eco.backpack = list(backpack)
        state.policy_memory['night_released_worker'] = eco.id
        # 6 个机器人在基地 7 格内：pressure 成立；守炮两人还没站到炮位旁。
        state.robot.roles = [RobotRole(1000 + i, Pos(15, 7 + i), 'smallRobot', 40) for i in range(6)]
        return state, eco

    def _decide_released(self, state, eco):
        from src.agent.tactics import pressure
        self.assertTrue(pressure(state))
        commands = V1Strategy(BasicActionValidator()).decide(state)
        codes = {e['code'] for e in state.decision_events if e.get('role_id') == eco.id}
        return commands.get(eco.id), codes

    def test_first_two_nights_never_recall(self):
        for round_no in (80, 210):
            for backpack in ([], ['WallFixer']):
                with self.subTest(round_no=round_no, backpack=backpack):
                    state, eco = self._state(round_no, backpack)
                    cmd, codes = self._decide_released(state, eco)
                    self.assertIn('night_worker_released_to_economy', codes)
                    self.assertNotIn('night_worker_release_uncovered', codes)
                    self.assertEqual((cmd or {}).get('action'), 'collect', cmd)

    def test_third_night_empty_handed_stays_out(self):
        for backpack in ([], ['Medicine']):
            with self.subTest(backpack=backpack):
                state, eco = self._state(340, backpack)
                cmd, codes = self._decide_released(state, eco)
                self.assertNotIn('night_worker_release_uncovered', codes)
                self.assertEqual((cmd or {}).get('action'), 'collect', cmd)

    def test_third_night_recalls_worker_carrying_defense_item(self):
        state, eco = self._state(340, ['WallFixer'])
        cmd, codes = self._decide_released(state, eco)
        self.assertIn('night_worker_release_uncovered', codes)
        self.assertEqual((cmd or {}).get('action'), 'move', cmd)
        self.assertGreater(cmd['targetPos'][0]['x'], eco.pos.x)  # 往基地方向走


class NightTwoOnThreeSimulationTests(unittest.TestCase):
    """正式炮位布局下连跑一段夜战：机器人逼近并贴墙开打，也始终两人三炮、一名工人在后院。"""

    def _run(self, night_start, rounds=20):
        import contextlib
        import io
        from src.agent.protocol import Zone
        state = _slot_layout_state(night_start)
        state.map_info.zones = [Zone(Pos(3, 10), 'iron'), Zone(Pos(4, 6), 'stone')]
        state.robot.roles = [RobotRole(1000 + i, Pos(26 + i // 9, 6 + i % 9), 'smallRobot', 40) for i in range(35)]
        people = {r.id: r for r in state.team_our.roles if r.role_type in ('worker', 'pioneer')}
        base_x = next(r.pos.x for r in state.team_our.roles if r.role_type == 'station')
        strategy = V1Strategy(BasicActionValidator())
        released = []
        for _ in range(rounds):
            with contextlib.redirect_stderr(io.StringIO()):
                commands = strategy.decide(state)
            ids = [e['role_id'] for e in state.decision_events if e['code'] == 'night_worker_released_to_economy']
            released.append(ids[0] if ids else None)
            self.assertFalse(any(c.get('action') == 'acceptTask' for c in commands.values()))
            occupied = {(r.pos.x, r.pos.y) for r in state.team_our.roles}
            for pid, person in people.items():
                cmd = commands.get(pid)
                if cmd and cmd['action'] == 'move':
                    person.pos = Pos(cmd['targetPos'][0]['x'], cmd['targetPos'][0]['y'])
            for robot in state.robot.roles:
                if (robot.pos.x - 1, robot.pos.y) not in occupied and robot.pos.x - 1 > base_x + 1:
                    robot.pos = Pos(robot.pos.x - 1, robot.pos.y)
            for weapon in state.team_our.roles:
                if weapon.role_type == 'rocket':
                    weapon.cooldown = 4 if weapon.id in commands else max(0, (weapon.cooldown or 0) - 1)
            state.last_sent_command = commands
            state.round_no += 1
        return released

    def test_one_worker_stays_out_all_wave(self):
        for night_start in (70, 200, 330):
            with self.subTest(night_start=night_start):
                released = self._run(night_start)
                steady = released[2:]
                self.assertTrue(all(r is not None for r in steady), released)
                self.assertEqual(len(set(steady)), 1, released)


class NightMinerSafetyTests(unittest.TestCase):
    def decide(self, state):
        return V1Strategy(BasicActionValidator()).decide(state)

    def _night(self):
        state = _slot_layout_state(80)
        state.map_info.zones.append(Zone(Pos(5, 10), 'iron'))
        state.robot.roles = [RobotRole(100, Pos(28, 10), 'smallRobot', 10)]
        return state

    def test_economist_in_danger_zone_is_not_released(self):
        state = self._night()
        economist = next(r for r in state.team_our.roles if r.id == 2)
        economist.pos = Pos(18, 10)  # 入夜时还在正面外、机器人来的方向上
        self.decide(state)
        codes = [e['code'] for e in state.decision_events if e.get('role_id') == economist.id]
        self.assertIn('night_worker_release_unsafe', codes)
        self.assertNotIn('night_worker_released_to_economy', codes)
        self.assertIn(str(economist.id), state.policy_memory['weapon_assignment'])  # 按守炮的人撤回

    def test_released_worker_never_takes_an_unsafe_route(self):
        from src.agent.grid import build_blocked_set
        from src.agent.opening import night_safe_path
        state = self._night()
        worker = next(r for r in state.team_our.roles if r.id == 2)
        worker.pos = Pos(6, 10)
        target = Pos(20, 10)  # 只能穿过正面才能到
        blocked = build_blocked_set(state)
        self.assertIsNotNone(night_safe_path(worker, target, blocked, state))  # 守炮的人会退回普通路线
        state.night_released_ids = {worker.id}
        self.assertIsNone(night_safe_path(worker, target, blocked, state))  # 外出的人不走危险路线
