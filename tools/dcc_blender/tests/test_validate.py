"""`bl/validate.py` の単体テスト（bpy 不要）。

実行:
    python tools/dcc_blender/tests/test_validate.py
    python tools/run_tests.py --no-blender

## なぜ bpy なしで叩けるのか

`validate.py` は `import bpy` / `from mathutils import Vector` をするが、
**モジュール読み込み時に bpy を使ってはいない。**実際に触るのは

    bpy.data.objects / bpy.data.collections / bpy.data.materials / bpy.context.scene

の4か所だけ。そこでテスト側で `sys.modules` に最小のスタブを差し込み、
検査対象には**ダックタイピングの疑似オブジェクト**を渡す。

Blender を起動せずに回せるので、回帰検知が CI でも成立する
（`validate.py` は 850 行超で最大のファイルなのに、
これまで `test_core.py` から一度も叩かれていなかった）。

## 疑似オブジェクトの方針

`validate.py` は型を見ずに属性だけを使う。したがって本物の `bpy` の構造を
真似る必要はなく、**使っている属性だけ**を持つ小さなクラスで足りる。
本物を真似すぎると、実装が属性を増やしたときにテストが先に壊れる。
"""

import math
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
#: アドオンの親 = tools/dcc_blender/addons（Blender の Script Directories 規約）
ADDON_ROOT = os.path.join(os.path.dirname(HERE), "addons")
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする（docs/99 P-9）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()


# --- bpy / mathutils のスタブ -------------------------------------------------
#
# **本物の Blender がある環境でも、ここで差し替えたスタブが使われる。**
# テストの結果を実行環境に左右されないようにするため（意図的）。


class FakeVector(object):
    """`mathutils.Vector` の代わり。使う演算だけ実装する。"""

    __slots__ = ("x", "y", "z")

    def __init__(self, values=(0.0, 0.0, 0.0)):
        self.x, self.y, self.z = (float(v) for v in values)

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __getitem__(self, index):
        return (self.x, self.y, self.z)[index]

    def __sub__(self, other):
        return FakeVector((self.x - other.x, self.y - other.y, self.z - other.z))

    def __add__(self, other):
        return FakeVector((self.x + other.x, self.y + other.y, self.z + other.z))

    def cross(self, other):
        return FakeVector((
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        ))

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    @property
    def length_squared(self):
        return self.dot(self)

    @property
    def length(self):
        return math.sqrt(self.length_squared)

    def __repr__(self):
        return "FakeVector({0}, {1}, {2})".format(self.x, self.y, self.z)


class FakeMatrix(object):
    """`mathutils.Matrix` の代わり（3x3 のみ）。

    `collect.py` がモジュール読み込み時に `BLENDER_TO_UE = Matrix(...)` を
    作るため、**import を通すだけでも必要**。`validate.py` のルールは
    この行列を使わないが、スタブが嘘をつくと後で混乱するので
    3x3 の積・逆行列は実際に計算する。
    """

    def __init__(self, rows):
        self.rows = [[float(v) for v in row] for row in rows]

    def __matmul__(self, other):
        if isinstance(other, FakeMatrix):
            return FakeMatrix([
                [sum(self.rows[i][k] * other.rows[k][j] for k in range(3))
                 for j in range(3)]
                for i in range(3)
            ])
        values = list(other)
        return FakeVector([
            sum(self.rows[i][k] * values[k] for k in range(3))
            for i in range(3)
        ])

    def to_3x3(self):
        return FakeMatrix(self.rows)

    def to_4x4(self):
        return FakeMatrix(self.rows)        # スタブでは 3x3 のまま扱う

    def inverted(self):
        a = self.rows
        det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
               - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
               + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
        if abs(det) < 1e-12:
            raise ValueError("逆行列が存在しません")
        cofactor = [
            [a[1][1] * a[2][2] - a[1][2] * a[2][1],
             a[0][2] * a[2][1] - a[0][1] * a[2][2],
             a[0][1] * a[1][2] - a[0][2] * a[1][1]],
            [a[1][2] * a[2][0] - a[1][0] * a[2][2],
             a[0][0] * a[2][2] - a[0][2] * a[2][0],
             a[0][2] * a[1][0] - a[0][0] * a[1][2]],
            [a[1][0] * a[2][1] - a[1][1] * a[2][0],
             a[0][1] * a[2][0] - a[0][0] * a[2][1],
             a[0][0] * a[1][1] - a[0][1] * a[1][0]],
        ]
        return FakeMatrix([[v / det for v in row] for row in cofactor])


class FakeBlendData(object):
    """`bpy.data` の代わり。テストから中身を差し替える。"""

    def __init__(self):
        self.objects = {}
        self.collections = []
        self.materials = []
        #: V-114（法線マップの色空間）が読む。既定は「画像が1枚もない」
        self.images = []


class FakeImage(object):
    """`bpy.types.Image` の代わり（V-114 が読む属性だけ）。"""

    def __init__(self, filepath, colorspace="sRGB"):
        self.filepath = filepath
        self.filepath_raw = filepath
        self.colorspace_settings = types.SimpleNamespace(name=colorspace)


def _install_stubs():
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = FakeVector
    mathutils.Matrix = FakeMatrix
    mathutils.Euler = object
    mathutils.Quaternion = object
    sys.modules["mathutils"] = mathutils

    bpy = types.ModuleType("bpy")
    bpy.data = FakeBlendData()
    bpy.context = types.SimpleNamespace(scene=None, selected_objects=[],
                                        view_layer=None)
    bpy.types = types.SimpleNamespace()
    bpy.ops = types.SimpleNamespace()
    bpy.path = types.SimpleNamespace(abspath=lambda p: p)
    bpy.app = types.SimpleNamespace(version_string="stub")
    sys.modules["bpy"] = bpy
    return bpy


BPY = _install_stubs()

from onepochi.bl import validate as validate_mod  # noqa: E402
#: `collect.lod_triangle_count()` は V-047 と**同じ数え方**をする約束なので、
#: 同じスタブの上で並べて確かめる（R-6 / schema v0.3.1）
from onepochi.bl import collect as collect_mod  # noqa: E402
from onepochi.core import issues as issues_mod  # noqa: E402


