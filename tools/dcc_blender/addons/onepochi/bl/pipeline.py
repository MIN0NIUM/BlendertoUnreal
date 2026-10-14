"""パイプラインの統合（docs/02_仕様書.md §3）。

第1層「Blender -> アセット元データ」の全体を1関数にまとめる。
UI からもコマンドライン（headless）からも同じ関数を呼ぶ。

    収集 -> 検証 -> (中断判定) -> 複製整形 -> FBX 出力 -> マニフェスト出力
"""

import getpass
import os

import bpy

from ..core import issues as issues_mod
from ..core import manifest as manifest_mod
from ..core import naming
from ..core import presets as presets_mod
from ..core import runs as runs_mod
from ..core import texture_discovery
from . import collect, exporter, prepare, skeletal, validate

DEFAULT_PRESET = "prop"


class ExportResult(object):
    """書き出し結果。UI とログの両方で使う。"""

    def __init__(self):
        self.ok = False
        self.dry_run = False
        self.issues = issues_mod.IssueList()
        self.run_id = None
        self.asset_name = None
        #: 書き出したアセット名の一覧（バッチでは2件以上 / N-5）。
        #: 単一アセット経路でも1件入るので、呼び側はこちらを見れば両方扱える
        self.asset_names = []
        #: バッチ経路（`export_batch()`）で作られた結果かどうか
        self.batch = False
        self.manifest_path = None
        self.manifest = None
        self.fbx_paths = []
        self.settings = None
        self.messages = []
        #: 実行ログの保存先（`%LOCALAPPDATA%/OnePochi/runs/<run_id>/`）。
        #: 保存に失敗したら None（書き出し自体は成功扱い / docs/13 P4-5）
        self.run_dir = None
        #: 保持期間を過ぎて削除した run_id
        self.pruned_runs = []

    def add_message(self, text):
        self.messages.append(text)

    def summary(self):
        lines = []
        head = "成功" if self.ok else ("検証のみ" if self.dry_run else "失敗")
        if self.batch:
            head += "（バッチ {0} 件）".format(len(self.asset_names))
        lines.append("[ワンポチ] {0}: {1}".format(head, self.asset_name or "(未特定)"))
        if self.run_id:
            lines.append("  runId: {0}".format(self.run_id))
        for path in self.fbx_paths:
            lines.append("  FBX: {0}".format(path))
        if self.manifest_path:
            lines.append("  マニフェスト: {0}".format(self.manifest_path))
        lines.append("  " + self.issues.summary())
        for issue in self.issues.sorted():
            lines.append("    " + issue.format_line())
        for message in self.messages:
            lines.append("  " + message)
        return "\n".join(lines)


# --- 設定の解決 ---------------------------------------------------------------


def resolve_asset_folder(asset_folder=None):
    """アセットフォルダを決める。未指定なら .blend の置き場を使う。"""
    if asset_folder:
        return os.path.abspath(bpy.path.abspath(asset_folder))
    blend_path = bpy.data.filepath
    if not blend_path:
        return None
    return os.path.dirname(os.path.abspath(blend_path))


def resolve_ue_path(settings, asset_name, explicit=None):
    """UE の出力先を決める。環境（Dev / 本番）の contentRoot が展開される（R-2）。"""
    return presets_mod.resolve_destination_path(settings, asset_name, explicit)


def resolve_output_dir(settings, asset_folder):
    """FBX とマニフェストの出力先。構造は dccLayout で決まる（R-1）。"""
    return presets_mod.asset_subfolder(settings, asset_folder, "fbxSubdir")


def resolve_texture_dir(settings, asset_folder):
    """テクスチャフォルダの絶対パス（R-4）。無ければ None。

    **存在しなくてもエラーにしない。**発見側（`texture_discovery`）が
    空リストを返すだけで、書き出しは止まらない。
    """
    return presets_mod.asset_subfolder(settings, asset_folder, "texturesSubdir")


