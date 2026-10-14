"""UE 側の Skeletal Mesh 取り込みと実測（docs/02_仕様書.md §6 / P3）。

## このモジュールの立場

**プロパティ名・enum 名は実機で確定済み**（2026-10-11 / UE 5.8.3）。

もともとは「導出値」だった。UE の Python スタブ
（`Intermediate/PythonStub/unreal.py`）がこの環境に生成されていないため、
名前はエンジンのヘッダと `PyGenUtil.cpp` の命名規則から手で導いていた。
それを次の2段で実機確認した:

    1. `probe_skeletal_api.py` を GUI エディタで実行 → **applied 22 件 / failed 0 件**
    2. 実際の取り込み（S-04 / S-05 / S-06）で
       `pipeline.applied` **20〜21 件 / `failed` 0 件**

つまり **`set_any()` の第1候補（snake_case）がすべて通っている。**

ただし**確定したのは「名前」まで**。取り込みが正しい SkeletalMesh を作るかは
往復テストで別に検証する（S-04 / S-05 / S-06 は
`boneCount` / `boneNames` / `materialSlots` / `morphTargets` の一致を確認済み。
`docs/12 §4.6`）。

候補を順に試す構えは**残す**:

    1. 推定した snake_case 名（例 `force_all_mesh_as_type`）
    2. C++ の生の名前（例 `ForceAllMeshAsType`）
       `set_editor_property()` は Python 名が解決できないとき
       **生の FName にフォールバックする**ため、これが効く

UE のバージョンが上がれば名前は変わりうるし、
どの名前が通ったかをレポートに残す価値も変わらない。
「設定したつもりで効いていない」が一番危ない（docs/99 P-5 / P-6 / P-15 の教訓）。

検証用のプローブ: `tools/ue_plugin/probe_skeletal_api.py`

**commandlet モード（`-run=pythonscript`）での動作は未確認。**上記はすべて
GUI エディタ（Remote Control 経由を含む）での実測。`StaticMeshEditorSubsystem` が
commandlet で取得できない前例があるため（docs/99 P-5）、別物として扱う。

## 設計の要点

- **パイプラインは `import_asset` 呼び出し時に複製される。**
  渡した後にプロパティを変えても効かないので、**呼ぶ前に全部設定する**
- `override_pipelines` は **`SoftObjectPath` の配列**（オブジェクトの配列ではない）
- 既存 Skeleton の流用は `skeleton` プロパティに**ロード済みの USkeleton を代入**する
  （パス文字列ではない）
- 実測の読み出しは**アセット自身のゲッター**を優先する。
  ゲッターは内部で非同期ビルドの完了を待つため、取り込み直後でも安全。
  アセットレジストリのタグはビルド中だと古い値を返すので、
  **0 や空は「ゼロ」ではなく「まだビルド中」として扱う**
"""

import unreal

from . import postprocess

#: SkeletalMesh の取り込みに使う Interchange パイプラインのクラス名候補
PIPELINE_CLASS_NAMES = ("InterchangeGenericAssetsPipeline",)

#: 「すべてスケルタルとして扱う」enum の候補（クラス名, メンバ名）
FORCE_SKELETAL_ENUMS = (
    ("InterchangeForceMeshType", "IFMT_SKELETAL_MESH"),
    ("InterchangeForceMeshType", "SKELETAL_MESH"),
)

#: スケルタルの取り込み内容 enum
CONTENT_TYPE_ENUMS = {
    "all": (("InterchangeSkeletalMeshContentType", "ALL"),),
    "geometry": (("InterchangeSkeletalMeshContentType", "GEOMETRY"),),
    "skinningWeights": (("InterchangeSkeletalMeshContentType", "SKINNING_WEIGHTS"),),
}

#: メッシュ結合の挙動 enum。マニフェスト1件=1アセットなので結合させない
COMBINE_ENUMS = (
    ("InterchangeCombineSkeletalMeshesBehavior", "DO_NOT_COMBINE"),
)


def _enum_value(sink, candidates):
    """(クラス名, メンバ名) の候補から最初に存在する enum 値を返す。"""
    for class_name, member in candidates:
        enum_class = getattr(unreal, class_name, None)
        if enum_class is None:
            continue
        value = getattr(enum_class, member, None)
        if value is not None:
            return value
    sink.append("enum が見つかりませんでした（候補: {0}）".format(
        ", ".join("{0}.{1}".format(c, m) for c, m in candidates)))
    return None


