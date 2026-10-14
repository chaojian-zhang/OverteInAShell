"""The add-on registers in headless Blender and its operators create linkable objects."""

import pytest

bpy = pytest.importorskip("bpy")

import overte_live_link  # noqa: E402
from overte_live_link import scene_sync as ss  # noqa: E402


@pytest.fixture
def addon():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    overte_live_link.register()
    yield
    overte_live_link.unregister()


@pytest.mark.parametrize("kind,etype,obj_type", [
    ("Box", "Box", "MESH"), ("Sphere", "Sphere", "MESH"), ("Cylinder", "Shape", "MESH"),
    ("SpotLight", "Light", "LIGHT"), ("Zone", "Zone", "EMPTY"), ("Text", "Text", "EMPTY"),
])
def test_add_entity_operator(addon, kind, etype, obj_type):
    bpy.context.scene.cursor.location = (1, 2, 3)
    assert bpy.ops.overte.add_entity("EXEC_DEFAULT", kind=kind) == {"FINISHED"}
    obj = bpy.context.active_object
    assert obj.type == obj_type and obj[ss.P_TYPE] == etype and obj.get(ss.P_TMP)
    assert tuple(obj.location) == (1, 2, 3)
    assert obj.name in bpy.data.collections[ss.COLLECTION].objects


def test_model_needs_url(addon):
    with pytest.raises(RuntimeError):
        bpy.ops.overte.add_entity("EXEC_DEFAULT", kind="Model")
    bpy.ops.overte.add_entity("EXEC_DEFAULT", kind="Model", model_url="https://example.com/a.glb")
    assert bpy.context.active_object[ss.P_URL] == "https://example.com/a.glb"


def test_start_stop_and_unlink(addon):
    st = bpy.context.scene.overte_link
    st.port = 0  # any free port
    assert bpy.ops.overte.start() == {"FINISHED"}
    from overte_live_link import runtime
    assert runtime.session is not None and runtime.session.port > 0
    bpy.ops.overte.add_entity("EXEC_DEFAULT", kind="Box")
    obj = bpy.context.active_object
    bpy.ops.overte.unlink()
    assert ss.P_TMP not in obj
    bpy.ops.overte.stop()
    assert runtime.session is None


def test_glb_export_import_roundtrip(addon):
    import bmesh
    from overte_live_link import models

    mesh = bpy.data.meshes.new("Wedge")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=(1.0, 2.0, 3.0), verts=bm.verts)        # 1 x 2 x 3
    bmesh.ops.translate(bm, vec=(0.0, 0.0, 5.0), verts=bm.verts)    # centred 5 up
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("Wedge", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = (10, 0, 0)  # object transform must not be baked into the model
    n_objects = len(bpy.data.objects)

    data, size, center = models.export_glb(obj)
    assert data[:4] == b"glTF"
    assert size == pytest.approx((1, 2, 3)) and center == pytest.approx((0, 0, 5))
    assert len(bpy.data.objects) == n_objects  # no temp objects left behind

    back = models.import_model(data, ".glb", "WedgeBack")
    size2, center2 = models._bbox(back)
    assert size2 == pytest.approx((1, 2, 3), abs=1e-5)
    assert center2 == pytest.approx((0, 0, 5), abs=1e-5)
    assert len(bpy.data.objects) == n_objects and len(bpy.data.scenes) == 1


def test_dirty_tracking_finds_changes(addon):
    sync = ss.SceneSync(bpy.context.scene, log=lambda m: None)
    col = ss.overte_collection(bpy.context.scene)
    objs = []
    for i in range(100):
        o = bpy.data.objects.new(f"B{i}", ss.unit_mesh("Cube"))
        o.material_slots[0].link = "OBJECT"
        o[ss.P_ID] = "{%08d-0000-0000-0000-000000000000}" % i
        o[ss.P_TYPE] = "Box"
        col.objects.link(o)
        objs.append(o)
    bpy.context.view_layer.update()
    for o in objs:
        rec = ss.Record(ss.norm_id(o[ss.P_ID]), "Box", o.name)
        rec.local = sync.local_fields(o, "Box")
        sync.records[rec.id] = rec
    sync.synced = True
    sync.track_dirty = True

    def handler(scene, depsgraph):
        sync.on_depsgraph_update(depsgraph)

    bpy.app.handlers.depsgraph_update_post.append(handler)
    try:
        assert sync.collect_outgoing() == []
        objs[42].location.z = 3
        bpy.context.view_layer.update()
        out = sync.collect_outgoing()
        assert [e["id"] for e in out[0]["edits"]] == [ss.norm_id(objs[42][ss.P_ID])]
        # Colour change through the material (a data-level update).
        ss._set_object_color(objs[7], (0, 0, 1))
        bpy.context.view_layer.update()
        out = sync.collect_outgoing()
        assert out and out[0]["edits"][0]["props"].get("color") == {"red": 0, "green": 0, "blue": 255}
    finally:
        bpy.app.handlers.depsgraph_update_post.remove(handler)

    # A change the handler never saw is still found by the round-robin scan.
    objs[99].location.x = 5
    bpy.context.view_layer.update()
    found = []
    for _ in range(sync.SCAN_TICKS + 1):
        for msg in sync.collect_outgoing():
            found += [e["id"] for e in msg.get("edits", [])]
    assert ss.norm_id(objs[99][ss.P_ID]) in found
