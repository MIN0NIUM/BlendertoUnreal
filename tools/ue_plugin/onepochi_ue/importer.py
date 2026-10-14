"""インポートのコア（UE 側 [C2] [C3] [C5]）。

**設計方針 D-1**: この関数は「マニフェストのパスを渡すと取り込む」純粋な処理。
起動方法（エディタのボタン / Remote Control API / コマンドライン）を一切知らない。

    import_from_manifest(manifest_path) -> report(dict)
"""

import datetime
import json
import os
import time

import unreal

from . import (lod, manifest_io, materials, postprocess, reimport, skeletal,
               skeleton_compat, textures)

TOOL_VERSION = "0.1.0"


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _log(message):
    unreal.log("[OnePochi] {0}".format(message))


def _log_error(message):
    unreal.log_error("[OnePochi] {0}".format(message))


def engine_version():
    try:
        return unreal.SystemLibrary.get_engine_version()
    except Exception:
        return "unknown"


def asset_exists(package_path):
    try:
        return unreal.EditorAssetLibrary.does_asset_exist(package_path)
    except Exception:
        return False


def list_assets(directory):
    try:
        if not unreal.EditorAssetLibrary.does_directory_exist(directory):
            return set()
        return set(unreal.EditorAssetLibrary.list_assets(directory, recursive=False,
                                                         include_folder=False))
    except Exception:
        return set()


def load_asset(package_path):
    try:
        return unreal.EditorAssetLibrary.load_asset(package_path)
    except Exception:
        return None


def _normalize_object_path(path):
    """オブジェクト参照からパッケージパスを取り出す。

    受け付ける形:
        '/Game/X/SM_A'
        '/Game/X/SM_A.SM_A'
        "/Script/Engine.StaticMesh'/Game/X/SM_A.SM_A'"   ← Interchange の戻り値
    """
    text = str(path)
    # クラス付き参照 Class'ObjectPath' の形なら引用符の中身を取る
    if "'" in text:
        start = text.find("'")
        end = text.rfind("'")
        if end > start:
            text = text[start + 1:end]
    if "." in text:
        head, _, _tail = text.rpartition(".")
        return head
    return text


def scan_assets(directory):
    """アセットレジストリに未登録の新規アセットを拾うため、強制スキャンする。"""
    try:
        registry = unreal.AssetRegistryHelpers.get_asset_registry()
        registry.scan_paths_synchronous([directory], True)
    except Exception:
        pass
    return list_assets(directory)


def _candidate_paths(destination_path, destination_name, name_hints):
    """アセットが生成されうるパッケージパスの候補を優先順に返す。"""
    names = [destination_name]
    for hint in (name_hints or []):
        if hint and hint not in names:
            names.append(hint)
    return ["{0}/{1}".format(destination_path.rstrip("/"), n) for n in names]


def discover_imported(destination_path, destination_name, name_hints, before,
                      task, sink, attempts=12, delay=0.25):
    """生成されたアセットを多重の手段で探す。

    **UE 5.8 実測の事情**:
      ・Interchange 経由だと `AssetImportTask.imported_object_paths` が空のまま返る
      ・保存前のパッケージはアセットレジストリに載らないので `list_assets` 差分でも拾えない
      ・インポートが非同期に完走するため、直後に探すと見つからないことがある
    そのため「複数手段 × リトライ」で探す。見つかった手段をログに残す。
    """
    diagnostics = []

    for attempt in range(1, attempts + 1):
        found = []
        used = None

        # 手段1: task.get_objects() — 生成された UObject を直接受け取る
        if task is not None and hasattr(task, "get_objects"):
            objects = postprocess.attempt(sink, "task.get_objects", task.get_objects) or []
            for obj in objects:
                try:
                    found.append(_normalize_object_path(obj.get_path_name()))
                except Exception:
                    continue
            if found:
                used = "task.get_objects()"

        # 手段2: imported_object_paths
        if not found and task is not None:
            paths = postprocess.get_property(sink, task, "imported_object_paths", None) or []
            found = [_normalize_object_path(p) for p in paths]
            if found:
                used = "imported_object_paths"

        # 手段3: 期待されるパスを直接ロード（保存前のパッケージでも見つかる）
        if not found:
            for package_path in _candidate_paths(destination_path, destination_name,
                                                 name_hints):
                if load_asset(package_path) is not None:
                    found.append(package_path)
            if found:
                used = "期待パスの直接ロード"

        # 手段4: アセットレジストリを強制スキャンして差分を取る
        if not found:
            after = scan_assets(destination_path)
            found = [_normalize_object_path(p) for p in sorted(after - before)]
            if found:
                used = "レジストリ強制スキャンの差分"

        if found:
            diagnostics.append("{0}回目の探索で『{1}』により特定".format(attempt, used))
            sink.extend(diagnostics)
            deduped = []
            for path in found:
                if path not in deduped:
                    deduped.append(path)
            return deduped

        if attempt < attempts:
            time.sleep(delay)

    diagnostics.append(
        "{0}回試行しても生成されたアセットを特定できませんでした"
        "（探した候補: {1}）".format(
            attempts,
            _candidate_paths(destination_path, destination_name, name_hints))
    )
    sink.extend(diagnostics)
    return []


