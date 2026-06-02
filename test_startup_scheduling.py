import unittest

from torrent.prioritizer import (
    PHASE_PLAYBACK,
    PHASE_STARTUP,
    PRIO_CRITICAL,
    PRIO_LOW,
    PRIO_MID,
    PRIO_PREFETCH,
    PRIO_SKIP,
    SeekPrioritizer,
)
from utils.settings import SettingsManager


MB = 1024 * 1024


class _FakeFiles:
    def __init__(self, file_offsets, file_sizes):
        self._file_offsets = file_offsets
        self._file_sizes = file_sizes

    def file_offset(self, file_index):
        return self._file_offsets[file_index]

    def file_size(self, file_index):
        return self._file_sizes[file_index]


class _FakeTorrentInfo:
    def __init__(self, piece_length, num_pieces, file_offsets, file_sizes):
        self._piece_length = piece_length
        self._num_pieces = num_pieces
        self._files = _FakeFiles(file_offsets, file_sizes)

    def piece_length(self):
        return self._piece_length

    def num_pieces(self):
        return self._num_pieces

    def files(self):
        return self._files


class _FakeHandle:
    def __init__(self, total_pieces):
        self.priorities = [PRIO_SKIP] * total_pieces
        self.deadlines = []

    def prioritize_pieces(self, priorities):
        self.priorities = list(priorities)

    def piece_priority(self, piece_index, priority=None):
        if priority is None:
            return self.priorities[piece_index]
        self.priorities[piece_index] = priority

    def get_piece_priorities(self):
        return list(self.priorities)

    def set_piece_deadline(self, piece_index, deadline_ms, flags=0):
        self.deadlines.append((piece_index, deadline_ms, flags))


class StartupSchedulingTests(unittest.TestCase):
    def test_startup_buffer_is_fixed_and_independent_from_playback_buffer(self):
        cfg = SettingsManager(buffer_mb=125)

        self.assertEqual(cfg.startup_buffer_mb, 24)
        self.assertEqual(cfg.startup_buffer_bytes, 24 * MB)
        self.assertEqual(cfg.buffer_bytes, 125 * MB)

    def test_prioritizer_uses_tight_startup_phase_then_expands_for_playback(self):
        piece_length = 2 * MB
        file_size = 1024 * MB
        total_pieces = 600
        info = _FakeTorrentInfo(piece_length, total_pieces, [0], [file_size])
        handle = _FakeHandle(total_pieces)

        prioritizer = SeekPrioritizer()
        prioritizer.attach(handle, info, 0)

        self.assertEqual(prioritizer._phase, PHASE_STARTUP)
        self.assertEqual(handle.priorities[0], PRIO_CRITICAL)
        self.assertEqual(handle.priorities[11], PRIO_CRITICAL)
        self.assertEqual(handle.priorities[12], PRIO_PREFETCH)
        self.assertEqual(handle.priorities[19], PRIO_PREFETCH)
        self.assertEqual(handle.priorities[20], PRIO_MID)
        self.assertEqual(handle.priorities[35], PRIO_MID)
        self.assertEqual(handle.priorities[36], PRIO_SKIP)
        self.assertTrue(any(piece == 0 for piece, _, _ in handle.deadlines))

        prioritizer.enter_playback_phase(0)

        self.assertEqual(prioritizer._phase, PHASE_PLAYBACK)
        self.assertEqual(handle.priorities[0], PRIO_CRITICAL)
        self.assertEqual(handle.priorities[15], PRIO_CRITICAL)
        self.assertEqual(handle.priorities[16], PRIO_PREFETCH)
        self.assertEqual(handle.priorities[63], PRIO_PREFETCH)
        self.assertEqual(handle.priorities[64], PRIO_MID)
        self.assertEqual(handle.priorities[300], PRIO_LOW)
        self.assertEqual(handle.priorities[511], PRIO_CRITICAL)


if __name__ == "__main__":
    unittest.main()
