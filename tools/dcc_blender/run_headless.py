"""headless 実行用エントリポイント（CI / 検証 / 自動テスト）。

使い方:
    blender.exe --background <file.blend> --python run_headless.py -- \
        --mode export --preset prop --asset-name Crate_01 \
        --asset-folder D:/assets/Crate_01 --report out.json

--mode:
    validate      ... AssetSpec を1件組み立てて検証のみ（書き出さない）
    validate-scene... シーン全体をざっと検証（アセット構成を問わない）
    export        ... 検証 -> 整形 -> FBX -> マニフェスト
    probe-fbx     ... この Blender の FBX エクスポータが受け付ける引数を列挙する

バッチ書き出し（複数アセットを1本のマニフェストにまとめる / N-5）:

    # コレクションを明示する
    blender.exe --background <file.blend> --python run_headless.py -- \
        --mode export --preset sample_debug --collections Crate_01,Barrel_01

    # シーン内のアセット用コレクションを全部まとめる
    blender.exe --background <file.blend> --python run_headless.py -- \
        --mode export --preset sample_debug --all-collections

`--collections` / `--all-collections` のどちらかを付けると `--mode export` は
`pipeline.export_batch()` を呼ぶ（`--collection` 単数は従来の単一アセット経路）。
**1つのアセットでも検証エラーがあればバッチ全体を中断する**（設計方針 D-3）。

結果は標準出力（人間向け）と --report で指定した JSON の両方に出す。
"""

import argparse
import json
import os
import sys

import bpy


def _ensure_import_path():
    """`addons/onepochi` パッケージを import できるようにする。"""
    here = os.path.dirname(os.path.abspath(__file__))
    addons = os.path.join(here, "addons")
    if addons not in sys.path:
        sys.path.insert(0, addons)


def _ensure_fbx_addon():
    """FBX エクスポータを有効化する。戻り値は有効かどうか。"""
    if hasattr(bpy.types, "EXPORT_SCENE_OT_fbx"):
        return True
    try:
        import addon_utils
        addon_utils.enable("io_scene_fbx", default_set=False, persistent=False)
    except Exception:
        pass
    return hasattr(bpy.types, "EXPORT_SCENE_OT_fbx")


def parse_args(argv):
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    parser = argparse.ArgumentParser(prog="run_headless.py", add_help=True)
    parser.add_argument("--mode", default="validate-scene",
                        choices=["validate", "validate-scene", "export", "probe-fbx"])
    parser.add_argument("--preset", default="prop")
    parser.add_argument("--asset-name", default=None)
    parser.add_argument("--asset-folder", default=None)
    parser.add_argument("--collection", default=None)
    parser.add_argument("--collections", default=None,
                        help="カンマ区切りのアセット用コレクション名。"
                             "指定するとバッチ書き出し（1マニフェストに複数アセット）になる")
    parser.add_argument("--all-collections", action="store_true",
                        help="シーン内のアセット用コレクションを全部まとめて"
                             "バッチ書き出しする（LOD<n> / Collision / Sockets を"
                             "子に持つコレクションを自動列挙）")
    parser.add_argument("--ue-path", default=None)
    parser.add_argument("--asset-type", default="StaticMesh")
    parser.add_argument("--objects", default=None,
                        help="カンマ区切りのオブジェクト名。省略時はシーン全体")
    parser.add_argument("--presets-root", default=None)
    parser.add_argument("--environment", default=None,
                        help="出力環境（dev / production）。省略時はプリセットに従う")
    parser.add_argument("--report", default=None, help="結果 JSON の出力先")
    parser.add_argument("--force", action="store_true",
                        help="検証エラーがあっても書き出す（デバッグ用）")
    return parser.parse_args(argv)


def parse_name_list(spec_string):
    """'A, B , C' -> ['A', 'B', 'C']。空なら None。"""
    if not spec_string:
        return None
    names = [n.strip() for n in spec_string.split(",") if n.strip()]
    return names or None


def resolve_objects(spec_string):
    if not spec_string:
        return None
    names = [n.strip() for n in spec_string.split(",") if n.strip()]
    objects = []
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            objects.append(obj)
    return objects


