"""End-to-end check in the real Blender UI (not the bpy module).

Run inside a GUI Blender with the extension installed, e.g. under Xvfb:

    blender --python tests/gui_e2e.py -- <result.json> [screenshot.png]

Unlike the pytest suite, nothing here calls the sync tick directly: the add-on
runs exactly as for a user, driven by Blender's timers and depsgraph handlers.
The scenario starts the link, launches a local Overte domain whose persistent
scripts are the bridge (served by this Blender) and an independent probe, and
then checks both directions, adding entities, publishing a mesh and the
automatic re-upload after a mesh edit. The result is written as JSON and
Blender quits.
"""

import importlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import traceback

import addon_utils
import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from probe_client import Probe  # noqa: E402

args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
RESULT = args[0] if args else os.path.join(tempfile.gettempdir(), "overte_gui_e2e.json")
SHOT = args[1] if len(args) > 1 else None
EXT = next((m.__name__ for m in addon_utils.modules() if m.__name__.endswith("overte_live_link")), None)

state = {"steps": [], "ok": False}
DOMAIN_HOME = tempfile.mkdtemp(prefix="overte-gui-")
env = dict(os.environ, DOMAIN_HOME=DOMAIN_HOME, FRESH="1")


def launcher(*a):
    return subprocess.run([os.path.join(ROOT, "scripts", "run_local_domain.sh"), *a], env=env,
                          capture_output=True, text=True, cwd=ROOT)


def view3d():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next(r for r in area.regions if r.type == "WINDOW")
                return window, area, region
    raise RuntimeError("no 3D viewport")


def op(fn, **kw):
    window, area, region = view3d()
    with bpy.context.temp_override(window=window, area=area, region=region):
        return fn(**kw)


def scenario():
    """Generator of steps; each yields a (description, condition, timeout)."""
    ss = importlib.import_module(EXT + ".scene_sync")
    rt = importlib.import_module(EXT + ".runtime")
    wsserver = importlib.import_module(EXT + ".wsserver")

    bpy.ops.object.select_all(action="DESELECT")
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)

    st = bpy.context.scene.overte_link
    st.port = 19430
    assert op(bpy.ops.overte.start) == {"FINISHED"}
    session = rt.session
    probe = Probe(19431, server_cls=wsserver.LinkServer)
    state["probe"] = probe
    r = launcher("start", session.bridge_url(), probe.url())
    assert r.returncode == 0, r.stderr
    yield "bridge connects and syncs", lambda: session.bridge_info and session.sync.synced, 120
    yield "probe connects", lambda: probe.pump() or probe.cid is not None, 60

    eid = probe.call("add", props={"type": "Box", "name": "GuiCrate", "position": {"x": 1, "y": 1, "z": -2},
                                   "color": {"red": 255, "green": 128, "blue": 0}})
    yield "remote box appears", lambda: session.sync.object_for(eid) is not None, 20
    crate = session.sync.object_for(eid)

    crate.location.z = 4.0  # Blender z == Overte y
    yield "Blender move reaches the domain", lambda: abs((probe.call("get", id=eid) or {}).get(
        "position", {}).get("y", 0) - 4.0) < 1e-3, 20

    bpy.context.scene.cursor.location = (-3, 0, 0.5)
    op(bpy.ops.overte.add_entity, kind="Sphere")
    sphere = bpy.context.view_layer.objects.active
    yield "Add > Sphere creates an entity", lambda: sphere.get("overte_id") and probe.call(
        "get", id=sphere["overte_id"]), 20

    bpy.ops.mesh.primitive_monkey_add(location=(0, 4, 1), rotation=(0, 0, math.radians(30)))
    monkey = bpy.context.view_layer.objects.active
    monkey.name = "GuiMonkey"
    bpy.ops.object.select_all(action="DESELECT")
    monkey.select_set(True)
    bpy.context.view_layer.objects.active = monkey
    assert op(bpy.ops.overte.publish) == {"FINISHED"}
    yield "Publish creates a Model entity", lambda: monkey.get("overte_id") and (
        probe.call("get", id=monkey["overte_id"]) or {}).get("type") == "Model", 30
    first_url = probe.call("get", id=monkey["overte_id"])["modelURL"]

    for v in monkey.data.vertices:
        v.co.z *= 1.5
    monkey.data.update()
    yield "mesh edit re-uploads the model", lambda: (probe.call("get", id=monkey["overte_id"]) or {}).get(
        "modelURL", first_url) != first_url, 30

    probe.call("edit", id=eid, props={"rotation": {"x": 0, "y": math.sin(math.pi / 8), "z": 0,
                                                   "w": math.cos(math.pi / 8)}})
    yield "remote rotation reaches Blender", lambda: abs(math.degrees(
        session.sync.object_for(eid).matrix_world.to_euler().z) - 45) < 0.1, 20

    crate = session.sync.object_for(eid)
    bpy.data.objects.remove(crate)
    yield "Blender delete reaches the domain", lambda: probe.call("get", id=eid) is None, 30

    # Show the sidebar on the Overte tab, frame the scene and redraw for the screenshot.
    window, area, region = view3d()
    area.spaces[0].show_region_ui = True
    area.spaces[0].shading.type = "SOLID"
    bpy.ops.object.select_all(action="DESELECT")
    monkey.select_set(True)
    bpy.context.view_layer.objects.active = monkey
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.view3d.view_all(center=False)

    def show_overte_tab():
        for reg in area.regions:
            if reg.type == "UI":
                try:
                    reg.active_panel_category = "Overte"  # categories exist once drawn
                except (AttributeError, TypeError, ValueError):
                    pass
        for a in window.screen.areas:
            a.tag_redraw()
        return False

    t0 = time.time()
    yield "UI redraw", lambda: show_overte_tab() or time.time() - t0 > 3.0, 6
    state["uploads"] = sum("Published" in m for m in rt.messages)
    state["ok"] = True