def import_via_interchange(fbx_path, destination_path, replace_existing, sink,
                           override_pipelines=None):
    """`InterchangeManager.import_asset`（**同期**）でインポートする。

    `AssetImportTask` 経由は Interchange が非同期に走るため、直後に結果を
    取得できない。こちらは同期 API なので完走が保証される。

    戻り値: 成功したかどうか（None = この経路が使えない）
    """
    if not hasattr(unreal, "InterchangeManager"):
        sink.append("unreal.InterchangeManager が存在しないため AssetImportTask 経路を使います")
        return None

    manager = None
    for getter in ("get_interchange_manager_scripted",):
        if hasattr(unreal.InterchangeManager, getter):
            manager = postprocess.attempt(sink, getter,
                                          getattr(unreal.InterchangeManager, getter))
            if manager is not None:
                break
    if manager is None:
        return None

    source_data = None
    if hasattr(unreal.InterchangeManager, "create_source_data"):
        source_data = postprocess.attempt(sink, "create_source_data",
                                          unreal.InterchangeManager.create_source_data,
                                          fbx_path)
    if source_data is None:
        sink.append("InterchangeSourceData を作れなかったため AssetImportTask 経路を使います")
        return None

    parameters = postprocess.attempt(sink, "ImportAssetParameters",
                                     unreal.ImportAssetParameters) \
        if hasattr(unreal, "ImportAssetParameters") else None
    if parameters is None:
        sink.append("ImportAssetParameters を作れなかったため AssetImportTask 経路を使います")
        return None

    # 存在するプロパティだけ設定する（UE バージョンで揺れるため）
    for name, value in (("is_automated", True),
                        ("replace_existing", bool(replace_existing)),
                        ("follow_redirectors", True)):
        try:
            parameters.set_editor_property(name, value)
        except Exception:
            pass

    # Skeletal Mesh では専用パイプラインを差し込む（P3）。
    # **SoftObjectPath の配列**で渡す（オブジェクトの配列ではない）
    if override_pipelines:
        applied = skeletal.set_any(
            sink, parameters,
            ("override_pipelines", "OverridePipelines"), override_pipelines,
            "override_pipelines")
        if applied is None:
            sink.append("override_pipelines を設定できませんでした。"
                        "既定パイプラインで取り込みます（Skeletal にならない"
                        "可能性があります）")
        else:
            sink.append("パイプラインを差し替えました: {0}".format(override_pipelines))

    result = postprocess.attempt(sink, "InterchangeManager.import_asset",
                                 manager.import_asset, destination_path, source_data,
                                 parameters)
    if result is None:
        return None

    # UE 5.8 実測: 戻り値は bool ではなく
    # ["/Script/Engine.StaticMesh'/Game/.../SM_A.SM_A'", ...] の**配列**。
    # ただし型は Python の list ではなく `unreal.Array` なので
    # isinstance(result, list) では判定できない。反復できるかで判断する。
    paths = None
    if not isinstance(result, bool):
        try:
            paths = [_normalize_object_path(entry) for entry in result]
        except TypeError:
            paths = None

    if paths is not None:
        sink.append("InterchangeManager.import_asset（同期）が {0} 件返しました: {1}".format(
            len(paths), paths))
        return paths

    sink.append("InterchangeManager.import_asset（同期）の戻り値: {0!r}".format(result))
    return [] if not result else True