def probe_fbx():
    from onepochi.bl import exporter
    properties = exporter.supported_properties()
    report = {
        "blenderVersion": bpy.app.version_string,
        "fbxOperatorAvailable": exporter.operator_available(),
        "propertyCount": len(properties),
        "properties": {},
    }
    for name in sorted(properties):
        prop = properties[name]
        entry = {"type": prop.type}
        if prop.type == "ENUM":
            entry["enumItems"] = exporter.enum_values(prop)
            entry["isFlag"] = bool(getattr(prop, "is_enum_flag", False))
        try:
            entry["default"] = prop.default
            if isinstance(entry["default"], (set, frozenset)):
                entry["default"] = sorted(entry["default"])
        except Exception:
            pass
        report["properties"][name] = entry
    return report


def issues_to_json(issues):
    return [i.to_manifest() for i in issues.sorted()]


def main():
    _ensure_import_path()
    args = parse_args(sys.argv)
    fbx_ready = _ensure_fbx_addon()

    from onepochi.bl import pipeline
    from onepochi.bl import validate as validate_mod
    from onepochi.core import presets as presets_mod

    report = {
        "mode": args.mode,
        "blenderVersion": bpy.app.version_string,
        "blendFile": bpy.data.filepath,
        "fbxExporterAvailable": fbx_ready,
        "ok": False,
    }

    if args.mode == "probe-fbx":
        report.update(probe_fbx())
        report["ok"] = report.get("fbxOperatorAvailable", False)

    elif args.mode == "validate-scene":
        settings = presets_mod.resolve_settings(
            args.preset, args.asset_folder, None, args.presets_root,
            args.environment
        )
        objects = resolve_objects(args.objects)
        issues = validate_mod.validate_scene_objects(settings, objects)
        report["preset"] = args.preset
        report["summary"] = issues.summary()
        report["issues"] = issues_to_json(issues)
        report["ok"] = not issues.has_error()
        print(issues.format_report())

    elif args.mode == "validate":
        result = pipeline.validate_only(
            preset=args.preset,
            asset_name=args.asset_name,
            objects=resolve_objects(args.objects),
            collection_name=args.collection,
            asset_folder=args.asset_folder,
            presets_root=args.presets_root,
            environment=args.environment,
            asset_type=args.asset_type,
        )
        report["preset"] = args.preset
        report["assetName"] = result.asset_name
        report["summary"] = result.issues.summary()
        report["issues"] = issues_to_json(result.issues)
        report["ok"] = result.ok
        print(result.summary())

    elif args.mode == "export":
        batch = bool(args.all_collections or args.collections)
        if batch:
            result = pipeline.export_batch(
                preset=args.preset,
                collection_names=parse_name_list(args.collections),
                all_collections=bool(args.all_collections),
                asset_folder=args.asset_folder,
                ue_path=args.ue_path,
                asset_type=args.asset_type,
                presets_root=args.presets_root,
                force=args.force,
                environment=args.environment,
            )
        else:
            result = pipeline.export_asset(
                preset=args.preset,
                asset_name=args.asset_name,
                objects=resolve_objects(args.objects),
                collection_name=args.collection,
                asset_folder=args.asset_folder,
                ue_path=args.ue_path,
                asset_type=args.asset_type,
                presets_root=args.presets_root,
                force=args.force,
                environment=args.environment,
            )
        report["preset"] = args.preset
        report["batch"] = batch
        report["assetName"] = result.asset_name
        report["assetNames"] = list(result.asset_names)
        report["runId"] = result.run_id
        report["summary"] = result.issues.summary()
        report["issues"] = issues_to_json(result.issues)
        report["fbxPaths"] = result.fbx_paths
        report["manifestPath"] = result.manifest_path
        report["messages"] = result.messages
        report["ok"] = result.ok
        print(result.summary())

    if args.report:
        directory = os.path.dirname(os.path.abspath(args.report))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("[ワンポチ] レポート: {0}".format(args.report))

    # headless では例外以外で終了コードを変えたいので明示的に終了する
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
