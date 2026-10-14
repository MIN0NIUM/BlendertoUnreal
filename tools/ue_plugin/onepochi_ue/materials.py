"""マテリアルとマテリアルインスタンス（Q-19 / docs/90 §2-5）。

方針:
    1. **仮のマスターマテリアル** `M_OnePochi_Base` をツール側で生成できる
       （BaseColor / Normal / ORM / Emissive の最小構成）
    2. Blender 側で**任意のマスターマテリアルを指定**できる
       （マニフェストの `material.masterMaterialPath`）
    3. Blender のマテリアル名から **MI を生成してアサイン**する
       （`M_Crate_Body` -> `MI_Crate_Body`）
    4. マテリアル名が `M_<名前>` 形式でなければ **DCC 側で弾く**ので、
       ここに来る名前は規約に適合している前提

UE 5.8 の API は未検証部分が多いため、**すべての操作を try で囲み、
失敗を例外にせずレポートへ記録する**（importer / postprocess と同じ方針）。
"""

import unreal

from . import postprocess
# `textures` という名前はこのモジュール内の**ローカル変数**として多用するので
# （`materialSlots[].textures`）、モジュールは別名で持つ
from . import textures as texture_lib

DEFAULT_MASTER_NAME = "M_OnePochi_Base"

#: マスターマテリアルのテクスチャパラメータ定義
#: (パラメータ名, 接続先プロパティ名, 出力名, サンプラー型, 既定テクスチャ候補)
TEXTURE_PARAMETERS = (
    ("BaseColor", "MP_BASE_COLOR", "RGB", "SAMPLERTYPE_COLOR",
     ("/Engine/EngineMaterials/DefaultDiffuse", "/Engine/EngineResources/WhiteSquareTexture")),
    ("Normal", "MP_NORMAL", "RGB", "SAMPLERTYPE_NORMAL",
     ("/Engine/EngineMaterials/DefaultNormal",)),
    ("Emissive", "MP_EMISSIVE_COLOR", "RGB", "SAMPLERTYPE_COLOR",
     ("/Engine/EngineMaterials/DefaultDiffuse",)),
)

#: ORM パック（R=AO / G=Roughness / B=Metallic）
ORM_CHANNELS = (
    ("R", "MP_AMBIENT_OCCLUSION"),
    ("G", "MP_ROUGHNESS"),
    ("B", "MP_METALLIC"),
)


def _load(path):
    try:
        return unreal.EditorAssetLibrary.load_asset(path)
    except Exception:
        return None


def _exists(path):
    try:
        return unreal.EditorAssetLibrary.does_asset_exist(path)
    except Exception:
        return False


def _material_property(name, sink):
    enum = getattr(unreal, "MaterialProperty", None)
    if enum is None:
        sink.append("unreal.MaterialProperty が存在しません")
        return None
    value = getattr(enum, name, None)
    if value is None:
        sink.append("MaterialProperty.{0} が存在しません".format(name))
    return value


def _sampler_type(name, sink):
    enum = getattr(unreal, "MaterialSamplerType", None)
    if enum is None:
        return None
    value = getattr(enum, name, None)
    if value is None:
        sink.append("MaterialSamplerType.{0} が存在しません".format(name))
    return value


def _editing_library(sink):
    library = getattr(unreal, "MaterialEditingLibrary", None)
    if library is None:
        sink.append("unreal.MaterialEditingLibrary が存在しません。"
                    "マテリアルの自動生成はできません")
    return library


def split_package_path(package_path):
    directory, _, name = package_path.rpartition("/")
    return directory, name


# --- マスターマテリアル -------------------------------------------------------


