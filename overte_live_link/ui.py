"""Sidebar panel, settings and operators."""

import uuid

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from . import runtime
from . import scene_sync as ss


# ------------------------------------------------------------- settings
def _live_update(self, context):
    """Apply changed settings to a running session."""
    s = runtime.session
    if s is None:
        return
    s.sync.sync_deletes = self.sync_deletes
    s.sync.models.auto_load = self.auto_load_models
    s.sync.models.auto_update = self.auto_update_models


class OverteLinkSettings(bpy.types.PropertyGroup):
    host: StringProperty(
        name="Listen address", default="127.0.0.1",
        description="Address the link server binds to. Use 0.0.0.0 when the Overte domain "
                    "runs on another machine and must connect to this computer")
    port: IntProperty(name="Port", default=9420, min=0, max=65535,
                      description="TCP port for the link (0 picks any free port)")
    sync_deletes: BoolProperty(
        name="Sync deletions", default=True, update=_live_update,
        description="Deleting a linked object in Blender deletes its entity in Overte")
    auto_load_models: BoolProperty(
        name="Load model geometry", default=True, update=_live_update,
        description="Download and show the geometry of Model entities (otherwise they stay as boxes)")
    auto_update_models: BoolProperty(
        name="Re-upload edited models", default=True, update=_live_update,
        description="When a mesh published from Blender is edited, upload the new version automatically")
    token: StringProperty(
        name="Access token", default="",
        description="Secret the bridge must present. Saved with the .blend so the URL in your "
                    "domain settings keeps working; generated on first start")


def _settings(context) -> OverteLinkSettings:
    return context.scene.overte_link


def _linked(context):
    obj = context.active_object
    return obj if obj is not None and (obj.get(ss.P_ID) or obj.get(ss.P_TMP)) else None