def set_any(sink, target, names, value, label=None):
    """候補名を順に試してプロパティを設定する。成功した名前を返す。

    Python 名と C++ の生の名前の両方を試すのが要点。
    `set_editor_property()` は Python 名が解決できないとき生の FName に
    フォールバックするので、`bImportSkeletalMeshes` のような名前も通る。
    """
    label = label or (names[0] if names else "?")
    if target is None:
        sink.append("設定できません（対象が None）: {0}".format(label))
        return None
    for name in names:
        try:
            target.set_editor_property(name, value)
        except Exception:  # noqa: BLE001
            continue
        return name
    sink.append("プロパティを設定できませんでした: {0}（候補: {1} / 値: {2!r}）".format(
        label, ", ".join(names), value))
    return None


def get_any(sink, target, names, default=None):
    """候補名を順に試してプロパティを読む。"""
    if target is None:
        return default
    for name in names:
        try:
            return target.get_editor_property(name)
        except Exception:  # noqa: BLE001
            continue
    return default


def _sub_object(sink, pipeline, names, label):
    """パイプラインのサブ設定オブジェクトを取り出す。"""
    value = get_any(sink, pipeline, names)
    if value is None:
        sink.append("パイプラインの {0} を取得できませんでした（候補: {1}）".format(
            label, ", ".join(names)))
    return value


