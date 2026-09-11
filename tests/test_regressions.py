"""Regression scenarios based on game invariants, including multi-round feedback."""
import json
from pathlib import Path
import tempfile
import unittest

from src.agent.protocol import MatchState, Pos, Zone
from src.agent.grid import move_towards, astar_next_step
from src.agent.brain import (
    BasicActionValidator, V1Strategy, decide_shop_item_job,
    learn_from_last_round, pick_build_target,
)
from src.agent.server import load_build_memory, save_build_memory
from test_shop_items import minimal_state, make_role


class RegressionTests(unittest.TestCase):
    def test_reaches_alternative_when_closest_interaction_cell_is_enclosed(self):
        blocked = {(4, 2), (2, 0), (2, 1), (2, 2), (3, 0), (3, 2), (4, 0), (4, 1)}
        pos = Pos(0, 2)
        for _ in range(10):
            if max(abs(pos.x - 4), abs(pos.y - 2)) <= 1:
                break
            pos = move_towards(pos, Pos(4, 2), blocked, 6, 5)
            self.assertIsNotNone(pos)
            self.assertNotIn((pos.x, pos.y), blocked)
        self.assertLessEqual(max(abs(pos.x - 4), abs(pos.y - 2)), 1)

    def test_astar_never_enters_blocked_goal(self):
        self.assertIsNone(astar_next_step(Pos(0, 0), Pos(1, 0), {(1, 0)}, 3, 3))

    def test_shared_gold_is_not_spent_twice_or_mutated(self):
        state = minimal_state(gold_num=10)
        state.team_our.roles += [make_role(1, 19, 20, 'pioneer', back_pack_capability=40),
                                 make_role(2, 20, 19, 'pioneer', back_pack_capability=40)]
        commands = V1Strategy(BasicActionValidator()).decide(state)
        buys = [c for c in commands.values() if c['action'] == 'buy']
        self.assertEqual(len(buys), 1)
        self.assertEqual(state.team_our.gold_num, 10)

    def test_only_one_build_for_last_weapon_slot(self):
        state = minimal_state(gold_num=100)
        state.map_info.zones = []
        state.team_our.roles += [make_role(10, 15, 15, 'gatling'), make_role(11, 16, 15, 'railgun'),
                                 make_role(1, 7, 7, 'worker', back_pack_capability=100),
                                 make_role(2, 13, 7, 'worker', back_pack_capability=100)]
        state.worker_build_targets = {1: (8, 8, 'weapon'), 2: (12, 8, 'weapon')}
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(sum(c['action'] == 'build' for c in commands.values()), 1)

    def test_two_pending_builds_cannot_use_same_cell(self):
        state = minimal_state(gold_num=75)
        state.map_info.zones = []
        state.team_our.roles += [make_role(1, 7, 7, 'worker', back_pack_capability=100),
                                 make_role(2, 9, 7, 'worker', back_pack_capability=100)]
        state.worker_build_targets = {1: (8, 8, 'weapon'), 2: (8, 8, 'weapon')}
        commands = V1Strategy(BasicActionValidator()).decide(state)
        self.assertEqual(sum(c['action'] == 'build' for c in commands.values()), 1)

    def test_failed_build_is_type_specific_and_expires(self):
        state = minimal_state()
        state.last_sent_command = {1: {'action': 'build', 'name': 'gatling', 'targetPos': [{'x': 8, 'y': 8}]}}
        state.last_round_role_action_results = {1: False}
        learn_from_last_round(state)
        self.assertIn((8, 8, 'weapon'), state.failed_build_spots)
        self.assertEqual(pick_build_target(state, Pos(10, 10), set(), 'wall'), Pos(8, 8))
        state.round_no += 30
        state.last_sent_command = {}
        learn_from_last_round(state)
        self.assertFalse(state.failed_build_spots)

    def test_unaffordable_job_is_released(self):
        state = minimal_state(gold_num=0)
        role = make_role(1, 19, 20, 'worker', back_pack_capability=100)
        state.team_our.roles.append(make_role(2, 12, 10, 'wall'))
        state.worker_item_jobs[1] = {'item': 'WallFixer', 'target': (12, 10), 'kind': 'wall'}
        self.assertIsNone(decide_shop_item_job(role, state, set(), set()))
        self.assertNotIn(1, state.worker_item_jobs)

    def test_use_failure_retries_and_success_clears_job(self):
        state = minimal_state()
        role = make_role(1, 12, 11, 'worker', backpack=['WallFixer'], back_pack_capability=100)
        state.team_our.roles.append(make_role(2, 12, 10, 'wall'))
        state.worker_item_jobs[1] = {'item': 'WallFixer', 'target': (12, 10), 'kind': 'wall'}
        cmd = decide_shop_item_job(role, state, set(), set())
        state.last_sent_command = {1: cmd}
        state.last_round_role_action_results = {1: False}
        self.assertEqual(decide_shop_item_job(role, state, set(), set()), cmd)
        state.last_round_role_action_results = {1: True}
        role.backpack.clear()
        self.assertIsNone(decide_shop_item_job(role, state, set(), set()))
        self.assertNotIn(1, state.worker_item_jobs)

    def test_validator_rejects_malformed_commands(self):
        state = minimal_state()
        commands = [ {'action': 'typo'}, {'action': 'build', 'targetPos': [{'x': 1, 'y': 1}]},
                     {'action': 'move', 'targetPos': [{'x': 999, 'y': 1}]},
                     {'action': 'move', 'targetPos': [{'x': True, 'y': 1}]},
                     {'action': 'buy', 'name': 'Medicine', 'num': -1},
                     {'action': 'use', 'name': 'WallFixer'} ]
        for cmd in commands:
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                BasicActionValidator().validate(cmd, state)

    def test_restart_preserves_context_and_resets_on_round_rewind(self):
        payload = json.loads((Path(__file__).parent / 'fixtures/sample_match_state.json').read_text(encoding='utf-8'))
        payload['roundNo'] = 100
        state = MatchState()
        state.update(payload)
        state.worker_build_targets[1] = (8, 8, 'weapon')
        state.build_retry_after[(8, 9, 'wall')] = 120
        with tempfile.TemporaryDirectory() as directory:
            save_build_memory(state, Path(directory))
            loaded = MatchState()
            load_build_memory(loaded, Path(directory))
            loaded.update(payload)
            self.assertEqual(loaded.worker_build_targets, state.worker_build_targets)
            self.assertEqual(loaded.build_retry_after, state.build_retry_after)
            payload['roundNo'] = 1
            loaded.update(payload)
            self.assertFalse(loaded.worker_build_targets)
            self.assertFalse(loaded.build_retry_after)
            save_build_memory(loaded, Path(directory))
            again = MatchState()
            load_build_memory(again, Path(directory))
            self.assertFalse(again.worker_build_targets)

    def test_side_change_clears_old_jobs(self):
        payload = json.loads((Path(__file__).parent / 'fixtures/sample_match_state.json').read_text(encoding='utf-8'))
        state = MatchState()
        state.update(payload)
        state.worker_item_jobs[1] = {'item': 'WallFixer', 'target': (1, 1), 'kind': 'wall'}
        payload['teamOur']['type'] = 'changed-side'
        state.update(payload)
        self.assertFalse(state.worker_item_jobs)
