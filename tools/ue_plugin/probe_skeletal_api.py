"""UE 5.8 の Skeletal Mesh 関連 API を実機で確認するプローブ（P3）。

## なぜ必要か

`onepochi_ue/skeletal.py` の Python プロパティ名は**すべて導出値**で、実測値ではない。
この環境には UE の Python スタブ（`Intermediate/PythonStub/unreal.py`）が
生成されていないため、エンジンのヘッダと `PyGenUtil.cpp` の命名規則から
手で導いている。**このスクリプトでその導出が正しいかを確認する。**

アセットは一切作らない / 変更しない。**読み取りとプロパティ名の解決だけ**を行う。

## 実行方法

UE エディタの Output Log（Python モード）または Cmd から:

    py "C:/Users/user/Documents/_STUDY/BlenderToUnreal/tools/ue_plugin/probe_skeletal_api.py"

headless（**GUI エディタを閉じてから**）:

    UnrealEditor-Cmd.exe <uproject> -run=pythonscript ^
        -script="tools/ue_plugin/probe_skeletal_api.py" -unattended -nopause -nosplash -stdout

結果は `%LOCALAPPDATA%/OnePochi/probe_skeletal_api.json` と Output Log に出る。
`failed` が空になれば `skeletal.py` の名前はすべて正しい。
"""

import io
import json
import os
import sys

try:
    import unreal
except ImportError:  # pragma: no cover - UE の外では動かない
    print("このスクリプトは UE の Python から実行してください")
    raise SystemExit(2)


def _force_utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def emit(message):
    text = "[OnePochi/probe] {0}".format(message)
    print(text)
    try:
        unreal.log(text)
    except Exception:
        pass


#: (ラベル, サブ設定の取得名の候補, プロパティ名の候補) の一覧。
#: `skeletal.py` が設定しようとしている全項目に対応させる
PIPELINE_CHECKS = (
    ("commonMeshes.force_all_mesh_as_type",
     ("common_meshes_properties", "CommonMeshesProperties"),
     ("force_all_mesh_as_type", "ForceAllMeshAsType")),
    ("commonMeshes.import_lods",
     ("common_meshes_properties", "CommonMeshesProperties"),
     ("import_lods", "bImportLods")),
    ("commonMeshes.bake_meshes",
     ("common_meshes_properties", "CommonMeshesProperties"),
     ("bake_meshes", "bBakeMeshes")),
    ("commonMeshes.import_sockets",
     ("common_meshes_properties", "CommonMeshesProperties"),
     ("import_sockets", "bImportSockets")),
    ("commonMeshes.vertex_color_import_option",
     ("common_meshes_properties", "CommonMeshesProperties"),
     ("vertex_color_import_option", "VertexColorImportOption")),

    ("skeletalCommon.skeleton",
     ("common_skeletal_meshes_and_animations_properties",
      "CommonSkeletalMeshesAndAnimationsProperties"),
     ("skeleton", "Skeleton")),
    ("skeletalCommon.try_auto_select_skeleton",
     ("common_skeletal_meshes_and_animations_properties",
      "CommonSkeletalMeshesAndAnimationsProperties"),
     ("try_auto_select_skeleton", "bTryAutoSelectSkeleton")),
    ("skeletalCommon.import_only_animations",
     ("common_skeletal_meshes_and_animations_properties",
      "CommonSkeletalMeshesAndAnimationsProperties"),
     ("import_only_animations", "bImportOnlyAnimations")),
    ("skeletalCommon.use_t0_as_ref_pose",
     ("common_skeletal_meshes_and_animations_properties",
      "CommonSkeletalMeshesAndAnimationsProperties"),
     ("use_t0_as_ref_pose", "bUseT0AsRefPose")),

    ("mesh.import_skeletal_meshes", ("mesh_pipeline", "MeshPipeline"),
     ("import_skeletal_meshes", "bImportSkeletalMeshes")),
    ("mesh.import_static_meshes", ("mesh_pipeline", "MeshPipeline"),
     ("import_static_meshes", "bImportStaticMeshes")),
    ("mesh.import_geometry_caches", ("mesh_pipeline", "MeshPipeline"),
     ("import_geometry_caches", "bImportGeometryCaches")),
    ("mesh.import_morph_targets", ("mesh_pipeline", "MeshPipeline"),
     ("import_morph_targets", "bImportMorphTargets")),
    ("mesh.create_physics_asset", ("mesh_pipeline", "MeshPipeline"),
     ("create_physics_asset", "bCreatePhysicsAsset")),
    ("mesh.update_skeleton_reference_pose", ("mesh_pipeline", "MeshPipeline"),
     ("update_skeleton_reference_pose", "bUpdateSkeletonReferencePose")),
    ("mesh.bone_influence_limit", ("mesh_pipeline", "MeshPipeline"),
     ("bone_influence_limit", "BoneInfluenceLimit")),
    ("mesh.skeletal_mesh_import_content_type", ("mesh_pipeline", "MeshPipeline"),
     ("skeletal_mesh_import_content_type", "SkeletalMeshImportContentType")),
    ("mesh.combine_skeletal_meshes_behavior", ("mesh_pipeline", "MeshPipeline"),
     ("combine_skeletal_meshes_behavior", "CombineSkeletalMeshesBehavior")),
    ("mesh.threshold_position", ("mesh_pipeline", "MeshPipeline"),
     ("threshold_position", "ThresholdPosition")),
    ("mesh.threshold_tangent_normal", ("mesh_pipeline", "MeshPipeline"),
     ("threshold_tangent_normal", "ThresholdTangentNormal")),
    ("mesh.threshold_uv", ("mesh_pipeline", "MeshPipeline"),
     ("threshold_uv", "ThresholdUV")),

    ("animation.import_animations", ("animation_pipeline", "AnimationPipeline"),
     ("import_animations", "bImportAnimations")),
)

