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
