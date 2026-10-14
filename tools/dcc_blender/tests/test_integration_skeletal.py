"""Skeletal Mesh の Blender 実機結合テスト（bpy が必要 / P3）。

実行:
    blender.exe --background <skeletal_sample.blend> \
        --python tools/dcc_blender/tests/test_integration_skeletal.py -- [--report out.json]

スキンメッシュの整形は「やってはいけないこと」が多く、壊れても
**Blender 上では一見正常に見える**（FBX の中身を見ないと分からない）。
そこで書き出した FBX を読み戻して機械的に確認する（docs/99 P-0 と同じ手法）。

検証内容:
    1. 収集: Armature だけが選択されていてもメッシュを拾えている（docs/99 P-12）
    2. 収集: ボーン階層・スキン統計・Shape Key が取れている
    3. 元シーンを汚していない（名前の復元 / 作業用コレクションの残骸なし）
    4. **FBX を読み戻して**:
       a. ボーン名・親子関係が元と一致する（`.001` などが付かない）
       b. ルートボーンが1本で原点にある
       c. Armature が1個だけ（退避した元が混入していない）
       d. メッシュの Armature モジファイアが**その Armature を指している**
       e. **ウェイトが正規化されている**（V-069 の自動修正が効いている）
       f. 未割当の頂点が無い
    5. **末端ボーンの長さは検証しない**（FBX は関節の位置しか持たないため
       往復で保存されない / docs/99 P-11）

マテリアル名などの規約違反があるサンプルでも整形経路を検証したいので、
書き出しは `force=True` で行う。
"""

import json
import os
import shutil
import sys
import tempfile

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.join(os.path.dirname(HERE), "addons")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if ADDON_ROOT not in sys.path:
    sys.path.insert(0, ADDON_ROOT)

from onepochi.bl import pipeline, prepare, skeletal  # noqa: E402
from onepochi.core import presets as presets_mod  # noqa: E402

PRESET = "character"

#: ウェイト合計の許容誤差（FBX の丸めを考慮）
WEIGHT_EPS = 1.0e-3

#: ルートボーンが原点にあるとみなす許容誤差（m）
ORIGIN_EPS = 1.0e-5


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
            "{0}（期待: {1!r} / 実際: {2!r}）".format(message, expected, actual))


def _import_fbx(fbx_path):
    """FBX を空シーンに読み込む。**この後 Blender のシーンは捨てられる。**"""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for op_path in ("wm.fbx_import", "import_scene.fbx"):
        module_name, operator_name = op_path.split(".")
        operator = getattr(getattr(bpy.ops, module_name), operator_name, None)
        if operator is None:
            continue
        try:
            operator(filepath=fbx_path)
            return True
        except Exception as error:  # noqa: BLE001
            print("  {0} 失敗: {1}".format(op_path, error))
    return False


def _hierarchy(armature_obj):
    return [(b.parent.name if b.parent else None, b.name)
            for b in armature_obj.data.bones]


