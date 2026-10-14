"""End-to-end tests for Model entities and registration points (real Overte domain)."""

import http.server
import math
import os
import threading
import time

import pytest

pytestmark = pytest.mark.overte

from test_live_overte import entity, pump_for, pump_until, vclose, world_props  # noqa: E402

SHARED = {}


def _monkey(bpy, name="Suzanne", location=(0, 0, 0)):
    import bmesh
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_monkey(bm)
    # Shift the geometry so the origin is NOT the bbox centre.
    bmesh.ops.translate(bm, verts=bm.verts, vec=(0.0, 0.0, 1.0))
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    return obj


def fetch(env, url):
    got = []
    env["session"].request({"type": "fetch", "url": url}, lambda m, d: got.append((m, d)))
    pump_until(env, lambda: got, what=f"fetch {url}")
    return got[0]


def test_publish_mesh_as_model(overte_env):
    env = overte_env
    bpy = env["bpy"]
    obj = _monkey(bpy, location=(2, 3, 0))
    obj.rotation_euler = (0, 0, math.radians(45))
    obj.scale = (2, 2, 2)
    bpy.context.view_layer.update()
    n, errors = env["sync"].models.publish([obj])
    assert n == 1 and not errors
    eid = pump_until(env, lambda: obj.get("overte_id"), timeout=30, what="model entity")
    e = pump_until(env, lambda: entity(env, eid), what="model in domain")
    assert e["type"] == "Model" and e["modelURL"].startswith("atp:/blender/Suzanne-")
    # Centre of the (offset) geometry, not the object origin.
    assert vclose(e["position"], world_props(obj, "Model")["position"])
    assert e["position"]["y"] == pytest.approx(2.0, abs=0.05)  # origin z=0, geometry centred ~1 m up, x2 scale
    assert vclose(e["dimensions"], world_props(obj, "Model")["dimensions"])
    reply, data = fetch(env, e["modelURL"])
    assert reply.get("error") is None and data[:4] == b"glTF"
    SHARED.update(model=eid.lower(), url=e["modelURL"], obj=obj.name)


def test_mesh_edit_republishes(overte_env):
    env = overte_env
    bpy = env["bpy"]
    obj = bpy.context.scene.objects[SHARED["obj"]]

    def handler(scene, depsgraph):
        env["sync"].models.on_depsgraph_update(depsgraph)

    bpy.app.handlers.depsgraph_update_post.append(handler)
    try:
        for v in obj.data.vertices:  # stretch the mesh 2x along X
            v.co.x *= 2
        obj.data.update()
        bpy.context.view_layer.update()
        e = pump_until(env, lambda: (lambda e: e if e and e["modelURL"] != SHARED["url"] else None)(
            entity(env, SHARED["model"])), timeout=30, what="republished model")
    finally:
        bpy.app.handlers.depsgraph_update_post.remove(handler)
    assert e["modelURL"].startswith("atp:/blender/Suzanne-")
    assert vclose(e["dimensions"], world_props(obj, "Model")["dimensions"])
    assert obj.get("overte_id").lower() == SHARED["model"]  # same entity, new version


def test_remote_model_geometry_is_loaded(overte_env):
    env = overte_env
    eid = env["probe"].call("add", props={
        "type": "Model", "name": "Imported", "modelURL": SHARED["url"],
        "position": {"x": -4, "y": 1, "z": 0}, "dimensions": {"x": 1, "y": 1, "z": 1}})
    obj = pump_until(env, lambda: (lambda o: o if o is not None and o.type == "MESH" else None)(
        env["sync"].object_for(eid)), timeout=30, what="model geometry")
    assert obj.name == "Imported" and len(obj.data.vertices) > 100
    pump_for(env, 0.5)
    env["bpy"].context.view_layer.update()
    # The bounding box of the loaded geometry fills the entity's box.
    e = entity(env, eid)
    assert vclose(world_props(obj, "Model")["position"], e["position"])
    assert vclose(world_props(obj, "Model")["dimensions"], e["dimensions"])
    # Still linked: moving it in Blender moves the entity.
    obj.location.x -= 1
    env["bpy"].context.view_layer.update()
    expected = world_props(obj, "Model")
    pump_until(env, lambda: vclose(entity(env, eid)["position"], expected["position"]), what="move loaded model")


def test_http_model_url(overte_env, tmp_path):
    env = overte_env
    _reply, data = fetch(env, SHARED["url"])
    (tmp_path / "chair.glb").write_bytes(data)
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_path), **k)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/chair.glb"
        eid = env["probe"].call("add", props={"type": "Model", "name": "Chair", "modelURL": url,
                                              "position": {"x": 0, "y": 1, "z": 6}})
        obj = pump_until(env, lambda: (lambda o: o if o is not None and o.type == "MESH" else None)(
            env["sync"].object_for(eid)), timeout=30, what="http model geometry")
        assert obj["overte_model_loaded"] == url
    finally:
        httpd.shutdown()


def test_registration_point(overte_env):
    env = overte_env
    eid = env["probe"].call("add", props={
        "type": "Box", "name": "Pillar", "position": {"x": 8, "y": 0, "z": 0},
        "dimensions": {"x": 1, "y": 2, "z": 1}, "registrationPoint": {"x": 0.5, "y": 0, "z": 0.5}})
    obj = pump_until(env, lambda: env["sync"].object_for(eid), what="bottom-anchored box")
    pump_for(env, 0.3)
    # Overte position is the bottom; Blender shows the box centred 1 m up.
    assert tuple(round(v, 3) for v in obj.matrix_world.translation) == (8.0, 0.0, 1.0)
    obj.location.z = 3.0
    env["bpy"].context.view_layer.update()
    pump_until(env, lambda: abs(entity(env, eid)["position"]["y"] - 2.0) < 1e-3, what="bottom at y=2")