def run_import_task(fbx_path, destination_path, destination_name, replace_existing,
                    uniform_scale, sink, name_hints=None, override_pipelines=None):
    """AssetImportTask で1ファイルをインポートする。

    options は None にして **Interchange の既定パイプライン**に任せる。
    細かい設定は意図的にポストプロセス側に寄せている（方針 D-6）。

    **重要（UE 5.8 実測）**: Interchange 経由だと `AssetImportTask.imported_object_paths`
    が**空のまま**返る。アセット自体は生成されている（ログに
    `Built static mesh /Game/.../SM_Crate_01` が出る）。
    さらに保存前のパッケージはアセットレジストリに載らないため
    `list_assets()` の差分でも拾えない。
    そこで取得手段を4段構えにしている。

    戻り値: インポートされたアセットのパッケージパスのリスト
    """
    before = list_assets(destination_path)

    # 経路1: InterchangeManager.import_asset（同期）
    interchange_result = import_via_interchange(fbx_path, destination_path,
                                                replace_existing, sink,
                                                override_pipelines)

    # 戻り値がパスのリストなら、それが最も確実な結果
    if isinstance(interchange_result, (list, tuple)) and interchange_result:
        deduped = []
        for path in interchange_result:
            if path not in deduped:
                deduped.append(path)
        return deduped

    if interchange_result is True:
        # 成功したが結果リストが得られなかった場合は探索に回す
        return discover_imported(destination_path, destination_name, name_hints,
                                 before, None, sink)

    if interchange_result is not None:
        sink.append("InterchangeManager.import_asset が結果を返さなかったため"
                    "AssetImportTask 経路を試します")

    # 経路2: AssetImportTask（Interchange が非同期に走る可能性があるためリトライ付きで探す）
    task = unreal.AssetImportTask()
    for name, value in (
        ("filename", fbx_path),
        ("destination_path", destination_path),
        ("destination_name", destination_name),
        ("automated", True),
        ("replace_existing", bool(replace_existing)),
        ("replace_existing_settings", False),
        ("save", False),
        ("options", None),
    ):
        postprocess.set_property(sink, task, name, value)

    tools = postprocess.attempt(sink, "get_asset_tools",
                                unreal.AssetToolsHelpers.get_asset_tools)
    if tools is None:
        return []

    if postprocess.attempt(sink, "import_asset_tasks",
                           tools.import_asset_tasks, [task]) is None:
        return []

    return discover_imported(destination_path, destination_name, name_hints,
                             before, task, sink)


def pick_primary_asset(package_paths, desired_name, expected_class="StaticMesh"):
    """インポート結果から主アセットを選ぶ。

    コリジョンメッシュなどが別アセットとして入ってしまった場合も検出したいので、
    (主アセット, その他) に分けて返す。
    """
    primary = None
    others = []
    for path in package_paths:
        asset = load_asset(path)
        if asset is None:
            others.append({"path": path, "class": None})
            continue
        class_name = type(asset).__name__
        entry = {"path": path, "class": class_name}
        name = path.rsplit("/", 1)[-1]
        if primary is None and class_name == expected_class and name == desired_name:
            primary = (path, asset)
        else:
            others.append(entry)

    if primary is None:
        # 名前が一致しなくても、期待クラスの最初のものを主アセットにする
        for index, entry in enumerate(list(others)):
            if entry["class"] == expected_class:
                asset = load_asset(entry["path"])
                primary = (entry["path"], asset)
                others.pop(index)
                break
    return primary, others


def rename_if_needed(package_path, desired_name, sink):
    """アセット名が期待と違う場合はリネームする。戻り値は最終パス。"""
    current_name = package_path.rsplit("/", 1)[-1]
    if current_name == desired_name:
        return package_path
    directory = package_path.rsplit("/", 1)[0]
    target = directory + "/" + desired_name
    if asset_exists(target):
        sink.append("リネーム先に既存アセットがあるためリネームを行いません: {0}".format(target))
        return package_path
    ok = postprocess.attempt(sink, "rename_asset",
                             unreal.EditorAssetLibrary.rename_asset,
                             package_path, target)
    if ok:
        sink.append("アセット名を '{0}' から '{1}' にリネームしました".format(
            current_name, desired_name))
        return target
    return package_path


