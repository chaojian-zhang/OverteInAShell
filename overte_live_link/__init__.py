"""Overte Live Link: edit an Overte domain's entities live from Blender."""

bl_info = {  # legacy add-on metadata (Blender < 4.2); 4.2+ reads blender_manifest.toml
    "name": "Overte Live Link",
    "author": "OverteInAShell contributors",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Overte",
    "description": "Live two-way link between Blender and an Overte domain",
    "category": "3D View",
}

try:
    import bpy  # noqa: F401
except ImportError:  # imported outside Blender (unit tests): pure modules still work
    bpy = None


def register():
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