def create_master_material(package_path, sink, force=False):
    """仮のマスターマテリアルを生成する。既存なら何もしない。

    戻り値: (material or None, 作成したか)
    """
    if _exists(package_path) and not force:
        material = _load(package_path)
        if material is not None:
            return material, False

    library = _editing_library(sink)
    if library is None:
        return None, False

    directory, name = split_package_path(package_path)
    tools = postprocess.attempt(sink, "get_asset_tools",
                                unreal.AssetToolsHelpers.get_asset_tools)
    if tools is None:
        return None, False

    factory = postprocess.attempt(sink, "MaterialFactoryNew", unreal.MaterialFactoryNew)
    if factory is None:
        return None, False

    material = postprocess.attempt(sink, "create_asset({0})".format(name),
                                   tools.create_asset, name, directory,
                                   unreal.Material, factory)
    if material is None:
        return None, False

    position_y = -400
    for param_name, property_name, output_name, sampler_name, defaults in TEXTURE_PARAMETERS:
        expression = postprocess.attempt(
            sink, "create_material_expression({0})".format(param_name),
            library.create_material_expression, material,
            unreal.MaterialExpressionTextureSampleParameter2D, -400, position_y
        )
        position_y += 260
        if expression is None:
            continue

        postprocess.set_property(sink, expression, "parameter_name", param_name)
        sampler = _sampler_type(sampler_name, sink)
        if sampler is not None:
            postprocess.set_property(sink, expression, "sampler_type", sampler)
        for default_path in defaults:
            texture = _load(default_path)
            if texture is not None:
                postprocess.set_property(sink, expression, "texture", texture)
                break

        target = _material_property(property_name, sink)
        if target is not None:
            postprocess.attempt(sink, "connect {0}".format(param_name),
                                library.connect_material_property,
                                expression, output_name, target)

    # ORM パック: 1枚のテクスチャから AO / Roughness / Metallic を取り出す
    orm = postprocess.attempt(sink, "create_material_expression(ORM)",
                              library.create_material_expression, material,
                              unreal.MaterialExpressionTextureSampleParameter2D,
                              -400, position_y)
    if orm is not None:
        postprocess.set_property(sink, orm, "parameter_name", "ORM")
        sampler = _sampler_type("SAMPLERTYPE_LINEAR_COLOR", sink)
        if sampler is not None:
            postprocess.set_property(sink, orm, "sampler_type", sampler)
        for channel, property_name in ORM_CHANNELS:
            target = _material_property(property_name, sink)
            if target is not None:
                postprocess.attempt(sink, "connect ORM.{0}".format(channel),
                                    library.connect_material_property,
                                    orm, channel, target)

    postprocess.attempt(sink, "layout_material_expressions",
                        library.layout_material_expressions, material)
    postprocess.attempt(sink, "recompile_material", library.recompile_material, material)
    postprocess.attempt(sink, "save_loaded_asset",
                        unreal.EditorAssetLibrary.save_loaded_asset, material, False)
    return material, True


def ensure_master_material(package_path, sink):
    """マスターマテリアルを取得する。無ければ生成する。"""
    if not package_path:
        sink.append("material.masterMaterialPath が指定されていません")
        return None
    material = _load(package_path)
    if material is not None:
        return material
    sink.append("マスターマテリアルが無いため生成します: {0}".format(package_path))
    material, _ = create_master_material(package_path, sink)
    return material


# --- マテリアルインスタンス ---------------------------------------------------


def create_material_instance(master, package_path, sink):
    """MI を生成する（既存ならロードして返す）。"""
    if _exists(package_path):
        return _load(package_path), False

    directory, name = split_package_path(package_path)
    tools = postprocess.attempt(sink, "get_asset_tools",
                                unreal.AssetToolsHelpers.get_asset_tools)
    if tools is None:
        return None, False

    factory = postprocess.attempt(sink, "MaterialInstanceConstantFactoryNew",
                                  unreal.MaterialInstanceConstantFactoryNew)
    if factory is None:
        return None, False

    instance = postprocess.attempt(sink, "create_asset({0})".format(name),
                                   tools.create_asset, name, directory,
                                   unreal.MaterialInstanceConstant, factory)
    if instance is None:
        return None, False

    library = _editing_library(sink)
    if library is not None:
        postprocess.attempt(sink, "set_material_instance_parent",
                            library.set_material_instance_parent, instance, master)
    else:
        postprocess.set_property(sink, instance, "parent", master)
    return instance, True


#: MI のテクスチャパラメータを**読む** API の候補（名前は要検証 / docs/99 P-15）。
#: `probe_texture_api.py` で確定させる
_TEXTURE_PARAMETER_GETTERS = (
    "get_material_instance_texture_parameter_value",
    "get_material_default_texture_parameter_value",
)


