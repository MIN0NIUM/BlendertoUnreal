"""シーン走査: 書き出し対象の収集（docs/02_仕様書.md [A1]）。

Blender のシーンから「アセット1件ぶんの構成」を読み取り、bpy に依存しない
値オブジェクト（AssetSpec）に変換する。以降の検証・整形・書き出しはこの
AssetSpec を入力にする。

対応する2つの指定モード:
    collection ... アセット名と同名のコレクションを起点にする（LOD0/Collision/Sockets）
    selection  ... 選択中のオブジェクトを起点にする（単純なアセット向け）
"""

import math

import bpy
from mathutils import Matrix, Vector

from ..core import naming

# --- Blender -> UE 座標変換 ---------------------------------------------------
#
# 要検証: この変換行列は docs/12_座標系実測.md の実測で確定させる。
# 既定は「Blender (x, y, z) m -> UE (x, -y, z) cm」。FBX 往復で一般に観測される対応。
# メッシュ自体の変換は FBX エクスポータに任せ（方針: 自前で行列を掛けない）、
# ここでの変換は「マニフェストに数値で持つもの」（ソケット等）にのみ使う。

UE_SCALE = 100.0

#: Blender 軸 -> UE 軸の符号付き対応
BLENDER_TO_UE = Matrix((
    (1.0, 0.0, 0.0),
    (0.0, -1.0, 0.0),
    (0.0, 0.0, 1.0),
))


def to_ue_location(vector, scale=UE_SCALE):
    """Blender のローカル座標(m) -> UE 座標(cm)。"""
    converted = BLENDER_TO_UE @ vector
    return [round(converted.x * scale, 6),
            round(converted.y * scale, 6),
            round(converted.z * scale, 6)]


def to_ue_rotation_euler(matrix):
    """Blender の回転行列 -> UE のオイラー角(度)。

    基底変換 B で共役をとる: R_ue = B * R_blender * B^-1
    """
    basis = BLENDER_TO_UE.to_4x4()
    rotation = matrix.to_3x3().to_4x4()
    converted = basis @ rotation @ basis.inverted()
    euler = converted.to_euler("XYZ")
    return [round(math.degrees(euler.x), 6),
            round(math.degrees(euler.y), 6),
            round(math.degrees(euler.z), 6)]


def to_ue_scale(vector):
    return [round(abs(vector.x), 6), round(abs(vector.y), 6), round(abs(vector.z), 6)]


# --- 値オブジェクト -----------------------------------------------------------


class MeshStats(object):
    __slots__ = ("vertex_count", "triangle_count", "uv_channels",
                 "bounds_min_cm", "bounds_max_cm", "dimensions_cm")

    def __init__(self, vertex_count=0, triangle_count=0, uv_channels=0,
                 bounds_min_cm=None, bounds_max_cm=None, dimensions_cm=None):
        self.vertex_count = vertex_count
        self.triangle_count = triangle_count
        self.uv_channels = uv_channels
        self.bounds_min_cm = bounds_min_cm or [0.0, 0.0, 0.0]
        self.bounds_max_cm = bounds_max_cm or [0.0, 0.0, 0.0]
        self.dimensions_cm = dimensions_cm or [0.0, 0.0, 0.0]

    def to_manifest(self):
        return {
            "vertexCount": self.vertex_count,
            "triangleCount": self.triangle_count,
            "uvChannelCount": self.uv_channels,
            "boundsMinCm": self.bounds_min_cm,
            "boundsMaxCm": self.bounds_max_cm,
            "dimensionsCm": self.dimensions_cm,
        }


class CollisionEntry(object):
    __slots__ = ("object_name", "shape", "target", "index", "triangle_count")

    def __init__(self, object_name, shape, target, index, triangle_count=0):
        self.object_name = object_name
        self.shape = shape
        self.target = target
        self.index = index
        self.triangle_count = triangle_count

    def to_manifest(self):
        return {
            "type": self.shape,
            "sourceNode": self.object_name,
            "triangleCount": self.triangle_count,
        }


class SocketEntry(object):
    __slots__ = ("name", "object_name", "parent_bone", "location", "rotation", "scale")

    def __init__(self, name, object_name, parent_bone, location, rotation, scale):
        self.name = name
        self.object_name = object_name
        self.parent_bone = parent_bone
        self.location = location
        self.rotation = rotation
        self.scale = scale

    def to_manifest(self):
        return {
            "name": self.name,
            "parentBone": self.parent_bone,
            "location": self.location,
            "rotation": self.rotation,
            "scale": self.scale,
        }


