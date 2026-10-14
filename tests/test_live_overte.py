"""End-to-end tests: Blender session <-> bridge agent <-> real Overte domain.

The tests run in order and share one domain (module-scoped fixture).
"""

import math
import time
import uuid

import pytest

pytestmark = pytest.mark.overte

from overte_live_link import convert as cv  # noqa: E402

NULL = "{00000000-0000-0000-0000-000000000000}"


def pump_until(env, cond, timeout=15.0, what="condition"):
    """Run sync ticks until ``cond()`` is truthy."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        env["session"].tick()
        last = cond()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}; logs: {env['logs'][-5:]}")


def pump_for(env, seconds):
    end = time.time() + seconds
    while time.time() < end:
        env["session"].tick()
        time.sleep(0.05)


def obj_for(env, eid):
    return env["sync"].object_for(eid)


def entity(env, eid):
    """The entity as the domain has it (from the independent probe agent), or None.

    An agent's tree can lose an entity for ~1 s after an edit, so None is only
    trusted once it has persisted for a few seconds.
    """
    end = time.time() + 5.0
    while True:
        e = env["probe"].call("get", id=eid)
        if e is not None or time.time() > end:
            return e
        env["session"].tick()
        time.sleep(0.1)


def vclose(a, b, eps=2e-3):
    return all(abs(a[k] - b[k]) <= eps for k in ("x", "y", "z"))


def world_props(obj, etype="Box"):
    loc, rot, scale = obj.matrix_world.decompose()
    base = tuple(obj.get("overte_base", (1, 1, 1)))
    center = tuple(obj.get("overte_center", (0, 0, 0)))
    return cv.object_to_entity(tuple(loc), tuple(rot), tuple(scale), base, center, etype)


SHARED = {}


def test_remote_entities_appear_in_blender(overte_env):
    env = overte_env
    probe = env["probe"]
    yaw = math.radians(30)
    box = probe.call("add", props={
        "type": "Box", "name": "Crate", "position": {"x": 1, "y": 2, "z": -3},
        "rotation": {"x": 0, "y": math.sin(yaw / 2), "z": 0, "w": math.cos(yaw / 2)},
        "dimensions": {"x": 1, "y": 2, "z": 3}, "color": {"red": 200, "green": 30, "blue": 10}})
    sphere = probe.call("add", props={
        "type": "Sphere", "name": "Ball", "position": {"x": -2, "y": 1, "z": 0},
        "dimensions": {"x": 0.5, "y": 0.5, "z": 0.5}})
    light = probe.call("add", props={
        "type": "Light", "name": "Spot", "position": {"x": 0, "y": 4, "z": 0}, "isSpotlight": True,
        "intensity": 2.5, "cutoff": 30, "dimensions": {"x": 10, "y": 10, "z": 10}})
    child = probe.call("add", props={
        "type": "Box", "name": "Lid", "parentID": box, "localPosition": {"x": 0, "y": 1.1, "z": 0},
        "dimensions": {"x": 1, "y": 0.2, "z": 3}})
    SHARED.update(box=box.lower(), sphere=sphere.lower(), light=light.lower(), child=child.lower())

    pump_until(env, lambda: all(obj_for(env, SHARED[k]) for k in ("box", "sphere", "light", "child")),
               what="entities to appear in Blender")
    pump_for(env, 0.5)

    crate = obj_for(env, SHARED["box"])
    assert crate.name == "Crate" and crate.type == "MESH"
    # Overte (1, 2, -3) is Blender (1, 3, 2); 1 x 2(tall) x 3(deep) is Blender scale (1, 3, 2)
    assert tuple(round(v, 3) for v in crate.matrix_world.translation) == (1.0, 3.0, 2.0)
    assert tuple(round(v, 3) for v in crate.matrix_world.to_scale()) == (1.0, 3.0, 2.0)
    assert math.degrees(crate.matrix_world.to_euler().z) == pytest.approx(30, abs=0.05)
    assert crate.active_material.diffuse_color[0] > 0.5  # red-ish

    spot = obj_for(env, SHARED["light"])
    assert spot.type == "LIGHT" and spot.data.type == "SPOT"
    assert spot.data.energy == pytest.approx(250)
    assert math.degrees(spot.data.spot_size) == pytest.approx(60, abs=0.1)

    lid = obj_for(env, SHARED["child"])
    assert lid.parent == crate
    # Child sits 1.1 m above the crate's centre along the crate's up axis.
    assert lid.matrix_world.translation.z == pytest.approx(2.0 + 1.1, abs=1e-3)


def test_blender_move_reaches_domain_without_echo(overte_env):
    env = overte_env
    crate = obj_for(env, SHARED["box"])
    crate.location.x += 2.0
    crate.scale.z = 4.0  # 4 m tall in Overte
    env["bpy"].context.view_layer.update()
    expected = world_props(crate)
    pump_until(env, lambda: vclose(entity(env, SHARED["box"])["position"], expected["position"])
               and vclose(entity(env, SHARED["box"])["dimensions"], expected["dimensions"]),
               what="move to reach the domain")
    # The child follows its parent in Overte as it does in Blender.
    lid = obj_for(env, SHARED["child"])
    env["bpy"].context.view_layer.update()
    pump_until(env, lambda: vclose(entity(env, SHARED["child"])["position"], world_props(lid)["position"]),
               what="child to follow")
    # Settled: our own edit must not come back and trigger further edits.
    pump_for(env, 1.5)
    sent = env["sync"].stats["sent"]
    pump_for(env, 1.5)
    assert env["sync"].stats["sent"] == sent
    assert crate.location.x == pytest.approx(3.0, abs=1e-4)


def test_remote_edit_updates_only_changed_fields(overte_env):
    env = overte_env
    ball = obj_for(env, SHARED["sphere"])
    ball.name = "BallRenamedInBlender"
    pump_until(env, lambda: entity(env, SHARED["sphere"])["name"] == "BallRenamedInBlender", what="rename")
    env["probe"].call("edit", id=SHARED["sphere"], props={
        "position": {"x": -2, "y": 5, "z": 0}, "color": {"red": 0, "green": 0, "blue": 255}})
    pump_until(env, lambda: abs(ball.matrix_world.translation.z - 5) < 1e-3, what="remote move")
    assert ball.name == "BallRenamedInBlender"  # untouched field kept
    assert ball.active_material.diffuse_color[2] > 0.9


def test_add_entity_from_blender(overte_env):
    env = overte_env
    bpy = env["bpy"]
    from overte_live_link import scene_sync as ss

    obj = bpy.data.objects.new("NewPillar", ss.unit_mesh("Cylinder"))
    obj.material_slots[0].link = "OBJECT"
    ss._set_object_color(obj, (0.0, 1.0, 0.0))
    obj["overte_type"] = "Shape"
    obj["overte_shape"] = "Cylinder"
    obj["overte_tmp"] = str(uuid.uuid4())
    obj.location = (5, 5, 1)
    obj.scale = (0.5, 0.5, 2)
    ss.overte_collection(bpy.context.scene).objects.link(obj)
    eid = pump_until(env, lambda: obj.get("overte_id"), what="entity id")
    SHARED["pillar"] = eid
    e = pump_until(env, lambda: entity(env, eid), what="probe to see the new entity")
    assert e["type"] == "Shape" and e["shape"] == "Cylinder" and e["name"] == "NewPillar"
    assert vclose(e["position"], {"x": 5, "y": 1, "z": -5})
    assert vclose(e["dimensions"], {"x": 0.5, "y": 2, "z": 0.5})
    assert e["color"] == {"red": 0, "green": 255, "blue": 0}


def test_duplicate_in_blender_clones_entity(overte_env):
    env = overte_env
    bpy = env["bpy"]
    crate = obj_for(env, SHARED["box"])
    dup = crate.copy()  # what Shift+D does: same custom props, so same overte_id
    dup.location.x += 10
    for col in crate.users_collection:
        col.objects.link(dup)
    new_id = pump_until(env, lambda: dup.get("overte_id") and dup["overte_id"].lower() != SHARED["box"]
                        and dup["overte_id"], what="clone id")
    e = pump_until(env, lambda: entity(env, new_id), what="clone in domain")
    assert e["type"] == "Box" and e["color"] == {"red": 200, "green": 30, "blue": 10}
    assert vclose(e["position"], world_props(dup)["position"])
    assert obj_for(env, SHARED["box"]) == crate  # original stays linked to the original
    SHARED["clone"] = new_id.lower()


def test_reparent_in_blender(overte_env):
    env = overte_env
    bpy = env["bpy"]
    ball = obj_for(env, SHARED["sphere"])
    pillar = obj_for(env, SHARED["pillar"])
    world = ball.matrix_world.copy()
    ball.parent = pillar
    ball.matrix_world = world
    bpy.context.view_layer.update()
    e = pump_until(env, lambda: (entity(env, SHARED["sphere"]) or {}).get("parentID", "").lower()
                   == SHARED["pillar"].lower() and entity(env, SHARED["sphere"]), what="parentID")
    assert vclose(e["position"], world_props(ball)["position"])
    # Moving the parent in Overte moves the child in Blender too.
    env["probe"].call("edit", id=SHARED["pillar"], props={"position": {"x": 5, "y": 1, "z": -8}})
    pump_until(env, lambda: abs(pillar.matrix_world.translation.y - 8) < 1e-3, what="parent move")
    bpy.context.view_layer.update()
    assert vclose(world_props(ball)["position"], entity(env, SHARED["sphere"])["position"])


def test_locked_entity(overte_env):
    env = overte_env
    eid = env["probe"].call("add", props={"type": "Box", "name": "Locked", "locked": True,
                                          "position": {"x": 0, "y": 0, "z": 9}})
    obj = pump_until(env, lambda: obj_for(env, eid), what="locked entity")
    pump_for(env, 0.3)
    assert all(obj.lock_location) and all(obj.lock_rotation) and all(obj.lock_scale)
    # Scripts can still move a locked object; the edit is refused and Blender reverts.
    obj.location.z += 3
    pump_until(env, lambda: abs(obj.location.z) < 1e-3, what="revert of a locked entity")
    assert entity(env, eid)["position"]["z"] == pytest.approx(9, abs=1e-3)
    # Unlocking in Blender unlocks in Overte, and then moves go through.
    obj.lock_location = obj.lock_rotation = obj.lock_scale = (False, False, False)
    obj.location.z += 3
    pump_until(env, lambda: (lambda e: e and not e["locked"] and abs(e["position"]["y"] - 3) < 1e-3)(entity(env, eid)),
               what="unlock and move")
    SHARED["locked"] = eid.lower()


def test_deletes_both_ways(overte_env):
    env = overte_env
    bpy = env["bpy"]
    clone = obj_for(env, SHARED["clone"])
    bpy.data.objects.remove(clone, do_unlink=True)
    pump_until(env, lambda: entity(env, SHARED["clone"]) is None, what="delete in domain")
    env["probe"].call("delete", id=SHARED["light"])
    pump_until(env, lambda: obj_for(env, SHARED["light"]) is None, what="delete in Blender")


def test_reconnect_relinks_without_duplicates(overte_env):
    env = overte_env
    bpy = env["bpy"]
    session, sync = env["session"], env["sync"]
    n_objects = len(bpy.context.scene.objects)
    session.stop()
    session.start()
    sync.synced = False
    pump_until(env, lambda: sync.synced, timeout=30, what="resync after reconnect")
    pump_for(env, 1.0)
    assert len(bpy.context.scene.objects) == n_objects
    assert not sync.missing_ids
    # And the link still works.
    crate = obj_for(env, SHARED["box"])
    crate.location.y += 1
    bpy.context.view_layer.update()
    expected = world_props(crate)
    pump_until(env, lambda: vclose(entity(env, SHARED["box"])["position"], expected["position"]),
               what="edit after reconnect")