def populate_skeletal(spec, settings):
    """AssetSpec にボーン・スキン・Shape Key を詰める（P3）。

    Armature が見つからない場合は何もしない（検証側が V-060 で弾く）。
    """
    if not spec.is_skeletal:
        return spec

    meshes = spec.mesh_objects()
    armature_obj = None
    if spec.armature_name:
        armature_obj = bpy.data.objects.get(spec.armature_name)
    if armature_obj is None:
        armature_obj = skeletal.find_armature(meshes)
        if armature_obj is not None:
            spec.armature_name = armature_obj.name
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return spec

    fbx_settings = (settings.get("export") or {}).get("fbx") or {}
    deform_only = bool(fbx_settings.get("use_armature_deform_only", False))

    spec.exported_bones = skeletal.exported_bone_names(armature_obj, deform_only)
    spec.bones = skeletal.collect_bones(armature_obj, spec.exported_bones)
    spec.skin = skeletal.collect_skin(
        meshes, armature_obj,
        tolerance=skeletal.weight_tolerance(settings),
        max_influences=skeletal.expected_influence_limit(settings),
    )
    spec.shape_keys = skeletal.collect_shape_keys(meshes)
    return spec


def _material_slots_manifest(spec, settings, ue_path, texture_files=None):
    """`materialSlots[]` を組み立てる。

    `texture_files` はテクスチャフォルダ**直下**のファイル名のリスト（R-4）。
    発見できたものだけ `textures` に入る。**`material.importTextures` が
    false でも入れる**: 発見は DCC 側の事実であり、取り込むかどうかは
    UE 側の方針（設計方針 D-3 / D-5）。
    """
    material_policy = settings.get("material") or {}
    mode = material_policy.get("mode", "none")
    instance_path = material_policy.get("instancePath")
    if not instance_path:
        instance_path = (ue_path or "") + "/Materials"

    slot_names = [name for _, name in spec.material_slots]
    # 警告（V-110〜V-113）はここでは捨てる。**検証側（`validate.check_textures`）が
    # 同じ発見を走らせて IssueList に積む**ので、ここで積むと二重になる
    discovered, _issues = texture_discovery.discover(
        slot_names, spec.asset_name, texture_files or [])

    slots = []
    for index, name in spec.material_slots:
        entry = {"index": index, "slotName": name, "ueMaterialPath": None}
        if mode == "createInstances":
            mi = naming.mi_name(name)
            if mi:
                entry["ueMaterialPath"] = instance_path + "/" + mi
        elif mode == "assignExisting":
            entry["ueMaterialPath"] = None  # UE 側で同名検索
        textures = discovered.get(name)
        if textures:
            entry["textures"] = textures
        slots.append(entry)
    return slots


def _static_mesh_block(spec, settings, lod_files):
    static = dict(settings.get("staticMesh") or {})

    collision_settings = dict(static.get("collision") or {})
    collision_settings["shapes"] = [e.to_manifest() for e in spec.collisions]
    if not spec.collisions and collision_settings.get("simple") == "fromDCC":
        collision_settings["simple"] = "none"
    static["collision"] = collision_settings

    lod_settings = dict(static.get("lod") or {"mode": "none"})
    # `lod_files` は LOD0 を含む全段。2 段以上あるときだけ manual に切り替える
    # （LOD0 しか無いアセットはプリセットの既定 mode を尊重する）
    if len(lod_files) >= 2:
        lod_settings["mode"] = "manual"
        lod_settings["levels"] = lod_files
    else:
        lod_settings.pop("levels", None)
    static["lod"] = lod_settings

    static["sockets"] = [s.to_manifest() for s in spec.sockets]
    return static


class AssetExport(object):
    """1アセットぶんの「書き出し済みの事実」。バッチの中間表現。

    `assets[]` の1要素を組み立てるのに必要な情報だけを持つ。
    単一アセット経路もこれを1件だけ作って同じ関数に通す（二重実装を避ける）。
    """

    __slots__ = ("spec", "ue_path", "main_fbx_name", "lod_files", "fbx_record")

    def __init__(self, spec, ue_path, main_fbx_name, lod_files, fbx_record=None):
        self.spec = spec
        self.ue_path = ue_path
        self.main_fbx_name = main_fbx_name
        self.lod_files = list(lod_files or [])
        self.fbx_record = dict(fbx_record or {})


