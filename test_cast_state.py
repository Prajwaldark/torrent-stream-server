import unittest
from types import SimpleNamespace
from unittest import mock

from utils.cast import (
    CONNECTION_STATUS_DISCONNECTED,
    CastManager,
)


class _FakeMediaStatus:
    def __init__(self, media_session_id=1, player_state="PLAYING"):
        self.media_session_id = media_session_id
        self.player_state = player_state


class _FakeMediaController:
    def __init__(self):
        self.status = _FakeMediaStatus()


class _FakeSocketClient:
    def __init__(self, is_connected=False):
        self.is_connected = is_connected


class _FakeCast:
    def __init__(self):
        self.media_controller = _FakeMediaController()
        self.socket_client = _FakeSocketClient(is_connected=False)
        self.status = SimpleNamespace(display_name="Receiver")


class CastStateTests(unittest.TestCase):
    def test_transient_disconnect_does_not_clear_live_media_session(self):
        cast = CastManager()
        fake_cast = _FakeCast()

        cast._current_cast = fake_cast
        cast.active_cast = fake_cast
        cast.active_media_controller = fake_cast.media_controller
        cast.cast_device_name = "Receiver"
        cast.cast_connected = True
        cast.cast_session["connected"] = True
        cast.cast_session["playback_state"] = "PLAYING"
        cast.cast_session["media_session_id"] = 1

        with mock.patch.object(cast, "_start_status_poller"), mock.patch.object(cast, "_refresh_status_async"):
            status = SimpleNamespace(
                status=CONNECTION_STATUS_DISCONNECTED,
                address=None,
                service=None,
            )
            cast.new_connection_status(status)

        self.assertTrue(cast.cast_session["connected"])
        self.assertEqual(cast.cast_session["playback_state"], "PLAYING")
        self.assertIsNotNone(cast._current_cast)


if __name__ == "__main__":
    unittest.main()
