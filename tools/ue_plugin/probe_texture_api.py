"""テクスチャ関連の UE Python API を実機で確認するプローブ（読み取り専用）。

`probe_lod_api.py` / `probe_skeletal_api.py` と同じ趣旨
（[docs/99_作業ルール.md](../../docs/99_作業ルール.md) P-15）。
**推測で名前を決めない。**実在する enum 名・プロパティ名をここで確定させる。

確認するもの:

1. `unreal.TextureCompressionSettings` の enum 名
   （`TC_DEFAULT` / `TC_NORMALMAP` / `TC_MASKS` が実在するか）
2. `unreal.Texture` / `Texture2D` のプロパティ名
   （`srgb` / `compression_settings` / `lod_group` / `mip_gen_settings` 等）
   —— **エンジン同梱のテクスチャを 1 枚ロードして実際に `get_editor_property` する**
3. `unreal.TextureGroup` の enum 名（`lod_group` に入れる値）
4. Interchange のテクスチャ関連（`InterchangeGenericTexturePipeline` の有無とプロパティ）
5. `unreal.AssetTools` / `TextureFactory` 系の有無
6. `MaterialEditingLibrary` のテクスチャパラメータ **読み取り** API
   （MI 割当を実測で確かめるのに使う / docs/99 P-23）
7. `onepochi_ue.textures` の候補名表（`COMPRESSION_ALIASES` / `_SRGB_NAMES` /
   `_COMPRESSION_NAMES`）が実機で解決するか —— **実装の答え合わせ**

---

## 実行方法

**UE エディタを閉じずに Remote Control 経由で呼ぶ**（docs/99 P-16。
エディタが開いている間に commandlet を走らせない）。
`http://127.0.0.1:30010/remote/object/call` に以下の Python を投げる:

    import sys; sys.path.insert(0, r"C:/Users/user/Documents/_STUDY/BlenderToUnreal/tools/ue_plugin")
    import probe_texture_api; probe_texture_api.main(r"C:/Users/user/Documents/_STUDY/BlenderToUnreal/docs/tmp/texture_probe.json")

UE の Output Log から直接実行する場合:

    py "C:/Users/user/Documents/_STUDY/BlenderToUnreal/tools/ue_plugin/probe_texture_api.py" "C:/Users/user/Documents/_STUDY/BlenderToUnreal/docs/tmp/texture_probe.json"

**アセットの生成・変更・保存は一切しない。**エンジン同梱のテクスチャを
読み取り専用でロードするだけ（`set_editor_property` は呼ばない）。
"""

import json
import os
import sys

#: 読み取り用に 1 枚ロードするエンジン同梱テクスチャの候補（先頭から順に試す）
ENGINE_TEXTURES = (
    "/Engine/EngineMaterials/DefaultDiffuse",
    "/Engine/EngineMaterials/DefaultNormal",
    "/Engine/EngineResources/WhiteSquareTexture",
    "/Engine/EngineResources/DefaultTexture",
    "/Engine/EngineMaterials/DefaultBokeh",
)

#: `Texture` / `Texture2D` に有ってほしいプロパティ名の候補
TEXTURE_PROPERTY_CANDIDATES = (
    "srgb", "SRGB", "s_rgb",
    "compression_settings", "CompressionSettings",
    "lod_group", "LODGroup", "lod_bias",
    "mip_gen_settings", "compression_no_alpha", "compression_quality",
    "max_texture_size", "virtual_texture_streaming", "never_stream",
    "address_x", "address_y", "filter", "asset_user_data",
)

#: MI のテクスチャパラメータを**読む** API の候補（docs/99 P-23 の実測用）
MATERIAL_READ_CANDIDATES = (
    "get_material_instance_texture_parameter_value",
    "get_material_default_texture_parameter_value",
    "get_texture_parameter_names",
    "set_material_instance_texture_parameter_value",
    "update_material_instance",
    "get_material_instance_scalar_parameter_value",
)


def _names(obj, keyword=None, limit=400):
    """obj の属性名（keyword を含むものだけ）を返す。"""
    try:
        found = [n for n in dir(obj) if not n.startswith("_")]
        if keyword:
            found = [n for n in found if keyword in n.lower()]
        return sorted(found)[:limit]
    except Exception as error:  # noqa: BLE001
        return ["<dir() 失敗: {0}>".format(error)]


def _enum_members(enum):
    """enum の要素名 -> 値（int にできなければ repr）。"""
    members = {}
    for name in _names(enum):
        try:
            value = getattr(enum, name)
        except Exception as error:  # noqa: BLE001
            members[name] = "<取得失敗: {0}>".format(error)
            continue
        if callable(value):
            continue
        try:
            members[name] = int(value)
        except Exception:  # noqa: BLE001
            members[name] = repr(value)
    return members


def _has(obj, name):
    return bool(obj is not None and hasattr(obj, name))