def build_asset_dict(spec, settings, ue_path, main_fbx_name, lod_files,
                     asset_id=None, texture_files=None):
    """`assets[]` の1要素を組み立てる。

    `asset_id` を省略すると `a001`（従来の単一アセット経路と同じ）。
    バッチでは `manifest.asset_id(index)` で `a001` / `a002` ... を渡す。
    `texture_files` はテクスチャフォルダ直下のファイル名のリスト（R-4）。
    """
    asset_type = spec.asset_type
    asset_name = naming.ue_asset_name(asset_type, spec.asset_name)

    asset = {
        "id": asset_id or manifest_mod.asset_id(0),
        "assetType": asset_type,
        "name": asset_name,
        "destinationPath": ue_path,
        "sourceFile": main_fbx_name,
        "sourceNode": (spec.main_object_names or [None])[0],
        "sourceCollection": spec.root_collection,
        "reimportPolicy": dict(settings.get("reimportPolicy")
                               or {"mode": "preserveSettings", "override": []}),
        "materialSlots": _material_slots_manifest(spec, settings, ue_path,
                                                  texture_files),
        "stats": spec.stats.to_manifest(),
    }

    if asset_type == "StaticMesh":
        asset["staticMesh"] = _static_mesh_block(spec, settings, lod_files)
    elif asset_type == "SkeletalMesh":
        asset["skeleton"] = skeletal.skeleton_block(settings, spec.bones,
                                                    spec.armature_name)
        asset["skeletalMesh"] = skeletal.skeletal_mesh_block(settings, spec)
        physics = settings.get("physicsAsset")
        if physics:
            asset["physicsAsset"] = dict(physics)
    else:
        raise NotImplementedError(
            "assetType '{0}' の書き出しは未実装です"
            "（対応済みは StaticMesh / SkeletalMesh）".format(asset_type)
        )

    return asset


def build_batch_manifest_dict(entries, settings, run_id, asset_folder, issues):
    """複数の `AssetExport` を **1本のマニフェスト**にまとめる（N-5）。

    `assets[]` の id は `a001` / `a002` ... と振られ、**衝突しない**。
    マニフェストに1つしか持てないブロック（`coordinateSystem.fbxExportSettings` /
    `source.assetName` / `destination.defaultPath`）は **先頭アセットの値**を使う:

        - `fbxExportSettings` は FBX エクスポータの引数の記録で、
          同じ設定で全アセットを書き出すため先頭で代表できる
        - `source.assetName` はスキーマが単一の `dccName` を要求する（複数不可）。
          アセット単位の名前は `assets[].name` / `assets[].sourceCollection` が正
        - `destination.defaultPath` は「既定」であり、実際の出力先は
          `assets[].destinationPath` が正（UE 側もそちらを見る）
    """
    if not entries:
        raise ValueError("書き出し対象のアセットが1件もありません")

    blend_path = bpy.data.filepath
    dcc_file = os.path.basename(blend_path) if blend_path else ""

    generator = {
        "dcc": "blender",
        "dccVersion": bpy.app.version_string,
        "toolVersion": manifest_mod.TOOL_VERSION,
        "exportedAt": manifest_mod.iso_now(),
        "user": _safe_user(),
    }

    coordinate_system = manifest_mod.blender_coordinate_system(
        applied_scale=collect.UE_SCALE,
        conversion_done_by="fbx_exporter",
        fbx_settings=entries[0].fbx_record,
        unit_in_meters=bpy.context.scene.unit_settings.scale_length,
    )

    layout = presets_mod.dcc_layout(settings)
    source = {
        "assetName": entries[0].spec.asset_name,
        "assetFolder": asset_folder.replace("\\", "/") if asset_folder else "",
        "dccFile": dcc_file,
        "textureFolder": layout.get("texturesSubdir", "textures"),
    }

    destination_settings = settings.get("destination") or {}
    destination = {
        "contentRoot": destination_settings.get("contentRoot", "/Game"),
        "environment": destination_settings.get(
            "environment", presets_mod.DEFAULT_ENVIRONMENT),
        "defaultPath": entries[0].ue_path,
        "allowOverwrite": bool(destination_settings.get("allowOverwrite", True)),
    }
    whitelist = destination_settings.get("pathWhitelist")
    if whitelist:
        destination["pathWhitelist"] = list(whitelist)

    # テクスチャの発見（R-4）。**走査は textureFolder 直下のみ / 再帰しない。**
    # フォルダが無い / 空でもエラーにしない（空リストが返る）
    texture_files = texture_discovery.list_texture_files(
        texture_discovery.texture_folder_path(source["assetFolder"],
                                              source["textureFolder"]))

    assets = [
        build_asset_dict(entry.spec, settings, entry.ue_path,
                         entry.main_fbx_name, entry.lod_files,
                         manifest_mod.asset_id(index), texture_files)
        for index, entry in enumerate(entries)
    ]

    material_policy = settings.get("material")
    limits = settings.get("limits")

    return manifest_mod.build_manifest(
        run_id=run_id,
        generator=generator,
        coordinate_system=coordinate_system,
        source=source,
        destination=destination,
        assets=assets,
        validation_status=issues.status(),
        validation_issues=issues.to_manifest(),
        preset=settings.get("name"),
        material=dict(material_policy) if material_policy else None,
        limits=dict(limits) if limits else None,
    )