def finish():
    probe = state.pop("probe", None)
    try:
        if SHOT:
            # Blender's own screenshot reads back black under Xvfb; grab the X screen.
            xwd = subprocess.run(["xwd", "-root", "-display", os.environ.get("DISPLAY", ":0")],
                                 capture_output=True, check=True).stdout
            subprocess.run(["convert", "xwd:-", SHOT], input=xwd, check=True)
    except Exception as exc:
        state["screenshot_error"] = str(exc)
    launcher("stop")
    if probe:
        probe.stop()
    rt = importlib.import_module(EXT + ".runtime")
    state["log"] = list(rt.messages)
    rt.stop()
    with open(RESULT, "w") as f:
        json.dump(state, f, indent=2)
    bpy.ops.wm.quit_blender()


def driver():
    gen = driver.gen
    try:
        if driver.current is None:
            driver.current = next(gen)
            driver.started = time.time()
            print("[gui-e2e] waiting:", driver.current[0], flush=True)
        name, cond, timeout = driver.current
        if cond():
            state["steps"].append({"step": name, "ok": True, "seconds": round(time.time() - driver.started, 2)})
            print("[gui-e2e] ok:", name, flush=True)
            driver.current = None
        elif time.time() - driver.started > timeout:
            state["steps"].append({"step": name, "ok": False})
            state["error"] = f"timed out: {name}"
            finish()
            return None
    except StopIteration:
        finish()
        return None
    except Exception:
        state["error"] = traceback.format_exc()
        finish()
        return None
    return 0.1


def begin():
    for window in bpy.context.window_manager.windows:  # close the splash screen
        for area in window.screen.areas:
            area.tag_redraw()
    bpy.context.preferences.view.show_splash = False
    if EXT is None:
        state["error"] = "overte_live_link extension is not installed"
        finish()
        return None
    addon_utils.enable(EXT, default_set=True)
    driver.gen = scenario()
    driver.current = None
    bpy.app.timers.register(driver, first_interval=0.1)
    return None


bpy.app.timers.register(begin, first_interval=1.0)