def import_static_mesh_asset(manifest_path, manifest, asset, uniform_scale, dry_run=False):
    """StaticMesh 1件をインポートしてポストプロセスする。"""
    entry = {
        "id": asset.get("id"),
        "sourceName": asset.get("sourceNode"),
        "assetType": "StaticMesh",
        "requestedName": asset.get("name"),
        "requestedPath": asset.get("destinationPath"),
        "action": "skipped",
        "assetPath": None,
        "warnings": [],
        "errors": [],
    }
    sink = entry["warnings"]

    try:
        fbx_path = manifest_io.resolve_source_file(manifest_path, asset)
        destination = manifest_io.check_destination(manifest, asset)
    except manifest_io.ManifestError as error:
        entry["errors"].append(str(error))
        entry["action"] = "failed"
        return entry

    entry["sourceFile"] = fbx_path
    desired_name = asset.get("name")
    target_path = "{0}/{1}".format(destination.rstrip("/"), desired_name)
    existed = asset_exists(target_path)
    entry["existedBefore"] = existed

    allow_overwrite = bool((manifest.get("destination") or {}).get("allowOverwrite", True))
    if existed and not allow_overwrite:
        entry["errors"].append(
            "既存アセットがあり、destination.allowOverwrite が false です: {0}".format(target_path)
        )
        entry["action"] = "failed"
        return entry

    # --- 再インポート時の設定保持（T-7）------------------------------------
    # 既存アセットがある場合は、**インポート前に**今の設定を記録しておく。
    # 「どの項目をマニフェストで上書きし、どの項目を UE 側のまま残すか」は
    # reimportPolicy が決める（アセット側 > マニフェスト側 > 既定）
    policy = reimport.resolve_policy(manifest, asset, sink)
    plan = reimport.plan_categories(policy, existed)
    entry["reimport"] = {
        "policy": policy,
        "apply": plan["apply"],
        "preserve": plan["preserve"],
        "snapshot": {},
        "restore": None,
    }
    if plan["preserve"]:
        # `comparison` はあくまで**マニフェストとの**突き合わせなので、
        # 保持したカテゴリが不一致になるのは正常。読み手が誤解しないようにする
        entry["reimport"]["note"] = (
            "保持したカテゴリ（{0}）は UE 側の値を残しているため、"
            "comparison がマニフェストと不一致になることがあります".format(
                ", ".join(plan["preserve"])))

    if dry_run:
        entry["action"] = "dryRun"
        return entry

    if existed:
        existing_mesh = load_asset(target_path)
        if existing_mesh is None:
            sink.append("既存アセットを読めなかったため設定のスナップショットを"
                        "取れません: {0}".format(target_path))
        else:
            entry["reimport"]["snapshot"] = reimport.capture(existing_mesh, sink)

    _log("インポート開始: {0} -> {1}".format(os.path.basename(fbx_path), target_path))
    # Interchange が FBX のノード名でアセットを作ることがあるため候補を渡す
    hints = [asset.get("sourceNode"), asset.get("source_node"),
             os.path.splitext(os.path.basename(fbx_path))[0]]
    imported = run_import_task(fbx_path, destination, desired_name,
                               replace_existing=True, uniform_scale=uniform_scale,
                               sink=sink, name_hints=hints)
    entry["importedObjectPaths"] = imported

    if not imported:
        entry["errors"].append(
            "インポート結果のアセットが見つかりません。"
            "FBX が壊れているか、Interchange の設定を確認してください: {0}".format(fbx_path)
        )
        entry["action"] = "failed"
        return entry

    primary, others = pick_primary_asset(imported, desired_name, "StaticMesh")
    entry["additionalAssets"] = others
    if others:
        meshes = [o for o in others if o.get("class") in ("StaticMesh", "SkeletalMesh")]
        if meshes:
            sink.append(
                "主アセット以外にメッシュアセットが {0} 件生成されました。"
                "コリジョンが別アセットになっていないか確認してください: {1}".format(
                    len(meshes), [o["path"] for o in meshes])
            )

        # Interchange は FBX のマテリアルから MaterialInstanceConstant を自動生成する。
        # その名前は Blender のマテリアル名（M_xxx）のままなので、
        # 命名規約（Material Instance は MI_ プレフィックス / docs/04 §1）に反する。
        for other in others:
            if other.get("class") != "MaterialInstanceConstant":
                continue
            name = other["path"].rsplit("/", 1)[-1]
            if not name.startswith("MI_"):
                sink.append(
                    "インポータが Material Instance '{0}' を自動生成しました。"
                    "命名規約では MI_ プレフィックスが必要です。"
                    "規約に沿った MI を使うには material.mode を createInstances に"
                    "してください".format(name)
                )

    if primary is None:
        entry["errors"].append(
            "StaticMesh アセットが生成されませんでした（生成物: {0}）".format(imported)
        )
        entry["action"] = "failed"
        return entry

    package_path, mesh = primary
    package_path = rename_if_needed(package_path, desired_name, sink)
    mesh = load_asset(package_path) or mesh
    entry["assetPath"] = package_path
    entry["action"] = "reimported" if existed else "created"

    entry["applied"] = postprocess.apply_static_mesh(mesh, asset, sink,
                                                     categories=plan["apply"])

    # LOD は「LOD ごとに別 FBX を読み込む」ため postprocess とは別モジュール（T-5）。
    # `reimportPolicy` で preserve 側に回ったときは**適用しない**
    # （UE 側で手で足した LOD を壊さないため）
    # **`mode = "manual"` の LOD は「設定」ではなく「データ」**なので、
    # `preserveSettings` でも適用する。アーティストが Blender で LOD1 を
    # 作り直して再インポートしたのに**何も起きない**のは事故に見えるため。
    # `mode = "auto"`（UE 側で生成するもの）は設定として扱い、保持を尊重する。
    declared_lod_mode, _ = lod.normalize_mode(
        (asset.get("staticMesh") or {}).get("lod") or {})
    lod_is_source_data = declared_lod_mode == "manual"
    if "lod" in plan["apply"] or lod_is_source_data:
        entry["lod"] = lod.apply_lod(mesh, asset, manifest_path, sink)
        if lod_is_source_data and "lod" not in plan["apply"]:
            note = ('staticMesh.lod.mode = "manual" のため、reimportPolicy が'
                    "保持側でも LOD を適用しました（LOD ごとの FBX は DCC 側が"
                    "持つデータで、設定ではありません）。UE 側の LOD を守りたい場合は"
                    'マニフェストで mode を "none" にしてください')
            sink.append(note)
            entry["lod"]["appliedDespitePreserve"] = note
        for message in entry["lod"].get("errors") or []:
            entry["errors"].append(message)
    else:
        sink.append("reimportPolicy により LOD の適用をスキップしました"
                    "（UE 側の LOD 構成を保持します）")
        entry["lod"] = {"skippedByReimportPolicy": True,
                        "declaredMode": declared_lod_mode}

    material_policy = manifest.get("material") or {}
    if material_policy.get("mode", "none") != "none":
        if "materials" in plan["apply"]:
            entry["material"] = materials.apply_material_slots(
                mesh, asset, material_policy, sink,
                manifest=manifest, manifest_path=manifest_path)
        else:
            sink.append("reimportPolicy により material の適用をスキップしました"
                        "（UE 側のマテリアル割当を保持します）")
            entry["material"] = {"mode": material_policy.get("mode"),
                                 "skippedByReimportPolicy": True}

    # **復元はポストプロセスの後**（マニフェストで上書きした分より後に戻す）。
    # 実測はさらにその後に行い、**最終状態**を記録する
    entry["reimport"]["restore"] = reimport.restore(
        mesh, entry["reimport"]["snapshot"], plan["preserve"], sink)

    expected_sockets = [s.get("name") for s in
                        ((asset.get("staticMesh") or {}).get("sockets") or [])]
    entry["measured"] = postprocess.measure_static_mesh(mesh, sink, expected_sockets)
    # LOD の実測は `StaticMesh` 自身のメソッドで取る（commandlet でも動く / P-5）。
    # 新しい要素は必ず `comparison` に項目を作る（K-8: 存在確認だけで合格にしない）
    entry["measured"].update(lod.measure_lod(mesh, sink))
    entry["comparison"] = postprocess.compare_with_manifest(asset, entry["measured"])
    entry["comparison"].update(lod.compare_lod(asset, entry["measured"]))
    # テクスチャは宣言しているアセットでだけ項目が増える（P5 の DoD / K-8）
    entry["comparison"].update(textures.compare_textures(
        asset, entry.get("material"), material_policy))

    # 主アセットだけでなく、インポータが一緒に生成したアセット
    # （Interchange は FBX のマテリアルから MaterialInstanceConstant を自動生成する）
    # も保存する。保存しないとメッシュ側の参照が切れる。
    # インポータが一緒に生成したアセットだけでなく、
    # **ツール自身が作った MI** も保存する（`InterchangeManager.import_asset` の
    # 戻り値には含まれないため、保存しないと参照が切れる / P-10）
    created_materials = list((entry.get("material") or {}).get("created") or [])
    # `material["textures"]` は**スロットごとのリスト**（materials.apply_material_slots）。
    # 各要素の `imported` が `import_slot_textures` の記録で、その `paths` に
    # 生成されたテクスチャのパッケージパスが入る。
    # **以前ここを dict として扱っていて、テクスチャを1枚でも取り込むと
    # `AttributeError` で落ちていた**（2026-10-14 実測）
    created_textures = []
    for slot_record in ((entry.get("material") or {}).get("textures") or []):
        if not isinstance(slot_record, dict):
            continue
        for path in ((slot_record.get("imported") or {}).get("paths") or []):
            if path and path not in created_textures:
                created_textures.append(path)
    entry["saved"] = save_all(
        imported + [package_path] + created_materials + created_textures, sink)
    return entry