def import_slot_textures(textures, texture_root, manifest, manifest_path, sink,
                         done=None):
    """`materialSlots[].textures` を UE 側へ取り込む（P5-3 / P5-4）。

    **`assign_textures` の前に呼ぶ。**取り込みが済んでいないと
    `assign_textures` が「テクスチャが UE 側に見つかりません」で全滅する。

    **1枚も取り込めなくてもエラーにしない。**警告を積んで MI 生成は続行する
    （アーティストの作業を止めないため / 方針どおり）。

    done: 同一レポート内ですでに取り込んだ `{assetPath: suffix}`。
          複数スロットが同じテクスチャを参照しても二重に取り込まない

    戻り値: `textures.import_textures` の記録 + `planned` / `skipped`。
    """
    record = {"planned": 0, "paths": [], "imported": [], "missing": [],
              "failed": [], "reused": [], "saved": 0, "verifyFailed": 0,
              "skipped": None}
    if not textures:
        return record

    if not manifest_path:
        # importer から `manifest_path` が渡ってこない経路。推測で cwd を
        # 基準にすると**別のフォルダのテクスチャを掴む**ので取り込まない
        record["skipped"] = "manifestPathUnknown"
        sink.append("マニフェストの位置が分からないためテクスチャの相対パスを"
                    "解決できません。テクスチャの取り込みをスキップします")
        return record

    folder = texture_lib.texture_folder_of(manifest or {}, manifest_path)
    if folder is None:
        record["skipped"] = "textureFolderIsNull"
        sink.append("source.textureFolder が null のためテクスチャを取り込みません")
        return record

    plans = texture_lib.plan_imports(textures, folder, manifest_path, texture_root)
    record["planned"] = len(plans)
    result = texture_lib.import_textures(plans, sink, done=done)
    record.update(result)

    if not result["imported"] and not result["reused"]:
        sink.append("テクスチャを1枚も取り込めませんでした（計画 {0} 枚 / "
                    "ソース不在 {1} 枚 / 失敗 {2} 枚）。"
                    "MI の生成は続行します".format(
                        len(plans), len(result["missing"]), len(result["failed"])))
    return record


def observed_texture_parameters(instance, parameter_names, sink):
    """MI に**実際に割り当たっている**テクスチャのパスを読み直す。

    `observed_slot_materials` と同じ思想（docs/99 P-23）:
    `set_..._texture_parameter_value` は成功を返しても効いていないことがある。

    読み取り API が無い UE バージョンでは **`{}`** を返す。
    空は「割当なし」ではなく**「確かめられなかった」**なので、
    呼び側は空を合格にしない。
    """
    observed = {}
    library = _editing_library(sink)
    if library is None or not parameter_names:
        return observed

    getter = None
    for name in _TEXTURE_PARAMETER_GETTERS:
        getter = getattr(library, name, None)
        if getter is not None:
            break
    if getter is None:
        sink.append("MI のテクスチャパラメータを読む API が見つかりません"
                    "（候補: {0}）。割当は実測できません".format(
                        ", ".join(_TEXTURE_PARAMETER_GETTERS)))
        return observed

    for parameter in parameter_names:
        texture = postprocess.attempt(
            sink, "get texture parameter {0}".format(parameter),
            getter, instance, parameter)
        if texture is None:
            observed[parameter] = None
            continue
        observed[parameter] = postprocess.attempt(sink, "get_path_name",
                                                  texture.get_path_name)
    return observed


def unassigned_textures(textures, parameter_map, texture_root, observed, sink):
    """宣言したテクスチャが MI に乗っていない項目を**実測で**洗い出す。

    `observed` が空（読めなかった）のときは**何も報告しない**
    （「確かめられなかった」を「不一致」にしない）。
    """
    problems = []
    if not observed or not textures:
        return problems
    root = str(texture_root or "").rstrip("/")
    for suffix in sorted(textures):
        parameter = parameter_map.get(suffix)
        if not parameter or parameter not in observed:
            continue
        expected = "{0}/{1}".format(
            root, texture_lib.asset_name_for(textures[suffix]))
        actual = observed.get(parameter)
        if actual and _same_asset(expected, actual):
            continue
        problems.append({"suffix": suffix, "parameter": parameter,
                         "expected": expected, "actual": actual})
        if actual:
            sink.append(
                "MI のテクスチャパラメータ '{0}' には '{1}' が入っています"
                "（'{2}' にしようとしました）".format(parameter, actual, expected))
        else:
            sink.append(
                "MI のテクスチャパラメータ '{0}' に何も入っていません"
                "（'{1}' にしようとしました）".format(parameter, expected))
    return problems