# --- 疑似オブジェクト ---------------------------------------------------------


class FakeVert(object):
    def __init__(self, co):
        self.co = FakeVector(co)


class FakeEdge(object):
    def __init__(self, key):
        self.key = key


class FakePolygon(object):
    def __init__(self, vertices, normal=(0.0, 0.0, 1.0), material_index=0):
        self.vertices = list(vertices)
        self.normal = FakeVector(normal)
        self.material_index = material_index

    @property
    def edge_keys(self):
        result = []
        count = len(self.vertices)
        for index in range(count):
            a = self.vertices[index]
            b = self.vertices[(index + 1) % count]
            result.append((a, b) if a < b else (b, a))
        return result


class FakeTri(object):
    def __init__(self, vertices):
        self.vertices = list(vertices)


class FakeMesh(object):
    def __init__(self, verts=(), polys=(), uv_names=(), edges=None,
                 shape_keys=None):
        self.vertices = [FakeVert(v) for v in verts]
        self.polygons = list(polys)
        self.uv_layers = [types.SimpleNamespace(name=n) for n in uv_names]
        self.shape_keys = shape_keys
        self.loop_triangles = []
        self.calc_loop_triangles_calls = 0
        if edges is None:
            keys = set()
            for polygon in self.polygons:
                keys.update(polygon.edge_keys)
            edges = sorted(keys)
        self.edges = [FakeEdge(k) for k in edges]

    def calc_loop_triangles(self):
        self.calc_loop_triangles_calls += 1
        triangles = []
        for polygon in self.polygons:
            indices = polygon.vertices
            for index in range(1, len(indices) - 1):
                triangles.append(FakeTri((indices[0], indices[index],
                                          indices[index + 1])))
        self.loop_triangles = triangles


class FakeSlot(object):
    def __init__(self, material_name=None):
        self.material = (types.SimpleNamespace(name=material_name)
                         if material_name else None)


class FakeObject(object):
    """`bpy.types.Object` の代わり。validate.py が触る属性だけ持つ。"""

    def __init__(self, name, obj_type="MESH", data=None, materials=(),
                 scale=(1.0, 1.0, 1.0), rotation=(0.0, 0.0, 0.0),
                 modifiers=(), parent=None, hide_viewport=False,
                 hide_render=False):
        self.name = name
        self.type = obj_type
        self.data = data
        self.material_slots = [FakeSlot(m) for m in materials]
        self.scale = list(scale)
        self.rotation_euler = list(rotation)
        self.modifiers = list(modifiers)
        self.parent = parent
        self.hide_viewport = hide_viewport
        self.hide_render = hide_render
        self.pose = None

    @property
    def matrix_world(self):
        """V-009（負のスケール）は行列式の符号で判定される。

        スケールだけから符号を再現すれば足りる（回転は行列式を変えない）。
        """
        determinant = self.scale[0] * self.scale[1] * self.scale[2]
        return types.SimpleNamespace(determinant=lambda: determinant)