class LodEntry(object):
    __slots__ = ("level", "collection_name", "object_names")

    def __init__(self, level, collection_name, object_names):
        self.level = level
        self.collection_name = collection_name
        self.object_names = list(object_names)


class AssetSpec(object):
    """アセット1件ぶんの構成。bpy オブジェクトは名前で参照する。"""

    def __init__(self, asset_name, asset_type="StaticMesh", mode="selection"):
        self.asset_name = asset_name
        self.asset_type = asset_type
        self.mode = mode
        self.root_collection = None
        self.lods = []               # list[LodEntry]
        self.collisions = []         # list[CollisionEntry]
        self.sockets = []            # list[SocketEntry]
        self.armature_name = None
        self.material_slots = []     # list[(index, material_name)]
        self.stats = MeshStats()
        self.extra_collections = []  # 予約語以外のサブコレクション（警告用）

        # --- Skeletal Mesh 用（P3）。収集は bl/skeletal.py が行う ---
        # ここで import すると循環するため、collect は枠だけ持つ
        self.bones = []              # list[skeletal.BoneEntry]
        self.skin = None             # skeletal.SkinStats
        self.shape_keys = None       # skeletal.ShapeKeyInfo
        self.exported_bones = set()  # FBX に入ると予測されるボーン名

    @property
    def is_skeletal(self):
        return self.asset_type == "SkeletalMesh"

    @property
    def root_bone_names(self):
        return [b.name for b in self.bones if b.parent is None]

    @property
    def lod0(self):
        for lod in self.lods:
            if lod.level == 0:
                return lod
        return None

    @property
    def main_object_names(self):
        lod0 = self.lod0
        return list(lod0.object_names) if lod0 else []

    def all_mesh_object_names(self):
        names = []
        for lod in self.lods:
            names.extend(lod.object_names)
        return names

    def mesh_objects(self):
        """LOD0 のメッシュオブジェクトを bpy から引き直す。"""
        return [bpy.data.objects[name] for name in self.main_object_names
                if name in bpy.data.objects]

    def __repr__(self):
        return "<AssetSpec {0} {1} lods={2} col={3} sockets={4} bones={5}>".format(
            self.asset_name, self.asset_type, len(self.lods),
            len(self.collisions), len(self.sockets), len(self.bones)
        )


# --- 収集 ---------------------------------------------------------------------


def mesh_triangle_count(obj):
    """評価後メッシュの三角形数。loop_triangles を使う。"""
    mesh = obj.data
    try:
        mesh.calc_loop_triangles()
        return len(mesh.loop_triangles)
    except Exception:
        # 面ごとに (頂点数 - 2) で近似
        return sum(max(len(p.vertices) - 2, 0) for p in mesh.polygons)


def lod_triangle_count(object_names):
    """LOD 1段ぶんの三角形数（`staticMesh.lod.levels[].triangleCount` 用 / R-6）。

    数え方は `collect_stats()` の `triangleCount` と
    バリデータ V-047（`validate.check_lods()`）に**揃えてある**
    （= `mesh_triangle_count()` / 評価後メッシュの `loop_triangles`）。

    メッシュオブジェクトが1つも引けないときは **None** を返す。
    呼び側はマニフェストに**キーごと出さない**こと。
    `0` を入れると「三角形が 0 枚」という別の主張になり、
    UE 側（`lod.declared_lod_triangle_counts()`）が
    **嘘の宣言値と厳密比較してしまう**（schema v0.3.1 は `integer` のみで
    `null` を許さない）。
    """
    total = 0
    found = False
    for name in object_names or ():
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            continue
        total += mesh_triangle_count(obj)
        found = True
    return total if found else None


def collect_stats(objects, scale=UE_SCALE):
    """複数オブジェクトをまとめた統計（cm 単位のワールド境界を含む）。"""
    vertex_count = 0
    triangle_count = 0
    uv_channels = 0
    mins = [None, None, None]
    maxs = [None, None, None]

    for obj in objects:
        if obj.type != "MESH":
            continue
        mesh = obj.data
        vertex_count += len(mesh.vertices)
        triangle_count += mesh_triangle_count(obj)
        uv_channels = max(uv_channels, len(mesh.uv_layers))

        # bound_box はローカル座標の8隅。ワールドへ変換してから UE 座標系に直す
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector((corner[0], corner[1], corner[2]))
            ue = to_ue_location(world, scale)
            for axis in range(3):
                value = ue[axis]
                if mins[axis] is None or value < mins[axis]:
                    mins[axis] = value
                if maxs[axis] is None or value > maxs[axis]:
                    maxs[axis] = value

    if mins[0] is None:
        mins = [0.0, 0.0, 0.0]
        maxs = [0.0, 0.0, 0.0]
    dimensions = [round(abs(maxs[i] - mins[i]), 6) for i in range(3)]
    return MeshStats(vertex_count, triangle_count, uv_channels, mins, maxs, dimensions)