def assign_textures(instance, textures, parameter_map, texture_root, sink):
    """MI のテクスチャパラメータを割り当てる。

    textures: {"_BC": "T_Crate_01_BC.png", ...}（textureFolder からの相対パス）
    parameter_map: {"_BC": "BaseColor", ...}
    texture_root: UE 側のテクスチャ格納パス（例 /Game/_Dev/Props/Crate_01/Textures）
    """
    if not textures:
        return 0
    library = _editing_library(sink)
    if library is None:
        return 0

    assigned = 0
    for suffix in sorted(textures):      # レポートの順序を安定させる
        relative = textures[suffix]
        parameter = parameter_map.get(suffix)
        if not parameter:
            sink.append("テクスチャサフィックス '{0}' に対応する"
                        "MI パラメータが定義されていません".format(suffix))
            continue
        # 拡張子の落とし方は `textures.asset_name_for` に合わせる
        # （バックスラッシュ区切りや非文字列でも落ちない）
        stem = texture_lib.asset_name_for(relative)
        if not stem:
            sink.append("materialSlots[].textures['{0}'] のパスが空です".format(suffix))
            continue
        candidate = "{0}/{1}".format(str(texture_root or "").rstrip("/"), stem)
        texture = _load(candidate)
        if texture is None:
            sink.append("テクスチャが UE 側に見つかりません: {0}".format(candidate))
            continue
        if postprocess.attempt(
            sink, "set texture parameter {0}".format(parameter),
            library.set_material_instance_texture_parameter_value,
            instance, parameter, texture
        ) is not None:
            assigned += 1

    if assigned:
        postprocess.attempt(sink, "update_material_instance",
                            library.update_material_instance, instance)
        # **MI を保存する**（docs/99 P-10: 保存しないと参照が切れる）。
        # MI はインポータの戻り値に含まれないので `importer.save_all()` では
        # 保存されない。テクスチャ割当を失わないためにここで保存する
        postprocess.attempt(sink, "save_loaded_asset(MI)",
                            unreal.EditorAssetLibrary.save_loaded_asset,
                            instance, False)
    return assigned