def cube_mesh():
    """閉じた立方体（1辺 1.0）。凸で、全辺が2面に共有される。"""
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
             (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    polys = [
        FakePolygon((0, 3, 2, 1), (0, 0, -1)),   # 下
        FakePolygon((4, 5, 6, 7), (0, 0, 1)),    # 上
        FakePolygon((0, 1, 5, 4), (0, -1, 0)),   # 前
        FakePolygon((2, 3, 7, 6), (0, 1, 0)),    # 後
        FakePolygon((1, 2, 6, 5), (1, 0, 0)),    # 右
        FakePolygon((3, 0, 4, 7), (-1, 0, 0)),   # 左
    ]
    return FakeMesh(verts, polys, uv_names=("UVMap",))


def open_plane_mesh():
    """1枚のポリゴン。辺が1面しか共有しないので閉じていない。"""
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    return FakeMesh(verts, [FakePolygon((0, 1, 2, 3), (0, 0, 1))],
                    uv_names=("UVMap",))


class FakeSpec(object):
    """`collect.AssetSpec` の代わり（validate.py が読む属性だけ）。"""

    def __init__(self, asset_name="Crate_01", **kwargs):
        self.asset_name = asset_name
        self.asset_type = kwargs.get("asset_type", "StaticMesh")
        self.main_object_names = kwargs.get("main_object_names", [])
        self.collisions = kwargs.get("collisions", [])
        self.lods = kwargs.get("lods", [])
        self.sockets = kwargs.get("sockets", [])
        self.bones = kwargs.get("bones", [])
        self.exported_bones = kwargs.get("exported_bones", [])
        self.armature_name = kwargs.get("armature_name")
        self.skin = kwargs.get("skin")
        self.shape_keys = kwargs.get("shape_keys")
        self.material_slots = kwargs.get("material_slots", [])


def socket(name, object_name=None):
    return types.SimpleNamespace(name=name,
                                 object_name=object_name or ("SOCKET_" + name))


def collision(object_name, shape="convex", target="Crate_01", index=1,
              triangle_count=12):
    return types.SimpleNamespace(object_name=object_name, shape=shape,
                                 target=target, index=index,
                                 triangle_count=triangle_count)


def lod(level, object_names, collection_name=None):
    return types.SimpleNamespace(level=level, object_names=list(object_names),
                                 collection_name=collection_name)


def bone(name, parent=None, head_cm=(0.0, 0.0, 0.0), use_deform=True,
         is_leaf=False, length_cm=35.0):
    return types.SimpleNamespace(name=name, parent=parent,
                                 head_cm=list(head_cm), use_deform=use_deform,
                                 is_leaf=is_leaf, length_cm=length_cm)


def skin_stats(**kwargs):
    defaults = {
        "vertex_count": 100, "unassigned_count": 0, "unassigned_samples": [],
        "zero_total_count": 0, "deviating_count": 0, "max_deviation": 0.0,
        "worst_total": 1.0, "over_influence_count": 0, "max_influences": 2,
        "bones_without_weight": [], "unknown_groups": [],
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


class ValidateCase(unittest.TestCase):
    """共通の後片付けと、ルール ID で結果を引く補助。"""

    def setUp(self):
        BPY.data = FakeBlendData()
        validate_mod.bpy.data = BPY.data
        self.result = issues_mod.IssueList()

    def register(self, *objects):
        for obj in objects:
            BPY.data.objects[obj.name] = obj
        return objects

    def rule_ids(self):
        return [issue.rule_id for issue in self.result.sorted()]

    def assertRule(self, rule_id, severity=None):
        matches = [i for i in self.result.sorted() if i.rule_id == rule_id]
        self.assertTrue(matches,
                        "{0} が出ていない（出たのは {1}）".format(
                            rule_id, self.rule_ids()))
        if severity:
            self.assertEqual(matches[0].severity, severity,
                             "{0} の重大度が {1} ではない".format(rule_id, severity))
        return matches

    def assertNoRule(self, rule_id):
        self.assertNotIn(rule_id, self.rule_ids())


# --- ジオメトリ判定ヘルパ -----------------------------------------------------


class TestGeometryHelpers(unittest.TestCase):
    def test_closed_cube_is_manifold(self):
        self.assertTrue(validate_mod.is_closed_manifold(cube_mesh()))

    def test_open_plane_is_not_manifold(self):
        self.assertFalse(validate_mod.is_closed_manifold(open_plane_mesh()))

    def test_empty_mesh_is_not_manifold(self):
        """面が無いものを「閉じている」と答えてはいけない。"""
        self.assertFalse(validate_mod.is_closed_manifold(FakeMesh()))

    def test_convex_cube_has_no_bad_faces(self):
        self.assertEqual(validate_mod.non_convex_face_count(cube_mesh()), 0)

    def test_non_convex_shape_is_detected(self):
        """面の平面より外側に頂点が出ると凸条件が崩れる。

        判定は「どの面の平面よりも外に出ている頂点が無いか」。
        角を外へ動かすと上面・側面の平面を突き抜けるため検出される。
        """
        mesh = cube_mesh()
        mesh.vertices[6].co = FakeVector((1.5, 1.5, 1.5))
        self.assertGreater(validate_mod.non_convex_face_count(mesh), 0)

    def test_degenerate_face_normal_is_skipped(self):
        """法線がゼロの面は判定から外す（0 除算と誤判定を避ける）。"""
        verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0)]
        mesh = FakeMesh(verts, [FakePolygon((0, 1, 2), (0.0, 0.0, 0.0))])
        self.assertEqual(validate_mod.non_convex_face_count(mesh), 0)

    def test_degenerate_triangles_counted(self):
        """面積が実質ゼロの三角形（同一直線上）を数える。"""
        verts = [(0, 0, 0), (1, 0, 0), (2, 0, 0)]
        mesh = FakeMesh(verts, [FakePolygon((0, 1, 2))])
        self.assertEqual(validate_mod.count_degenerate_triangles(mesh), 1)

    def test_healthy_triangles_not_counted(self):
        verts = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        mesh = FakeMesh(verts, [FakePolygon((0, 1, 2))])
        self.assertEqual(validate_mod.count_degenerate_triangles(mesh), 0)

    def test_degenerate_triangles_uses_evaluated_triangles(self):
        """四角形も三角形に割って数える（calc_loop_triangles を通す）。"""
        mesh = cube_mesh()
        self.assertEqual(validate_mod.count_degenerate_triangles(mesh), 0)
        self.assertEqual(mesh.calc_loop_triangles_calls, 1)
        self.assertEqual(len(mesh.loop_triangles), 12)

    def test_ngons_counted(self):
        verts = [(0, 0, 0)] * 5
        mesh = FakeMesh(verts, [FakePolygon((0, 1, 2, 3, 4)),
                                FakePolygon((0, 1, 2, 3)),
                                FakePolygon((0, 1, 2))])
        self.assertEqual(validate_mod.count_ngons(mesh), 1)

    def test_loose_geometry_counted(self):
        """面に使われていない頂点と辺を数える。"""
        verts = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (5, 5, 5)]
        mesh = FakeMesh(verts, [FakePolygon((0, 1, 2))],
                        edges=[(0, 1), (1, 2), (0, 2), (2, 3)])
        loose_verts, loose_edges = validate_mod.count_loose_geometry(mesh)
        self.assertEqual(loose_verts, 1)
        self.assertEqual(loose_edges, 1)

    def test_unused_material_slots(self):
        mesh = FakeMesh([(0, 0, 0)] * 3,
                        [FakePolygon((0, 1, 2), material_index=0)])
        obj = FakeObject("Crate_01", data=mesh,
                         materials=("M_A", "M_B", "M_C"))
        self.assertEqual(validate_mod.count_unused_material_slots(obj), 2)

    def test_single_slot_is_never_unused(self):
        """スロットが1つなら未使用扱いにしない（割当なしの新規メッシュ対策）。"""
        mesh = FakeMesh([(0, 0, 0)] * 3, [])
        obj = FakeObject("Crate_01", data=mesh, materials=("M_A",))
        self.assertEqual(validate_mod.count_unused_material_slots(obj), 0)


# --- 名前・マテリアル ---------------------------------------------------------


