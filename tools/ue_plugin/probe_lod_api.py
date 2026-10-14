"""LOD 関連の UE Python API を実機で確認するプローブ（読み取り専用 / アセットを作らない）。

`probe_skeletal_api.py` と同じ趣旨（docs/99_作業ルール.md P-15）。
**推測で名前を決めない。**実在する属性名とシグネチャをここで確定させる。

実行方法（UE エディタを閉じずに Remote Control 経由で呼ぶ）:

    import sys; sys.path.insert(0, r"<repo>/tools/ue_plugin")
    import probe_lod_api; probe_lod_api.main(r"<repo>/docs/tmp/lod_probe.json")

UE の Output Log から直接実行する場合:

    py "<repo>/tools/ue_plugin/probe_lod_api.py"

**アセットの生成・変更は一切しない。**既存アセットを1つ読むが、書き込みはしない。
"""

import json
import os
import sys


def _names(obj, keyword):
    """obj の属性名のうち keyword を含むものを返す。"""
    try:
        return sorted(n for n in dir(obj) if keyword in n.lower())
    except Exception as error:  # noqa: BLE001
        return ["<dir() 失敗: {0}>".format(error)]


def _has(obj, name):
    return bool(obj is not None and hasattr(obj, name))


def probe():
    import unreal

    result = {
        "engineVersion": None,
        "isCommandlet": None,
        "staticMeshEditorSubsystem": {},
        "staticMeshMethods": {},
        "editorStaticMeshLibrary": {},
        "types": {},
        "interchange": {},
        "notes": [],
    }

    try:
        result["engineVersion"] = unreal.SystemLibrary.get_engine_version()
    except Exception as error:  # noqa: BLE001
        result["notes"].append("get_engine_version 失敗: {0}".format(error))

    # --- StaticMeshEditorSubsystem（P-5: commandlet では None になる） ---------
    subsystem = None
    try:
        subsystem = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    except Exception as error:  # noqa: BLE001
        result["notes"].append("get_editor_subsystem 失敗: {0}".format(error))
    result["staticMeshEditorSubsystem"]["available"] = subsystem is not None
    if subsystem is not None:
        result["staticMeshEditorSubsystem"]["lodMembers"] = _names(subsystem, "lod")
        for name in ("import_lod", "set_lod_from_static_mesh", "set_lods",
                     "get_lod_count", "set_lod_build_settings",
                     "get_lod_build_settings", "remove_lods",
                     "set_lod_screen_sizes", "reimport_all_custom_lods",
                     "enable_section_cast_shadow", "set_generate_lightmap_u_vs"):
            result["staticMeshEditorSubsystem"][name] = _has(subsystem, name)

    # --- StaticMesh 自身のメソッド（commandlet でも動くもの / P-5） -----------
    static_mesh_class = getattr(unreal, "StaticMesh", None)
    if static_mesh_class is not None:
        result["staticMeshMethods"]["lodMembers"] = _names(static_mesh_class, "lod")
        for name in ("get_num_lods", "get_num_triangles", "get_num_vertices",
                     "set_num_source_models", "add_source_model",
                     "get_lod_group", "set_lod_group", "get_lod_screen_sizes"):
            result["staticMeshMethods"][name] = _has(static_mesh_class, name)
        result["staticMeshMethods"]["editorProperties"] = _names(
            static_mesh_class, "lod")

    # --- 非推奨の EditorStaticMeshLibrary（UE 4 系の名残） --------------------
    library = getattr(unreal, "EditorStaticMeshLibrary", None)
    result["editorStaticMeshLibrary"]["available"] = library is not None
    if library is not None:
        result["editorStaticMeshLibrary"]["lodMembers"] = _names(library, "lod")

    # --- LOD 設定に使う型 ----------------------------------------------------
    for type_name in ("StaticMeshReductionOptions", "StaticMeshReductionSettings",
                      "EditorScriptingMeshReductionOptions",
                      "EditorScriptingMeshReductionSettings",
                      "MeshReductionSettings", "MeshBuildSettings",
                      "StaticMeshSourceModel", "PerPlatformFloat"):
        klass = getattr(unreal, type_name, None)
        entry = {"available": klass is not None}
        if klass is not None:
            try:
                entry["properties"] = sorted(
                    p for p in dir(klass) if not p.startswith("_"))[:60]
            except Exception as error:  # noqa: BLE001
                entry["properties"] = ["<失敗: {0}>".format(error)]
        result["types"][type_name] = entry

    # --- Interchange 側に LOD の取り込み設定があるか -------------------------
    for type_name in ("InterchangeGenericAssetsPipeline",
                      "InterchangeGenericMeshPipeline",
                      "InterchangeGenericCommonMeshesProperties",
                      "InterchangeGenericStaticMeshesProperties",
                      "InterchangeFbxTranslator"):
        klass = getattr(unreal, type_name, None)
        entry = {"available": klass is not None}
        if klass is not None:
            entry["lodMembers"] = _names(klass, "lod")
        result["interchange"][type_name] = entry

    return result


def main(report_path=None):
    report = {"result": "failed", "errors": []}
    try:
        report = probe()
        report["result"] = "ok"
    except Exception as error:  # noqa: BLE001
        import traceback
        report = {"result": "failed",
                  "errors": [str(error), traceback.format_exc()]}

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print("[OnePochi] LOD プローブ結果:")
    print(text)

    if report_path:
        directory = os.path.dirname(os.path.abspath(report_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print("[OnePochi] 書き出し: {0}".format(report_path))
    return report


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    main(target)