def build_pipeline(settings, sink, existing_skeleton=None):
    """SkeletalMesh 取り込み用の Interchange パイプラインを作る。

    `settings` はマニフェストの `skeleton` / `skeletalMesh` ブロック由来の辞書。
    戻り値: (パイプラインオブジェクト, 適用できた設定の記録)。
    作れなければ (None, 記録)。

    プロパティ名は **UE 5.8.3 で実測済み**（`applied` 20〜21 件 / `failed` 0 件）。
    それでも `applied` / `failed` はレポートに残し続ける
    — バージョンが上がれば名前は変わりうるため。
    """
    applied = {}
    failed = []

    pipeline = None
    for class_name in PIPELINE_CLASS_NAMES:
        pipeline_class = getattr(unreal, class_name, None)
        if pipeline_class is None:
            continue
        pipeline = postprocess.attempt(sink, class_name, pipeline_class)
        if pipeline is not None:
            break
    if pipeline is None:
        sink.append("Interchange のパイプラインクラスが見つかりませんでした"
                    "（候補: {0}）。既定パイプラインで取り込みます".format(
                        ", ".join(PIPELINE_CLASS_NAMES)))
        return None, {"applied": applied, "failed": ["pipelineClass"]}

    def record(key, name):
        if name:
            applied[key] = name
        else:
            failed.append(key)

    # --- 共通メッシュ設定 ---------------------------------------------------
    common = _sub_object(
        sink, pipeline,
        ("common_meshes_properties", "CommonMeshesProperties"),
        "common_meshes_properties")

    force_skeletal = _enum_value(sink, FORCE_SKELETAL_ENUMS)
    if force_skeletal is not None:
        record("forceAllMeshAsType", set_any(
            sink, common,
            ("force_all_mesh_as_type", "ForceAllMeshAsType"),
            force_skeletal))
    else:
        failed.append("forceAllMeshAsType")

    record("importLods", set_any(sink, common, ("import_lods", "bImportLods"),
                                 bool(settings.get("importLods", True))))
    record("bakeMeshes", set_any(sink, common, ("bake_meshes", "bBakeMeshes"), True))
    record("importSockets", set_any(
        sink, common, ("import_sockets", "bImportSockets"),
        bool(settings.get("importSockets", True))))

    # --- Skeleton 関連 ------------------------------------------------------
    skeletal_common = _sub_object(
        sink, pipeline,
        ("common_skeletal_meshes_and_animations_properties",
         "CommonSkeletalMeshesAndAnimationsProperties"),
        "common_skeletal_meshes_and_animations_properties")

    # 自動選択は切る。どの Skeleton を使うかはマニフェストで宣言する（D-2）
    record("tryAutoSelectSkeleton", set_any(
        sink, skeletal_common,
        ("try_auto_select_skeleton", "bTryAutoSelectSkeleton"), False))
    record("importOnlyAnimations", set_any(
        sink, skeletal_common,
        ("import_only_animations", "bImportOnlyAnimations"), False))
    record("useT0AsRefPose", set_any(
        sink, skeletal_common, ("use_t0_as_ref_pose", "bUseT0AsRefPose"),
        bool(settings.get("useT0AsRefPose", False))))

    if existing_skeleton is not None:
        # **パス文字列ではなくロード済みの USkeleton を代入する**
        record("skeleton", set_any(sink, skeletal_common,
                                   ("skeleton", "Skeleton"), existing_skeleton))

    # --- メッシュパイプライン ----------------------------------------------
    mesh = _sub_object(sink, pipeline, ("mesh_pipeline", "MeshPipeline"),
                       "mesh_pipeline")

    record("importSkeletalMeshes", set_any(
        sink, mesh, ("import_skeletal_meshes", "bImportSkeletalMeshes"), True))
    record("importStaticMeshes", set_any(
        sink, mesh, ("import_static_meshes", "bImportStaticMeshes"), False))
    record("importGeometryCaches", set_any(
        sink, mesh, ("import_geometry_caches", "bImportGeometryCaches"), False))

    morph_targets = (settings.get("morphTargets") or {})
    record("importMorphTargets", set_any(
        sink, mesh, ("import_morph_targets", "bImportMorphTargets"),
        bool(morph_targets.get("import", True))))

    record("createPhysicsAsset", set_any(
        sink, mesh, ("create_physics_asset", "bCreatePhysicsAsset"),
        bool(settings.get("createPhysicsAsset", False))))
    record("updateSkeletonReferencePose", set_any(
        sink, mesh,
        ("update_skeleton_reference_pose", "bUpdateSkeletonReferencePose"),
        bool(settings.get("updateSkeletonReferencePose", False))))

    influences = settings.get("maxBoneInfluences")
    if influences is not None:
        # 0 = プロジェクト設定に従う
        record("boneInfluenceLimit", set_any(
            sink, mesh, ("bone_influence_limit", "BoneInfluenceLimit"),
            int(influences)))

    content_type = settings.get("importContentType", "all")
    content_enum = _enum_value(sink, CONTENT_TYPE_ENUMS.get(
        content_type, CONTENT_TYPE_ENUMS["all"]))
    if content_enum is not None:
        record("skeletalMeshImportContentType", set_any(
            sink, mesh,
            ("skeletal_mesh_import_content_type",
             "SkeletalMeshImportContentType"),
            content_enum))

    combine_enum = _enum_value(sink, COMBINE_ENUMS)
    if combine_enum is not None:
        # マニフェスト1件=1アセットなので、勝手に結合させない
        record("combineSkeletalMeshesBehavior", set_any(
            sink, mesh,
            ("combine_skeletal_meshes_behavior",
             "CombineSkeletalMeshesBehavior"),
            combine_enum))

    thresholds = settings.get("thresholds") or {}
    for key, names in (
        ("position", ("threshold_position", "ThresholdPosition")),
        ("tangentNormal", ("threshold_tangent_normal", "ThresholdTangentNormal")),
        ("uv", ("threshold_uv", "ThresholdUV")),
    ):
        if thresholds.get(key) is not None:
            record("threshold_" + key,
                   set_any(sink, mesh, names, float(thresholds[key])))

    # --- アニメーション（Q-08: 将来対応。今は切る）---------------------------
    animation = _sub_object(sink, pipeline,
                            ("animation_pipeline", "AnimationPipeline"),
                            "animation_pipeline")
    record("importAnimations", set_any(
        sink, animation, ("import_animations", "bImportAnimations"), False))

    sink.append("スケルタル用パイプラインを構成しました（成功 {0} 件 / 失敗 {1} 件）".format(
        len(applied), len(failed)))
    if failed:
        sink.append("設定できなかった項目: {0}".format(", ".join(failed)))

    return pipeline, {"applied": applied, "failed": failed}


def pipeline_override_paths(pipeline, sink):
    """`ImportAssetParameters.override_pipelines` に渡す値を作る。

    **`SoftObjectPath` の配列**であってオブジェクトの配列ではない
    （`InterchangeManager.h` の `TArray<FSoftObjectPath>`）。
    """
    if pipeline is None:
        return None
    try:
        path_name = pipeline.get_path_name()
    except Exception as error:  # noqa: BLE001
        sink.append("パイプラインのパスを取得できませんでした: {0}".format(error))
        return None

    soft_class = getattr(unreal, "SoftObjectPath", None)
    if soft_class is None:
        sink.append("unreal.SoftObjectPath が無いため、文字列で渡します")
        return [path_name]
    try:
        return [soft_class(path_name)]
    except Exception as error:  # noqa: BLE001
        sink.append("SoftObjectPath を作れませんでした（文字列で渡します）: {0}".format(error))
        return [path_name]