#: 存在を確認したい enum（クラス名, メンバ名）
ENUM_CHECKS = (
    ("InterchangeForceMeshType", "IFMT_SKELETAL_MESH"),
    ("InterchangeForceMeshType", "IFMT_STATIC_MESH"),
    ("InterchangeForceMeshType", "IFMT_NONE"),
    ("InterchangeSkeletalMeshContentType", "ALL"),
    ("InterchangeSkeletalMeshContentType", "GEOMETRY"),
    ("InterchangeSkeletalMeshContentType", "SKINNING_WEIGHTS"),
    ("InterchangeCombineSkeletalMeshesBehavior", "DO_NOT_COMBINE"),
    ("InterchangeVertexColorImportOption", "IVCIO_REPLACE"),
)

#: 存在を確認したい型・ヘルパ
TYPE_CHECKS = (
    "InterchangeManager",
    "InterchangeGenericAssetsPipeline",
    "ImportAssetParameters",
    "SoftObjectPath",
    "SkeletalMesh",
    "Skeleton",
    "AssetRegistryHelpers",
    "AnimPoseExtensions",
    "SkeletonModifier",
    "SkeletalMeshEditorSubsystem",
    "InterchangePipelineStackOverride",
)

#: `ImportAssetParameters` のプロパティ
PARAMETER_CHECKS = (
    ("is_automated", "bIsAutomated"),
    ("replace_existing", "bReplaceExisting"),
    ("follow_redirectors", "bFollowRedirectors"),
    ("override_pipelines", "OverridePipelines"),
)


def probe_types():
    found = {}
    for name in TYPE_CHECKS:
        found[name] = hasattr(unreal, name)
    return found


def probe_enums():
    results = {}
    for class_name, member in ENUM_CHECKS:
        key = "{0}.{1}".format(class_name, member)
        enum_class = getattr(unreal, class_name, None)
        if enum_class is None:
            results[key] = "クラスが無い"
            continue
        results[key] = "OK" if hasattr(enum_class, member) else "メンバが無い"
    return results


#: 「まだ読めていない」を表す番兵。None は正当な現在値なので区別が必要
_UNREAD = object()


def _resolve(target, names, value):
    """候補名を順に試して、通った名前を返す。値は読み戻して確認する。"""
    for name in names:
        try:
            target.set_editor_property(name, value)
        except Exception as error:  # noqa: BLE001
            last = "{0}: {1}".format(type(error).__name__, error)
            continue
        try:
            read_back = target.get_editor_property(name)
        except Exception:  # noqa: BLE001
            read_back = "(読み戻せない)"
        return name, read_back, None
    return None, None, last if names else "候補なし"


