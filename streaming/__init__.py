"""
streaming/ — Localhost HTTP layer that turns the partially-downloaded
torrent file into a controlled byte stream for MPV.

Public surface:
    PieceWaiter   — block HTTP threads until a piece arrives
    StreamSource  — bridge between the HTTP server and the torrent
    StreamServer  — ThreadingHTTPServer bound to 127.0.0.1
"""
from streaming.piece_waiter import PieceWaiter
from streaming.source import StreamSource
from streaming.http_server import StreamServer

__all__ = ["PieceWaiter", "StreamSource", "StreamServer"]
