"""Model entities: publish Blender meshes to Overte and load model geometry back.

Publishing
    The evaluated mesh (modifiers applied) is exported as GLB in the object's
    own space, uploaded through the bridge to the domain's asset server
    (``atp:/blender/<name>-<hash>.glb``) and becomes a Model entity. The object
    keeps its mesh, and its local bounding box becomes the entity's natural
    size (``overte_base``/``overte_center``), so moving or scaling it in either
    program keeps the two in step. Editing the mesh re-uploads it (content
    hashed, so every version gets its own URL and caches never serve stale
    geometry).

Loading
    Model entities arriving from Overte start as cube empties. Their model
    (``atp:`` through the bridge, ``http(s):`` downloaded directly) is
    imported and swapped in as a mesh object, keeping the link. Each model URL
    is fetched and imported once; every entity using it shares that mesh.

Converting
    Box, Sphere and Shape entities are drawn by Overte, so their Blender mesh
    can't be edited. Converting one publishes its mesh as a Model entity in
    its place and deletes the original.

All Blender data work happens on the main thread, inside temporary scenes so
the user's scene and selection are never touched.
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

import bpy

from . import scene_sync as ss

P_LOADED = "overte_model_loaded"     # model URL whose geometry the object shows
P_HASH = "overte_model_hash"         # content hash of the last uploaded version
P_SOURCE = "overte_model_source"     # on imported meshes: the model URL they came from
ASSET_DIR = "/blender"
PUBLISHABLE = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
IMPORTERS = {".glb": "gltf", ".gltf": "gltf", ".fbx": "fbx", ".obj": "obj"}
MAX_DOWNLOAD = 256 * 1024 * 1024
REPUBLISH_DELAY = 1.0  # seconds after the last edit before re-uploading


def _safe_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return s[:48] or "model"


def _bbox(mesh) -> tuple[tuple, tuple]:
    xs = [v.co.x for v in mesh.vertices]
    ys = [v.co.y for v in mesh.vertices]
    zs = [v.co.z for v in mesh.vertices]
    lo = (min(xs), min(ys), min(zs))
    hi = (max(xs), max(ys), max(zs))
    size = tuple(max(hi[i] - lo[i], 1e-4) for i in range(3))
    center = tuple((hi[i] + lo[i]) / 2 for i in range(3))
    return size, center


class _TempScene:
    """A throwaway scene to run import/export operators in."""

    def __enter__(self):
        self.scene = bpy.data.scenes.new("__overte_tmp__")
        return self.scene

    def __exit__(self, *exc):
        sc = self.scene
        for obj in list(sc.objects):
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if isinstance(data, bpy.types.Mesh) and data.users == 0:
                bpy.data.meshes.remove(data)
        bpy.data.scenes.remove(sc)
        return False


def export_glb(obj) -> tuple[bytes, tuple, tuple]:
    """GLB bytes of ``obj``'s evaluated geometry in its local space, plus bbox size/centre."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph),
                                           preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        if not mesh.vertices:
            raise ValueError(f"'{obj.name}' has no geometry to publish")
        # Materials linked to the object rather than its mesh (as on primitives)
        # don't come with the mesh; put them on the exported copy.
        for i, slot in enumerate(obj.material_slots):
            if slot.link == "OBJECT" and slot.material is not None:
                while len(mesh.materials) <= i:
                    mesh.materials.append(None)
                mesh.materials[i] = slot.material
        size, center = _bbox(mesh)
        fd, path = tempfile.mkstemp(suffix=".glb", prefix="overte_")
        os.close(fd)
        try:
            with _TempScene() as sc:
                tmp = bpy.data.objects.new(obj.name, mesh)
                sc.collection.objects.link(tmp)
                with bpy.context.temp_override(scene=sc, view_layer=sc.view_layers[0]):
                    bpy.ops.export_scene.gltf(
                        filepath=path, export_format="GLB", use_active_scene=True, export_yup=True,
                        export_cameras=False, export_lights=False, export_animations=False,
                        export_extras=False)
                bpy.data.objects.remove(tmp, do_unlink=True)
            with open(path, "rb") as f:
                return f.read(), size, center
        finally:
            os.unlink(path)
    finally:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def import_model(data: bytes, ext: str, name: str) -> Optional[bpy.types.Mesh]:
    """Import model bytes into a single mesh (Blender Z-up, model-local space)."""
    kind = IMPORTERS.get(ext.lower())
    if kind is None:
        raise ValueError(f"unsupported model format '{ext}'")
    # A folder of its own: importers look for textures next to the file (the
    # FBX one recursively), which in the shared temp folder can take minutes.
    tmpdir = tempfile.mkdtemp(prefix="overte_")
    path = os.path.join(tmpdir, "model" + ext.lower())
    with open(path, "wb") as f:
        f.write(data)
    meshes_before = set(bpy.data.meshes)
    result = None
    try:
        with _TempScene() as sc:
            with bpy.context.temp_override(scene=sc, view_layer=sc.view_layers[0], collection=sc.collection):
                if kind == "gltf":
                    bpy.ops.import_scene.gltf(filepath=path)
                elif kind == "fbx" and "fbx_import" in dir(bpy.ops.wm):
                    # Blender 4.5+'s native importer. The Python one switches the
                    # *window's* active object into Edit Mode to build armatures,
                    # so rigged models fail inside a temporary scene.
                    bpy.ops.wm.fbx_import(filepath=path)
                elif kind == "fbx":
                    bpy.ops.import_scene.fbx(filepath=path)
                else:
                    bpy.ops.wm.obj_import(filepath=path)
            sc.view_layers[0].update()
            meshes = [o for o in sc.objects if o.type == "MESH"]
            if not meshes:
                return None
            for o in meshes:  # make sure each mesh is single-user before joining
                if o.data.users > 1:
                    o.data = o.data.copy()
            if len(meshes) > 1:
                with bpy.context.temp_override(scene=sc, view_layer=sc.view_layers[0], active_object=meshes[0],
                                               object=meshes[0], selected_objects=meshes,
                                               selected_editable_objects=meshes):
                    bpy.ops.object.join()
            sc.view_layers[0].update()
            joined = meshes[0]
            mesh = joined.data
            mesh.transform(joined.matrix_world)  # bake the importer's transforms
            mesh.name = name
            joined.data = bpy.data.meshes.new("__overte_placeholder__")  # detach so cleanup keeps ours
            result = mesh
            return mesh
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        for m in set(bpy.data.meshes) - meshes_before:  # importer/join leftovers
            if m != result and m.users == 0:
                bpy.data.meshes.remove(m)