# --- 既存 Skeleton の読み出し -------------------------------------------------


def read_skeleton_bone_names(skeleton, sink):
    """既存 Skeleton のボーン名を**スケルトンのインデックス順**で返す。

    `FReferenceSkeleton` は USTRUCT ではないため Python から直接触れない。
    `AnimPoseExtensions.get_reference_pose()` 経由が最も確実
    （`AnimationBlueprintLibrary` は UnrealEd の固定依存）。

    戻り値: (名前のリスト, 使った手段) / 取れなければ ([], None)
    """
    extensions = getattr(unreal, "AnimPoseExtensions", None)
    if extensions is None:
        sink.append("unreal.AnimPoseExtensions が無いため"
                    "既存 Skeleton のボーン名を読めません")
        return [], None

    pose = postprocess.attempt(sink, "AnimPoseExtensions.get_reference_pose",
                               extensions.get_reference_pose, skeleton)
    if pose is None:
        return [], None
    names = postprocess.attempt(sink, "AnimPoseExtensions.get_bone_names",
                                extensions.get_bone_names, pose)
    if names is None:
        return [], None
    return [str(name) for name in names], "animPose"


def find_mesh_for_skeleton(skeleton, sink):
    """既存 Skeleton を使っている SkeletalMesh を1つ探す（実測で確認済み）。

    `SkeletonModifier` は **SkeletalMesh を要求する**ため、Skeleton だけを
    渡された状態では親子関係が読めない。そこで取り込み**前**に、
    その Skeleton を既に使っているメッシュを探して橋渡しする。

    手段は2つ。順に試す（UE 5.8.3 で両方とも動作を確認）:
        1. `Skeleton.get_skeleton_preview_mesh()` — Interchange が作った
           Skeleton には取り込み元のメッシュが入っている
        2. アセットレジストリの `Skeleton` タグ検索 — プレビューメッシュが
           未設定でも、同じ Skeleton を参照するメッシュがあれば拾える
           （タグはロード不要で読めるので安い）

    戻り値: (SkeletalMesh or None, 使った手段 or None)
    """
    mesh = postprocess.attempt(sink, "Skeleton.get_skeleton_preview_mesh",
                               skeleton.get_skeleton_preview_mesh)
    if mesh is not None:
        return mesh, "previewMesh"

    helpers = getattr(unreal, "AssetRegistryHelpers", None)
    top_level = getattr(unreal, "TopLevelAssetPath", None)
    if helpers is None or top_level is None:
        sink.append("アセットレジストリが使えないため、Skeleton を使っている"
                    "メッシュを探せません")
        return None, None

    skeleton_path = postprocess.attempt(sink, "Skeleton.get_path_name",
                                        skeleton.get_path_name)
    if not skeleton_path:
        return None, None

    registry = postprocess.attempt(sink, "get_asset_registry",
                                   helpers.get_asset_registry)
    if registry is None:
        return None, None
    entries = postprocess.attempt(
        sink, "get_assets_by_class(SkeletalMesh)",
        registry.get_assets_by_class,
        top_level("/Script/Engine", "SkeletalMesh"), False)
    for entry in entries or []:
        tag = postprocess.attempt(sink, "get_tag_value(Skeleton)",
                                  entry.get_tag_value, "Skeleton")
        if tag and str(skeleton_path) in str(tag):
            found = postprocess.attempt(sink, "load_asset(SkeletalMesh)",
                                        unreal.load_asset,
                                        str(entry.package_name))
            if found is not None:
                return found, "assetRegistry"
    return None, None