class TestNameRules(ValidateCase):
    def test_valid_names_pass(self):
        validate_mod.check_names(self.result, ["Crate_01", "SM_Crate"])
        self.assertEqual(self.rule_ids(), [])

    def test_japanese_name_is_an_error(self):
        validate_mod.check_names(self.result, ["木 箱"])
        self.assertRule("V-003", "error")

    def test_leading_digit_can_be_allowed(self):
        validate_mod.check_names(self.result, ["01_static"],
                                 allow_leading_digit=True)
        self.assertEqual(self.rule_ids(), [])

    def test_default_material_name_is_an_error(self):
        validate_mod.check_materials(self.result, ["Material"])
        self.assertRule("V-003c", "error")

    def test_material_without_prefix_is_an_error(self):
        validate_mod.check_materials(self.result, ["Crate_Body"])
        self.assertTrue(self.rule_ids())

    def test_conforming_material_passes(self):
        validate_mod.check_materials(self.result, ["M_Crate_Body"])
        self.assertEqual(self.rule_ids(), [])

    def test_leading_space_in_material_is_caught(self):
        """docs/14 §8.2 で実際に踏んだケース。"""
        validate_mod.check_materials(self.result, [" M_Dummy_Body"])
        self.assertTrue(self.rule_ids())

    def test_duplicate_suffix_on_lod_collection_is_stripped(self):
        """`LOD0.001` を V-003 にしない（docs/99 P-24 / バッチ書き出しの前提）。

        コレクション名は .blend 全体で一意なので、2 つめのアセットの
        `LOD0` には Blender が必ず `.001` を付ける。**ユーザーには直せない**。
        """
        spec = FakeSpec(lods=[lod(0, ["Barrel_01"], collection_name="LOD0.001")])
        self.assertEqual(validate_mod.lod_collection_names(spec), ["LOD0"])
        validate_mod.check_names(self.result,
                                 validate_mod.lod_collection_names(spec),
                                 kind="コレクション名")
        self.assertEqual(self.rule_ids(), [])

    def test_duplicate_suffix_on_other_collection_is_still_an_error(self):
        """予約語でないコレクション名のドットは**そのまま**エラーにする。"""
        spec = FakeSpec(lods=[lod(0, ["Crate_01"], collection_name="Crate_01.001")])
        self.assertEqual(validate_mod.lod_collection_names(spec),
                         ["Crate_01.001"])
        validate_mod.check_names(self.result,
                                 validate_mod.lod_collection_names(spec),
                                 kind="コレクション名")
        self.assertRule("V-003b", "error")


# --- トランスフォーム ---------------------------------------------------------


class TestTransformRules(ValidateCase):
    def test_identity_transform_passes(self):
        obj = FakeObject("Crate_01", data=cube_mesh())
        validate_mod.check_object_transform(self.result, obj, auto_fix=False)
        self.assertEqual(self.rule_ids(), [])

    def test_unapplied_scale_is_reported(self):
        obj = FakeObject("Crate_01", data=cube_mesh(), scale=(2.0, 2.0, 2.0))
        validate_mod.check_object_transform(self.result, obj, auto_fix=False)
        self.assertRule("V-006")

    def test_unapplied_rotation_is_reported(self):
        obj = FakeObject("Crate_01", data=cube_mesh(),
                         rotation=(math.radians(45.0), 0.0, 0.0))
        validate_mod.check_object_transform(self.result, obj, auto_fix=False)
        self.assertRule("V-007")

    def test_negative_scale_is_reported(self):
        obj = FakeObject("Crate_01", data=cube_mesh(), scale=(-1.0, 1.0, 1.0))
        validate_mod.check_object_transform(self.result, obj, auto_fix=False)
        self.assertRule("V-009")

    def test_armature_scale_is_an_error(self):
        """V-077。Armature のスケールはボーン長と食い違う原因になる。"""
        armature = FakeObject("Armature", "ARMATURE", scale=(2.0, 2.0, 2.0))
        validate_mod.check_armature_object(self.result, armature,
                                           auto_fix=False)
        self.assertRule("V-077", "error")

    def test_armature_none_is_ignored(self):
        validate_mod.check_armature_object(self.result, None)
        self.assertEqual(self.rule_ids(), [])


# --- シーン単位 ---------------------------------------------------------------


class TestSceneUnits(ValidateCase):
    def _scene(self, system="METRIC", scale_length=1.0):
        return types.SimpleNamespace(
            name="Scene",
            unit_settings=types.SimpleNamespace(system=system,
                                                scale_length=scale_length))

    def test_metric_one_meter_passes(self):
        validate_mod.check_scene_units(self.result, self._scene())
        self.assertEqual(self.rule_ids(), [])

    def test_wrong_scale_length_is_reported(self):
        validate_mod.check_scene_units(self.result,
                                       self._scene(scale_length=0.01))
        self.assertTrue(self.rule_ids())

    def test_imperial_is_reported(self):
        validate_mod.check_scene_units(self.result,
                                       self._scene(system="IMPERIAL"))
        self.assertTrue(self.rule_ids())


# --- コリジョン ---------------------------------------------------------------


class TestCollisionRules(ValidateCase):
    def test_matching_body_passes(self):
        self.register(FakeObject("UCX_Crate_01_01", data=cube_mesh()))
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UCX_Crate_01_01")])
        validate_mod.check_collisions(self.result, spec, {})
        self.assertNoRule("V-041")
        self.assertNoRule("V-040")

    def test_missing_body_is_an_error(self):
        self.register(FakeObject("UCX_Box_01", data=cube_mesh()))
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UCX_Box_01", target="Box")])
        validate_mod.check_collisions(self.result, spec, {})
        self.assertRule("V-041", "error")

    def test_missing_serial_is_a_warning_only(self):
        """S-08 の実例。連番なしは「推奨」であってエラーではない。"""
        self.register(FakeObject("UCX_Crate_01", data=cube_mesh()))
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UCX_Crate_01", index=None)])
        validate_mod.check_collisions(self.result, spec, {})
        self.assertRule("V-040", "warning")
        self.assertNoRule("V-041")

    def test_open_convex_collision_is_an_error(self):
        self.register(FakeObject("UCX_Crate_01_01", data=open_plane_mesh()))
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UCX_Crate_01_01")])
        validate_mod.check_collisions(self.result, spec, {})
        self.assertRule("V-042", "error")

    def test_non_convex_collision_is_a_warning(self):
        mesh = cube_mesh()
        mesh.vertices[6].co = FakeVector((1.5, 1.5, 1.5))
        self.register(FakeObject("UCX_Crate_01_01", data=mesh))
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UCX_Crate_01_01")])
        validate_mod.check_collisions(self.result, spec, {})
        self.assertRule("V-043", "warning")

    def test_box_collision_skips_convex_checks(self):
        """UBX_ は箱なので凸判定を持ち込まない。"""
        self.register(FakeObject("UBX_Crate_01_01", data=open_plane_mesh()))
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UBX_Crate_01_01", shape="box")])
        validate_mod.check_collisions(self.result, spec, {})
        self.assertNoRule("V-042")
        self.assertNoRule("V-043")

    def test_triangle_limit_is_a_warning(self):
        self.register(FakeObject("UCX_Crate_01_01", data=cube_mesh()))
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UCX_Crate_01_01",
                                              triangle_count=500)])
        validate_mod.check_collisions(self.result, spec,
                                      {"maxCollisionTrianglesPerShape": 64})
        self.assertRule("V-044", "warning")

    def test_material_on_collision_is_a_warning(self):
        self.register(FakeObject("UCX_Crate_01_01", data=cube_mesh(),
                                 materials=("M_Crate_Body",)))
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UCX_Crate_01_01")])
        validate_mod.check_collisions(self.result, spec, {})
        self.assertRule("V-034", "warning")

    def test_unregistered_collision_object_is_skipped(self):
        """シーンから消えたオブジェクトで落ちない。"""
        spec = FakeSpec(main_object_names=["Crate_01"],
                        collisions=[collision("UCX_Gone_01")])
        validate_mod.check_collisions(self.result, spec, {})
        self.assertEqual(self.rule_ids(), [])