def build_manifest_dict(spec, settings, run_id, asset_folder, ue_path,
                        main_fbx_name, lod_files, fbx_record, issues):
    """単一アセットのマニフェストを作る（従来の経路。シグネチャは変えない）。"""
    entry = AssetExport(spec, ue_path, main_fbx_name, lod_files, fbx_record)
    return build_batch_manifest_dict([entry], settings, run_id, asset_folder,
                                     issues)


def _safe_user():
    try:
        return getpass.getuser()
    except Exception:
        return None


# --- 公開 API -----------------------------------------------------------------


def collect_spec(asset_name=None, objects=None, collection_name=None,
                 asset_type="StaticMesh"):
    """AssetSpec を作る。collection_name があればコレクションモード。"""
    if collection_name:
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            raise ValueError("コレクションが見つかりません: {0}".format(collection_name))
        return collect.collect_from_collection(collection, asset_type)

    if objects is None:
        objects = [o for o in bpy.context.selected_objects]
        if not objects:
            objects = [o for o in bpy.context.scene.objects
                       if o.type in ("MESH", "EMPTY", "ARMATURE")]
    if not asset_name:
        meshes = [o for o in objects if o.type == "MESH"
                  and not naming.is_collision_name(o.name)]
        if not meshes:
            # Armature だけ選ばれている場合はスキンされたメッシュから名前を取る
            for obj in objects:
                if obj.type == "ARMATURE":
                    meshes = [m for m in collect.meshes_skinned_to(obj)
                              if not naming.is_collision_name(m.name)]
                    if meshes:
                        break
        asset_name = naming.strip_known_prefix(meshes[0].name) if meshes else "Asset"
    return collect.collect_from_objects(asset_name, objects, asset_type)


def validate_only(preset=DEFAULT_PRESET, asset_name=None, objects=None,
                  collection_name=None, asset_folder=None, overrides=None,
                  presets_root=None, environment=None, asset_type="StaticMesh"):
    """検証のみ実行（Dry Run / UC-11）。何も書き出さない。"""
    result = ExportResult()
    result.dry_run = True

    folder = resolve_asset_folder(asset_folder)
    try:
        settings = presets_mod.resolve_settings(preset, folder, overrides,
                                               presets_root, environment)
    except ValueError as error:
        result.issues.error("V-105", str(error))
        return result
    result.settings = settings

    try:
        spec = collect_spec(asset_name, objects, collection_name, asset_type)
    except ValueError as error:
        result.issues.error("V-001", str(error))
        return result

    result.asset_name = spec.asset_name
    populate_skeletal(spec, settings)
    ue_path = resolve_ue_path(settings, spec.asset_name)
    output_dir = resolve_output_dir(settings, folder)

    result.issues = validate.validate_asset(
        spec, settings, ue_path=ue_path, output_dir=output_dir,
        blend_path=bpy.data.filepath,
        texture_dir=resolve_texture_dir(settings, folder),
    )
    result.ok = not result.issues.has_error()
    return result