def read_skeleton_hierarchy(skeleton, sink, skeletal_mesh=None):
    """既存 Skeleton のボーン階層を [(親, 名前), ...] で返す。

    親子関係は `USkeleton` だけからは取れない（`FReferenceSkeleton` が
    リフレクションに載っていない）。`SkeletonModifier` が使えれば
    そこから取るが、無い環境では**ボーン名の並びだけ**を返す。

    `skeletal_mesh` を省略した場合は `find_mesh_for_skeleton()` で
    「その Skeleton を使っているメッシュ」を探す。これで**取り込み前**でも
    親子関係を検証できる（UE 5.8.3 で実測）。

    戻り値: dict(pairs, names, method, notes)
        method: "skeletonModifier" / "namesOnly" / None
    """
    notes = []
    names, _method = read_skeleton_bone_names(skeleton, sink)

    modifier_class = getattr(unreal, "SkeletonModifier", None)
    if modifier_class is not None and skeletal_mesh is None:
        # Skeleton だけ渡された（取り込み前の互換判定）。
        # その Skeleton を使っているメッシュを探して橋渡しする
        skeletal_mesh, how = find_mesh_for_skeleton(skeleton, sink)
        if skeletal_mesh is not None:
            notes.append("親子関係を読むため、この Skeleton を使っている"
                         "メッシュを利用しました（{0}）".format(how))
        else:
            notes.append("この Skeleton を使っている SkeletalMesh が"
                         "見つかりませんでした（プレビューメッシュ未設定 かつ "
                         "参照メッシュ無し）")
    if modifier_class is not None and skeletal_mesh is not None:
        modifier = postprocess.attempt(sink, "SkeletonModifier", modifier_class)
        if modifier is not None:
            ok = postprocess.attempt(sink, "SkeletonModifier.set_skeletal_mesh",
                                     modifier.set_skeletal_mesh, skeletal_mesh)
            if ok:
                bone_names = postprocess.attempt(
                    sink, "SkeletonModifier.get_all_bone_names",
                    modifier.get_all_bone_names)
                if bone_names:
                    pairs = []
                    for bone in bone_names:
                        parent = postprocess.attempt(
                            sink, "SkeletonModifier.get_parent_name",
                            modifier.get_parent_name, bone)
                        parent_name = str(parent) if parent else None
                        if parent_name in ("None", "", "NAME_None"):
                            parent_name = None
                        pairs.append([parent_name, str(bone)])
                    return {"pairs": pairs,
                            "names": [str(b) for b in bone_names],
                            "method": "skeletonModifier",
                            "notes": notes}
            notes.append("SkeletonModifier で階層を読めませんでした"
                         "（メッシュに Skeleton が無い / LOD0 が空 / "
                         "/Engine 配下のアセット のいずれか）")
    elif modifier_class is None:
        notes.append("unreal.SkeletonModifier が無いため親子関係を読めません"
                     "（MeshModelingToolset プラグインが必要）")

    if names:
        notes.append("既存 Skeleton は**ボーン名の並びだけ**読めました。"
                     "親子関係は検証できないため、exact 判定は"
                     "「名前と並び順の一致」で代用します")
        return {"pairs": None, "names": names, "method": "namesOnly",
                "notes": notes}

    notes.append("既存 Skeleton のボーン情報を読めませんでした")
    return {"pairs": None, "names": [], "method": None, "notes": notes}


# --- 実測 ---------------------------------------------------------------------

#: アセットレジストリのタグ名（`USkeletalMesh::GetAssetRegistryTags` より）
REGISTRY_TAGS = {
    "bones": "Bones",
    "vertices": "Vertices",
    "triangles": "Triangles",
    "lods": "LODs",
    "morphTargetNames": "MorphTargetNames",
    "maxBoneInfluences": "MaxBoneInfluences",
}