# --- LOD ----------------------------------------------------------------------


class TestLodRules(ValidateCase):
    def _lod_object(self, name, triangles, materials=("M_A",)):
        verts = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        polys = [FakePolygon((0, 1, 2)) for _ in range(triangles)]
        return FakeObject(name, data=FakeMesh(verts, polys),
                          materials=materials)

    def test_decreasing_triangles_pass(self):
        self.register(self._lod_object("A0", 100), self._lod_object("A1", 50),
                      self._lod_object("A2", 20))
        spec = FakeSpec(lods=[lod(0, ["A0"]), lod(1, ["A1"]), lod(2, ["A2"])])
        validate_mod.check_lods(self.result, spec)
        self.assertEqual(self.rule_ids(), [])

    def test_swapped_lods_are_reported(self):
        """S-07 の実例: LOD1 と LOD2 が入れ替わっている。"""
        self.register(self._lod_object("A0", 100), self._lod_object("A1", 20),
                      self._lod_object("A2", 50))
        spec = FakeSpec(lods=[lod(0, ["A0"]), lod(1, ["A1"]), lod(2, ["A2"])])
        validate_mod.check_lods(self.result, spec)
        self.assertRule("V-047", "warning")

    def test_missing_lod0_is_an_error(self):
        self.register(self._lod_object("A1", 50))
        spec = FakeSpec(lods=[lod(1, ["A1"])])
        validate_mod.check_lods(self.result, spec)
        self.assertRule("V-045a", "error")

    def test_gap_in_levels_is_an_error(self):
        self.register(self._lod_object("A0", 100), self._lod_object("A2", 20))
        spec = FakeSpec(lods=[lod(0, ["A0"]), lod(2, ["A2"])])
        validate_mod.check_lods(self.result, spec)
        self.assertRule("V-045", "error")

    def test_material_mismatch_is_an_error(self):
        self.register(self._lod_object("A0", 100, materials=("M_A",)),
                      self._lod_object("A1", 50, materials=("M_B",)))
        spec = FakeSpec(lods=[lod(0, ["A0"]), lod(1, ["A1"])])
        validate_mod.check_lods(self.result, spec)
        self.assertRule("V-046", "error")

    def test_no_lods_is_an_error(self):
        validate_mod.check_lods(self.result, FakeSpec(lods=[]))
        self.assertRule("V-045", "error")


