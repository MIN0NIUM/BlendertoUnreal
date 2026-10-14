"""ジオメトリ整形（docs/02_仕様書.md [A3]）。

**設計方針 D-4**: 破壊的なジオメトリ整形は必ず複製オブジェクト上で行い、
元シーンを汚さない。Undo に依存しない。

処理内容:
    1. 対象オブジェクトを複製し、作業用コレクションに入れる
    2. 親子関係を解除（ワールド変換は維持）。複製同士の親子は張り直す
    3. モディファイアを適用（評価済みメッシュに置き換え）
    4. 回転・スケールをメッシュに焼き込み（位置は維持）
    5. 三角形化
処理後は必ず `cleanup()` を呼ぶ。

## Skeletal Mesh での違い（P3）

スキンメッシュに上の整形をそのまま適用すると**スキニングが壊れる**。
具体的に避けなければならないこと:

    - **Armature モディファイアを適用してはいけない。**
      適用するとスキンが焼き込まれ、ボーンで動かないメッシュになる
    - **複製メッシュの Armature モディファイアは複製 Armature に張り替える。**
      元の Armature は `__onepochi_orig__` に退避されているため、
      張り替えないと FBX に退避中の Armature が引きずられる
    - **回転・スケールの焼き込みをしてはいけない。**
      メッシュだけ変換しても Armature 側は変換されず、バインドがずれる
      （トランスフォームは V-006 / V-077 で上流で弾く）

代わりにここで行う自動修正:

    - **ウェイトの正規化**（V-069）。複製側で行うので元のシーンは汚れない（D-4）
"""

import bmesh
import bpy
from mathutils import Matrix

WORK_COLLECTION_NAME = "OnePochi_Work"

#: 元オブジェクトを一時退避させるときの接頭辞
ORIGINAL_PREFIX = "__onepochi_orig__"

#: ウェイト合計が「実質ゼロ」とみなすしきい値。
#: `bl/skeletal.py` の ZERO_WEIGHT_TOTAL と揃えること（V-070）
ZERO_WEIGHT_TOTAL = 1.0e-3


