"""Blender 実機での結合テスト（bpy が必要）。

実行:
    blender.exe --background <sample.blend> \
        --python tools/dcc_blender/tests/test_integration_blender.py -- \
        [--report out.json] [--collection <アセット用コレクション名>]

`--collection` を付けるとコレクション方式で収集する（省略時は**選択オブジェクト**）。
**LOD サンプルは選択モードだと LOD0 しか集まらない**ので、LOD の検証をしたいときは
必ず付ける。例（07 の修正版 / LOD 3 段）::

    blender.exe --background samples/blender/07_static_lod_fixed/07_static_lod_fixed.blend \
        --python tools/dcc_blender/tests/test_integration_blender.py -- --collection Crate_01

検証内容（**元シーンを汚さない**という方針 D-4 の担保が中心）:
    1. 書き出し後、元オブジェクトの名前が完全に復元されている
    2. 作業用コレクション `OnePochi_Work` が残っていない
    3. 退避名 `__onepochi_orig__*` のオブジェクトが残っていない
    4. 元オブジェクトのトランスフォーム・頂点数・マテリアルが変化していない
    5. FBX 内のノード名が元の名前（`.001` が付かない）
    6. 2回連続実行しても同じ結果になる（冪等性）
    7. `staticMesh.lod.levels[].triangleCount` の不変条件（R-6 / schema v0.3.1）
"""

import json
import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
#: アドオンの親 = tools/dcc_blender/addons（Blender の Script Directories 規約）
ADDON_ROOT = os.path.join(os.path.dirname(HERE), "addons")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

from onepochi.bl import pipeline, prepare  # noqa: E402


class Checker(object):
    def __init__(self):
        self.failures = []
        self.checks = 0

    def check(self, condition, message):
        self.checks += 1
        if not condition:
            self.failures.append(message)
        return bool(condition)

    def equal(self, actual, expected, message):
        return self.check(
            actual == expected,
            "{0}（期待: {1} / 実際: {2}）".format(message, expected, actual))


def snapshot_scene():
    """元シーンの状態を記録する。"""
    state = {}
    for obj in bpy.data.objects:
        entry = {
            "type": obj.type,
            "location": tuple(round(v, 6) for v in obj.location),
            "rotation": tuple(round(v, 6) for v in obj.rotation_euler),
            "scale": tuple(round(v, 6) for v in obj.scale),
            "dataName": obj.data.name if obj.data else None,
            "materials": [s.material.name if s.material else None
                          for s in obj.material_slots],
        }
        if obj.type == "MESH":
            entry["vertexCount"] = len(obj.data.vertices)
            entry["polygonCount"] = len(obj.data.polygons)
        state[obj.name] = entry
    return state