def check_skeleton_compatibility(asset, sink):
    """取り込み前に既存 Skeleton との互換を確認する（Q-09）。

    戻り値: (ロード済み USkeleton or None, 判定レポート or None)

    `skeleton.mode` が `create_new` のときは何もしない。
    `reuse` のときは `assetPath` の Skeleton を読み、ボーン階層を
    突き合わせて**互換でなければ取り込みを止める**。
    """
    skeleton_block = asset.get("skeleton") or {}
    if skeleton_block.get("mode") != "reuse":
        return None, None

    path = skeleton_block.get("assetPath")
    if not path:
        return None, {"compatible": False,
                      "reasons": ["skeleton.mode が 'reuse' ですが "
                                  "skeleton.assetPath がありません"]}

    existing = load_asset(path)
    if existing is None:
        return None, {"compatible": False,
                      "reasons": ["既存 Skeleton が見つかりません: {0}".format(path)]}

    incoming = skeleton_compat.hierarchy_from_manifest(skeleton_block)
    mode = skeleton_block.get("compatibilityMode", "exact")
    hierarchy = skeletal.read_skeleton_hierarchy(existing, sink)

    if hierarchy["pairs"]:
        verdict = skeleton_compat.compare(incoming, hierarchy["pairs"], mode)
        verdict["method"] = hierarchy["method"]
    else:
        verdict = skeleton_compat.compare_names_only(
            incoming, hierarchy["names"], mode)
    verdict["existingSkeletonPath"] = path
    for note in hierarchy["notes"]:
        verdict.setdefault("notes", []).append(note)
    return existing, verdict