def _registry_tags(asset, sink):
    """アセットレジストリのタグを読む（保存前のアセットでも読める）。

    **ビルド中は古い値／空が返る。**0 や空を「ゼロ」と解釈してはいけない。
    """
    helpers = getattr(unreal, "AssetRegistryHelpers", None)
    if helpers is None or not hasattr(helpers, "create_asset_data"):
        sink.append("AssetRegistryHelpers.create_asset_data が無いため"
                    "レジストリタグを読めません")
        return {}
    data = postprocess.attempt(sink, "create_asset_data",
                               helpers.create_asset_data, asset)
    if data is None:
        return {}

    values = {}
    for key, tag in REGISTRY_TAGS.items():
        value = None
        for getter in ("get_tag_value",):
            method = getattr(data, getter, None)
            if method is None:
                continue
            try:
                value = method(tag)
            except Exception:  # noqa: BLE001
                value = None
            break
        if value is not None:
            values[key] = value
    return values


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def measure(skeletal_mesh, sink):
    """取り込んだ SkeletalMesh を実測する。

    **アセット自身のゲッターを優先する。**ゲッターは内部で非同期ビルドの
    完了を待つため、取り込み直後でも安全（レジストリタグは待たない）。
    """
    measured = {}

    # --- ゲッター経由（非同期ビルドに対して安全） ---
    materials = get_any(sink, skeletal_mesh, ("materials", "Materials"))
    if materials is not None:
        slots = []
        try:
            for entry in materials:
                material = get_any(sink, entry,
                                   ("material_interface", "MaterialInterface"))
                slot_name = get_any(sink, entry,
                                    ("material_slot_name", "MaterialSlotName"))
                imported = get_any(sink, entry, ("imported_material_slot_name",
                                                 "ImportedMaterialSlotName"))
                slots.append({
                    "slotName": str(slot_name) if slot_name else None,
                    "importedSlotName": str(imported) if imported else None,
                    "material": (material.get_path_name()
                                 if material is not None
                                 and hasattr(material, "get_path_name") else None),
                })
        except TypeError:
            sink.append("materials を反復できませんでした")
        measured["materialSlots"] = slots

    for key, method_names in (
        ("morphTargetNames", ("get_all_morph_target_names",)),
        ("socketCount", ("num_sockets",)),
    ):
        for method_name in method_names:
            method = getattr(skeletal_mesh, method_name, None)
            if method is None:
                continue
            value = postprocess.attempt(sink, method_name, method)
            if value is None:
                continue
            measured[key] = ([str(v) for v in value]
                             if key == "morphTargetNames" else value)
            break

    skeleton = get_any(sink, skeletal_mesh, ("skeleton", "Skeleton"))
    if skeleton is not None and hasattr(skeleton, "get_path_name"):
        measured["skeletonPath"] = skeleton.get_path_name()

    physics = get_any(sink, skeletal_mesh, ("physics_asset", "PhysicsAsset"))
    measured["physicsAssetPath"] = (physics.get_path_name()
                                    if physics is not None
                                    and hasattr(physics, "get_path_name") else None)

    for key, method_name in (("bounds", "get_bounds"),
                             ("importedBounds", "get_imported_bounds")):
        method = getattr(skeletal_mesh, method_name, None)
        if method is None:
            continue
        bounds = postprocess.attempt(sink, method_name, method)
        if bounds is None:
            continue
        origin = get_any(sink, bounds, ("origin", "Origin"))
        extent = get_any(sink, bounds, ("box_extent", "BoxExtent"))
        if origin is None or extent is None:
            continue
        measured[key] = {
            "min": [round(origin.x - extent.x, 6), round(origin.y - extent.y, 6),
                    round(origin.z - extent.z, 6)],
            "max": [round(origin.x + extent.x, 6), round(origin.y + extent.y, 6),
                    round(origin.z + extent.z, 6)],
            "dimensions": [round(extent.x * 2.0, 6), round(extent.y * 2.0, 6),
                           round(extent.z * 2.0, 6)],
        }

    # --- ボーン階層（SkeletonModifier があれば） ---
    if skeleton is not None:
        hierarchy = read_skeleton_hierarchy(skeleton, sink, skeletal_mesh)
        measured["boneNames"] = hierarchy["names"]
        measured["boneHierarchy"] = hierarchy["pairs"]
        measured["boneReadMethod"] = hierarchy["method"]
        for note in hierarchy["notes"]:
            sink.append(note)

    # --- レジストリタグ（ビルド中は信用しない） ---
    tags = _registry_tags(skeletal_mesh, sink)
    for key in ("bones", "vertices", "triangles", "lods", "maxBoneInfluences"):
        if key in tags:
            measured[key] = _as_int(tags[key])
    if "morphTargetNames" in tags and "morphTargetNames" not in measured:
        measured["morphTargetNames"] = [
            name for name in str(tags["morphTargetNames"]).split(";") if name]

    if measured.get("bones") in (None, 0) and measured.get("boneNames"):
        # タグが使えなくてもボーン名が取れていれば本数は分かる
        measured["bones"] = len(measured["boneNames"])
        sink.append("ボーン数はレジストリタグではなくボーン名の数から求めました")

    if measured.get("vertices") in (None, 0):
        sink.append("頂点数が 0 / 取得不能でした。"
                    "**スキンメッシュの非同期ビルドが未完了の可能性**があります"
                    "（0 を実測値として扱わないこと）")

    return measured