# ------------------------------------------------------------ operators
class OVERTE_OT_start(bpy.types.Operator):
    bl_idname = "overte.start"
    bl_label = "Start Live Link"
    bl_description = "Start the server the Overte bridge script connects to"

    def execute(self, context):
        st = _settings(context)
        if not st.token:
            from .session import new_token
            st.token = new_token()
        try:
            runtime.start(context.scene, st.host, st.port, st.sync_deletes,
                          st.auto_load_models, st.auto_update_models, token=st.token)
        except OSError as exc:
            self.report({"ERROR"}, f"Cannot listen on {st.host}:{st.port}: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class OVERTE_OT_stop(bpy.types.Operator):
    bl_idname = "overte.stop"
    bl_label = "Stop"
    bl_description = "Stop the live link. Objects stay linked and resync on the next start"

    def execute(self, context):
        runtime.stop()
        return {"FINISHED"}


class OVERTE_OT_copy_bridge_url(bpy.types.Operator):
    bl_idname = "overte.copy_bridge_url"
    bl_label = "Copy Bridge URL"
    bl_description = "Copy the URL of the bridge script, for the domain's Persistent Scripts or Interface"

    def execute(self, context):
        s = runtime.session
        if s is None:
            return {"CANCELLED"}
        context.window_manager.clipboard = s.bridge_url()
        self.report({"INFO"}, f"Copied {s.bridge_url()}")
        return {"FINISHED"}


class OVERTE_OT_new_token(bpy.types.Operator):
    bl_idname = "overte.new_token"
    bl_label = "New Access Token"
    bl_description = ("Replace the access token. Bridges using the old URL can no longer connect; "
                      "update the URL in your domain settings")

    def execute(self, context):
        from .session import new_token
        _settings(context).token = new_token()
        return {"FINISHED"}


class OVERTE_OT_save_bridge(bpy.types.Operator):
    bl_idname = "overte.save_bridge"
    bl_label = "Save Bridge Script…"
    bl_description = ("Save the bridge script with this computer's WebSocket address filled in, "
                      "to host it yourself (e.g. on the domain's asset server)")

    filepath: StringProperty(subtype="FILE_PATH", default="overte_bridge.js")
    ws_host: StringProperty(name="Address Overte connects to", default="127.0.0.1")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        from .session import bridge_script
        st = _settings(context)
        port = runtime.session.port if runtime.session else st.port
        q = f"?token={st.token}" if st.token else ""
        with open(bpy.path.abspath(self.filepath), "w", encoding="utf-8") as f:
            f.write(bridge_script(f"ws://{self.ws_host}:{port}/ws{q}"))
        self.report({"INFO"}, f"Saved {self.filepath}")
        return {"FINISHED"}


class OVERTE_OT_resync(bpy.types.Operator):
    bl_idname = "overte.resync"
    bl_label = "Resync"
    bl_description = "Fetch the full entity list from Overte again"

    @classmethod
    def poll(cls, context):
        return runtime.session is not None and runtime.session.bridge_info is not None

    def execute(self, context):
        s = runtime.session
        for msg in s.sync.request_resync():
            s.send(msg)
        return {"FINISHED"}


ENTITY_KINDS = [
    ("Box", "Box", "Box primitive", "MESH_CUBE", 0),
    ("Sphere", "Sphere", "Sphere primitive", "MESH_UVSPHERE", 1),
    ("Cylinder", "Cylinder", "Cylinder shape", "MESH_CYLINDER", 2),
    ("Cone", "Cone", "Cone shape", "MESH_CONE", 3),
    ("PointLight", "Point Light", "Light entity", "LIGHT_POINT", 4),
    ("SpotLight", "Spot Light", "Spot light entity", "LIGHT_SPOT", 5),
    ("Zone", "Zone", "Zone entity (skybox, ambient light, ...)", "CUBE", 6),
    ("Text", "Text", "Text entity", "FONT_DATA", 7),
    ("Model", "Model (URL)", "Model entity loaded from a URL (glTF, FBX, OBJ)", "FILE_3D", 8),
]


class OVERTE_OT_add_entity(bpy.types.Operator):
    bl_idname = "overte.add_entity"
    bl_label = "Add Overte Entity"
    bl_description = "Create a new entity in Overte at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(name="Type", items=ENTITY_KINDS)
    model_url: StringProperty(name="Model URL", default="")
    text: StringProperty(name="Text", default="Text")

    def invoke(self, context, event):
        if self.kind in ("Model", "Text"):
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def execute(self, context):
        cursor = context.scene.cursor.location
        k = self.kind
        if k in ("Box", "Sphere", "Cylinder", "Cone"):
            etype = {"Box": "Box", "Sphere": "Sphere"}.get(k, "Shape")
            shape = {"Box": "Cube"}.get(k, k)
            obj = bpy.data.objects.new(k, ss.unit_mesh(shape))
            obj[ss.P_SHAPE] = shape
            obj.material_slots[0].link = "OBJECT"
            ss._set_object_color(obj, (0.8, 0.8, 0.8))
        elif k in ("PointLight", "SpotLight"):
            etype = "Light"
            light = bpy.data.lights.new(k, "SPOT" if k == "SpotLight" else "POINT")
            light.energy = ss.LIGHT_WATTS_PER_INTENSITY
            obj = bpy.data.objects.new(k, light)
            obj.scale = (10, 10, 10)  # light range (entity dimensions)
        else:
            etype = k
            obj = bpy.data.objects.new(k, None)
            obj.empty_display_type = "CUBE"
            obj.empty_display_size = 0.5
            if k == "Zone":
                obj.scale = (20, 20, 20)
            elif k == "Text":
                obj.scale = (1.0, 0.01, 0.3)
                obj[ss.P_TEXT] = self.text
            elif k == "Model":
                if not self.model_url:
                    self.report({"ERROR"}, "A model URL is required")
                    return {"CANCELLED"}
                obj[ss.P_URL] = self.model_url
        obj[ss.P_TYPE] = etype
        obj[ss.P_TMP] = str(uuid.uuid4())
        obj.location = cursor
        ss.overte_collection(context.scene).objects.link(obj)
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        if runtime.session is None or runtime.session.bridge_info is None:
            self.report({"WARNING"}, "Not connected: the entity is created when the bridge connects")
        return {"FINISHED"}


class OVERTE_OT_publish(bpy.types.Operator):
    bl_idname = "overte.publish"
    bl_label = "Publish to Overte"
    bl_description = ("Upload the selected meshes to the domain's asset server as Model entities "
                      "(or update them), and add selected lights as Light entities")

    @classmethod
    def poll(cls, context):
        return (runtime.session is not None and runtime.session.bridge_info is not None
                and bool(context.selected_objects))

    def execute(self, context):
        n, errors = runtime.session.sync.models.publish(context.selected_objects)
        for e in errors:
            self.report({"WARNING"}, e)
        if n:
            self.report({"INFO"}, f"Publishing {n} object(s)")
        return {"FINISHED"} if n else {"CANCELLED"}


class OVERTE_OT_load_model(bpy.types.Operator):
    bl_idname = "overte.load_model"
    bl_label = "Load Model Geometry"
    bl_description = "Download the selected Model entities' models and show their geometry"

    @classmethod
    def poll(cls, context):
        return runtime.session is not None and any(
            o.get(ss.P_TYPE) == "Model" and o.get(ss.P_URL) and o.get(ss.P_ID) for o in context.selected_objects)

    def execute(self, context):
        models = runtime.session.sync.models
        n = 0
        for o in context.selected_objects:
            if o.get(ss.P_TYPE) == "Model" and o.get(ss.P_URL) and o.get(ss.P_ID):
                models.load(o, str(o[ss.P_URL]))
                n += 1
        self.report({"INFO"}, f"Loading {n} model(s)")
        return {"FINISHED"}


class OVERTE_OT_unlink(bpy.types.Operator):
    bl_idname = "overte.unlink"
    bl_label = "Unlink"
    bl_description = "Detach the selected objects from their entities (the entities are kept)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        n = 0
        for obj in context.selected_objects:
            if obj.get(ss.P_ID) or obj.get(ss.P_TMP):
                if runtime.session:
                    runtime.session.sync.unlink(obj)
                else:
                    for k in (ss.P_ID, ss.P_TMP):
                        if k in obj:
                            del obj[k]
                n += 1
        self.report({"INFO"}, f"Unlinked {n} object(s)")
        return {"FINISHED"}


class OVERTE_OT_recreate_missing(bpy.types.Operator):
    bl_idname = "overte.recreate_missing"
    bl_label = "Recreate in Overte"
    bl_description = "Create new entities for linked objects whose entity no longer exists"

    @classmethod
    def poll(cls, context):
        return runtime.session is not None and bool(runtime.session.sync.missing_ids)

    def execute(self, context):
        n = runtime.session.sync.recreate_missing()
        self.report({"INFO"}, f"Recreating {n} entit{'y' if n == 1 else 'ies'}")
        return {"FINISHED"}


class OVERTE_OT_select_missing(bpy.types.Operator):
    bl_idname = "overte.select_missing"
    bl_label = "Select"
    bl_description = "Select linked objects whose entity no longer exists in the domain"

    @classmethod
    def poll(cls, context):
        return runtime.session is not None and bool(runtime.session.sync.missing_ids)

    def execute(self, context):
        missing = runtime.session.sync.missing_ids
        for obj in context.scene.objects:
            eid = obj.get(ss.P_ID)
            obj.select_set(bool(eid) and ss.norm_id(eid) in missing)
        return {"FINISHED"}


# ---------------------------------------------------------------- panel
class OVERTE_MT_add(bpy.types.Menu):
    bl_idname = "OVERTE_MT_add"
    bl_label = "Add Entity"

    def draw(self, context):
        for ident, name, _d, icon, _n in ENTITY_KINDS:
            self.layout.operator(OVERTE_OT_add_entity.bl_idname, text=name, icon=icon).kind = ident


class OVERTE_PT_link(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Overte"
    bl_label = "Overte Live Link"

    def draw(self, context):
        layout = self.layout
        st = _settings(context)
        s = runtime.session
        if s is None:
            col = layout.column(align=True)
            col.prop(st, "host")
            col.prop(st, "port")
            layout.prop(st, "sync_deletes")
            layout.prop(st, "auto_load_models")
            layout.prop(st, "auto_update_models")
            row = layout.row(align=True)
            row.label(text="Token: " + (st.token or "(generated on start)"))
            row.operator(OVERTE_OT_new_token.bl_idname, text="", icon="FILE_REFRESH")
            layout.operator(OVERTE_OT_start.bl_idname, icon="PLAY")
        else:
            row = layout.row()
            row.label(text=f"Listening on {s.server.host}:{s.port}", icon="LINKED")
            row.operator(OVERTE_OT_stop.bl_idname, text="", icon="PAUSE")
            col = layout.column(align=True)
            col.prop(st, "sync_deletes")
            col.prop(st, "auto_load_models")
            col.prop(st, "auto_update_models")
            info = s.bridge_info
            box = layout.box()
            if info is None:
                box.label(text="Waiting for the Overte bridge…", icon="TIME")
                box.label(text="Add this URL as a domain Persistent Script")
                box.label(text="or load it in Interface (Running Scripts):")
                row = box.row(align=True)
                row.label(text=s.bridge_url())
                row.operator(OVERTE_OT_copy_bridge_url.bl_idname, text="", icon="COPYDOWN")
                box.operator(OVERTE_OT_save_bridge.bl_idname, icon="FILE_SCRIPT")
            else:
                sync = s.sync
                mode = "domain agent" if info.get("mode") == "agent" else "Interface"
                box.label(text=f"Bridge connected ({mode})", icon="CHECKMARK")
                if not info.get("canRez", True):
                    box.label(text="No rez permission: edits will be rejected", icon="ERROR")
                if not sync.synced:
                    box.label(text="Loading entities…", icon="TIME")
                else:
                    box.label(text=f"{len(sync.records)} entities linked")
                if sync.models.in_flight:
                    box.label(text=f"Transferring {sync.models.in_flight} model(s)…", icon="SORTTIME")
                row = box.row(align=True)
                row.operator(OVERTE_OT_resync.bl_idname, icon="FILE_REFRESH")
                row.operator(OVERTE_OT_publish.bl_idname, text="Publish", icon="EXPORT")
                if sync.missing_ids:
                    b2 = layout.box()
                    b2.label(text=f"{len(sync.missing_ids)} linked object(s) not in domain", icon="ERROR")
                    r = b2.row(align=True)
                    r.operator(OVERTE_OT_select_missing.bl_idname)
                    r.operator(OVERTE_OT_recreate_missing.bl_idname)
            if s.last_error:
                layout.label(text=s.last_error, icon="ERROR")
        layout.menu(OVERTE_MT_add.bl_idname, icon="ADD")
        for line in runtime.messages[-3:]:
            layout.label(text=line)


class OVERTE_PT_entity(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Overte"
    bl_label = "Entity"

    @classmethod
    def poll(cls, context):
        return _linked(context) is not None

    def draw(self, context):
        obj = _linked(context)
        layout = self.layout
        etype = obj.get(ss.P_TYPE, "?")
        layout.label(text=f"{etype}" + (f" ({obj[ss.P_SHAPE]})" if obj.get(ss.P_SHAPE) else ""), icon="OBJECT_DATA")
        if obj.get(ss.P_ID):
            row = layout.row()
            row.enabled = False
            row.label(text=str(obj[ss.P_ID]))
        else:
            layout.label(text="Pending creation…", icon="TIME")
        if ss.P_URL in obj:
            layout.prop(obj, f'["{ss.P_URL}"]', text="Model URL")
        if ss.P_TEXT in obj:
            layout.prop(obj, f'["{ss.P_TEXT}"]', text="Text")
        if etype == "Model" and obj.get(ss.P_ID):
            row = layout.row(align=True)
            row.operator(OVERTE_OT_load_model.bl_idname, text="Load Model", icon="IMPORT")
            if obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
                row.operator(OVERTE_OT_publish.bl_idname, text="Re-upload", icon="EXPORT")
        layout.prop(obj, "hide_render", text="Hidden in Overte", toggle=False)
        layout.operator(OVERTE_OT_unlink.bl_idname, icon="UNLINKED")


def _add_menu(self, context):
    self.layout.menu(OVERTE_MT_add.bl_idname, icon="WORLD")


def _object_menu(self, context):
    self.layout.operator(OVERTE_OT_publish.bl_idname, icon="WORLD")


CLASSES = (
    OverteLinkSettings,
    OVERTE_OT_start, OVERTE_OT_stop, OVERTE_OT_copy_bridge_url, OVERTE_OT_new_token, OVERTE_OT_save_bridge,
    OVERTE_OT_resync, OVERTE_OT_add_entity, OVERTE_OT_publish, OVERTE_OT_load_model, OVERTE_OT_unlink, OVERTE_OT_recreate_missing,
    OVERTE_OT_select_missing, OVERTE_MT_add, OVERTE_PT_link, OVERTE_PT_entity,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.overte_link = PointerProperty(type=OverteLinkSettings)
    bpy.types.VIEW3D_MT_add.append(_add_menu)
    bpy.types.VIEW3D_MT_object.append(_object_menu)
    bpy.app.handlers.load_pre.append(runtime._on_load_pre)
    bpy.app.handlers.depsgraph_update_post.append(runtime._on_depsgraph_update)


def unregister():
    runtime.stop()
    if runtime._on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(runtime._on_load_pre)
    if runtime._on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(runtime._on_depsgraph_update)
    bpy.types.VIEW3D_MT_object.remove(_object_menu)
    bpy.types.VIEW3D_MT_add.remove(_add_menu)
    del bpy.types.Scene.overte_link
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