def probe_pipeline():
    results = {"applied": {}, "failed": {}, "subObjects": {}}

    pipeline_class = getattr(unreal, "InterchangeGenericAssetsPipeline", None)
    if pipeline_class is None:
        results["failed"]["pipelineClass"] = \
            "unreal.InterchangeGenericAssetsPipeline が無い"
        return results
    try:
        pipeline = pipeline_class()
    except Exception as error:  # noqa: BLE001
        results["failed"]["pipelineInstance"] = "{0}: {1}".format(
            type(error).__name__, error)
        return results

    results["pipelinePathName"] = pipeline.get_path_name()

    # サブ設定オブジェクトが取れるか
    sub_cache = {}
    for _label, sub_names, _prop_names in PIPELINE_CHECKS:
        key = sub_names[0]
        if key in sub_cache:
            continue
        resolved = None
        for name in sub_names:
            try:
                resolved = pipeline.get_editor_property(name)
            except Exception:  # noqa: BLE001
                continue
            if resolved is not None:
                results["subObjects"][key] = {
                    "resolvedBy": name,
                    "class": type(resolved).__name__,
                }
                break
        if resolved is None:
            results["subObjects"][key] = {"resolvedBy": None,
                                          "class": None}
        sub_cache[key] = resolved

    # プロパティ名が解決できるか（真偽値は現在値をそのまま書き戻す）
    for label, sub_names, prop_names in PIPELINE_CHECKS:
        target = sub_cache.get(sub_names[0])
        if target is None:
            results["failed"][label] = "サブ設定オブジェクトが取れない"
            continue
        # 現在値を読む。**読めた時点でその名前は正しい**。
        # 値が None でも「読めなかった」と混同しないよう番兵で区別する
        # （未割り当ての SoftObjectProperty は None が正当な現在値）
        current = _UNREAD
        read_by = None
        for name in prop_names:
            try:
                current = target.get_editor_property(name)
            except Exception:  # noqa: BLE001
                continue
            read_by = name
            break
        if read_by is None:
            results["failed"][label] = "読み出せる候補名が無い（候補: {0}）".format(
                ", ".join(prop_names))
            continue
        # 書き戻しは bool のときだけ行う。オブジェクト型や enum に
        # 既定値以外を書くと型変換エラーになるだけで、名前の検証にならない
        if not isinstance(current, bool):
            results["applied"][label] = {
                "resolvedBy": read_by,
                "value": str(current),
                "note": "読み出しのみで確認（非 bool のため書き戻しはしない）",
            }
            continue
        resolved, read_back, error = _resolve(target, prop_names, current)
        if resolved:
            results["applied"][label] = {"resolvedBy": resolved,
                                         "value": str(read_back)}
        else:
            results["failed"][label] = error
    return results


def probe_parameters():
    results = {"applied": {}, "failed": {}}
    params_class = getattr(unreal, "ImportAssetParameters", None)
    if params_class is None:
        results["failed"]["ImportAssetParameters"] = "クラスが無い"
        return results
    try:
        params = params_class()
    except Exception as error:  # noqa: BLE001
        results["failed"]["instance"] = "{0}: {1}".format(
            type(error).__name__, error)
        return results

    for names in PARAMETER_CHECKS:
        label = names[0]
        if label == "override_pipelines":
            # SoftObjectPath の配列を受け取れるかを確認する
            soft_class = getattr(unreal, "SoftObjectPath", None)
            value = []
            if soft_class is not None:
                try:
                    value = [soft_class("/Engine/Transient.Probe")]
                except Exception as error:  # noqa: BLE001
                    results["failed"]["SoftObjectPath"] = "{0}: {1}".format(
                        type(error).__name__, error)
        else:
            value = True
        resolved, read_back, error = _resolve(params, names, value)
        if resolved:
            results["applied"][label] = {"resolvedBy": resolved,
                                         "value": str(read_back)}
        else:
            results["failed"][label] = error
    return results


