import math
import random

import pytest

from overte_live_link import convert as cv


def rand_quat(rng):
    return cv.q_normalize([rng.uniform(-1, 1) for _ in range(4)])


def axis_angle(axis, deg):
    h = math.radians(deg) / 2
    return (math.cos(h), axis[0] * math.sin(h), axis[1] * math.sin(h), axis[2] * math.sin(h))


def approx(a, b, eps=1e-6):
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert x == pytest.approx(y, abs=eps), (a, b)


def test_vector_basis():
    # Blender up (+Z) is Overte up (+Y); Blender forward (+Y) is Overte -Z.
    approx(cv.b2o_vec((0, 0, 1)), (0, 1, 0))
    approx(cv.b2o_vec((0, 1, 0)), (0, 0, -1))
    approx(cv.b2o_vec((1, 0, 0)), (1, 0, 0))
    rng = random.Random(1)
    for _ in range(50):
        v = [rng.uniform(-10, 10) for _ in range(3)]
        approx(cv.o2b_vec(cv.b2o_vec(v)), v)


def test_quaternion_basis_change_is_consistent_with_vectors():
    rng = random.Random(2)
    for _ in range(100):
        q = rand_quat(rng)
        v = [rng.uniform(-5, 5) for _ in range(3)]
        lhs = cv.b2o_vec(cv.q_rotate(q, v))
        rhs = cv.q_rotate(cv.b2o_quat(q), cv.b2o_vec(v))
        approx(lhs, rhs)
        approx(cv.o2b_quat(cv.b2o_quat(q)), q)


def test_yaw_about_up_axis():
    q_b = axis_angle((0, 0, 1), 90)  # Blender yaw
    approx(cv.b2o_quat(q_b), axis_angle((0, 1, 0), 90))  # Overte yaw


def test_object_entity_roundtrip_with_offset_center():
    rng = random.Random(3)
    for kind in ("Box", "Model", "Light", "Zone"):
        for _ in range(50):
            loc = [rng.uniform(-20, 20) for _ in range(3)]
            rot = cv.q_canonical(rand_quat(rng))
            scale = [rng.uniform(0.1, 4) for _ in range(3)]
            base = [rng.uniform(0.2, 3) for _ in range(3)]
            center = [rng.uniform(-1, 1) for _ in range(3)]
            props = cv.object_to_entity(loc, rot, scale, base, center, kind)
            loc2, rot2, scale2 = cv.entity_to_object(props, base, center, kind)
            approx(loc2, loc)
            assert cv.quat_close(rot2, rot)
            approx(scale2, scale)


def test_entity_box_matches_blender_box():
    """Every corner of the Blender object's bbox must be a corner of the Overte entity box."""
    rng = random.Random(4)
    for _ in range(30):
        loc = [rng.uniform(-5, 5) for _ in range(3)]
        rot = rand_quat(rng)
        scale = [rng.uniform(0.5, 2) for _ in range(3)]
        base = [rng.uniform(0.5, 2) for _ in range(3)]
        center = [rng.uniform(-1, 1) for _ in range(3)]
        props = cv.object_to_entity(loc, rot, scale, base, center, "Model")
        pos_o = cv.json_vec(props["position"])
        rot_o = cv.json_quat(props["rotation"])
        dim_o = cv.json_vec(props["dimensions"])
        for sx in (-1, 1):
            for sy in (-1, 1):
                for sz in (-1, 1):
                    local = [center[0] + sx * base[0] / 2, center[1] + sy * base[1] / 2,
                             center[2] + sz * base[2] / 2]
                    world_b = cv.q_rotate(rot, [scale[i] * local[i] for i in range(3)])
                    world_b = [world_b[i] + loc[i] for i in range(3)]
                    # Same corner in Overte's local frame: blender (sx,sy,sz) -> overte (sx,sz,-sy)
                    lo = (sx * dim_o[0] / 2, sz * dim_o[1] / 2, -sy * dim_o[2] / 2)
                    world_o = cv.q_rotate(rot_o, lo)
                    world_o = [world_o[i] + pos_o[i] for i in range(3)]
                    approx(cv.b2o_vec(world_b), world_o, 1e-5)


def test_light_direction_preserved():
    # Default Blender light points down its local -Z: straight down in the world.
    props = cv.object_to_entity((0, 0, 3), cv.IDENTITY, (1, 1, 1), entity_type="Light")
    q_o = cv.json_quat(props["rotation"])
    approx(cv.q_rotate(q_o, (0, 0, -1)), (0, -1, 0))  # Overte down
    # A light tilted 90 degrees about X now points along Blender +Y -> Overte -Z.
    q_b = axis_angle((1, 0, 0), 90)
    props = cv.object_to_entity((0, 0, 0), q_b, (1, 1, 1), entity_type="Light")
    q_o = cv.json_quat(props["rotation"])
    approx(cv.q_rotate(q_o, (0, 0, -1)), cv.b2o_vec(cv.q_rotate(q_b, (0, 0, -1))))


def test_dimension_axes():
    # A Blender object 1 wide (X), 2 deep (Y), 3 tall (Z) is 1 x 3 tall x 2 deep in Overte.
    props = cv.object_to_entity((0, 0, 0), cv.IDENTITY, (1, 2, 3))
    assert props["dimensions"] == {"x": 1.0, "y": 3.0, "z": 2.0}


def test_colors_roundtrip():
    for c in [(0, 0, 0), (1, 1, 1), (0.2, 0.5, 0.8)]:
        j = cv.color_to_json(c)
        approx(cv.json_to_color(j), c, 0.01)
    assert cv.color_to_json((1, 0, 0)) == {"red": 255, "green": 0, "blue": 0}


def test_quat_close_tolerates_overte_quantization():
    # Value Overte actually returned for an identity rotation.
    q = cv.json_quat({"x": -0.0000152587890625, "y": -0.0000152587890625,
                      "z": -0.0000152587890625, "w": 1})
    assert cv.quat_close(q, cv.IDENTITY)
    assert not cv.quat_close(axis_angle((0, 0, 1), 1.0), cv.IDENTITY)