def _type_entry(unreal, type_name, keyword=None):
    klass = getattr(unreal, type_name, None)
    entry = {"available": klass is not None}
    if klass is not None:
        entry["members"] = _names(klass, keyword)
    return entry


def _enum_value(value):
    """UE の enum から整数を取り出す。

    **`int(value)` は `TypeError` になる**（実測 / UE 5.8.3）:
    `int() argument must be ... not 'TextureCompressionSettings'`。
    `.value` を持っているのでそれを使う。取れなければ文字列化して返す。
    """
    if value is None:
        return None
    inner = getattr(value, "value", None)
    if isinstance(inner, int):
        return inner
    try:
        return int(inner)
    except Exception:  # noqa: BLE001
        return str(value)


def probe():
    import unreal

    result = {
        "engineVersion": None,
        "compressionSettings": {},
        "textureGroup": {},
        "textureTypes": {},
        "loadedTexture": {},
        "interchange": {},
        "factories": {},
        "materialEditingLibrary": {},
        "implementationCheck": {},
        "notes": [],
    }

    try:
        result["engineVersion"] = unreal.SystemLibrary.get_engine_version()
    except Exception as error:  # noqa: BLE001
        result["notes"].append("get_engine_version 失敗: {0}".format(error))

    # --- 1. TextureCompressionSettings の enum 名 ---------------------------
    enum = getattr(unreal, "TextureCompressionSettings", None)
    result["compressionSettings"]["available"] = enum is not None
    if enum is None:
        result["notes"].append(
            "unreal.TextureCompressionSettings が存在しない。"
            "textures.py の COMPRESSION_ALIASES を全面的に見直す必要がある")
    else:
        result["compressionSettings"]["members"] = _enum_members(enum)
        for name in ("TC_DEFAULT", "TC_NORMALMAP", "TC_MASKS", "TC_GRAYSCALE",
                     "TC_HDR", "TC_ALPHA", "TC_DISPLACEMENTMAP",
                     "TC_VECTOR_DISPLACEMENTMAP", "TC_EDITOR_ICON",
                     "TC_BC7", "TC_SINGLE_FLOAT", "TC_HALF_FLOAT"):
            result["compressionSettings"][name] = _has(enum, name)

    # --- 3. TextureGroup の enum 名（lod_group 用） -------------------------
    group = getattr(unreal, "TextureGroup", None)
    result["textureGroup"]["available"] = group is not None
    if group is not None:
        result["textureGroup"]["members"] = _enum_members(group)
        for name in ("TEXTUREGROUP_WORLD", "TEXTUREGROUP_WORLD_NORMAL_MAP",
                     "TEXTUREGROUP_WORLD_SPECULAR", "TEXTUREGROUP_CHARACTER",
                     "TEXTUREGROUP_CHARACTER_NORMAL_MAP", "TEXTUREGROUP_UI"):
            result["textureGroup"][name] = _has(group, name)

    # --- 2. Texture / Texture2D の型情報 ------------------------------------
    for type_name in ("Texture", "Texture2D", "TextureCube",
                      "TextureRenderTarget2D", "VolumeTexture"):
        result["textureTypes"][type_name] = _type_entry(unreal, type_name)

    # --- 2'. 実機のテクスチャを 1 枚ロードして実際に読む（読み取り専用） ----
    texture = None
    for path in ENGINE_TEXTURES:
        try:
            if not unreal.EditorAssetLibrary.does_asset_exist(path):
                continue
            texture = unreal.EditorAssetLibrary.load_asset(path)
        except Exception as error:  # noqa: BLE001
            result["notes"].append("load_asset 失敗 {0}: {1}".format(path, error))
            continue
        if texture is not None:
            result["loadedTexture"]["path"] = path
            break

    result["loadedTexture"]["available"] = texture is not None
    if texture is None:
        result["notes"].append(
            "エンジン同梱テクスチャを 1 枚もロードできなかった。"
            "プロパティ名は型の dir() だけが根拠になる（弱い）")
    else:
        result["loadedTexture"]["class"] = type(texture).__name__
        readable = {}
        for name in TEXTURE_PROPERTY_CANDIDATES:
            try:
                value = texture.get_editor_property(name)
            except Exception as error:  # noqa: BLE001
                readable[name] = {"readable": False,
                                  "error": "{0}: {1}".format(
                                      type(error).__name__, error)}
                continue
            try:
                shown = int(value)
            except Exception:  # noqa: BLE001
                shown = repr(value)[:200]
            readable[name] = {"readable": True, "value": shown}
        result["loadedTexture"]["properties"] = readable
        result["loadedTexture"]["srgbLike"] = _names(texture, "srgb")
        result["loadedTexture"]["compressionLike"] = _names(texture, "compress")
        result["loadedTexture"]["lodLike"] = _names(texture, "lod")

    # --- 4. Interchange のテクスチャ関連 ------------------------------------
    for type_name in ("InterchangeManager", "ImportAssetParameters",
                      "InterchangeGenericTexturePipeline",
                      "InterchangeGenericAssetsPipeline",
                      "InterchangeTextureFactoryNode",
                      "InterchangeTexture2DNode",
                      "InterchangeProjectSettings",
                      "InterchangeSourceData"):
        result["interchange"][type_name] = _type_entry(unreal, type_name)

    manager_class = getattr(unreal, "InterchangeManager", None)
    if manager_class is not None:
        for name in ("get_interchange_manager_scripted", "create_source_data",
                     "import_asset", "import_scene", "is_interchange_import_enabled"):
            result["interchange"].setdefault("managerMethods", {})[name] = _has(
                manager_class, name)

    pipeline = getattr(unreal, "InterchangeGenericTexturePipeline", None)
    if pipeline is not None:
        result["interchange"]["texturePipelineProperties"] = _names(pipeline)
    else:
        result["notes"].append(
            "InterchangeGenericTexturePipeline が無い。"
            "テクスチャの圧縮設定はインポート後に Texture へ直接書く方針のままでよい")

    # --- 5. AssetTools / Factory 系 -----------------------------------------
    for type_name in ("AssetToolsHelpers", "AssetTools", "TextureFactory",
                      "Texture2DFactoryNew", "TextureRenderTargetFactoryNew",
                      "AutomatedAssetImportData", "AssetImportTask"):
        result["factories"][type_name] = _type_entry(unreal, type_name)
    # **AssetImportTask は使わない**（P-6: Interchange が非同期で生成物が取れない）。
    # 有無だけ記録する

    # --- 6. MaterialEditingLibrary の読み取り API --------------------------
    library = getattr(unreal, "MaterialEditingLibrary", None)
    result["materialEditingLibrary"]["available"] = library is not None
    if library is not None:
        for name in MATERIAL_READ_CANDIDATES:
            result["materialEditingLibrary"][name] = _has(library, name)
        result["materialEditingLibrary"]["textureLike"] = _names(library, "texture")

    # --- 7. 実装（textures.py）の候補名が解決するか ------------------------
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:                      # exec されたとき（RC 経由）
        here = None
    if here and here not in sys.path:
        sys.path.insert(0, here)
    try:
        from onepochi_ue import textures as impl
    except Exception as error:  # noqa: BLE001
        result["implementationCheck"]["imported"] = False
        result["implementationCheck"]["error"] = str(error)
        result["notes"].append(
            "onepochi_ue.textures を import できなかった（sys.path に "
            "tools/ue_plugin を足してから呼ぶ）: {0}".format(error))
        return result

    result["implementationCheck"]["imported"] = True
    resolved = {}
    for canonical, candidates in sorted(impl.COMPRESSION_ALIASES.items()):
        hit = None
        for candidate in candidates:
            if enum is not None and _has(enum, candidate):
                hit = candidate
                break
        resolved[canonical] = {"resolvedAs": hit, "candidates": list(candidates)}
        if hit is None:
            result["notes"].append(
                "COMPRESSION_ALIASES['{0}'] がどの候補でも解決しない: {1}".format(
                    canonical, list(candidates)))
    result["implementationCheck"]["compressionAliases"] = resolved

    # サフィックス表の全要素が実機で値を引けるか（0 を「取れない」と誤読しない）
    per_suffix = {}
    for suffix, settings in sorted(impl.TEXTURE_SETTINGS.items()):
        sink = []
        value, name = impl.compression_value(settings["compression"], sink)
        per_suffix[suffix] = {"compression": settings["compression"],
                              "srgb": settings["srgb"],
                              "resolvedEnum": name,
                              # **`int(enum)` は TypeError になる**（実測）。
                              # UE の enum は `.value` で整数を持つ
                              "value": _enum_value(value),
                              "warnings": sink}
    result["implementationCheck"]["perSuffix"] = per_suffix

    if texture is not None:
        srgb_name = None
        for name in impl._SRGB_NAMES:          # noqa: SLF001 - プローブなので可
            try:
                texture.get_editor_property(name)
            except Exception:  # noqa: BLE001
                continue
            srgb_name = name
            break
        compression_name = None
        for name in impl._COMPRESSION_NAMES:   # noqa: SLF001
            try:
                texture.get_editor_property(name)
            except Exception:  # noqa: BLE001
                continue
            compression_name = name
            break
        result["implementationCheck"]["srgbPropertyResolvedAs"] = srgb_name
        result["implementationCheck"]["compressionPropertyResolvedAs"] = \
            compression_name
        if srgb_name is None or compression_name is None:
            result["notes"].append(
                "textures.py の _SRGB_NAMES / _COMPRESSION_NAMES が解決しない。"
                "loadedTexture.properties を見て候補を直す")

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
    print("[OnePochi] テクスチャ API プローブ結果:")
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