class TestLodTriangleCount(ValidateCase):
    """`collect.lod_triangle_count()`（`levels[].triangleCount` の中身 / R-6）。

    マニフェスト v0.3.1 で `staticMesh.lod.levels[].triangleCount` を出すように
    なった。値は **V-047（`check_lods`）と同じ数え方**でなければならない
    — 宣言値が違えば UE 側（`lod.compare_lod()`）が厳密比較で赤くなる。
    """

    def _lod_object(self, name, triangles, obj_type="MESH"):
        verts = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
        polys = [FakePolygon((0, 1, 2)) for _ in range(triangles)]
        return FakeObject(name, obj_type=obj_type,
                          data=FakeMesh(verts, polys), materials=("M_A",))

    def test_counts_triangles_of_one_object(self):
        self.register(self._lod_object("A0", 1280))
        self.assertEqual(collect_mod.lod_triangle_count(["A0"]), 1280)

    def test_sums_multiple_objects(self):
        self.register(self._lod_object("A0", 100), self._lod_object("A1", 23))
        self.assertEqual(collect_mod.lod_triangle_count(["A0", "A1"]), 123)

    def test_quads_count_as_two_triangles(self):
        """四角ポリゴンは 2 三角形。`loop_triangles` を使う根拠。"""
        mesh = FakeMesh([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                        [FakePolygon((0, 1, 2, 3))])
        self.register(FakeObject("Quad", data=mesh, materials=("M_A",)))
        self.assertEqual(collect_mod.lod_triangle_count(["Quad"]), 2)

    def test_matches_v047(self):
        """V-047 が「減っていない」と判定する境界と、宣言値が食い違わない。"""
        self.register(self._lod_object("A0", 100), self._lod_object("A1", 50))
        counts = [collect_mod.lod_triangle_count(["A0"]),
                  collect_mod.lod_triangle_count(["A1"])]
        self.assertEqual(counts, [100, 50])
        spec = FakeSpec(lods=[lod(0, ["A0"]), lod(1, ["A1"])])
        validate_mod.check_lods(self.result, spec)
        self.assertNoRule("V-047")

    def test_non_mesh_objects_are_ignored(self):
        self.register(self._lod_object("Empty", 5, obj_type="EMPTY"))
        self.assertIsNone(collect_mod.lod_triangle_count(["Empty"]))

    def test_missing_object_is_none_not_zero(self):
        """値が取れない段は **None**。0 を返すと「三角形 0 枚」という
        別の主張になり、UE 側が嘘の宣言値と厳密比較してしまう。"""
        self.assertIsNone(collect_mod.lod_triangle_count(["Gone"]))
        self.assertIsNone(collect_mod.lod_triangle_count([]))
        self.assertIsNone(collect_mod.lod_triangle_count(None))

    def test_an_empty_mesh_still_reports_zero(self):
        """メッシュが**在る**のに三角形が 0 枚なら、0 を宣言してよい。"""
        self.register(FakeObject("Flat", data=FakeMesh([(0, 0, 0)], []),
                                 materials=("M_A",)))
        self.assertEqual(collect_mod.lod_triangle_count(["Flat"]), 0)


# --- ソケット（V-049 / V-050） ------------------------------------------------


class TestSocketRules(ValidateCase):
    def test_valid_socket_passes(self):
        spec = FakeSpec(sockets=[socket("Lid"), socket("weapon_r")])
        validate_mod.check_sockets(self.result, spec)
        self.assertEqual(self.rule_ids(), [])

    def test_duplicate_socket_names_are_an_error(self):
        spec = FakeSpec(sockets=[socket("Lid", "SOCKET_Lid"),
                                 socket("Lid", "SOCKET_Lid_dup")])
        validate_mod.check_sockets(self.result, spec)
        self.assertRule("V-049", "error")

    def test_leading_digit_socket_is_an_error(self):
        """V-050。スキーマの `ueAssetName` に違反するものを DCC 側で止める（D-3）。"""
        spec = FakeSpec(sockets=[socket("1_weapon", "SOCKET_1_weapon")])
        validate_mod.check_sockets(self.result, spec)
        self.assertRule("V-050", "error")

    def test_socket_with_space_is_an_error(self):
        spec = FakeSpec(sockets=[socket("weapon r", "SOCKET_weapon r")])
        validate_mod.check_sockets(self.result, spec)
        self.assertRule("V-050", "error")

    def test_socket_with_blender_suffix_is_an_error(self):
        """複製で付く `.001` が UE のソケット名に混ざるのを止める。"""
        spec = FakeSpec(sockets=[socket("Lid.001", "SOCKET_Lid.001")])
        validate_mod.check_sockets(self.result, spec)
        self.assertRule("V-050", "error")


# --- 出力先パス ---------------------------------------------------------------


class TestDestinationPath(ValidateCase):
    def test_valid_path_passes(self):
        validate_mod.check_destination_path(self.result,
                                            "/Game/_Dev/Samples/S01_Minimal",
                                            ["/Game/_Dev"])
        self.assertEqual(self.rule_ids(), [])

    def test_path_outside_whitelist_is_an_error(self):
        validate_mod.check_destination_path(self.result, "/Game/Characters",
                                            ["/Game/_Dev"])
        self.assertTrue(self.rule_ids())

    def test_windows_style_path_is_an_error(self):
        """docs/99 P-4。Git Bash が UE パスをドライブパスに書き換える事故。"""
        validate_mod.check_destination_path(
            self.result, "C:/Program Files/Git/Game/_Dev/Samples", ["/Game"])
        self.assertTrue(self.rule_ids())


# --- ボーン -------------------------------------------------------------------


class TestBoneRules(ValidateCase):
    def test_valid_bone_names_pass(self):
        validate_mod.check_bone_names(self.result,
                                      [bone("root"), bone("bone_01", "root")])
        self.assertEqual(self.rule_ids(), [])

    def test_leading_space_in_bone_name_is_an_error(self):
        """docs/14 §1.3 で実際に踏んだケース。UI では見えない。"""
        validate_mod.check_bone_names(self.result, [bone(" root")])
        self.assertRule("V-066", "error")

    def test_case_collision_is_a_warning(self):
        validate_mod.check_bone_names(self.result,
                                      [bone("Root"), bone("root")])
        self.assertRule("V-078", "warning")

    def _armature(self, bones):
        """`skeletal.degenerate_bones` / `posed_bones` が触る形だけ作る。"""
        data_bones = []
        for entry in bones:
            data_bones.append(types.SimpleNamespace(
                name=entry.name,
                length=entry.length_cm / 100.0,
                parent=None,
                use_deform=entry.use_deform))
        return FakeObject("Armature", "ARMATURE",
                          data=types.SimpleNamespace(bones=data_bones))

    def test_single_root_at_origin_passes(self):
        bones = [bone("root"), bone("bone_01", "root", (0, 0, 40))]
        spec = FakeSpec(bones=bones, armature_name="Armature",
                        exported_bones=[b.name for b in bones])
        validate_mod.check_bone_hierarchy(
            self.result, spec, self._armature(bones),
            {"skeleton": {"rootBoneName": "root"}}, {})
        self.assertEqual(self.rule_ids(), [])

    def test_two_roots_are_an_error(self):
        """V-061。90_broken_all に無いため実機では未確認だった経路（docs/14 E-1）。"""
        bones = [bone("root"), bone("root2")]
        spec = FakeSpec(bones=bones, armature_name="Armature",
                        exported_bones=[b.name for b in bones])
        validate_mod.check_bone_hierarchy(
            self.result, spec, self._armature(bones), {}, {})
        self.assertRule("V-061", "error")

    def test_no_bones_is_an_error(self):
        spec = FakeSpec(bones=[], armature_name="Armature")
        validate_mod.check_bone_hierarchy(
            self.result, spec, self._armature([]), {}, {})
        self.assertRule("V-060", "error")

    def test_root_away_from_origin_is_an_error(self):
        bones = [bone("root", head_cm=(0, 0, 50))]
        spec = FakeSpec(bones=bones, armature_name="Armature",
                        exported_bones=["root"])
        validate_mod.check_bone_hierarchy(
            self.result, spec, self._armature(bones), {}, {})
        self.assertRule("V-063", "error")

    def test_root_name_mismatch_is_a_warning(self):
        bones = [bone("pelvis")]
        spec = FakeSpec(bones=bones, armature_name="Armature",
                        exported_bones=["pelvis"])
        validate_mod.check_bone_hierarchy(
            self.result, spec, self._armature(bones),
            {"skeleton": {"rootBoneName": "root"}}, {})
        self.assertRule("V-062", "warning")

    def test_deform_only_drops_a_weightless_root(self):
        """V-062。`use_armature_deform_only` でルートが消える事故を止める。"""
        bones = [bone("root", use_deform=False), bone("bone_01", "root")]
        spec = FakeSpec(bones=bones, armature_name="Armature",
                        exported_bones=[b.name for b in bones])
        validate_mod.check_bone_hierarchy(
            self.result, spec, self._armature(bones),
            {"export": {"fbx": {"use_armature_deform_only": True}}}, {})
        self.assertRule("V-062", "error")

    def test_bone_count_limit_is_a_warning(self):
        bones = [bone("root")] + [bone("b%d" % i, "root") for i in range(5)]
        spec = FakeSpec(bones=bones, armature_name="Armature",
                        exported_bones=[b.name for b in bones])
        validate_mod.check_bone_hierarchy(
            self.result, spec, self._armature(bones), {}, {"maxBones": 3})
        self.assertRule("V-073", "warning")

    def test_zero_length_bone_is_an_error(self):
        """V-067。長さ実質ゼロは FBX で向きが不定になる。"""
        bones = [bone("root", length_cm=0.0)]
        spec = FakeSpec(bones=bones, armature_name="Armature",
                        exported_bones=["root"])
        validate_mod.check_bone_hierarchy(
            self.result, spec, self._armature(bones), {}, {})
        self.assertRule("V-067", "error")


# --- スキンウェイト -----------------------------------------------------------


class TestSkinRules(ValidateCase):
    def test_clean_skin_passes(self):
        spec = FakeSpec(skin=skin_stats())
        validate_mod.check_skin_weights(self.result, spec, {}, {})
        self.assertEqual(self.rule_ids(), [])

    def test_no_skin_is_ignored(self):
        validate_mod.check_skin_weights(self.result, FakeSpec(skin=None), {}, {})
        self.assertEqual(self.rule_ids(), [])

    def test_unassigned_vertices_are_an_error(self):
        """V-068。UE で原点に吸い寄せられる実害がある。"""
        spec = FakeSpec(skin=skin_stats(unassigned_count=3,
                                        unassigned_samples=[1, 2, 3]))
        validate_mod.check_skin_weights(self.result, spec, {}, {})
        self.assertRule("V-068", "error")

    def test_zero_total_is_an_error(self):
        spec = FakeSpec(skin=skin_stats(zero_total_count=2))
        validate_mod.check_skin_weights(self.result, spec, {}, {})
        self.assertRule("V-070", "error")

    def test_normalization_drift_is_a_warning_not_an_error(self):
        """docs/14 §8.3 の決着。手付けウェイトの 1% ズレで止めない。"""
        spec = FakeSpec(skin=skin_stats(deviating_count=96,
                                        max_deviation=0.0159,
                                        worst_total=0.984))
        validate_mod.check_skin_weights(self.result, spec, {}, {})
        issue = self.assertRule("V-069", "warning")[0]
        self.assertTrue(issue.auto_fixed,
                        "複製側で正規化することを autoFixed で示すべき")
        self.assertFalse(self.result.has_error())

    def test_over_influence_is_a_warning(self):
        spec = FakeSpec(skin=skin_stats(over_influence_count=5,
                                        max_influences=12))
        validate_mod.check_skin_weights(self.result, spec, {},
                                        {"maxBoneInfluences": 8})
        self.assertRule("V-071", "warning")

    def test_bones_without_weight_is_a_warning(self):
        spec = FakeSpec(skin=skin_stats(bones_without_weight=["bone_02"]))
        validate_mod.check_skin_weights(self.result, spec, {}, {})
        self.assertRule("V-072", "warning")

    def test_unknown_vertex_groups_are_a_warning(self):
        spec = FakeSpec(skin=skin_stats(unknown_groups=["mask"]))
        validate_mod.check_skin_weights(self.result, spec, {}, {})
        self.assertRule("V-074", "warning")


# --- Shape Key / Morph Target -------------------------------------------------


class TestShapeKeyRules(ValidateCase):
    def _info(self, basis_name="Basis", morph_names=(), mesh_name="eye"):
        return types.SimpleNamespace(basis_name=basis_name,
                                     morph_names=list(morph_names),
                                     mesh_name=mesh_name)

    def test_basis_first_passes(self):
        """S-06 の実測構成（Basis / Open / Smile）。"""
        spec = FakeSpec(shape_keys=self._info("Basis", ["Open", "Smile"]))
        validate_mod.check_shape_keys(self.result, spec, {})
        self.assertEqual(self.rule_ids(), [])

    def test_no_shape_keys_is_ignored(self):
        validate_mod.check_shape_keys(self.result,
                                      FakeSpec(shape_keys=None), {})
        self.assertEqual(self.rule_ids(), [])

    def test_non_basis_first_block_is_a_warning(self):
        """先頭ブロックは基準形状なので UE に渡らない（docs/14 §3）。"""
        spec = FakeSpec(shape_keys=self._info("Close", ["Open"]))
        validate_mod.check_shape_keys(self.result, spec, {})
        self.assertRule("V-075", "warning")

    def test_morphs_disabled_by_preset_is_a_warning(self):
        spec = FakeSpec(shape_keys=self._info("Basis", ["Open"]))
        validate_mod.check_shape_keys(
            self.result, spec,
            {"skeletalMesh": {"morphTargets": {"import": False}}})
        self.assertRule("V-076", "warning")

    def test_bad_morph_name_is_a_warning(self):
        spec = FakeSpec(shape_keys=self._info("Basis", ["1_open"]))
        validate_mod.check_shape_keys(self.result, spec, {})
        self.assertRule("V-075", "warning")


# --- Skeleton の流用設定 ------------------------------------------------------


class TestSkeletonSettings(ValidateCase):
    def test_create_new_passes(self):
        validate_mod.check_skeleton_settings(
            self.result, {"skeleton": {"mode": "create_new"}})
        self.assertEqual(self.rule_ids(), [])

    def test_reuse_with_path_passes(self):
        """S-05 の onepochi_asset.json と同じ形。"""
        validate_mod.check_skeleton_settings(self.result, {"skeleton": {
            "mode": "reuse",
            "assetPath": "/Game/_Dev/Samples/S04_Skeletal/SK_Dummy_Body_Skeleton",
            "compatibilityMode": "exact"}})
        self.assertEqual(self.rule_ids(), [])

    def test_reuse_without_path_is_an_error(self):
        validate_mod.check_skeleton_settings(
            self.result, {"skeleton": {"mode": "reuse"}})
        self.assertRule("V-079", "error")

    def test_unknown_mode_is_an_error(self):
        validate_mod.check_skeleton_settings(
            self.result, {"skeleton": {"mode": "borrow"}})
        self.assertRule("V-079", "error")

    def test_unknown_compatibility_mode_is_an_error(self):
        validate_mod.check_skeleton_settings(self.result, {"skeleton": {
            "mode": "create_new", "compatibilityMode": "loose"}})
        self.assertRule("V-079", "error")

    def test_bad_asset_path_is_an_error(self):
        validate_mod.check_skeleton_settings(self.result, {"skeleton": {
            "mode": "reuse", "assetPath": "Game/NoLeadingSlash"}})
        self.assertRule("V-079", "error")


class TestTextureRules(ValidateCase):
    """V-110 〜 V-114: テクスチャの発見（R-4 / docs/05 §E）。

    **すべて警告・情報。書き出しは止めない。**
    実フォルダを見るので、一時ディレクトリを作って渡す。
    """

    def setUp(self):
        ValidateCase.setUp(self)
        self._tmp = tempfile.mkdtemp(prefix="onepochi_vt_")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def put(self, *names):
        for name in names:
            with open(os.path.join(self._tmp, name), "w") as handle:
                handle.write("x")
        return self._tmp

    def spec(self, *slot_names, **kwargs):
        return FakeSpec(
            kwargs.get("asset_name", "Crate_01"),
            material_slots=[(i, n) for i, n in enumerate(slot_names)])

    def test_no_texture_dir_is_silent(self):
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"), None)
        self.assertEqual(self.rule_ids(), [])

    def test_missing_folder_is_not_an_error(self):
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    os.path.join(self._tmp, "nope"))
        self.assertEqual(self.rule_ids(), [])

    def test_empty_folder_says_nothing(self):
        """テクスチャを置いていないアセット（S-01〜S-10）に嘘を言わない。"""
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    self._tmp)
        self.assertEqual(self.rule_ids(), [])

    def test_folder_with_only_non_textures_says_nothing(self):
        self.put("notes.txt", "source.psd")
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    self._tmp)
        self.assertEqual(self.rule_ids(), [])

    def test_v110_when_a_slot_has_nothing(self):
        self.put("T_Slot_A_BC.png")
        validate_mod.check_textures(
            self.result, self.spec("M_Slot_A", "M_Slot_B"), self._tmp)
        matches = self.assertRule("V-110", "warning")
        self.assertEqual([m.target for m in matches], ["M_Slot_B"])

    def test_v111_when_duplicated(self):
        self.put("T_Crate_Body_BC.png", "T_Crate_Body_BC.tga")
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    self._tmp)
        self.assertRule("V-111", "warning")

    def test_v112_for_bad_file_names(self):
        self.put("T_Crate_Body_BC.png", "crate_body_n.png")
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    self._tmp)
        self.assertRule("V-112", "warning")

    def test_v113_for_jpg_normal_map(self):
        self.put("T_Crate_Body_N.jpg")
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    self._tmp)
        self.assertRule("V-113", "warning")

    def test_v114_when_normal_is_not_non_color(self):
        """S-11 の実測（`_N` の色空間が sRGB）。**情報**であって警告ではない。"""
        self.put("T_M_Crate_Body_N.png")
        BPY.data.images = [FakeImage(r"//textures\T_M_Crate_Body_N.png",
                                     "sRGB")]
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    self._tmp)
        self.assertRule("V-114", "info")

    def test_no_v114_when_normal_is_non_color(self):
        self.put("T_M_Crate_Body_N.png")
        BPY.data.images = [FakeImage("//textures/T_M_Crate_Body_N.png",
                                     "Non-Color")]
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    self._tmp)
        self.assertNoRule("V-114")

    def test_image_colorspace_of_handles_blender_relative_paths(self):
        """`//` 始まりのパスを `os.path.basename()` で切ると**空文字になる**。

        Windows の `ntpath` が `//textures/T_x.png` を UNC パスと解釈するため
        （実測）。ここが壊れると V-114 が**黙って出なくなる**ので固定しておく。
        """
        BPY.data.images = [
            FakeImage("//textures/T_A_N.png", "sRGB"),
            FakeImage(r"//textures\T_B_N.png", "Non-Color"),
            FakeImage(r"C:ssets\Crate_01	extures\T_C_N.png", "sRGB"),
        ]
        self.assertEqual(validate_mod.image_colorspace_of("T_A_N.png"), "sRGB")
        self.assertEqual(validate_mod.image_colorspace_of("T_B_N.png"),
                         "Non-Color")
        self.assertEqual(validate_mod.image_colorspace_of("T_C_N.png"), "sRGB")
        self.assertIsNone(validate_mod.image_colorspace_of("T_D_N.png"))

    def test_no_v114_when_the_image_is_not_in_the_blend(self):
        """フォルダにあるが Blender に読み込まれていない画像には何も言わない。"""
        self.put("T_M_Crate_Body_N.png")
        validate_mod.check_textures(self.result, self.spec("M_Crate_Body"),
                                    self._tmp)
        self.assertNoRule("V-114")

    def test_textures_never_stop_the_export(self):
        self.put("T_Crate_Body_N.jpg", "bad name.png", "T_X_ZZZ.png")
        validate_mod.check_textures(self.result,
                                    self.spec("M_Crate_Body", "M_Other"),
                                    self._tmp)
        self.assertFalse(self.result.has_error())


if __name__ == "__main__":
    unittest.main(verbosity=2)