def collect_material_slots(objects):
    """index 順のマテリアルスロットを決定的に作る。

    複数オブジェクトがある場合は「最初に現れた順」で index を振り、
    同名スロットは1つにまとめる。
    """
    slots = []
    seen = set()
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            name = slot.material.name if slot.material else ""
            if name in seen:
                continue
            seen.add(name)
            slots.append((len(slots), name))
    return slots


def _socket_from_empty(obj, armature_name=None):
    socket_name = naming.parse_socket_name(obj.name)
    if not socket_name:
        return None
    parent_bone = None
    if obj.parent and obj.parent_type == "BONE" and obj.parent_bone:
        parent_bone = obj.parent_bone
    # 親に対するローカル行列を使う（親がなければワールド）
    matrix = obj.matrix_local if obj.parent else obj.matrix_world
    return SocketEntry(
        name=socket_name,
        object_name=obj.name,
        parent_bone=parent_bone,
        location=to_ue_location(matrix.to_translation()),
        rotation=to_ue_rotation_euler(matrix),
        scale=to_ue_scale(matrix.to_scale()),
    )


def meshes_skinned_to(armature_obj):
    """その Armature にスキンされているメッシュを返す。

    Skeletal Mesh では **Armature だけを選んで実行する**のが自然な操作だが、
    それだとメッシュが1つも集まらない。Armature から辿れるメッシュを
    拾い直すことで「Armature を選んでワンポチ」が成立する。
    """
    found = []
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for modifier in obj.modifiers:
            if modifier.type == "ARMATURE" and modifier.object is armature_obj:
                found.append(obj)
                break
    if found:
        return found
    # モディファイアが無くても親子付けされているケースを拾う
    return [obj for obj in bpy.data.objects
            if obj.type == "MESH" and obj.parent is armature_obj]


def collect_from_objects(asset_name, objects, asset_type="StaticMesh"):
    """選択モード: 与えられたオブジェクト群から AssetSpec を作る。"""
    spec = AssetSpec(asset_name, asset_type, mode="selection")

    # Armature だけが選ばれている場合、スキンされたメッシュを補う
    objects = list(objects)
    if not any(o.type == "MESH" for o in objects):
        for obj in list(objects):
            if obj.type == "ARMATURE":
                objects.extend(meshes_skinned_to(obj))

    # **本体メッシュを先に確定させる。**コリジョン名の
    # 「本体名 / 連番」の切り分けに本体名の一覧が必要なため
    # （`UCX_Crate_01` は本体 `Crate_01` とも `Crate` の 1 番とも読める）
    meshes = [o for o in objects
              if o.type == "MESH" and not naming.is_collision_name(o.name)]
    body_names = {o.name for o in meshes}

    for obj in objects:
        if obj.type == "MESH":
            if naming.is_collision_name(obj.name):
                shape, target, index = naming.parse_collision_name(obj.name,
                                                                   body_names)
                spec.collisions.append(
                    CollisionEntry(obj.name, shape, target, index, mesh_triangle_count(obj))
                )
        elif obj.type == "EMPTY":
            socket = _socket_from_empty(obj)
            if socket:
                spec.sockets.append(socket)
        elif obj.type == "ARMATURE":
            spec.armature_name = obj.name

    if spec.armature_name is None:
        # 選択に Armature が入っていなくても、メッシュのモジファイアから辿れる
        for obj in meshes:
            for modifier in obj.modifiers:
                if modifier.type == "ARMATURE" and modifier.object is not None:
                    spec.armature_name = modifier.object.name
                    break
            if spec.armature_name:
                break

    spec.lods.append(LodEntry(0, None, [o.name for o in meshes]))
    spec.material_slots = collect_material_slots(meshes)
    spec.stats = collect_stats(meshes)
    return spec


