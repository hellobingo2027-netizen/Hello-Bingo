"""
socket_patches.py
------------------
Patches SocketManager.handle_legacy_join_room so the admin panel's socket
ALWAYS subscribes to bingo_room_{stake}, even when no WAITING room exists
for that stake at the moment it connects (i.e. a game is already RUNNING).

Without this patch, the admin panel's join_room('10') / join_room('20')
calls on connect can silently do nothing if that stake's game happens to
be RUNNING at that instant - meaning the admin socket never joins the
channel and never receives ball_called / countdown_update / winner_found
broadcasts for it.

This file does NOT modify socket_manager.py. Import it once, and call
apply_socket_patches() BEFORE SocketManager(...) is instantiated in
main.py:

    from backend.multiplayer.socket_patches import apply_socket_patches
    apply_socket_patches()
    socket_manager = SocketManager(socketio, room_manager, game_engine)

That is the only change required in main.py for this half of the fix.
"""
from flask_socketio import join_room

from backend.multiplayer.socket_manager import SocketManager, GAME_ROOM_PREFIX


def apply_socket_patches():
    def handle_legacy_join_room(self, data):
        stake = self._stake_from(data)
        # ALWAYS subscribe to the stake room, regardless of game state.
        # This is the one-line fix: previously this only happened inside
        # the `if not room: return` guard below, so a RUNNING game (no
        # open WAITING room) meant the admin socket never joined at all.
        join_room(f"bingo_room_{stake}")

        room = self.manager.get_open_room(stake)
        if not room:
            return
        join_room(f"{GAME_ROOM_PREFIX}{room.game_id}")
        self.engine.ensure_loop(room.game_id)
        self.emit_game_legacy(room.game_id, 'countdown_update', {
            'room': stake,
            'game_id': room.game_id,
            'time_left': room.time_left(),
        })

    SocketManager.handle_legacy_join_room = handle_legacy_join_room
    print("✅ socket_patches applied: admin join_room now always subscribes to bingo_room_{stake}")
