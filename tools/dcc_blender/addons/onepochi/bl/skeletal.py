"""Skeletal Mesh の収集（docs/02_仕様書.md §6 / P3）。

Armature・ボーン階層・スキンウェイト・Shape Key を読み取り、bpy に依存しない
値オブジェクトに変換する。`collect.py` から呼ばれる。

ここで扱うのは **「予測値」** であることが要点。UE 側は取り込み後に実測し、
`ue_report.json` の `comparison` でこの予測値と突き合わせる。
予測が外れたらそれは仕様のバグなので、必ず表に出す（P1 と同じ方針）。

## use_armature_deform_only との関係（重要）

FBX エクスポータの `use_armature_deform_only = True` は
**ウェイトを持たないボーンを書き出しから除外する。**
そのため「マニフェストに書くボーン階層」と「FBX に実際に入るボーン階層」が
ずれる可能性がある。ずれたまま UE 側で比較すると必ず不一致になる。

対策として `exported_bone_names()` で **書き出される集合を予測**し、
マニフェストにはその集合だけを載せる。予測ロジックが正しいかは
「FBX を Blender に読み戻してノード名を確認する」方法で検証する
（docs/99 P-0 と同じ手法）。
"""

import math

from mathutils import Vector

from ..core import manifest as manifest_mod
from . import collect as collect_mod


class BoneEntry(object):
    """ボーン1本。座標は UE 系（cm）に変換済み。"""

    __slots__ = ("name", "parent", "index", "use_deform", "use_connect",
                 "is_leaf", "length_cm", "head_cm", "tail_cm", "rotation")

    def __init__(self, name, parent, index, use_deform=True, use_connect=False,
                 is_leaf=False, length_cm=0.0, head_cm=None, tail_cm=None,
                 rotation=None):
        self.name = name
        self.parent = parent
        self.index = index
        self.use_deform = use_deform
        self.use_connect = use_connect
        self.is_leaf = is_leaf
        self.length_cm = length_cm
        self.head_cm = head_cm or [0.0, 0.0, 0.0]
        self.tail_cm = tail_cm or [0.0, 0.0, 0.0]
        self.rotation = rotation or [0.0, 0.0, 0.0]

    def to_manifest(self):
        return {
            "name": self.name,
            "parent": self.parent,
            "index": self.index,
            "useDeform": self.use_deform,
            "isLeaf": self.is_leaf,
            "lengthCm": self.length_cm,
            "headCm": self.head_cm,
            "tailCm": self.tail_cm,
            "rotation": self.rotation,
        }


class SkinStats(object):
    """スキンウェイトの統計。検証（V-068 / V-069 / V-070）の入力になる。"""

    __slots__ = ("vertex_count", "unassigned_count", "unassigned_samples",
                 "zero_total_count", "deviating_count", "worst_total",
                 "max_deviation", "max_influences", "over_influence_count",
                 "bones_without_weight", "unknown_groups")

    def __init__(self):
        self.vertex_count = 0
        self.unassigned_count = 0
        self.unassigned_samples = []
        self.zero_total_count = 0
        self.deviating_count = 0
        self.worst_total = None
        self.max_deviation = 0.0
        self.max_influences = 0
        self.over_influence_count = 0
        self.bones_without_weight = []
        self.unknown_groups = []

    def to_manifest(self):
        return {
            "vertexCount": self.vertex_count,
            "unassignedVertexCount": self.unassigned_count,
            "zeroWeightVertexCount": self.zero_total_count,
            "notNormalizedVertexCount": self.deviating_count,
            "worstWeightTotal": self.worst_total,
            "maxWeightDeviation": round(self.max_deviation, 6),
            "maxInfluencesPerVertex": self.max_influences,
            "bonesWithoutWeight": list(self.bones_without_weight),
        }


class ShapeKeyInfo(object):
    """Shape Key -> Morph Target。先頭ブロックは基準形状で UE には渡らない。"""

    __slots__ = ("basis_name", "morph_names", "mesh_name")

    def __init__(self, basis_name=None, morph_names=None, mesh_name=None):
        self.basis_name = basis_name
        self.morph_names = list(morph_names or [])
        self.mesh_name = mesh_name

    def to_manifest(self):
        return {
            "basisName": self.basis_name,
            "names": list(self.morph_names),
            "count": len(self.morph_names),
        }