def _url_ext(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    return os.path.splitext(path)[1].lower()


def _url_name(url: str) -> str:
    return os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path)) or "model"


def _imported_mesh(url: str) -> Optional[bpy.types.Mesh]:
    """The mesh already imported from ``url``, which every entity using it shares."""
    for mesh in bpy.data.meshes:
        if mesh.get(P_SOURCE) == url and len(mesh.vertices):
            return mesh
    return None


def convertible(obj) -> bool:
    """A linked Box, Sphere or Shape entity that can become a Model of its mesh."""
    return obj.type == "MESH" and bool(obj.get(ss.P_ID)) and obj.get(ss.P_TYPE) in ss.PRIMITIVE_TYPES


class ModelManager:
    def __init__(self, sync, auto_load: bool = True, auto_update: bool = True):
        self.sync = sync
        self.auto_load = auto_load
        self.auto_update = auto_update
        self.in_flight = 0
        self._results: "queue.Queue[tuple]" = queue.Queue()
        self._dirty: dict[str, float] = {}       # object name -> last geometry change
        self._loading: set[str] = set()          # entity ids with a load in progress
        self._waiting: dict[str, set[str]] = {}  # model URL in flight -> entity ids that want it
        self._publishing: set[str] = set()       # object names being uploaded

    @property
    def session(self):
        return getattr(self.sync, "session", None)

    @property
    def connected(self) -> bool:
        s = self.session
        return s is not None and s.bridge_info is not None

    # ---------------------------------------------------------- publish
    def publish(self, objects) -> tuple[int, list[str]]:
        """Publish objects as Model (meshes) or Light entities. Returns (started, errors)."""
        started, errors = 0, []
        if not self.connected:
            return 0, ["no Overte bridge connected"]
        for obj in objects:
            linked = obj.get(ss.P_ID) or obj.get(ss.P_TMP)
            if obj.type == "LIGHT" and not linked:
                obj[ss.P_TYPE] = "Light"
                obj[ss.P_TMP] = ss.new_tmp_id()
                started += 1
            elif obj.type in PUBLISHABLE:
                etype = obj.get(ss.P_TYPE)
                if linked and etype in ss.PRIMITIVE_TYPES:
                    errors.append(f"'{obj.name}' is a {etype} entity, whose shape Overte draws itself; "
                                  "use Convert to Model to replace it with this mesh")
                    continue
                if linked and etype != "Model":
                    errors.append(f"'{obj.name}' is already a {etype} entity")
                    continue
                try:
                    if self._upload(obj):
                        started += 1
                    else:
                        errors.append(f"'{obj.name}' is unchanged since its last upload")
                except Exception as exc:  # export problems are reported, not fatal
                    errors.append(f"'{obj.name}': {exc}")
            else:
                errors.append(f"'{obj.name}': {obj.type.lower()} objects can't be published")
        return started, errors

    def convert(self, objects) -> tuple[int, list[str]]:
        """Replace Box, Sphere and Shape entities with Model entities of their meshes,
        in place. Each original entity is deleted once its replacement exists."""
        started, errors = 0, []
        if not self.connected:
            return 0, ["no Overte bridge connected"]
        for obj in objects:
            if not convertible(obj):
                errors.append(f"'{obj.name}' is not a Box, Sphere or Shape entity")
                continue
            if obj.data.users > 1:
                # Primitives share one unit mesh; give this one its own to edit.
                obj.data = obj.data.copy()
                obj.data.use_fake_user = False
                obj.data.name = obj.name
            try:
                self._upload(obj, replace=True)
                started += 1
            except Exception as exc:
                errors.append(f"'{obj.name}': {exc}")
        return started, errors

    def _upload(self, obj, replace: bool = False) -> bool:
        """Export and upload ``obj``; False when the content is unchanged since the last upload.

        ``replace``: the object's primitive entity is replaced by the new Model."""
        data, size, center = export_glb(obj)
        digest = hashlib.sha256(data).hexdigest()[:12]
        if (not replace and obj.get(P_HASH) == digest and obj.get(ss.P_URL)
                and (obj.get(ss.P_ID) or obj.get(ss.P_TMP))):
            return False
        path = f"{ASSET_DIR}/{_safe_name(obj.name)}-{digest}.glb"
        name = obj.name
        self.in_flight += 1
        self._publishing.add(name)
        self._dirty.pop(name, None)

        def done(reply, _binary):
            self.in_flight -= 1
            self._publishing.discard(name)
            self._results.put(("uploaded", name, reply, size, center, digest, replace))

        self.session.request({"type": "upload", "path": path, "size": len(data)}, done, binary=data)
        return True

    def _finish_upload(self, name, reply, size, center, digest, replace=False) -> None:
        obj = self.sync.scene.objects.get(name)
        if obj is None:
            return
        if reply.get("error") or not reply.get("url"):
            self.sync.log(f"Upload of '{name}' failed: {reply.get('error') or reply.get('message')}")
            return
        url = reply["url"]
        if replace and convertible(obj):
            self.sync.begin_replace(obj)  # the object becomes a new (Model) entity
        obj[ss.P_BASE] = size
        obj[ss.P_CENTER] = center
        obj[ss.P_URL] = url
        obj[P_LOADED] = url
        obj[P_HASH] = digest
        obj[ss.P_TYPE] = "Model"
        if not obj.get(ss.P_ID) and not obj.get(ss.P_TMP):
            obj[ss.P_TMP] = ss.new_tmp_id()  # create the entity on the next sync tick
        # A linked object: modelURL and dimensions change, and the regular
        # sync sends them as an edit.
        self.sync.log(f"Published '{name}' as {url}")

    # ------------------------------------------------------- auto update
    def on_depsgraph_update(self, depsgraph) -> None:
        if not self.auto_update:
            return
        now = time.time()
        meshes = set()
        for upd in depsgraph.updates:
            idb = upd.id.original if hasattr(upd.id, "original") else upd.id
            if isinstance(idb, bpy.types.Object):
                # Every object reports a shading update whenever the scene's
                # relations change (e.g. another object is added), so only
                # geometry counts here; material edits arrive as Material updates.
                if upd.is_updated_geometry and self._is_published(idb):
                    self._dirty[idb.name] = now
            elif isinstance(idb, (bpy.types.Mesh, bpy.types.Material)):
                if upd.is_updated_geometry or upd.is_updated_shading:
                    meshes.add(idb)
        if meshes:
            for obj in self.sync.scene.objects:
                if self._is_published(obj) and (obj.data in meshes or
                                                any(s.material in meshes for s in obj.material_slots)):
                    self._dirty[obj.name] = now

    def _is_published(self, obj) -> bool:
        return (obj.get(ss.P_TYPE) == "Model" and obj.type in PUBLISHABLE and bool(obj.get(ss.P_ID))
                and str(obj.get(P_LOADED, "")).startswith(f"atp:{ASSET_DIR}/"))

    # --------------------------------------------------------- loading
    def maybe_load(self, obj, url: str) -> None:
        """Called for Model entities whose modelURL is new or changed."""
        if not self.auto_load or not url or obj.get(P_LOADED) == url:
            return
        self.load(obj, url)

    def load(self, obj, url: str) -> None:
        eid = ss.norm_id(obj.get(ss.P_ID))
        if eid in self._loading:
            return
        ext = _url_ext(url)
        if ext not in IMPORTERS:
            self.sync.log(f"Model '{obj.name}': can't load '{ext or url}' files")
            obj[P_LOADED] = url  # don't retry
            return
        if not url.startswith(("atp:", "http://", "https://")):
            obj[P_LOADED] = url
            return
        self._loading.add(eid)
        waiting = self._waiting.get(url)
        if waiting is not None:  # the same model is already on its way
            waiting.add(eid)
            return
        self._waiting[url] = {eid}
        if _imported_mesh(url) is not None:
            # Already imported for another entity: share it (applied on the next tick).
            self._results.put(("loaded", url, ext, None, None))
            return
        self.in_flight += 1
        if url.startswith("atp:"):
            def done(reply, data):
                self.in_flight -= 1
                self._results.put(("loaded", url, ext, data, reply.get("error")))
            if not self.connected:
                done({"error": "no bridge"}, None)
            else:
                self.session.request({"type": "fetch", "url": url}, done)
        else:
            threading.Thread(target=self._download, args=(url, ext), daemon=True).start()

    def _download(self, url, ext) -> None:
        data, err = None, None
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read(MAX_DOWNLOAD + 1)
            if len(data) > MAX_DOWNLOAD:
                data, err = None, "model file too large"
        except Exception as exc:
            err = str(exc)
        self.in_flight -= 1
        self._results.put(("loaded", url, ext, data, err))

    def _finish_load(self, url, ext, data, err) -> None:
        targets = []
        for eid in self._waiting.pop(url, ()):
            self._loading.discard(eid)
            proxy = self.sync.object_for(eid)
            # Skip entities whose model changed while this one was on its way.
            if proxy is not None and str(proxy.get(ss.P_URL, "")) == url:
                targets.append((eid, proxy))
        if not targets:
            return
        mesh = _imported_mesh(url)
        if mesh is None:
            names = ", ".join(f"'{p.name}'" for _e, p in targets)
            if err or not data:
                self.sync.log(f"Could not load model for {names}: {err}")
            else:
                try:
                    mesh = import_model(data, ext, f"Overte {_url_name(url)}")
                except Exception as exc:
                    self.sync.log(f"Could not import model for {names}: {exc}")
            if mesh is None or not mesh.vertices:
                for _eid, proxy in targets:
                    proxy[P_LOADED] = url  # don't retry
                return
            mesh[P_SOURCE] = url
        size, center = _bbox(mesh)
        for eid, proxy in targets:
            if proxy.data != mesh:
                self.sync.replace_object_data(proxy, mesh, size, center)
            obj = self.sync.object_for(eid)
            if obj is not None:
                obj[P_LOADED] = url

    # ------------------------------------------------------------ tick
    def tick(self) -> None:
        while True:
            try:
                item = self._results.get_nowait()
            except queue.Empty:
                break
            try:
                if item[0] == "uploaded":
                    self._finish_upload(*item[1:])
                else:
                    self._finish_load(*item[1:])
            except Exception as exc:
                self.sync.log(f"model update failed: {exc}")
        if self._dirty and self.connected:
            now = time.time()
            for name, t in list(self._dirty.items()):
                if now - t < REPUBLISH_DELAY or name in self._publishing:
                    continue
                obj = self.sync.scene.objects.get(name)
                if obj is None or not self._is_published(obj):
                    self._dirty.pop(name, None)
                    continue
                if obj.mode == "EDIT":
                    continue  # wait until the user leaves edit mode
                self._dirty.pop(name, None)
                try:
                    self._upload(obj)
                except Exception as exc:
                    self.sync.log(f"Could not update model '{name}': {exc}")
