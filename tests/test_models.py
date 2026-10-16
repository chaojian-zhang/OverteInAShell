"""Model loading and primitive conversion, against a fake bridge session (no Overte)."""

import json
import os
import struct

import pytest

bpy = pytest.importorskip("bpy")

from overte_live_link import models  # noqa: E402
from overte_live_link import scene_sync as ss  # noqa: E402

NULL = ss.NULL_ID


class FakeSession:
    """Records the requests ModelManager makes; tests answer them by hand."""

    def __init__(self):
        self.bridge_info = {"mode": "agent"}
        self.requests = []  # (msg, callback, binary)

    def request(self, msg, callback, binary=None):
        self.requests.append((msg, callback, binary))

    def of_type(self, t):
        return [r for r in self.requests if r[0]["type"] == t]


def ent(eid, etype, **kw):
    p = {"id": eid, "type": etype, "name": etype, "position": {"x": 0, "y": 0, "z": 0},
         "rotation": {"x": 0, "y": 0, "z": 0, "w": 1}, "dimensions": {"x": 1, "y": 1, "z": 1},
         "registrationPoint": {"x": 0.5, "y": 0.5, "z": 0.5}, "parentID": NULL, "visible": True,
         "locked": False, "lastEdited": 1, "entityHostType": "domain"}
    if etype in ss.PRIMITIVE_TYPES:
        p["color"] = {"red": 255, "green": 255, "blue": 255}
    p.update(kw)
    return p


def eid(n):
    return "{%08d-0000-0000-0000-000000000000}" % n


@pytest.fixture
def linked():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    session = FakeSession()
    sync = ss.SceneSync(bpy.context.scene, log=lambda m: None)
    sync.attach(session)

    def snapshot(*entities):
        sync.handle_message({"type": "changes", "full": True, "upserts": [dict(e) for e in entities],
                             "deletes": [], "ids": [e["id"] for e in entities]})
    return sync, session, snapshot


def cube_glb():
    mesh = bpy.data.meshes.new("C")
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("C", mesh)
    bpy.context.scene.collection.objects.link(obj)
    data = models.export_glb(obj)[0]
    bpy.data.objects.remove(obj)
    bpy.data.meshes.remove(mesh)
    return data


def glb_json(data):
    n = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20:20 + n])


def test_same_model_url_is_fetched_and_imported_once(linked):
    sync, session, snapshot = linked
    glb = cube_glb()
    snapshot(ent(eid(1), "Model", modelURL="atp:/a.glb"), ent(eid(2), "Model", modelURL="atp:/a.glb"),
             ent(eid(3), "Model", modelURL="atp:/c.glb"))
    assert sorted(m["url"] for m, _, _ in session.of_type("fetch")) == ["atp:/a.glb", "atp:/c.glb"]
    for _msg, cb, _ in session.requests:
        cb({"type": "fetched", "error": None}, glb)
    sync.models.tick()
    a, b, c = (sync.object_for(eid(i)) for i in (1, 2, 3))
    assert a.type == b.type == c.type == "MESH"
    assert a.data == b.data and a.data != c.data
    assert all(o[models.P_LOADED] for o in (a, b, c))

    # A later entity using the same model shares the mesh without fetching it again.
    session.requests.clear()
    sync.handle_message({"type": "changes", "upserts": [ent(eid(4), "Model", modelURL="atp:/a.glb")]})
    assert not session.of_type("fetch")
    sync.models.tick()
    assert sync.object_for(eid(4)).data == a.data


def test_rigged_fbx_imports(linked, tmp_path):
    # The Python FBX importer used to fail on armatures inside the temporary scene.
    bpy.ops.object.armature_add()
    arm = bpy.context.active_object
    bpy.ops.mesh.primitive_cube_add()
    body = bpy.context.active_object
    body.vertex_groups.new(name=arm.data.bones[0].name).add(range(len(body.data.vertices)), 1.0, "REPLACE")
    body.modifiers.new("Armature", "ARMATURE").object = arm
    body.parent = arm
    path = str(tmp_path / "rig.fbx")
    bpy.ops.export_scene.fbx(filepath=path)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with open(path, "rb") as f:
        mesh = models.import_model(f.read(), ".fbx", "Rig")
    assert mesh is not None and len(mesh.vertices) == 8
    assert len(bpy.data.objects) == 0 and len(bpy.data.scenes) == 1


def test_import_does_not_leave_files(linked):
    import tempfile
    before = set(os.listdir(tempfile.gettempdir()))
    models.import_model(cube_glb(), ".glb", "X")
    assert not {n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith("overte_")}