def compare_skeletal_with_manifest(asset, measured):
    """DCC 側の予測値と UE 側の実測値を突き合わせる（P1 と同じ方針）。

    **末端ボーンの長さは比較しない。**FBX は関節の位置しか持たないため
    往復で保存されない（docs/99 P-11）。
    """
    skeleton_block = asset.get("skeleton") or {}
    skeletal_block = asset.get("skeletalMesh") or {}
    stats = asset.get("stats") or {}
    comparison = {}

    expected_names = list(skeleton_block.get("boneNames") or [])
    actual_names = [str(n) for n in (measured.get("boneNames") or [])]
    if expected_names and actual_names:
        comparison["boneNames"] = {
            "expected": expected_names,
            "actual": actual_names,
            "matches": expected_names == actual_names,
        }
    expected_count = skeleton_block.get("boneCount")
    actual_count = measured.get("bones")
    if expected_count is not None and actual_count is not None:
        comparison["boneCount"] = {
            "expected": expected_count,
            "actual": actual_count,
            "matches": int(expected_count) == int(actual_count),
        }

    expected_pairs = skeleton_compat.normalize_pairs(
        skeleton_block.get("boneHierarchy"))
    actual_pairs = measured.get("boneHierarchy")
    if expected_pairs and actual_pairs:
        normalized = skeleton_compat.normalize_pairs(actual_pairs)
        comparison["boneHierarchy"] = {
            "matches": expected_pairs == normalized,
            "expectedHash": skeleton_compat.bone_hierarchy_hash(expected_pairs),
            "actualHash": skeleton_compat.bone_hierarchy_hash(normalized),
            "note": "親子関係まで比較できた（SkeletonModifier 経由）",
        }
    elif expected_pairs:
        comparison["boneHierarchy"] = {
            "matches": None,
            "note": "UE 側で親子関係を読めなかったため比較できません"
                    "（ボーン名の並びで代用 / 要検証）",
        }

    expected_slots = [s.get("slotName") for s in (asset.get("materialSlots") or [])]
    actual_slots = [s.get("importedSlotName") or s.get("slotName")
                    for s in (measured.get("materialSlots") or [])]
    if expected_slots:
        comparison["materialSlots"] = {
            "expected": expected_slots,
            "actual": actual_slots,
            "matches": expected_slots == actual_slots,
        }

    expected_morphs = list((skeletal_block.get("morphTargets") or {}).get("names") or [])
    actual_morphs = [str(n) for n in (measured.get("morphTargetNames") or [])]
    # **両方とも空でも項目を出す。**「比較できなかった」と
    # 「比較して一致した（どちらも 0 件）」は意味が違う。
    # 省略すると往復テストが前者と区別できない
    comparison["morphTargets"] = {
        "expected": sorted(expected_morphs),
        "actual": sorted(actual_morphs),
        "matches": sorted(expected_morphs) == sorted(actual_morphs),
    }
    if not expected_morphs and not actual_morphs:
        comparison["morphTargets"]["note"] = \
            "DCC 側にも UE 側にもモーフターゲットはありません"

    expected_sockets = len(skeletal_block.get("sockets") or [])
    actual_sockets = measured.get("socketCount")
    if actual_sockets is not None:
        comparison["socketCount"] = {
            "expected": expected_sockets,
            "actual": int(actual_sockets),
            "matches": expected_sockets == int(actual_sockets),
        }

    # 以下は**要検証**。スキンメッシュの非同期ビルドが終わる前だと 0 が返るため、
    # 現時点では「記録するが一致を要求しない」扱いにする
    expected_tris = stats.get("triangleCount")
    actual_tris = measured.get("triangles")
    if expected_tris is not None and actual_tris:
        comparison["triangleCount"] = {
            "expected": expected_tris,
            "actual": actual_tris,
            "matches": int(expected_tris) == int(actual_tris),
            "note": "要検証: 非同期ビルド中は 0 が返ることがある",
        }
    expected_verts = stats.get("vertexCount")
    actual_verts = measured.get("vertices")
    if expected_verts is not None and actual_verts:
        comparison["vertexCount"] = {
            "expected": expected_verts,
            "actual": actual_verts,
            "matches": int(expected_verts) == int(actual_verts),
            "note": "要検証: FBX の分割で頂点数は増えることがある",
        }

    return comparison