def probe_subsystem():
    """`SkeletalMeshEditorSubsystem` が commandlet で使えるか（要検証項目）。"""
    results = {}
    subsystem_class = getattr(unreal, "SkeletalMeshEditorSubsystem", None)
    results["classExists"] = subsystem_class is not None
    if subsystem_class is None:
        return results

    try:
        instance = unreal.get_editor_subsystem(subsystem_class)
        results["getEditorSubsystem"] = (
            "None が返った（commandlet では使えない）" if instance is None
            else "取得できた")
    except Exception as error:  # noqa: BLE001
        results["getEditorSubsystem"] = "{0}: {1}".format(
            type(error).__name__, error)

    try:
        transient = subsystem_class()
        results["directConstruction"] = "できた: {0}".format(
            type(transient).__name__)
    except Exception as error:  # noqa: BLE001
        results["directConstruction"] = "{0}: {1}".format(
            type(error).__name__, error)

    for name in ("get_bone_parent", "get_bone_children", "get_lod_count",
                 "get_num_verts", "get_num_sections"):
        results["has_" + name] = hasattr(subsystem_class, name)
    return results


def probe_readback():
    """SkeletalMesh の読み出し手段の有無（アセットが無くても型で確認できる）。"""
    results = {}
    mesh_class = getattr(unreal, "SkeletalMesh", None)
    results["SkeletalMesh"] = mesh_class is not None
    if mesh_class is not None:
        for name in ("get_all_morph_target_names", "num_sockets",
                     "get_socket_by_index", "find_socket", "get_bounds",
                     "get_imported_bounds", "add_socket"):
            results["SkeletalMesh." + name] = hasattr(mesh_class, name)

    helpers = getattr(unreal, "AssetRegistryHelpers", None)
    results["AssetRegistryHelpers.create_asset_data"] = (
        helpers is not None and hasattr(helpers, "create_asset_data"))

    extensions = getattr(unreal, "AnimPoseExtensions", None)
    results["AnimPoseExtensions"] = extensions is not None
    if extensions is not None:
        for name in ("get_reference_pose", "get_bone_names", "get_socket_names"):
            results["AnimPoseExtensions." + name] = hasattr(extensions, name)

    modifier = getattr(unreal, "SkeletonModifier", None)
    results["SkeletonModifier"] = modifier is not None
    if modifier is not None:
        for name in ("set_skeletal_mesh", "get_all_bone_names",
                     "get_parent_name", "get_children_names"):
            results["SkeletonModifier." + name] = hasattr(modifier, name)
    return results


def main():
    emit("Skeletal Mesh API プローブを開始します（アセットは作成・変更しません）")

    report = {
        "engineVersion": None,
        "pythonVersion": sys.version,
        "types": probe_types(),
        "enums": probe_enums(),
        "pipeline": probe_pipeline(),
        "importAssetParameters": probe_parameters(),
        "skeletalMeshEditorSubsystem": probe_subsystem(),
        "readback": probe_readback(),
    }
    try:
        report["engineVersion"] = unreal.SystemLibrary.get_engine_version()
    except Exception:
        pass

    pipeline_failed = report["pipeline"].get("failed") or {}
    params_failed = report["importAssetParameters"].get("failed") or {}
    missing_types = [k for k, v in report["types"].items() if not v]
    bad_enums = {k: v for k, v in report["enums"].items() if v != "OK"}

    emit("--- 結果 ---")
    emit("解決できたパイプライン設定: {0} 件".format(
        len(report["pipeline"].get("applied") or {})))
    emit("解決できなかったパイプライン設定: {0} 件".format(len(pipeline_failed)))
    for key, reason in pipeline_failed.items():
        emit("  ✖ {0}: {1}".format(key, reason))
    for key, reason in params_failed.items():
        emit("  ✖ ImportAssetParameters.{0}: {1}".format(key, reason))
    if missing_types:
        emit("存在しなかった型: {0}".format(", ".join(missing_types)))
    for key, value in bad_enums.items():
        emit("  ✖ enum {0}: {1}".format(key, value))
    emit("SkeletalMeshEditorSubsystem: {0}".format(
        report["skeletalMeshEditorSubsystem"]))

    ok = not pipeline_failed and not params_failed and not bad_enums
    report["ok"] = ok
    emit("総合判定: {0}".format(
        "すべて解決（skeletal.py の名前は正しい）" if ok
        else "**要修正**（上の ✖ を skeletal.py に反映してください）"))

    out_dir = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "OnePochi")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    out_path = os.path.join(out_dir, "probe_skeletal_api.json")
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2,
                                default=str) + "\n")
    emit("レポート: {0}".format(out_path))
    return 0 if ok else 1


if __name__ == "__main__":
    main()