# --- ボーン -------------------------------------------------------------------

#: ウェイト合計が「実質ゼロ」とみなすしきい値（V-070）。
#: 正規化は合計で割るため、合計が極小だと**微小なウェイトが 1.0 まで増幅される**。
#: どのボーンに付くかが実質ランダムになるので、これは正規化で救わずエラーにする。
#: 実測: 90_broken_all の Suzanne に合計 0.000116 の頂点があった
ZERO_WEIGHT_TOTAL = 1.0e-3

#: Blender の最小ボーン長。これ以下は実質ゼロ長として扱う（docs/14 §1.2）
#: Blender は tail を head に重ねても微小な長さにクランプするため、
#: 「ゼロ長」は等値比較では検出できない。
DEGENERATE_BONE_LENGTH_M = 1.0e-4


def bone_world_matrix(armature_obj):
    return armature_obj.matrix_world


def _bone_rotation(armature_obj, bone):
    """ボーンの rest 姿勢を UE のオイラー角（度）に変換する。

    **要検証**: UE 側の実測が済んでいないため、現時点では
    「マニフェストに記録するだけ」で検証には使わない（docs/02 §6）。
    """
    matrix = armature_obj.matrix_world @ bone.matrix_local
    return collect_mod.to_ue_rotation_euler(matrix)


def collect_bones(armature_obj, include=None):
    """Armature からボーン階層を読み取る。

    `include` に名前の集合を渡すと、その集合だけを対象にする
    （`use_armature_deform_only` で除外されるボーンを落とすため）。
    除外されたボーンを親に持つボーンは、**残っている最も近い祖先**に繋ぎ直す。
    こうしないと親が存在しない宙ぶらりんのボーンができる。
    """
    armature = armature_obj.data
    world = bone_world_matrix(armature_obj)
    bones = [b for b in armature.bones if include is None or b.name in include]

    def nearest_kept_parent(bone):
        parent = bone.parent
        while parent is not None:
            if include is None or parent.name in include:
                return parent.name
            parent = parent.parent
        return None

    kept_names = set(b.name for b in bones)
    has_child = set()
    for bone in bones:
        parent_name = nearest_kept_parent(bone)
        if parent_name in kept_names:
            has_child.add(parent_name)

    entries = []
    for index, bone in enumerate(bones):
        head = world @ Vector(bone.head_local)
        tail = world @ Vector(bone.tail_local)
        scale = world.to_scale()
        # 長さはワールドスケールを反映させる（Armature にスケールが残っている場合）
        length_m = bone.length * max(abs(scale.x), abs(scale.y), abs(scale.z))
        entries.append(BoneEntry(
            name=bone.name,
            parent=nearest_kept_parent(bone),
            index=index,
            use_deform=bool(bone.use_deform),
            use_connect=bool(bone.use_connect),
            is_leaf=bone.name not in has_child,
            length_cm=round(length_m * collect_mod.UE_SCALE, 6),
            head_cm=collect_mod.to_ue_location(head),
            tail_cm=collect_mod.to_ue_location(tail),
            rotation=_bone_rotation(armature_obj, bone),
        ))
    return entries


def exported_bone_names(armature_obj, deform_only=False):
    """FBX に実際に書き出されるボーン名の集合を予測する。

    `deform_only` が False なら全ボーン。True のときは
    **ウェイトを持つ（use_deform な）ボーンと、その祖先すべて**。
    祖先を含めるのは、親が消えると階層が繋がらないため。

    **要検証**: Blender 5.2 の FBX エクスポータの実際の挙動と一致するかは
    「FBX を読み戻してノード名を確認する」結合テストで担保する。
    """
    bones = list(armature_obj.data.bones)
    if not deform_only:
        return set(b.name for b in bones)

    keep = set()
    for bone in bones:
        if not bone.use_deform:
            continue
        keep.add(bone.name)
        parent = bone.parent
        while parent is not None:
            keep.add(parent.name)
            parent = parent.parent
    return keep