def import_skeletal_mesh_asset(manifest_path, manifest, asset, uniform_scale,
                               dry_run=False):
    """SkeletalMesh 1件をインポートして実測する（P3）。

    **未実機検証**: Interchange のパイプライン設定名はエンジンのヘッダからの
    導出。`entry["pipeline"]` の `applied` / `failed` を必ず確認すること。

    **再インポート時の設定保持（T-7 / `reimportPolicy`）は未対応。**
    `onepochi_ue.reimport` は現時点で **StaticMesh のみ**を扱う
    （`capture` / `restore` が `StaticMesh` のプロパティ名を前提にしている）。
    そのため SkeletalMesh を再インポートすると、UE 側で手修正した設定は
    **失われる可能性がある**。`reimportPolicy` が宣言されていても無視するので、
    そのことを warning としてレポートに残す。
    """
    entry = {
        "id": asset.get("id"),
        "sourceName": asset.get("sourceNode"),
        "assetType": "SkeletalMesh",
        "requestedName": asset.get("name"),
        "requestedPath": asset.get("destinationPath"),
        "action": "skipped",
        "assetPath": None,
        "warnings": [],
        "errors": [],
    }
    sink = entry["warnings"]

    try:
        fbx_path = manifest_io.resolve_source_file(manifest_path, asset)
        destination = manifest_io.check_destination(manifest, asset)
    except manifest_io.ManifestError as error:
        entry["errors"].append(str(error))
        entry["action"] = "failed"
        return entry

    entry["sourceFile"] = fbx_path
    desired_name = asset.get("name")
    target_path = "{0}/{1}".format(destination.rstrip("/"), desired_name)
    existed = asset_exists(target_path)
    entry["existedBefore"] = existed

    allow_overwrite = bool((manifest.get("destination") or {}).get("allowOverwrite", True))
    if existed and not allow_overwrite:
        entry["errors"].append(
            "既存アセットがあり、destination.allowOverwrite が false です: {0}".format(
                target_path))
        entry["action"] = "failed"
        return entry

    # --- 再インポート保持は未対応（T-7 は StaticMesh のみ）-----------------
    policy = reimport.resolve_policy(manifest, asset, sink)
    entry["reimport"] = {"policy": policy, "supported": False,
                         "note": "再インポート時の設定保持は StaticMesh のみ対応です"}
    if existed:
        sink.append(
            "再インポート時の設定保持（reimportPolicy）は **StaticMesh のみ対応**です。"
            "SkeletalMesh では UE 側で手修正した設定（マテリアル割当 / ソケット / "
            "LOD など）が失われる可能性があります（mode={0}）".format(policy["mode"]))

    # --- 取り込み前に Skeleton の互換を確認する（Q-09）---------------------
    existing_skeleton, verdict = check_skeleton_compatibility(asset, sink)
    if verdict is not None:
        entry["skeletonCompatibility"] = verdict
        if not verdict.get("compatible"):
            entry["errors"].append(
                "既存 Skeleton と互換がないため取り込みを中止しました: "
                + " / ".join(verdict.get("reasons") or []))
            entry["action"] = "failed"
            return entry
        for reason in verdict.get("reasons") or []:
            sink.append("Skeleton 互換: {0}".format(reason))

    if dry_run:
        entry["action"] = "dryRun"
        return entry

    # --- パイプラインを構成する -------------------------------------------
    pipeline_settings = dict(asset.get("skeletalMesh") or {})
    pipeline_settings["createPhysicsAsset"] = bool(
        (asset.get("physicsAsset") or {}).get("create", False))
    pipeline, pipeline_record = skeletal.build_pipeline(
        pipeline_settings, sink, existing_skeleton)
    entry["pipeline"] = pipeline_record
    override = skeletal.pipeline_override_paths(pipeline, sink)

    _log("インポート開始（Skeletal）: {0} -> {1}".format(
        os.path.basename(fbx_path), target_path))
    hints = [asset.get("sourceNode"),
             os.path.splitext(os.path.basename(fbx_path))[0]]
    imported = run_import_task(fbx_path, destination, desired_name,
                               replace_existing=True, uniform_scale=uniform_scale,
                               sink=sink, name_hints=hints,
                               override_pipelines=override)
    entry["importedObjectPaths"] = imported

    if not imported:
        entry["errors"].append(
            "インポート結果のアセットが見つかりません: {0}".format(fbx_path))
        entry["action"] = "failed"
        return entry

    primary, others = pick_primary_asset(imported, desired_name, "SkeletalMesh")
    entry["additionalAssets"] = others

    if primary is None:
        entry["errors"].append(
            "SkeletalMesh アセットが生成されませんでした（生成物: {0}）。"
            "パイプラインの force_all_mesh_as_type が効いているか "
            "entry['pipeline'] を確認してください".format(imported))
        entry["action"] = "failed"
        return entry

    package_path, mesh = primary
    package_path = rename_if_needed(package_path, desired_name, sink)
    mesh = load_asset(package_path) or mesh
    entry["assetPath"] = package_path
    entry["action"] = "reimported" if existed else "created"

    entry["measured"] = skeletal.measure(mesh, sink)
    entry["comparison"] = compare_skeletal_with_manifest(asset, entry["measured"])

    # Skeleton / PhysicsAsset / MaterialInstance も一緒に保存する。
    # 保存しないとメッシュ側の参照が切れる（docs/99 P-10）
    entry["saved"] = save_all(imported + [package_path], sink)
    return entry


