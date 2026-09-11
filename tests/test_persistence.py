from pathlib import Path
import tempfile
import unittest

from src.agent import MatchState, load_build_memory, save_build_memory


class BuildMemoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "state"

    def test_save_then_load_round_trips_failed_spots(self):
        state = MatchState()
        state.failed_build_spots = {(3, 4), (5, 6)}
        save_build_memory(state, self.state_dir)

        loaded = MatchState()
        load_build_memory(loaded, self.state_dir)
        self.assertEqual(loaded.failed_build_spots, {(3, 4), (5, 6)})

    def test_save_then_load_round_trips_worker_build_targets(self):
        state = MatchState()
        state.worker_build_targets = {10010: (12, 24, "wall"), 10012: (9, 20, "weapon")}
        save_build_memory(state, self.state_dir)

        loaded = MatchState()
        load_build_memory(loaded, self.state_dir)
        self.assertEqual(loaded.worker_build_targets, {10010: (12, 24, "wall"), 10012: (9, 20, "weapon")})

    def test_save_then_load_round_trips_last_sent_command(self):
        state = MatchState()
        state.last_sent_command = {10010: {"action": "build", "name": "wall", "targetPos": [{"x": 1, "y": 2}]}}
        save_build_memory(state, self.state_dir)

        loaded = MatchState()
        load_build_memory(loaded, self.state_dir)
        self.assertEqual(
            loaded.last_sent_command,
            {10010: {"action": "build", "name": "wall", "targetPos": [{"x": 1, "y": 2}]}},
        )

    def test_load_is_a_noop_when_nothing_was_ever_saved(self):
        state = MatchState()
        load_build_memory(state, self.state_dir)
        self.assertEqual(state.failed_build_spots, set())
        self.assertFalse(self.state_dir.exists() and any(self.state_dir.iterdir()))

    def test_save_skips_disk_write_when_nothing_worth_remembering(self):
        state = MatchState()
        save_build_memory(state, self.state_dir)
        self.assertFalse(self.state_dir.exists())

    def test_load_only_reads_disk_once_per_process(self):
        state = MatchState()
        state.failed_build_spots = {(1, 1)}
        save_build_memory(state, self.state_dir)

        loaded = MatchState()
        load_build_memory(loaded, self.state_dir)
        self.assertEqual(loaded.failed_build_spots, {(1, 1)})

        loaded.failed_build_spots.add((2, 2))
        save_build_memory(loaded, self.state_dir)
        other_state = MatchState()
        other_state.memory_loaded = True
        load_build_memory(other_state, self.state_dir)
        self.assertEqual(other_state.failed_build_spots, set())

    def test_corrupt_file_does_not_crash_load(self):
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "build_memory.json").write_text("{not valid json", encoding="utf-8")
        state = MatchState()
        load_build_memory(state, self.state_dir)
        self.assertEqual(state.failed_build_spots, set())


if __name__ == "__main__":
    unittest.main()