class PreparedScene(object):
    """整形済みの複製オブジェクト群。with 文で使える。

    **重要**: FBX 内のノード名は UE 側の命名規約（`UCX_<本体名>_##` など）の
    解釈に直結する。Blender はオブジェクト名を一意に保つため、素朴に複製すると
    `UCX_Crate_01_01.001` のような名前になり、UE がコリジョンとして認識できない。

    そこで複製後に
        元オブジェクト -> `__onepochi_orig__<名前>` へ一時退避
        複製           -> 元の名前を引き継ぐ
    とし、`cleanup()` で必ず元の名前へ戻す。
    """

    def __init__(self, collection, name_map, renamed_originals=None):
        self.collection = collection
        #: 元オブジェクト名 -> 複製オブジェクト名（整形後は同じ名前になる）
        self.name_map = name_map
        #: 退避中の名前 -> 戻すべき元の名前
        self.renamed_originals = dict(renamed_originals or {})

    @property
    def objects(self):
        return [bpy.data.objects[n] for n in self.name_map.values()
                if n in bpy.data.objects]

    def duplicate_of(self, original_name):
        new_name = self.name_map.get(original_name)
        if new_name is None:
            return None
        return bpy.data.objects.get(new_name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()
        return False

    def cleanup(self):
        """複製を削除し、**元オブジェクトの名前を必ず元に戻す**。

        例外が起きても元シーンを壊さないことが最重要（方針 D-4）。
        """
        # 1) 複製を消す（先に消さないと名前が戻せない）
        meshes = []
        armatures = []
        for name in list(self.name_map.values()):
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            if obj.type == "MESH" and obj.data is not None:
                meshes.append(obj.data)
            elif obj.type == "ARMATURE" and obj.data is not None:
                armatures.append(obj.data)
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
        for mesh in meshes:
            if mesh.users == 0:
                try:
                    bpy.data.meshes.remove(mesh)
                except Exception:
                    pass
        for armature in armatures:
            if armature.users == 0:
                try:
                    bpy.data.armatures.remove(armature)
                except Exception:
                    pass

        # 2) 元オブジェクトの名前を戻す
        for temp_name, original_name in list(self.renamed_originals.items()):
            obj = bpy.data.objects.get(temp_name)
            if obj is None:
                continue
            try:
                obj.name = original_name
            except Exception:
                pass
        self.renamed_originals = {}

        # 3) 作業用コレクションを消す
        if self.collection is not None:
            collection = bpy.data.collections.get(self.collection)
            if collection is not None:
                try:
                    bpy.data.collections.remove(collection)
                except Exception:
                    pass
        self.name_map = {}
        self.collection = None

    def verify_names(self):
        """複製が元の名前を引き継げているか確認する。

        戻り値: 引き継げなかった (期待名, 実際の名前) のリスト。空なら OK。
        """
        mismatched = []
        for expected, actual in self.name_map.items():
            if expected != actual:
                mismatched.append((expected, actual))
        return mismatched


def _ensure_work_collection(scene=None):
    scene = scene or bpy.context.scene
    existing = bpy.data.collections.get(WORK_COLLECTION_NAME)
    if existing is not None:
        # 前回の残骸があれば中身ごと消す
        for obj in list(existing.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(existing)
    collection = bpy.data.collections.new(WORK_COLLECTION_NAME)
    scene.collection.children.link(collection)
    return collection


def _duplicate_object(obj, collection):
    """オブジェクトとそのデータを複製して collection にリンクする。"""
    new_obj = obj.copy()
    if obj.data is not None:
        new_obj.data = obj.data.copy()
    new_obj.animation_data_clear()
    collection.objects.link(new_obj)
    return new_obj


def _duplicate_taking_name(obj, collection):
    """複製に元の名前を引き継がせる。

    Blender はオブジェクト名を一意に保つため、素朴な複製は `Crate_01.001` に
    なってしまう。FBX のノード名は UE 側の命名規約（`UCX_<本体名>_##` 等）の
    解釈に直結するので、**複製が元の名前を持つ**必要がある。

    手順: 複製を作る → 元を退避名にリネーム → 複製に元の名前を付ける

    戻り値: (複製オブジェクト, 退避中の元の名前 or None)
    """
    original_name = obj.name
    duplicate = _duplicate_object(obj, collection)

    # 元を退避させて名前を空ける
    obj.name = ORIGINAL_PREFIX + original_name
    parked_name = obj.name

    duplicate.name = original_name
    if duplicate.name != original_name:
        # 何らかの理由で名前を取れなかった場合は退避を戻して諦める
        obj.name = original_name
        return duplicate, None

    # メッシュデータ名も揃えておく（インポータによってはデータ名を見る）
    if duplicate.data is not None:
        try:
            duplicate.data.name = original_name
        except Exception:
            pass

    return duplicate, parked_name


def _unparent_keep_transform(obj):
    world = obj.matrix_world.copy()
    obj.parent = None
    obj.matrix_world = world


#: 適用してはいけないモディファイア（スキニング・形状の情報を壊す）
PRESERVED_MODIFIER_TYPES = ("ARMATURE",)


def has_armature_modifier(obj):
    return any(m.type == "ARMATURE" for m in getattr(obj, "modifiers", []))


def apply_modifiers(obj, keep_types=PRESERVED_MODIFIER_TYPES):
    """モディファイアを適用した評価済みメッシュに置き換える。

    `keep_types` のモディファイアは**適用せずに残す**。Armature を適用すると
    スキンが焼き込まれ、ボーンで動かないメッシュになるため。

    残すモディファイアがある場合は、一時的に無効化して評価し、
    評価後に同じ設定で付け直す（評価結果に混ざらないようにする）。
    """
    if obj.type != "MESH" or not obj.modifiers:
        return

    kept = [m for m in obj.modifiers if m.type in keep_types]
    kept_specs = []
    for modifier in kept:
        kept_specs.append({
            "name": modifier.name,
            "type": modifier.type,
            "object": getattr(modifier, "object", None),
            "use_vertex_groups": getattr(modifier, "use_vertex_groups", True),
            "use_bone_envelopes": getattr(modifier, "use_bone_envelopes", False),
            "show_viewport": modifier.show_viewport,
        })
        # 評価結果に Armature の変形が混ざらないよう、一時的に切る
        modifier.show_viewport = False

    if len(kept) == len(list(obj.modifiers)):
        # 残すものだけなら評価は不要。切った表示を戻して終わり
        for modifier, spec in zip(kept, kept_specs):
            modifier.show_viewport = spec["show_viewport"]
        return

    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    new_mesh = bpy.data.meshes.new_from_object(
        evaluated, preserve_all_data_layers=True, depsgraph=depsgraph
    )
    old_mesh = obj.data
    obj.modifiers.clear()
    obj.data = new_mesh
    if old_mesh.users == 0:
        bpy.data.meshes.remove(old_mesh)

    # 残すモディファイアを同じ設定で付け直す
    for spec in kept_specs:
        modifier = obj.modifiers.new(name=spec["name"], type=spec["type"])
        if spec["object"] is not None:
            modifier.object = spec["object"]
        if hasattr(modifier, "use_vertex_groups"):
            modifier.use_vertex_groups = spec["use_vertex_groups"]
        if hasattr(modifier, "use_bone_envelopes"):
            modifier.use_bone_envelopes = spec["use_bone_envelopes"]
        modifier.show_viewport = spec["show_viewport"]


def rewire_armature_modifiers(name_map):
    """複製メッシュの Armature モディファイアを**複製 Armature** に張り替える。

    `_duplicate_taking_name()` で元オブジェクトは `__onepochi_orig__<名前>` に
    退避されている。張り替えないと、複製メッシュのモディファイアが退避中の
    元 Armature を指したままになり、FBX に意図しない Armature が入る。

    戻り値: 張り替えた (メッシュ名, Armature 名) のリスト。
    """
    rewired = []
    for original_name, new_name in name_map.items():
        obj = bpy.data.objects.get(new_name)
        if obj is None or obj.type != "MESH":
            continue
        for modifier in obj.modifiers:
            if modifier.type != "ARMATURE" or modifier.object is None:
                continue
            target_original = modifier.object.name
            if target_original.startswith(ORIGINAL_PREFIX):
                target_original = target_original[len(ORIGINAL_PREFIX):]
            replacement_name = name_map.get(target_original)
            if not replacement_name:
                continue
            replacement = bpy.data.objects.get(replacement_name)
            if replacement is None or replacement.type != "ARMATURE":
                continue
            modifier.object = replacement
            rewired.append((new_name, replacement_name))
    return rewired


def normalize_vertex_weights(obj, deform_bone_names=None, tolerance=0.001):
    """ウェイトの合計を 1.0 に正規化する（V-069 の自動修正）。

    **複製オブジェクト上でのみ呼ぶ**こと（方針 D-4）。
    合計が実質ゼロの頂点は触らない（V-070 でエラーにしている）。

    bmesh の deform レイヤを直接いじるので、頂点数が多くても速い。
    戻り値: 正規化した頂点数。
    """
    if obj.type != "MESH" or not obj.vertex_groups:
        return 0

    target_indices = None
    if deform_bone_names is not None:
        target_indices = set(
            index for index, group in enumerate(obj.vertex_groups)
            if group.name in deform_bone_names
        )
        if not target_indices:
            return 0

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    layer = bm.verts.layers.deform.active
    if layer is None:
        bm.free()
        return 0

    fixed = 0
    for vertex in bm.verts:
        weights = vertex[layer]
        keys = [key for key, value in weights.items()
                if value > 0.0 and (target_indices is None or key in target_indices)]
        if not keys:
            continue
        total = sum(weights[key] for key in keys)
        # 合計が実質ゼロの頂点は触らない。割ると微小値が 1.0 まで増幅され、
        # どのボーンに付くかが実質ランダムになる（V-070 でエラーにしている）
        if total <= ZERO_WEIGHT_TOTAL or abs(total - 1.0) <= tolerance:
            continue
        for key in keys:
            weights[key] = weights[key] / total
        fixed += 1

    if fixed:
        bm.to_mesh(obj.data)
        obj.data.update()
    bm.free()
    return fixed


def apply_rotation_scale(obj):
    """回転とスケールをメッシュに焼き込む。位置は維持する。

    オペレータ（transform_apply）を使わないのでコンテキストに依存しない。
    """
    if obj.type != "MESH":
        return
    location, rotation, scale = obj.matrix_basis.decompose()
    baked = Matrix.LocRotScale(None, rotation, scale)
    if baked == Matrix.Identity(4):
        return
    mesh = obj.data
    has_shape_keys = bool(getattr(mesh, "shape_keys", None))
    try:
        mesh.transform(baked, shape_keys=has_shape_keys)
    except TypeError:
        # 古い API では shape_keys 引数が無い
        mesh.transform(baked)
    obj.matrix_basis = Matrix.Translation(location)
    # 非均一スケールを焼き込むと法線が狂うため再計算させる
    mesh.update()


def triangulate(obj):
    """bmesh で三角形化する（オペレータ不要・background でも動く）。"""
    if obj.type != "MESH":
        return 0
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    before = len(bm.faces)
    bmesh.ops.triangulate(bm, faces=bm.faces[:],
                          quad_method="BEAUTY", ngon_method="BEAUTY")
    after = len(bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return after - before


def remove_loose_geometry(obj):
    """孤立頂点・孤立辺を削除する。"""
    if obj.type != "MESH":
        return 0
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    loose_verts = [v for v in bm.verts if not v.link_faces]
    removed = len(loose_verts)
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return removed


def _reparent_within_duplicates(name_map):
    """複製同士の親子関係を張り直し、それ以外の親は解除する。

    Armature -> メッシュの親子は維持したい（FBX の階層に出る）。
    一方、複製されていない親は参照すると退避中の元を指してしまうため解除する。
    """
    duplicates = set(name_map.values())
    for original_name, new_name in name_map.items():
        obj = bpy.data.objects.get(new_name)
        if obj is None or obj.parent is None:
            continue

        parent_original = obj.parent.name
        if parent_original.startswith(ORIGINAL_PREFIX):
            parent_original = parent_original[len(ORIGINAL_PREFIX):]
        replacement_name = name_map.get(parent_original)

        if replacement_name and replacement_name in duplicates:
            replacement = bpy.data.objects.get(replacement_name)
            if replacement is not None and replacement is not obj:
                world = obj.matrix_world.copy()
                obj.parent = replacement
                obj.matrix_parent_inverse = replacement.matrix_world.inverted()
                obj.matrix_world = world
                continue
        _unparent_keep_transform(obj)


def prepare(object_names, settings, scene=None, skeletal_mode=False,
            deform_bone_names=None):
    """整形済みの複製を作って PreparedScene を返す。

    object_names の順序は保持される（マテリアルスロット順の決定性のため）。

    `skeletal_mode=True` のとき、スキニングを壊す整形を行わない:
        - Armature モディファイアを適用しない（複製 Armature に張り替える）
        - 回転・スケールの焼き込みをしない（メッシュだけ動くとバインドがずれる）
        - 代わりにウェイトを正規化する（V-069 の自動修正）
    """
    export_settings = settings.get("export", {})
    do_triangulate = bool(export_settings.get("triangulate", True))
    do_apply_modifiers = bool(export_settings.get("applyModifiers", True))
    auto_fix = bool((settings.get("validation") or {}).get("autoFix", True))
    tolerance = float((settings.get("limits") or {}).get(
        "weightNormalizationTolerance", 0.001))

    collection = _ensure_work_collection(scene)
    name_map = {}
    renamed_originals = {}

    for name in object_names:
        source = bpy.data.objects.get(name)
        if source is None:
            continue
        duplicate, parked_name = _duplicate_taking_name(source, collection)
        name_map[name] = duplicate.name
        if parked_name:
            renamed_originals[parked_name] = name

    _reparent_within_duplicates(name_map)
    if skeletal_mode:
        rewire_armature_modifiers(name_map)

    for new_name in name_map.values():
        obj = bpy.data.objects.get(new_name)
        if obj is None:
            continue
        skinned = skeletal_mode and has_armature_modifier(obj)

        if do_apply_modifiers:
            apply_modifiers(obj)
        if auto_fix:
            # スキンメッシュはメッシュだけ変換するとバインドがずれるため焼き込まない
            if not skinned:
                apply_rotation_scale(obj)
            remove_loose_geometry(obj)
        if skinned:
            normalize_vertex_weights(obj, deform_bone_names, tolerance)
        if do_triangulate:
            triangulate(obj)

    # 評価結果を確定させる
    bpy.context.view_layer.update()
    return PreparedScene(collection.name, name_map, renamed_originals)
