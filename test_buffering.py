import unittest

from torrent.buffering import BufferMonitor


class _FakeFiles:
    def __init__(self, file_offsets, file_sizes):
        self._file_offsets = file_offsets
        self._file_sizes = file_sizes

    def file_offset(self, file_index):
        return self._file_offsets[file_index]

    def file_size(self, file_index):
        return self._file_sizes[file_index]


class _FakeTorrentInfo:
    def __init__(self, piece_length, file_offsets, file_sizes):
        self._piece_length = piece_length
        self._files = _FakeFiles(file_offsets, file_sizes)

    def piece_length(self):
        return self._piece_length

    def files(self):
        return self._files


class _FakeHandle:
    def __init__(self, pieces):
        self._pieces = set(pieces)

    def have_piece(self, piece_index):
        return piece_index in self._pieces


class BufferMonitorTests(unittest.TestCase):
    def test_requires_contiguous_startup_window(self):
        info = _FakeTorrentInfo(
            piece_length=4,
            file_offsets=[0],
            file_sizes=[16],
        )
        handle = _FakeHandle({0, 1, 3})

        monitor = BufferMonitor(buffer_bytes=10)
        monitor.attach(handle, info, 0)

        state = monitor.startup_buffer_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["required_first_piece"], 0)
        self.assertEqual(state["required_last_piece"], 2)
        self.assertEqual(state["contiguous_last_piece"], 1)
        self.assertEqual(state["missing_pieces"], [2])
        self.assertFalse(monitor.is_ready())
        self.assertEqual(monitor.buffer_bytes_downloaded(), 8)
        self.assertAlmostEqual(monitor.buffer_percent(), 80.0)


if __name__ == "__main__":
    unittest.main()