def export_asset(preset=DEFAULT_PRESET, asset_name=None, objects=None,
                 collection_name=None, asset_folder=None, ue_path=None,
                 asset_type="StaticMesh", overrides=None, presets_root=None,
                 force=False, environment=None):
    """書き出しの本体。検証 -> 整形 -> FBX -> マニフェスト。

    force=True のとき、検証エラーがあっても書き出しを試みる（デバッグ用）。
    """
    result = ExportResult()
    result.run_id = manifest_mod.make_run_id()

    folder = resolve_asset_folder(asset_folder)
    if folder is None:
        result.issues.error("V-102",
                            ".blend が未保存で、アセットフォルダも指定されていません。"
                            "先に .blend を保存するか、アセットフォルダを指定してください")
        return result

    try:
        settings = presets_mod.resolve_settings(preset, folder, overrides,
                                               presets_root, environment)
    except ValueError as error:
        result.issues.error("V-105", str(error))
        return result
    result.settings = settings

    try:
        spec = collect_spec(asset_name, objects, collection_name, asset_type)
    except ValueError as error:
        result.issues.error("V-001", str(error))
        return result

    result.asset_name = spec.asset_name
    result.asset_names = [spec.asset_name]
    populate_skeletal(spec, settings)
    resolved_ue_path = resolve_ue_path(settings, spec.asset_name, ue_path)

    output_dir = resolve_output_dir(settings, folder)

    # --- 検証 ---------------------------------------------------------------
    result.issues = validate.validate_asset(
        spec, settings, ue_path=resolved_ue_path, output_dir=output_dir,
        blend_path=bpy.data.filepath,
        texture_dir=resolve_texture_dir(settings, folder),
    )
    if result.issues.has_error() and not force:
        result.add_message("検証エラーがあるため書き出しを中止しました。"
                           "上の指摘を修正してから再実行してください")
        return result

    if not exporter.operator_available():
        result.issues.error("V-103",
                            "FBX エクスポータ（export_scene.fbx）が利用できません。"
                            "Blender の FBX アドオンを有効にしてください")
        return result

    # --- 整形と書き出し -----------------------------------------------------
    entry = _export_fbx_set(spec, settings, output_dir, resolved_ue_path, result)
    if entry is None:
        return result

    # --- マニフェスト -------------------------------------------------------
    try:
        manifest = build_batch_manifest_dict([entry], settings, result.run_id,
                                             folder, result.issues)
    except NotImplementedError as error:
        result.issues.error("V-001", str(error))
        return result

    return _finish_export(result, manifest, settings, output_dir,
                          log_extra={"destinationPath": resolved_ue_path})


# --- バッチ書き出し（複数アセットを1回で / N-5） -------------------------------


def find_asset_collection_names():
    """アセット用コレクションの名前を**安定した順序**で返す。

    「LOD<n> / Collision / Sockets のいずれかを子に持つコレクション」が
    アセット用コレクション（`collect.find_asset_collections()`）。
    `bpy.data.collections` の列挙順に依存しないよう名前でソートする
    （マニフェストの `assets[]` の順序と id の割り当てを決定的にするため）。
    """
    return sorted(c.name for c in collect.find_asset_collections())


def _check_duplicate_assets(issues, entries):
    """同じ出力先に同名のアセットが2件以上ないか見る（V-107）。

    `--ue-path` で全アセットの出力先を明示したときに起こりうる。
    放置すると**後のアセットが前のアセットを上書きする**（P-8 と同じ事故）。
    """
    seen = {}
    for spec, ue_path in entries:
        key = (ue_path or "", naming.ue_asset_name(spec.asset_type, spec.asset_name))
        seen.setdefault(key, []).append(spec.root_collection or spec.asset_name)
    for (ue_path, name), sources in seen.items():
        if len(sources) > 1:
            issues.error(
                "V-107",
                "同じ出力先に同名のアセットが {0} 件あります（{1}/{2}）。"
                "後の書き出しが前を上書きしてしまうため中断しました。"
                "元: {3}".format(len(sources), ue_path, name, ", ".join(sources)),
                name)