def fbx_node_names(fbx_path):
    """FBX を一時シーンに読み込んでノード名を返す（この後シーンは捨てる）。"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for op_path in ("wm.fbx_import", "import_scene.fbx"):
        module_name, operator_name = op_path.split(".")
        operator = getattr(getattr(bpy.ops, module_name), operator_name, None)
        if operator is None:
            continue
        try:
            operator(filepath=fbx_path)
            return sorted(o.name for o in bpy.data.objects)
        except Exception:
            continue
    return None


def check_lod_levels(checker, manifest_path, attempt):
    """`staticMesh.lod.levels[].triangleCount` の不変条件（schema v0.3.1 / R-6）。

    **書き出した JSON を読み直して**確かめる（docs/99 P-22 の教訓:
    「スキーマが許す形 ≠ 仕様書が要求する形」。実際のファイルを見る）。

    - `levels[]` に入っている段には `triangleCount` が必ず入っていること
    - 値は整数で、段が進むごとに**減っている**こと（V-047 と同じ不変条件）
    - **LOD の無いアセットには `levels` ごと存在しない**こと
      （= `triangleCount` が紛れ込まない）

    戻り値はレポート用の要約。
    """
    summary = []
    if not manifest_path or not os.path.isfile(manifest_path):
        checker.check(False,
                      "{0}回目のマニフェストが見つからない: {1}".format(attempt, manifest_path))
        return summary
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    for asset in manifest.get("assets", []):
        name = asset.get("name")
        lod = (asset.get("staticMesh") or {}).get("lod") or {}
        levels = lod.get("levels")
        if not levels:
            checker.check("levels" not in lod,
                          "{0}回目: '{1}' は LOD が無いのに levels がある".format(attempt, name))
            summary.append({"asset": name, "triangleCounts": None})
            continue

        counts = []
        for entry in levels:
            level = entry.get("level")
            value = entry.get("triangleCount")
            ok = checker.check(
                isinstance(value, int) and not isinstance(value, bool),
                "{0}回目: '{1}' の LOD{2} に triangleCount が無い"
                "（schema v0.3.1 / R-6）".format(attempt, name, level))
            if ok:
                counts.append(value)
        for index in range(1, len(counts)):
            checker.check(
                counts[index] < counts[index - 1],
                "{0}回目: '{1}' の LOD 三角形数が段ごとに減っていない: {2}".format(
                    attempt, name, counts))
        summary.append({"asset": name, "triangleCounts": counts})
    return summary


def lod1plus_object_names(manifest_path):
    """LOD1 以降のコレクションに入っているオブジェクト名の集合。

    主 FBX（LOD0）に入らないので、ノード名の期待値から外すために使う。
    **シーンを破棄する前に呼ぶこと。**
    """
    names = set()
    if not manifest_path or not os.path.isfile(manifest_path):
        return names
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    for asset in manifest.get("assets", []):
        lod = (asset.get("staticMesh") or {}).get("lod") or {}
        for entry in (lod.get("levels") or []):
            if not entry.get("level"):      # 0 / None は LOD0 扱い
                continue
            collection = bpy.data.collections.get(entry.get("sourceCollection") or "")
            if collection is None:
                continue
            names.update(o.name for o in collection.all_objects)
    return names


def _option(extra, name):
    if name in extra:
        index = extra.index(name)
        if index + 1 < len(extra):
            return extra[index + 1]
    return None


def main():
    report_path = None
    collection_name = None
    if "--" in sys.argv:
        extra = sys.argv[sys.argv.index("--") + 1:]
        report_path = _option(extra, "--report")
        collection_name = _option(extra, "--collection")

    checker = Checker()
    blend_path = bpy.data.filepath
    before = snapshot_scene()
    original_names = sorted(before)

    results = []
    for attempt in (1, 2):
        result = pipeline.export_asset(preset="sample_debug",
                                       collection_name=collection_name)
        results.append({
            "attempt": attempt,
            "ok": result.ok,
            "assetName": result.asset_name,
            "runId": result.run_id,
            "fbxPaths": result.fbx_paths,
            "manifestPath": result.manifest_path,
            "summary": result.issues.summary(),
            "errors": [i.format_line() for i in result.issues.errors],
        })
        checker.check(result.ok, "{0}回目の書き出しが成功していない: {1}".format(
            attempt, result.issues.summary()))

        if result.ok:
            results[-1]["lodLevels"] = check_lod_levels(
                checker, result.manifest_path, attempt)

        # --- 元シーンが汚れていないか ---
        after = snapshot_scene()
        checker.equal(sorted(after), original_names,
                      "{0}回目の書き出し後にオブジェクト名が変わっている".format(attempt))

        leftovers = [n for n in after if n.startswith(prepare.ORIGINAL_PREFIX)]
        checker.equal(leftovers, [],
                      "{0}回目の書き出し後に退避名のオブジェクトが残っている".format(attempt))

        checker.check(bpy.data.collections.get(prepare.WORK_COLLECTION_NAME) is None,
                      "{0}回目の書き出し後に作業用コレクションが残っている".format(attempt))

        for name in original_names:
            if name not in after:
                continue
            checker.equal(after[name], before[name],
                          "{0}回目の書き出しで '{1}' の状態が変化している".format(attempt, name))

    # --- 2回の結果が同じか（冪等性）---
    if len(results) == 2 and results[0]["ok"] and results[1]["ok"]:
        checker.equal(results[1]["summary"], results[0]["summary"],
                      "2回の検証結果が一致しない")
        checker.equal([os.path.basename(p) for p in results[1]["fbxPaths"]],
                      [os.path.basename(p) for p in results[0]["fbxPaths"]],
                      "2回の FBX ファイル名が一致しない")

    # --- FBX のノード名（最後に行う。シーンを破棄するため）---
    node_names = None
    if results and results[-1]["ok"] and results[-1]["fbxPaths"]:
        # 先頭の FBX は **LOD0**。LOD1 以降は別 FBX なので期待値から外す
        # （外さないと LOD サンプルで必ず落ちる / docs/03 §11.4）
        lod_only = lod1plus_object_names(results[-1]["manifestPath"])
        expected_nodes = sorted(
            n for n in original_names
            if before[n]["type"] in ("MESH", "EMPTY", "ARMATURE") and n not in lod_only
        )
        node_names = fbx_node_names(results[-1]["fbxPaths"][0])
        if checker.check(node_names is not None, "FBX を読み戻せなかった"):
            checker.equal(node_names, expected_nodes,
                          "FBX 内のノード名が元の名前と一致しない（.001 が付いていないか）")

    report = {
        "blendFile": blend_path,
        "collection": collection_name,
        "blenderVersion": bpy.app.version_string,
        "originalObjects": original_names,
        "exports": results,
        "fbxNodeNames": node_names,
        "checks": checker.checks,
        "failures": checker.failures,
        "ok": not checker.failures,
    }

    print("\n" + "=" * 68)
    print("[結合テスト] {0}".format(os.path.basename(blend_path) or "(未保存)"))
    print("  チェック {0} 件 / 失敗 {1} 件".format(checker.checks, len(checker.failures)))
    for failure in checker.failures:
        print("  ✖ {0}".format(failure))
    if not checker.failures:
        print("  ✔ すべて成功")
    print("=" * 68)

    if report_path:
        directory = os.path.dirname(os.path.abspath(report_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("[結合テスト] レポート: {0}".format(report_path))

    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
