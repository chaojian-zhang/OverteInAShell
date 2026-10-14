"""Blender <-> Overte transform conversion (pure Python, no ``bpy``).

Conventions
-----------
* Blender: right-handed, **Z up**, quaternions as ``(w, x, y, z)``.
* Overte:  right-handed, **Y up** (same as glTF), JSON vectors ``{x, y, z}``,
  quaternions ``{x, y, z, w}``. Both use metres.

The basis change is a -90 degree rotation about X:
``Blender (x, y, z) -> Overte (x, z, -y)``.

Entities vs objects
-------------------
An Overte entity has ``position`` (the centre of its box, with the default
``registrationPoint`` of 0.5), ``rotation`` and ``dimensions`` (its size along
its own local axes). A Blender object has a location (its origin), a rotation
and a *scale*. To relate the two, every linked object stores:

* ``base_size``: the object's unscaled local bounding-box size
  (1,1,1 for the unit primitives this add-on creates), and
* ``center``: the unscaled local bounding-box centre (0,0,0 for primitives).

Then ``dimensions = scale * base_size`` and
``position = location + rotation * (scale * center)``.
This lets an arbitrary mesh (origin anywhere) be published as a Model entity
and still move consistently in both directions.

Some entity types face a direction (lights shine along their local -Z in
Overte, as in Blender). Blender's local -Z would otherwise map to Overte's
local -Y, so those kinds apply an extra local correction rotation.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence, Tuple

Vec3 = Tuple[float, float, float]
Quat = Tuple[float, float, float, float]  # (w, x, y, z)

_H = math.sqrt(0.5)
# Local-frame correction for kinds whose "forward" must stay -Z (lights).
# Rx(-90deg): Overte local -Z -> what Blender's -Z becomes after conversion.
LIGHT_CORRECTION: Quat = (_H, -_H, 0.0, 0.0)
IDENTITY: Quat = (1.0, 0.0, 0.0, 0.0)

# Entity types whose local frame needs LIGHT_CORRECTION.
DIRECTIONAL_TYPES = frozenset({"Light"})


# ---------------------------------------------------------------- vectors
def b2o_vec(v: Sequence[float]) -> Vec3:
    return (float(v[0]), float(v[2]), -float(v[1]))


def o2b_vec(v: Sequence[float]) -> Vec3:
    return (float(v[0]), -float(v[2]), float(v[1]))


def b2o_size(v: Sequence[float]) -> Vec3:
    """Sizes are magnitudes: just swap axes, no sign."""
    return (abs(float(v[0])), abs(float(v[2])), abs(float(v[1])))


def o2b_size(v: Sequence[float]) -> Vec3:
    return (abs(float(v[0])), abs(float(v[2])), abs(float(v[1])))


# ------------------------------------------------------------- quaternions
def q_mul(a: Quat, b: Quat) -> Quat:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def q_conj(q: Quat) -> Quat:
    return (q[0], -q[1], -q[2], -q[3])


def q_normalize(q: Sequence[float]) -> Quat:
    n = math.sqrt(sum(c * c for c in q))
    if n < 1e-12:
        return IDENTITY
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def q_rotate(q: Quat, v: Sequence[float]) -> Vec3:
    w, x, y, z = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v)
    tx = 2 * (y * vz - z * vy)
    ty = 2 * (z * vx - x * vz)
    tz = 2 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def q_canonical(q: Quat) -> Quat:
    """q and -q are the same rotation; prefer w >= 0 for stable comparisons."""
    return q if q[0] >= 0 else (-q[0], -q[1], -q[2], -q[3])


def b2o_quat(q: Sequence[float]) -> Quat:
    """Change of basis for a rotation: rotate its axis like a vector."""
    w, x, y, z = q
    return (float(w), float(x), float(z), -float(y))


def o2b_quat(q: Sequence[float]) -> Quat:
    w, x, y, z = q
    return (float(w), float(x), -float(z), float(y))


# --------------------------------------------------------- JSON helpers
def vec_json(v: Sequence[float]) -> dict:
    return {"x": v[0], "y": v[1], "z": v[2]}


def quat_json(q: Quat) -> dict:
    """(w, x, y, z) -> Overte {x, y, z, w}."""
    return {"x": q[1], "y": q[2], "z": q[3], "w": q[0]}


def json_vec(d, default: Vec3 = (0.0, 0.0, 0.0)) -> Vec3:
    if not d:
        return default
    return (float(d.get("x", 0.0)), float(d.get("y", 0.0)), float(d.get("z", 0.0)))


def json_quat(d) -> Quat:
    if not d:
        return IDENTITY
    return q_normalize((float(d.get("w", 1.0)), float(d.get("x", 0.0)),
                        float(d.get("y", 0.0)), float(d.get("z", 0.0))))


# ---------------------------------------------------- object <-> entity
def object_to_entity(location: Sequence[float], rotation: Sequence[float], scale: Sequence[float],
                     base_size: Sequence[float] = (1.0, 1.0, 1.0),
                     center: Sequence[float] = (0.0, 0.0, 0.0),
                     entity_type: str = "Box") -> dict:
    """World-space Blender transform -> Overte ``position``/``rotation``/``dimensions``."""
    rot = q_normalize(rotation)
    sc = tuple(float(s) for s in scale)
    offset = q_rotate(rot, (sc[0] * center[0], sc[1] * center[1], sc[2] * center[2]))
    pos_b = (location[0] + offset[0], location[1] + offset[1], location[2] + offset[2])
    size_b = (sc[0] * base_size[0], sc[1] * base_size[1], sc[2] * base_size[2])
    q_o = b2o_quat(rot)
    if entity_type in DIRECTIONAL_TYPES:
        q_o = q_mul(q_o, LIGHT_CORRECTION)
        # The correction swaps the entity's local Y/Z, so its size does too.
        size_o = (abs(size_b[0]), abs(size_b[1]), abs(size_b[2]))
    else:
        size_o = b2o_size(size_b)
    return {
        "position": vec_json(b2o_vec(pos_b)),
        "rotation": quat_json(q_canonical(q_normalize(q_o))),
        "dimensions": vec_json(size_o),
    }


def entity_to_object(props: dict,
                     base_size: Sequence[float] = (1.0, 1.0, 1.0),
                     center: Sequence[float] = (0.0, 0.0, 0.0),
                     entity_type: str = "Box") -> Tuple[Vec3, Quat, Vec3]:
    """Overte entity properties -> Blender world (location, rotation wxyz, scale)."""
    q_o = json_quat(props.get("rotation"))
    dims_o = json_vec(props.get("dimensions"), (1.0, 1.0, 1.0))
    if entity_type in DIRECTIONAL_TYPES:
        q_o = q_mul(q_o, q_conj(LIGHT_CORRECTION))
        size_b = dims_o
    else:
        size_b = o2b_size(dims_o)
    rot = q_canonical(q_normalize(o2b_quat(q_o)))
    scale = tuple(size_b[i] / base_size[i] if abs(base_size[i]) > 1e-9 else 1.0 for i in range(3))
    pos_b = o2b_vec(json_vec(props.get("position")))
    offset = q_rotate(rot, (scale[0] * center[0], scale[1] * center[1], scale[2] * center[2]))
    loc = (pos_b[0] - offset[0], pos_b[1] - offset[1], pos_b[2] - offset[2])
    return loc, rot, scale  # type: ignore[return-value]


# ------------------------------------------------------------ colours
def color_to_json(rgb: Iterable[float]) -> dict:
    """Linear 0..1 Blender colour -> Overte 0..255 (sRGB-encoded) colour."""
    out = []
    for c in list(rgb)[:3]:
        c = min(max(float(c), 0.0), 1.0)
        s = c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
        out.append(int(round(s * 255)))
    return {"red": out[0], "green": out[1], "blue": out[2]}


def json_to_color(d) -> Vec3:
    if not d:
        return (1.0, 1.0, 1.0)
    out = []
    for k in ("red", "green", "blue"):
        s = min(max(float(d.get(k, 255)) / 255.0, 0.0), 1.0)
        out.append(s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4)
    return tuple(out)  # type: ignore[return-value]


# -------------------------------------------------------- comparisons
def close(a: Sequence[float], b: Sequence[float], eps: float = 1e-4) -> bool:
    return all(abs(x - y) <= eps for x, y in zip(a, b))


def quat_close(a: Quat, b: Quat, eps: float = 1e-7) -> bool:
    """Rotation equality (q == -q). eps=1e-7 is ~0.05 degrees; Overte's
    rotation quantization (~1.5e-5 per component) is far below that."""
    d = abs(sum(x * y for x, y in zip(a, b)))
    return d >= 1.0 - eps