def save_all(package_paths, sink):
    """指定パスのアセットをすべて保存する。戻り値は保存できた数。"""
    saved = 0
    seen = set()
    for package_path in package_paths:
        if not package_path or package_path in seen:
            continue
        seen.add(package_path)
        asset = load_asset(package_path)
        if asset is None:
            continue
        if postprocess.attempt(sink, "save_loaded_asset({0})".format(package_path),
                               unreal.EditorAssetLibrary.save_loaded_asset,
                               asset, False) is not None:
            saved += 1
        else:
            sink.append("アセットを保存できませんでした: {0}".format(package_path))
    return saved


def import_from_manifest(manifest_path, allow_failed=False, dry_run=False,
                         report_path=None):
    """マニフェストを読んでアセットを取り込む。戻り値はレポート辞書。

    この関数は例外を投げない（レポートの errors に入れて返す）。
    """
    started = _now()
    report = {
        "toolVersion": TOOL_VERSION,
        "engineVersion": engine_version(),
        "manifestPath": os.path.abspath(manifest_path),
        "startedAt": started,
        "finishedAt": None,
        "result": "failed",
        "runId": None,
        "warnings": [],
        "errors": [],
        "assets": [],
    }

    try:
        prepared = manifest_io.prepare(manifest_path, allow_failed)
    except manifest_io.ManifestError as error:
        report["errors"].append(str(error))
        report["finishedAt"] = _now()
        _log_error(str(error))
        if report_path:
            write_report(report_path, report)
        return report

    manifest = prepared["manifest"]
    report["runId"] = manifest.get("runId")
    report["schemaVersion"] = manifest.get("schemaVersion")
    report["preset"] = manifest.get("preset")
    report["warnings"].extend(prepared["warnings"])
    report["uniformScale"] = prepared["uniformScale"]

    for warning in prepared["warnings"]:
        _log("警告: {0}".format(warning))

    for asset in manifest.get("assets", []):
        asset_type = asset.get("assetType")
        if asset_type not in manifest_io.IMPLEMENTED_TYPES:
            report["assets"].append({
                "id": asset.get("id"),
                "assetType": asset_type,
                "requestedName": asset.get("name"),
                "action": "skipped",
                "warnings": ["assetType '{0}' は UE 側で未実装です".format(asset_type)],
                "errors": [],
            })
            continue
        if asset_type == "SkeletalMesh":
            entry = import_skeletal_mesh_asset(manifest_path, manifest, asset,
                                               prepared["uniformScale"], dry_run)
        else:
            entry = import_static_mesh_asset(manifest_path, manifest, asset,
                                             prepared["uniformScale"], dry_run)
        report["assets"].append(entry)

    succeeded = [a for a in report["assets"] if a["action"] in ("created", "reimported")]
    failed = [a for a in report["assets"] if a["action"] == "failed"]

    if dry_run:
        report["result"] = "dryRun"
    elif failed and succeeded:
        report["result"] = "partial"
    elif failed:
        report["result"] = "failed"
    elif succeeded:
        report["result"] = "success"
    else:
        report["result"] = "nothingToDo"

    report["finishedAt"] = _now()
    _log("結果: {0}（成功 {1} 件 / 失敗 {2} 件）".format(
        report["result"], len(succeeded), len(failed)))

    if report_path:
        write_report(report_path, report)
    return report


def write_report(report_path, report):
    directory = os.path.dirname(os.path.abspath(report_path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        # **`default` は保険**。UE の enum や構造体を1つ record に入れただけで
        # `TypeError: ... is not JSON serializable` になり、
        # **取り込みは成功しているのにレポートが丸ごと消える**（2026-10-14 実測 / P-33）。
        # 値を正しい形で入れるのは呼び側の責任だが、証拠を失うよりは文字列化する
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    _log("レポート: {0}".format(report_path))
    return report_path