def export_batch(preset=DEFAULT_PRESET, collection_names=None,
                 all_collections=False, asset_folder=None, ue_path=None,
                 asset_type="StaticMesh", overrides=None, presets_root=None,
                 force=False, environment=None):
    """複数のアセットコレクションを **1本のマニフェスト**にまとめて書き出す。

    collection_names ... アセット用コレクション名のリスト
    all_collections  ... True なら `find_asset_collection_names()` で自動列挙する

    FBX は**アセットごとに別ファイル**（`SM_<名前>.fbx`）になり、出力先は
    単一アセット経路と同じ `dccLayout.fbxSubdir`。マニフェストは1本だけ出る。

    **バリデーションはアセットごとに実行し、1つでもエラーがあれば全体を中断する。**
    上流で止めるのが設計方針 D-3 であり、「半分だけ UE に入った」状態は
    片付けるコストが高い（どこまで入ったかをユーザーが追えない）。
    部分成功が欲しい場合は `force=True`（検証エラーを押し切る）か、
    コレクションを分けて複数回呼ぶ。
    """
    result = ExportResult()
    result.batch = True
    result.run_id = manifest_mod.make_run_id()

    folder = resolve_asset_folder(asset_folder)
    if folder is None:
        result.issues.error("V-102",
                            ".blend が未保存で、アセットフォルダも指定されていません。"
                            "先に .blend を保存するか、アセットフォルダを指定してください")
        return result

    try:
        settings = presets_mod.resolve_settings(preset, folder, overrides,
                                               presets_root, environment)
    except ValueError as error:
        result.issues.error("V-105", str(error))
        return result
    result.settings = settings

    names = [n for n in (collection_names or []) if n]
    if all_collections or not names:
        names = find_asset_collection_names()
    if not names:
        result.issues.error(
            "V-001",
            "アセット用コレクションが見つかりません。"
            "LOD<n> / Collision / Sockets のいずれかを子に持つコレクションを"
            "用意するか、--collections でコレクション名を指定してください")
        return result

    output_dir = resolve_output_dir(settings, folder)
    texture_dir = resolve_texture_dir(settings, folder)

    # --- 収集と検証（アセットごと）-------------------------------------------
    pairs = []
    for name in names:
        try:
            spec = collect_spec(None, None, name, asset_type)
        except ValueError as error:
            result.issues.error("V-001", str(error), name)
            continue
        populate_skeletal(spec, settings)
        resolved = resolve_ue_path(settings, spec.asset_name, ue_path)
        result.issues.extend(validate.validate_asset(
            spec, settings, ue_path=resolved, output_dir=output_dir,
            blend_path=bpy.data.filepath,
            texture_dir=texture_dir,
        ))
        pairs.append((spec, resolved))

    result.asset_names = [spec.asset_name for spec, _ in pairs]
    result.asset_name = ", ".join(result.asset_names) or None
    _check_duplicate_assets(result.issues, pairs)

    if result.issues.has_error() and not force:
        result.add_message(
            "検証エラーがあるため **バッチ全体** の書き出しを中止しました"
            "（{0} 件のアセットを対象にしていました）。"
            "上の指摘を修正してから再実行してください".format(len(pairs)))
        return result
    if not pairs:
        return result

    if not exporter.operator_available():
        result.issues.error("V-103",
                            "FBX エクスポータ（export_scene.fbx）が利用できません。"
                            "Blender の FBX アドオンを有効にしてください")
        return result

    # --- 整形と書き出し（アセットごとに別 FBX）-------------------------------
    entries = []
    for spec, resolved in pairs:
        entry = _export_fbx_set(spec, settings, output_dir, resolved, result)
        if entry is None:
            return result
        entries.append(entry)

    # --- マニフェスト（1本にまとめる）---------------------------------------
    try:
        manifest = build_batch_manifest_dict(entries, settings, result.run_id,
                                             folder, result.issues)
    except (NotImplementedError, ValueError) as error:
        result.issues.error("V-001", str(error))
        return result

    paths = []
    for asset in manifest.get("assets", []):
        path = asset.get("destinationPath")
        if path and path not in paths:
            paths.append(path)
    return _finish_export(result, manifest, settings, output_dir,
                          log_extra={"destinationPaths": paths})


# --- 書き出しの実体（単一・バッチで共有）---------------------------------------


