import unittest
from unittest import mock

from utils.network import get_lan_ip


class NetworkTests(unittest.TestCase):
    def test_get_lan_ip_prefers_route_to_target_host(self):
        socket_ctx = mock.MagicMock()
        socket_ctx.getsockname.return_value = ("192.168.50.12", 54321)

        socket_factory = mock.MagicMock()
        socket_factory.return_value.__enter__.return_value = socket_ctx

        with mock.patch("utils.network.socket.socket", socket_factory):
            result = get_lan_ip(target_host="192.168.50.20")

        self.assertEqual(result, "192.168.50.12")
        socket_ctx.connect.assert_called_once_with(("192.168.50.20", 80))

    def test_get_lan_ip_falls_back_to_private_hostname_address(self):
        hostname_infos = [
            (mock.sentinel.af_inet, None, None, None, ("127.0.0.1", 0)),
            (mock.sentinel.af_inet, None, None, None, ("192.168.1.44", 0)),
        ]

        with mock.patch(
            "utils.network.socket.socket",
            side_effect=OSError("route probe failed"),
        ), mock.patch(
            "utils.network.socket.getaddrinfo",
            return_value=hostname_infos,
        ), mock.patch(
            "utils.network.socket.AF_INET",
            mock.sentinel.af_inet,
        ):
            result = get_lan_ip()

        self.assertEqual(result, "192.168.1.44")


if __name__ == "__main__":
    unittest.main()
