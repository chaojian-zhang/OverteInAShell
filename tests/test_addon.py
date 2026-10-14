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