def _export_fbx_set(spec, settings, output_dir, ue_path, result):
    """1アセットぶんの FBX（LOD 全段）を書き出す。

    成功したら `AssetExport`、失敗したら **None**（理由は `result.issues` に積む）。
    複製・退避・後片付けは `prepare.prepare()` に任せる（D-4 / P-0 / P-0b）。
    """
    export_settings = settings.get("export") or {}
    ue_asset_name = naming.ue_asset_name(spec.asset_type, spec.asset_name)
    fbx_settings = export_settings.get("fbx") or {}
    object_types = list(fbx_settings.get("object_types") or ["MESH", "EMPTY"])
    if spec.is_skeletal and "ARMATURE" not in object_types:
        # Armature を含めないとボーンが1本も入らない
        object_types.append("ARMATURE")
    lod_files = []
    fbx_record = {}

    lods = sorted(spec.lods, key=lambda l: l.level)
    main_fbx_name = None

    try:
        for lod in lods:
            names = list(lod.object_names)
            if lod.level == 0:
                names.extend(e.object_name for e in spec.collisions)
                names.extend(s.object_name for s in spec.sockets)
                if spec.armature_name:
                    names.append(spec.armature_name)
                filename = ue_asset_name + ".fbx"
            else:
                filename = "{0}_LOD{1}.fbx".format(ue_asset_name, lod.level)

            filepath = os.path.join(output_dir, filename)

            # 三角形数は **prepare() の前**（= 元オブジェクト）で数える。
            # prepare 中は元が `__onepochi_orig__*` に退避され、名前で引くと
            # 複製（モディファイア適用済み）が返るため、V-047 / stats.triangleCount と
            # 数え方が食い違ってしまう
            triangle_count = collect.lod_triangle_count(lod.object_names)

            deform_bones = set(b.name for b in spec.bones if b.use_deform)
            with prepare.prepare(names, settings,
                                 skeletal_mode=spec.is_skeletal,
                                 deform_bone_names=deform_bones or None) as prepared:
                targets = prepared.objects
                if not targets:
                    result.issues.error("V-001",
                                        "LOD{0} に書き出せるオブジェクトがありません".format(lod.level),
                                        lod.collection_name or spec.asset_name)
                    return None
                ok, kwargs, warnings = exporter.export_fbx(
                    filepath, targets, fbx_settings, object_types
                )

            for warning in warnings:
                result.issues.warn("V-103", warning)

            if not ok:
                result.issues.error("V-103",
                                    "FBX の書き出しに失敗しました: {0}".format(filepath),
                                    filename)
                return None

            result.fbx_paths.append(filepath)
            if lod.level == 0:
                main_fbx_name = filename
                fbx_record = exporter.recorded_settings(kwargs)
            # LOD0 も levels[] に含める（docs/03 §11.4「LOD0 必須・欠番はエラー」）。
            # UE 側は levels を見て LOD スロットを埋めるので、LOD0 が抜けていると
            # 「どの FBX が LOD0 か」をマニフェストから決められない
            level_entry = {
                "level": lod.level,
                "sourceFile": filename,
                "sourceCollection": lod.collection_name,
                "sourceNode": (lod.object_names or [None])[0],
                "screenSize": None,
            }
            if triangle_count is not None:
                # schema v0.3.1 / R-6。宣言すると UE 側（`lod.compare_lod()`）が
                # 実測値と**厳密比較**する。数えられなかった段は
                # **キーごと出さない**（0 やダミーを入れない）
                level_entry["triangleCount"] = triangle_count
            lod_files.append(level_entry)
    except Exception as error:  # noqa: BLE001
        result.issues.error("V-103",
                            "書き出し中に予期しないエラーが発生しました: {0}: {1}".format(
                                type(error).__name__, error))
        return None

    return AssetExport(spec, ue_path, main_fbx_name, lod_files, fbx_record)


def _finish_export(result, manifest, settings, output_dir, log_extra=None):
    """マニフェストの構造チェック -> 書き出し -> 実行ログ（単一・バッチ共通）。"""
    problems = manifest_mod.check_manifest_shape(manifest)
    for problem in problems:
        result.issues.error("V-104", "マニフェストの構造に問題があります: {0}".format(problem))
    if problems:
        return result

    result.manifest = manifest
    result.manifest_path = manifest_mod.write_manifest(
        output_dir, manifest,
        presets_mod.dcc_layout(settings).get("manifestName",
                                             manifest_mod.MANIFEST_FILENAME))
    result.ok = True

    # 実行ログを `%LOCALAPPDATA%/OnePochi/runs/<run_id>/` に残す（docs/13 P4-5）。
    # アセットの隣に出るマニフェストは**次の書き出しで上書きされる**ので、
    # 事故の調査材料が残らない。`runs/latest.json` は UE 側のメニュー（P6-4）が
    # 「最後に書き出したマニフェスト」を知るための唯一の出所になる。
    # **ログの保存に失敗しても書き出しは成功扱い**にする（警告だけ出す）
    payload = {"result": "success", "preset": settings.get("name"),
               "assetNames": [a.get("name") for a in manifest.get("assets", [])],
               "warnings": len(result.issues.warnings),
               "errors": len(result.issues.errors)}
    payload.update(log_extra or {})
    result.run_dir = runs_mod.record(
        result.run_id, manifest=manifest, manifest_path=result.manifest_path,
        result=payload)
    if result.run_dir is None:
        result.issues.warn(
            "V-106", "実行ログを保存できませんでした（書き出し自体は成功しています）")
    else:
        result.pruned_runs = runs_mod.prune()
    return result