def test_convert_box_to_model(linked):
    sync, session, snapshot = linked
    snapshot(ent(eid(1), "Box", color={"red": 255, "green": 0, "blue": 0},
                 position={"x": 1, "y": 2, "z": 3}, dimensions={"x": 2, "y": 1, "z": 1}),
             ent(eid(2), "Box"),
             ent(eid(3), "Sphere", parentID=eid(1)))
    box, other, child = (sync.object_for(eid(i)) for i in (1, 2, 3))
    assert box.data == other.data  # primitives share one unit mesh
    sync.track_dirty = True
    matrix = box.matrix_world.copy()

    n, errors = sync.models.convert([box])
    assert (n, errors) == (1, [])
    assert box.data != other.data and box.data.users == 1
    (msg, cb, data), = session.of_type("upload")
    assert data[:4] == b"glTF"
    colors = [m["pbrMetallicRoughness"]["baseColorFactor"] for m in glb_json(data)["materials"]]
    assert colors and colors[0][:3] == pytest.approx([1, 0, 0], abs=1e-3)

    cb({"type": "uploaded", "url": "atp:" + msg["path"]}, None)
    out = sync.collect_outgoing()
    assert [m["type"] for m in out] == ["add"]  # nothing deleted or re-parented yet
    item, = out[0]["items"]
    assert item["props"]["type"] == "Model" and item["props"]["modelURL"] == "atp:" + msg["path"]
    assert "shape" not in item["props"]
    assert item["props"]["dimensions"] == pytest.approx({"x": 2, "y": 1, "z": 1})
    assert box.matrix_world == matrix

    # Updates for the old entity while the new one is created don't bring it back.
    sync.handle_message({"type": "changes", "upserts": [ent(eid(1), "Box")]})
    assert sum(1 for o in bpy.context.scene.objects if str(o.get(ss.P_ID, "")) == eid(1)) == 0

    new = "{99999999-0000-0000-0000-000000000000}"
    sync.handle_message({"type": "added", "items": [{"tmp": item["tmp"], "id": new, "props": dict(
        ent(new, "Model"), **item["props"])}]})
    out = {m["type"]: m for m in sync.collect_outgoing()}
    assert box[ss.P_ID] == new and box[ss.P_TYPE] == "Model" and ss.P_REPLACES not in box
    assert [(e["id"], e["props"]["parentID"]) for e in out["edit"]["edits"]] == [(eid(3), new)]
    assert out["delete"]["ids"] == [eid(1)]
    assert child.parent == box
    assert sync.collect_outgoing() == []


def test_refused_conversion_relinks_the_original(linked):
    sync, session, snapshot = linked
    snapshot(ent(eid(1), "Box"))
    box = sync.object_for(eid(1))
    sync.models.convert([box])
    (msg, cb, _), = session.of_type("upload")
    cb({"type": "uploaded", "url": "atp:" + msg["path"]}, None)
    item, = sync.collect_outgoing()[0]["items"]
    reply = sync.handle_message({"type": "added", "items": [{"tmp": item["tmp"], "id": None}]})
    assert reply == [{"type": "sync"}]
    assert box[ss.P_ID] == eid(1) and box[ss.P_TYPE] == "Box" and ss.P_URL not in box
    snapshot(ent(eid(1), "Box"))
    assert sync.object_for(eid(1)) == box and eid(1) in sync.records


def test_only_real_edits_mark_published_models_for_upload(linked):
    sync, _session, _snapshot = linked
    bpy.ops.mesh.primitive_monkey_add()
    obj = bpy.context.active_object
    ss._set_object_color(obj, (1, 1, 1))
    obj[ss.P_ID], obj[ss.P_TYPE] = eid(1), "Model"
    obj[models.P_LOADED] = obj[ss.P_URL] = "atp:/blender/Suzanne-0123456789ab.glb"
    bpy.context.view_layer.update()
    dirty = sync.models._dirty

    def handler(scene, depsgraph):
        sync.models.on_depsgraph_update(depsgraph)

    bpy.app.handlers.depsgraph_update_post.append(handler)
    try:
        bpy.ops.mesh.primitive_cube_add()  # tags every object's shading
        bpy.ops.object.duplicate()
        bpy.context.view_layer.update()
        assert obj.name not in dirty
        ss._set_object_color(obj, (1, 0, 0))
        bpy.context.view_layer.update()
        assert obj.name in dirty
        dirty.clear()
        obj.data.vertices[0].co.x += 1
        obj.data.update()
        bpy.context.view_layer.update()
        assert obj.name in dirty
    finally:
        bpy.app.handlers.depsgraph_update_post.remove(handler)


def test_publish_explains_primitives(linked):
    sync, _session, snapshot = linked
    snapshot(ent(eid(1), "Box"))
    n, errors = sync.models.publish([sync.object_for(eid(1))])
    assert n == 0 and "Convert to Model" in errors[0]
