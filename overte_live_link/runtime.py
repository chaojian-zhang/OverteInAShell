"""Process-wide link session and the Blender timer that drives it."""

from __future__ import annotations

import traceback
from typing import Optional

import bpy

from .scene_sync import SceneSync
from .session import LinkSession

TICK_SECONDS = 0.05

session: Optional[LinkSession] = None
messages: list[str] = []  # recent log lines shown in the panel


def log(msg: str) -> None:
    print("[Overte Live Link]", msg)
    messages.append(msg)
    del messages[:-6]


def _redraw() -> None:
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def start(scene, host: str, port: int, sync_deletes: bool = True,
          auto_load_models: bool = True, auto_update_models: bool = True,
          token: Optional[str] = None) -> LinkSession:
    global session
    stop()
    sync = SceneSync(scene, sync_deletes=sync_deletes, log=log)
    sync.models.auto_load = auto_load_models
    sync.models.auto_update = auto_update_models
    sync.on_change = _redraw
    sync.track_dirty = True  # fed by _on_depsgraph_update
    s = LinkSession(sync, host, port, log=log, token=token)
    s.start()
    session = s
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=TICK_SECONDS, persistent=True)
    return s


def stop() -> None:
    global session
    if session is not None:
        session.stop()
        log("Overte Live Link stopped")
    session = None
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)


def _tick():
    if session is None:
        return None
    try:
        session.tick()
    except Exception:  # keep the link alive; report and continue
        log("sync error (see console)")
        traceback.print_exc()
    return TICK_SECONDS


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, depsgraph):
    if session is not None:
        try:
            session.sync.on_depsgraph_update(depsgraph)
        except Exception:
            traceback.print_exc()


@bpy.app.handlers.persistent
def _on_load_pre(*_args):
    # A different .blend is about to load: its objects are not this domain's
    # scene, and missing objects must never be read as deletions.
    if session is not None:
        stop()