def _weight_summary(mesh_obj):
    """(未割当数, 合計が1.0でない数, 最悪の合計) を返す。"""
    groups = {index: group.name for index, group in enumerate(mesh_obj.vertex_groups)}
    unassigned = 0
    deviating = 0
    worst = None
    for vertex in mesh_obj.data.vertices:
        entries = [(groups.get(g.group), g.weight) for g in vertex.groups
                   if g.weight > 0.0]
        if not entries:
            unassigned += 1
            continue
        total = sum(weight for _name, weight in entries)
        if abs(total - 1.0) > WEIGHT_EPS:
            deviating += 1
            if worst is None or abs(total - 1.0) > abs(worst - 1.0):
                worst = total
    return unassigned, deviating, worst


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    report_path = None
    if "--report" in argv:
        index = argv.index("--report")
        if index + 1 < len(argv):
            report_path = argv[index + 1]

    checker = Checker()
    blend_path = bpy.data.filepath
    settings = presets_mod.resolve_settings(PRESET)

    # ---- 1. 収集 --------------------------------------------------------
    selected = sorted(o.name for o in bpy.context.selected_objects)
    print("[結合テスト] 保存済みの選択状態: {0!r}".format(selected))

    spec = pipeline.collect_spec(None, None, None, "SkeletalMesh")
    pipeline.populate_skeletal(spec, settings)

    checker.check(bool(spec.main_object_names),
                  "書き出し対象のメッシュを拾えていない"
                  "（Armature だけ選択されている場合の補完が効いていない / P-12）")
    checker.check(bool(spec.armature_name), "Armature を特定できていない")
    checker.check(bool(spec.bones), "ボーンを収集できていない")
    checker.check(spec.skin is not None, "スキン統計を収集できていない")

    source_hierarchy = [(b.parent, b.name) for b in spec.bones]
    source_roots = spec.root_bone_names
    checker.equal(len(source_roots), 1, "ルートボーンが1本ではない")

    original_names = sorted(o.name for o in bpy.data.objects)
    original_armature = bpy.data.objects.get(spec.armature_name)
    source_bone_count = len(spec.bones)
    source_leaf_names = sorted(b.name for b in spec.bones if b.is_leaf)
    source_deform = sorted(b.name for b in spec.bones if b.use_deform)
    print("[結合テスト] ボーン {0} 本 / ルート {1!r} / 末端 {2!r}".format(
        source_bone_count, source_roots, source_leaf_names))
    if spec.skin is not None:
        print("[結合テスト] スキン: {0}".format(spec.skin.to_manifest()))

    # ---- 2. 書き出し ----------------------------------------------------
    # **samples/ を汚さない**ため一時フォルダへ書き出す。
    # サンプル配下に書くと、規約違反のあるサンプルから作られた
    # 不正なマニフェストが残り、スキーマ検証が落ちる
    work_dir = tempfile.mkdtemp(prefix="onepochi_skeletal_test_")
    result = pipeline.export_asset(
        preset=PRESET, asset_type="SkeletalMesh", force=True,
        asset_folder=work_dir,
    )
    checker.check(result.ok, "書き出しが成功しなかった: " + result.issues.summary())
    fbx_paths = list(result.fbx_paths)
    checker.check(bool(fbx_paths), "FBX が書き出されていない")

    # ---- 3. 元シーンを汚していないか ------------------------------------
    checker.equal(sorted(o.name for o in bpy.data.objects), original_names,
                  "元シーンのオブジェクト名が変化した")
    leftovers = [o.name for o in bpy.data.objects
                 if o.name.startswith(prepare.ORIGINAL_PREFIX)]
    checker.equal(leftovers, [], "退避名のオブジェクトが残っている")
    checker.check(bpy.data.collections.get(prepare.WORK_COLLECTION_NAME) is None,
                  "作業用コレクション {0} が残っている".format(
                      prepare.WORK_COLLECTION_NAME))

    if original_armature is not None:
        checker.equal(len(original_armature.data.bones), source_bone_count,
                      "元 Armature のボーン数が変化した")
        checker.equal(
            tuple(round(v, 6) for v in original_armature.scale), (1.0, 1.0, 1.0),
            "元 Armature のスケールが変化した")

    # 元メッシュのウェイトは**変更されていてはいけない**（正規化は複製側で行う / D-4）
    for name in spec.main_object_names:
        mesh_obj = bpy.data.objects.get(name)
        if mesh_obj is None:
            continue
        _unassigned, deviating, _worst = _weight_summary(mesh_obj)
        if spec.skin is not None and spec.skin.deviating_count:
            checker.check(
                deviating > 0,
                "元メッシュのウェイトが正規化されてしまっている"
                "（整形は複製側で行う方針 D-4 に違反 / 対象 {0}）".format(name))

    # ---- 4. FBX を読み戻して確認 ---------------------------------------
    fbx_findings = {}
    if fbx_paths:
        fbx_path = fbx_paths[0]
        if not checker.check(_import_fbx(fbx_path), "FBX を読み戻せなかった"):
            pass
        else:
            armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
            meshes = [o for o in bpy.data.objects if o.type == "MESH"]
            checker.equal(len(armatures), 1,
                          "FBX 内の Armature が1個ではない"
                          "（退避した元が混入している可能性）")
            checker.check(bool(meshes), "FBX にメッシュが入っていない")

            if armatures:
                imported = armatures[0]
                imported_hierarchy = _hierarchy(imported)
                checker.equal(imported_hierarchy, source_hierarchy,
                              "ボーン階層が往復で一致しない")

                roots = [b.name for b in imported.data.bones if b.parent is None]
                checker.equal(roots, source_roots, "ルートボーンが一致しない")
                for bone in imported.data.bones:
                    if bone.parent is None:
                        checker.check(
                            all(abs(v) <= ORIGIN_EPS for v in bone.head_local),
                            "ルートボーン {0} が原点にない（{1}）".format(
                                bone.name,
                                [round(v, 6) for v in bone.head_local]))

                # 末端以外の長さは往復で保存される（末端は P-11 により検証しない）
                source_lengths = {b.name: b.length_cm for b in spec.bones
                                  if not b.is_leaf}
                for bone in imported.data.bones:
                    if bone.name not in source_lengths:
                        continue
                    expected = source_lengths[bone.name]
                    actual = round(bone.length * 100.0, 6)
                    checker.check(
                        abs(actual - expected) < 0.1,
                        "ボーン {0} の長さが一致しない（期待 {1} cm / 実際 {2} cm）".format(
                            bone.name, expected, actual))

                fbx_findings["boneHierarchy"] = imported_hierarchy
                fbx_findings["roots"] = roots

            for mesh_obj in meshes:
                modifier = skeletal.armature_modifier(mesh_obj)
                checker.check(modifier is not None,
                              "{0} に Armature モジファイアが無い"
                              "（スキンが焼き込まれた可能性）".format(mesh_obj.name))
                if modifier is not None and armatures:
                    checker.equal(
                        getattr(modifier.object, "name", None),
                        armatures[0].name,
                        "{0} の Armature モジファイアの参照先が違う".format(mesh_obj.name))

                unassigned, deviating, worst = _weight_summary(mesh_obj)
                checker.equal(unassigned, 0,
                              "{0} に未割当の頂点がある".format(mesh_obj.name))
                checker.equal(
                    deviating, 0,
                    "{0} のウェイトが正規化されていない"
                    "（V-069 の自動修正が効いていない / 最悪の合計 {1}）".format(
                        mesh_obj.name, worst))

                groups = sorted(g.name for g in mesh_obj.vertex_groups)
                for name in groups:
                    checker.check(
                        name in source_deform or name in [b.name for b in spec.bones],
                        "{0} に元に無い頂点グループ {1!r} がある".format(
                            mesh_obj.name, name))
                fbx_findings.setdefault("meshes", []).append({
                    "name": mesh_obj.name,
                    "vertexCount": len(mesh_obj.data.vertices),
                    "vertexGroups": groups,
                    "unassigned": unassigned,
                    "deviating": deviating,
                })

    report = {
        "blendFile": blend_path,
        "blenderVersion": bpy.app.version_string,
        "preset": PRESET,
        "savedSelection": selected,
        "sourceBoneCount": source_bone_count,
        "sourceRoots": source_roots,
        "sourceLeafBones": source_leaf_names,
        "sourceSkin": spec.skin.to_manifest() if spec.skin else None,
        "fbxPaths": fbx_paths,
        "fbx": fbx_findings,
        "checks": checker.checks,
        "failures": checker.failures,
        "ok": not checker.failures,
    }

    print("\n" + "=" * 68)
    print("[スケルタル結合テスト] {0}".format(
        os.path.basename(blend_path) or "(未保存)"))
    print("  チェック {0} 件 / 失敗 {1} 件".format(checker.checks,
                                                   len(checker.failures)))
    for failure in checker.failures:
        print("  ✖ {0}".format(failure))
    if not checker.failures:
        print("  ✔ すべて成功")
    print("=" * 68)

    shutil.rmtree(work_dir, ignore_errors=True)

    if report_path:
        directory = os.path.dirname(os.path.abspath(report_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("[スケルタル結合テスト] レポート: {0}".format(report_path))

    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