def collect_from_collection(collection, asset_type="StaticMesh"):
    """コレクションモード: アセット用コレクションから AssetSpec を作る。

    想定構成（docs/04 §2.2）:
        <AssetName>/
        ├── LOD0/ ... LODn/
        ├── Collision/
        └── Sockets/
    LOD コレクションが無い場合は、直下のメッシュを LOD0 とみなす。
    """
    spec = AssetSpec(collection.name, asset_type, mode="collection")
    spec.root_collection = collection.name

    lod_collections = {}
    collision_objects = []
    socket_objects = []

    for child in collection.children:
        # Blender の重複サフィックス（`LOD0.001`）を剥がしてから判定する。
        # **コレクション名は .blend 全体で一意**なので、2 つめのアセットの
        # 予約コレクションには必ず `.001` が付く（docs/99 P-24）
        base_name = naming.strip_duplicate_suffix(child.name)
        level = naming.parse_lod_collection(child.name)
        if level is not None:
            existing = lod_collections.get(level)
            if existing is None:
                lod_collections[level] = child
            else:
                # 同じアセット内に LOD0 と LOD0.001 が**両方ある**ケース。
                # 黙って一方を捨てると事故るので、サフィックスの無いほうを
                # 採用し、もう一方は V-045b の警告に回す
                if naming.has_duplicate_suffix(existing.name):
                    lod_collections[level] = child
                    spec.extra_collections.append(existing.name)
                else:
                    spec.extra_collections.append(child.name)
        elif base_name == "Collision":
            collision_objects.extend(child.all_objects)
        elif base_name == "Sockets":
            socket_objects.extend(child.all_objects)
        else:
            spec.extra_collections.append(child.name)

    direct_meshes = [o for o in collection.objects if o.type == "MESH"]

    if lod_collections:
        for level in sorted(lod_collections):
            child = lod_collections[level]
            meshes = [o for o in child.all_objects
                      if o.type == "MESH" and not naming.is_collision_name(o.name)]
            spec.lods.append(LodEntry(level, child.name, [o.name for o in meshes]))
            # LOD コレクション内のコリジョンも拾う（LOD0 のみ対象）
            if level == 0:
                collision_objects.extend(
                    o for o in child.all_objects
                    if o.type == "MESH" and naming.is_collision_name(o.name)
                )
    else:
        meshes = [o for o in direct_meshes if not naming.is_collision_name(o.name)]
        spec.lods.append(LodEntry(0, None, [o.name for o in meshes]))
        collision_objects.extend(o for o in direct_meshes if naming.is_collision_name(o.name))

    # 直下の Empty / Armature も拾う
    for obj in collection.objects:
        if obj.type == "EMPTY":
            socket_objects.append(obj)
        elif obj.type == "ARMATURE":
            spec.armature_name = obj.name
    for obj in collection.all_objects:
        if obj.type == "ARMATURE" and spec.armature_name is None:
            spec.armature_name = obj.name

    # LOD すべての本体メッシュ名を本体候補にする（コリジョンは LOD0 基準だが、
    # 名前の切り分けだけなら全 LOD を見てよい）
    body_names = set()
    for lod in spec.lods:
        body_names.update(lod.object_names)

    for obj in collision_objects:
        parsed = naming.parse_collision_name(obj.name, body_names)
        if parsed:
            shape, target, index = parsed
            spec.collisions.append(
                CollisionEntry(obj.name, shape, target, index, mesh_triangle_count(obj))
            )

    for obj in socket_objects:
        socket = _socket_from_empty(obj, spec.armature_name)
        if socket:
            spec.sockets.append(socket)

    lod0_objects = [bpy.data.objects[name] for name in spec.main_object_names
                    if name in bpy.data.objects]
    spec.material_slots = collect_material_slots(lod0_objects)
    spec.stats = collect_stats(lod0_objects)
    return spec


def find_asset_collections(scene=None):
    """アセット用コレクションの候補を返す。

    「LOD<n> / Collision / Sockets のいずれかを子に持つコレクション」を
    アセット用コレクションとみなす。
    """
    scene = scene or bpy.context.scene
    found = []
    for collection in bpy.data.collections:
        for child in collection.children:
            if naming.is_reserved_collection(child.name):
                found.append(collection)
                break
    return found


def guess_asset_name(context=None):
    """UI のアセット名フィールドの初期値を推測する。"""
    context = context or bpy.context
    active = context.active_object
    if active is not None:
        return naming.strip_known_prefix(active.name)
    return ""
