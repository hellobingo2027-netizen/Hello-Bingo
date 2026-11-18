"""
admin_room_resolver.py
-----------------------
Resolves which room an ADMIN action (manual call, pause, cancel, set max
winners) should apply to.

room_manager.get_open_room() only returns a room while it is still in the
WAITING lobby. The instant a game transitions to RUNNING - exactly when an
admin most needs to reach it - get_open_room() returns None. That is why
the admin panel's Game Control page appeared to do nothing: manual_call and
set_max_winners silently created a brand-new EMPTY phantom room instead of
touching the real game, while pause_game and cancel_game silently no-op'd.

get_active_room() fixes this WITHOUT modifying room_manager.py at all: it
first tries the open waiting room (unchanged behavior), then falls back to
the most recently started RUNNING room for that stake. If neither exists,
it returns None so the caller can report a clear error instead of creating
a phantom room.

Usage (from main.py):

    from backend.multiplayer.admin_room_resolver import get_active_room
    ...
    room_obj = get_active_room(room_manager, stake)
    if not room_obj:
        return jsonify({'success': False, 'error': 'No active game for this stake'}), 400
"""
from backend.multiplayer.room_manager import Room


def get_active_room(room_manager, stake):
    """Return the room an admin should control for this stake, or None."""
    with room_manager.lock:
        room = room_manager.get_open_room(stake)
        if room:
            return room
        running = [r for r in room_manager.rooms.values()
                   if r.stake == stake and r.status == Room.RUNNING]
        if running:
            return max(running, key=lambda r: r.started_at or 0)
        return None