def apply_material_slots(mesh, asset, material_policy, sink,
                         manifest=None, manifest_path=None):
    """マニフェストの materialSlots に従って MI を生成・割当する。

    manifest / manifest_path: **テクスチャの取り込みに必要**（P5-3）。
        `source.textureFolder` と相対パスの基準を決めるのに使う。
        省略された経路では**テクスチャの取り込みだけをスキップ**し、
        MI の生成と割当は従来どおり行う（後方互換）。

    戻り値: 処理内容の記録辞書
    """
    record = {"mode": (material_policy or {}).get("mode", "none"),
              "created": [], "assigned": [], "skipped": [], "textures": []}
    mode = record["mode"]
    slots = asset.get("materialSlots") or []
    if mode == "none" or not slots:
        return record

    materials = postprocess.get_property(sink, mesh, "static_materials", [])
    if not materials:
        sink.append("StaticMesh にマテリアルスロットがありません")
        return record

    master = None
    if mode == "createInstances":
        master = ensure_master_material(material_policy.get("masterMaterialPath"), sink)
        if master is None:
            sink.append("マスターマテリアルを用意できなかったため MI 生成を中止します")
            return record

    parameter_map = (material_policy or {}).get("textureParameterMap") or {}
    changed = False
    # 複数スロットが同じテクスチャを参照していても二重に取り込まない
    texture_done = {}

    for entry in slots:
        index = entry.get("index")
        slot_name = entry.get("slotName")
        if index is None or index >= len(materials):
            record["skipped"].append(slot_name)
            continue

        target_path = entry.get("ueMaterialPath")
        material_asset = None

        if mode == "assignExisting":
            # DCC 側のマテリアル名と同名のアセットを探す
            candidates = [target_path] if target_path else []
            candidates.append("{0}/{1}".format(
                asset.get("destinationPath", "").rstrip("/"), slot_name))
            for candidate in candidates:
                if candidate:
                    material_asset = _load(candidate)
                    if material_asset is not None:
                        break
            if material_asset is None:
                sink.append("同名の既存マテリアルが見つかりません: {0}".format(slot_name))
                record["skipped"].append(slot_name)
                continue

        elif mode == "createInstances":
            if not target_path:
                sink.append("materialSlots[{0}].ueMaterialPath が空のため"
                            "MI を生成できません".format(index))
                record["skipped"].append(slot_name)
                continue
            material_asset, created = create_material_instance(master, target_path, sink)
            if material_asset is None:
                record["skipped"].append(slot_name)
                continue
            if created:
                record["created"].append(target_path)
            textures = entry.get("textures")
            if textures and (material_policy or {}).get("importTextures"):
                # 格納先は**既存実装のまま**（`<destinationPath>/Textures`）
                texture_root = "{0}/Textures".format(
                    asset.get("destinationPath", "").rstrip("/"))
                # ★ **取り込みは割当より前**（P5-3）。順序を逆にすると
                #    `assign_textures` が「UE 側に見つかりません」で全滅する
                imported = import_slot_textures(
                    textures, texture_root, manifest, manifest_path, sink,
                    done=texture_done)
                assigned = assign_textures(material_asset, textures,
                                           parameter_map, texture_root, sink)
                # **戻り値だけに頼らず読み直して実測する**（docs/99 P-23）
                wanted = []
                for suffix in sorted(textures):
                    parameter = parameter_map.get(suffix)
                    if parameter and parameter not in wanted:
                        wanted.append(parameter)
                observed = observed_texture_parameters(material_asset, wanted, sink)
                record["textures"].append({
                    "index": index,
                    "slotName": slot_name,
                    "textureRoot": texture_root,
                    "declared": len(textures),
                    "imported": imported,
                    "assigned": assigned,
                    "observed": observed,
                    "unassigned": unassigned_textures(
                        textures, parameter_map, texture_root, observed, sink),
                })

        if material_asset is None:
            continue

        # **構造体配列の要素はコピー**なので代入し直す（docs/99 P-23）。
        # 以前は `materials[index].set_editor_property(...)` としていたため
        # MI は作られるのに**割当が効いていなかった**（実測で判明）
        if postprocess.update_struct_in_array(
                sink, materials, index,
                (("material_interface", material_asset),),
                label="static_materials") is not None:
            record["assigned"].append({"index": index, "slotName": slot_name,
                                       "material": target_path})
            changed = True

    if changed:
        postprocess.set_property(sink, mesh, "static_materials", materials)
        postprocess.attempt(sink, "save_loaded_asset",
                            unreal.EditorAssetLibrary.save_loaded_asset, mesh, False)
        # **割当は実測で確かめる**（存在確認だけで合格にしない / K-8）
        record["observed"] = observed_slot_materials(mesh, sink)
        for item in record["assigned"]:
            expected = item["material"]
            actual = (record["observed"] or {}).get(item["index"])
            if expected and actual and not _same_asset(expected, actual):
                sink.append(
                    "マテリアルスロット {0} には '{1}' が割り当たっています"
                    "（'{2}' にしようとしました）".format(
                        item["index"], actual, expected))
                item["applied"] = False
            else:
                item["applied"] = True
    return record


def observed_slot_materials(mesh, sink):
    """スロット index -> 実際に割り当たっているマテリアルのパス。"""
    materials = postprocess.get_property(sink, mesh, "static_materials") or []
    observed = {}
    for index in range(len(materials)):
        asset = postprocess.get_property(sink, materials[index],
                                         "material_interface")
        if asset is None:
            observed[index] = None
            continue
        path = postprocess.attempt(sink, "get_path_name", asset.get_path_name)
        observed[index] = path
    return observed


def _same_asset(expected, actual):
    """`/Game/X/MI_A` と `/Game/X/MI_A.MI_A` を同じものとして比べる。"""
    def normalize(value):
        text = str(value or "")
        if "." in text.rsplit("/", 1)[-1]:
            text = text.rsplit(".", 1)[0]
        return text
    return normalize(expected) == normalize(actual)