def root_bones(bone_entries):
    return [b for b in bone_entries if b.parent is None]


def hierarchy_pairs(bone_entries):
    """`manifest.bone_hierarchy_hash()` に渡す [(親, 名前), ...]。"""
    return [(b.parent, b.name) for b in bone_entries]


def hierarchy_hash(bone_entries):
    return manifest_mod.bone_hierarchy_hash(hierarchy_pairs(bone_entries))


def degenerate_bones(armature_obj, include=None):
    """実質ゼロ長のボーン名を返す（docs/14 §1.2 で実際に踏んだ問題）。"""
    found = []
    for bone in armature_obj.data.bones:
        if include is not None and bone.name not in include:
            continue
        if bone.length < DEGENERATE_BONE_LENGTH_M:
            found.append((bone.name, round(bone.length, 9)))
    return found


def non_uniform_bone_scales(armature_obj):
    """ポーズでスケールが入っているボーンを返す（rest に焼かれていない）。"""
    found = []
    pose = getattr(armature_obj, "pose", None)
    if pose is None:
        return found
    for pose_bone in pose.bones:
        scale = pose_bone.matrix_basis.to_scale()
        if any(abs(value - 1.0) > 1.0e-4 for value in scale):
            found.append((pose_bone.name,
                          [round(v, 6) for v in scale]))
    return found


def posed_bones(armature_obj):
    """ポーズに移動・回転が入っているボーンを返す（rest で書き出すべき）。"""
    found = []
    pose = getattr(armature_obj, "pose", None)
    if pose is None:
        return found
    for pose_bone in pose.bones:
        basis = pose_bone.matrix_basis
        translation = basis.to_translation()
        quaternion = basis.to_quaternion()
        moved = any(abs(v) > 1.0e-4 for v in translation)
        # 単位クォータニオン (1,0,0,0) からのずれ
        rotated = (abs(abs(quaternion.w) - 1.0) > 1.0e-4
                   or any(abs(v) > 1.0e-4 for v in (quaternion.x, quaternion.y,
                                                    quaternion.z)))
        if moved or rotated:
            found.append(pose_bone.name)
    return found


# --- スキンウェイト -----------------------------------------------------------


def armature_modifier(mesh_obj):
    for modifier in mesh_obj.modifiers:
        if modifier.type == "ARMATURE":
            return modifier
    return None


def find_armature(mesh_objects):
    """メッシュに付いている Armature オブジェクトを返す（最初に見つかったもの）。"""
    for obj in mesh_objects:
        modifier = armature_modifier(obj)
        if modifier is not None and modifier.object is not None:
            return modifier.object
    return None


def collect_skin(mesh_objects, armature_obj, tolerance=0.001, max_influences=8,
                 sample_limit=8):
    """スキンウェイトを調べる。

    「未割当」の判定は **deform ボーンに対応する頂点グループのウェイト**だけを
    数える。deform でないボーンや、ボーンに対応しない頂点グループ
    （マスク用など）は UE に渡らないため、合計に入れてはいけない。
    """
    stats = SkinStats()
    deform_names = set()
    if armature_obj is not None:
        deform_names = set(b.name for b in armature_obj.data.bones if b.use_deform)

    used_groups = set()
    unknown = set()

    for obj in mesh_objects:
        if obj.type != "MESH":
            continue
        group_names = {index: group.name
                       for index, group in enumerate(obj.vertex_groups)}
        for name in group_names.values():
            if deform_names and name not in deform_names:
                unknown.add(name)

        for vertex in obj.data.vertices:
            stats.vertex_count += 1
            entries = []
            for element in vertex.groups:
                name = group_names.get(element.group)
                if name is None:
                    continue
                if deform_names and name not in deform_names:
                    continue
                if element.weight <= 0.0:
                    continue
                entries.append((name, element.weight))

            if not entries:
                stats.unassigned_count += 1
                if len(stats.unassigned_samples) < sample_limit:
                    stats.unassigned_samples.append(vertex.index)
                continue

            for name, _weight in entries:
                used_groups.add(name)

            total = sum(weight for _name, weight in entries)
            deviation = abs(total - 1.0)
            if total <= ZERO_WEIGHT_TOTAL:
                stats.zero_total_count += 1
            if deviation > tolerance:
                stats.deviating_count += 1
            if deviation > stats.max_deviation:
                stats.max_deviation = deviation
                stats.worst_total = round(total, 8)

            influences = len(entries)
            stats.max_influences = max(stats.max_influences, influences)
            if influences > max_influences:
                stats.over_influence_count += 1

    stats.bones_without_weight = sorted(deform_names - used_groups)
    stats.unknown_groups = sorted(unknown)
    return stats


# --- Shape Key ----------------------------------------------------------------


def collect_shape_keys(mesh_objects):
    """Shape Key を読み取る。先頭ブロックは基準形状として扱う。

    Blender の Shape Key は **先頭が基準形状**（通常 `Basis`）で、
    UE に Morph Target として渡るのは2番目以降（docs/14 §3）。
    """
    for obj in mesh_objects:
        if obj.type != "MESH":
            continue
        keys = getattr(obj.data, "shape_keys", None)
        if keys is None or not keys.key_blocks:
            continue
        blocks = list(keys.key_blocks)
        return ShapeKeyInfo(
            basis_name=blocks[0].name,
            morph_names=[block.name for block in blocks[1:]],
            mesh_name=obj.name,
        )
    return ShapeKeyInfo()


# --- マニフェストブロック -----------------------------------------------------


def skeleton_block(settings, bone_entries, armature_name):
    """マニフェストの `skeleton` ブロック（docs/03 §7）。"""
    skeleton_settings = dict(settings.get("skeleton") or {})
    roots = root_bones(bone_entries)
    block = {
        "mode": skeleton_settings.get("mode", "create_new"),
        "compatibilityMode": skeleton_settings.get("compatibilityMode", "exact"),
        # **null を入れない。**スキーマ（`rootBoneName`）は `"type": "string"` で
        # null を許さない。通常は V-061（ルートボーンが見つかりません）で
        # 止まるが、`force=True` はそれを飛び越えるため、ここで型を守る。
        # 空文字は「ルートが特定できなかった」の意味で、
        # `check_manifest_shape()` が書き出し前に弾く（force でも弾く）
        "rootBoneName": roots[0].name if roots else "",
        "boneCount": len(bone_entries),
        "boneNames": [entry.name for entry in bone_entries],
        "boneHierarchy": [[entry.parent, entry.name] for entry in bone_entries],
        "boneHierarchyHash": hierarchy_hash(bone_entries),
        "sourceArmature": armature_name,
        "bones": [entry.to_manifest() for entry in bone_entries],
    }
    if skeleton_settings.get("assetPath"):
        block["assetPath"] = skeleton_settings["assetPath"]
    if skeleton_settings.get("assetName"):
        block["assetName"] = skeleton_settings["assetName"]
    return block


def skeletal_mesh_block(settings, spec):
    """マニフェストの `skeletalMesh` ブロック（docs/03 §8）。"""
    skeletal = dict(settings.get("skeletalMesh") or {})
    skeletal["skin"] = spec.skin.to_manifest() if spec.skin else None
    skeletal["morphTargets"] = dict(skeletal.get("morphTargets") or {"import": True})
    if spec.shape_keys is not None:
        skeletal["morphTargets"].update(spec.shape_keys.to_manifest())
    skeletal["sockets"] = [socket.to_manifest() for socket in spec.sockets]
    return skeletal


def expected_influence_limit(settings):
    limits = settings.get("limits") or {}
    skeletal = settings.get("skeletalMesh") or {}
    return int(skeletal.get("maxBoneInfluences")
               or limits.get("maxBoneInfluences") or 8)


def weight_tolerance(settings):
    limits = settings.get("limits") or {}
    value = limits.get("weightNormalizationTolerance")
    return float(value) if value is not None else 0.001


def degrees_or_zero(value):
    try:
        return round(math.degrees(float(value)), 6)
    except (TypeError, ValueError):
        return 0.0
