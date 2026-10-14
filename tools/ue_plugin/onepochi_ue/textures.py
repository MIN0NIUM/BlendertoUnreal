"""テクスチャの取り込みと設定（P5-3 / P5-4）。

`materialSlots[].textures`（サフィックス -> `source.textureFolder` からの相対パス）を
読み、UE 側へ取り込んで**サフィックスから圧縮設定 / sRGB を自動決定**する。
対応表は [docs/02_仕様書.md §7.3](../../../docs/02_仕様書.md) が唯一の出所。

層の分け方（単体テストできるようにするため）:

    純粋ロジック（`unreal` 不要）   TEXTURE_SETTINGS / settings_for /
                                    asset_name_for / naming_warnings /
                                    texture_folder_of / plan_imports
    UE 依存                         import_textures / apply_texture_settings

**既知の落とし穴への対応**:

* **P-6**: `AssetImportTask` 経由の Interchange は非同期で生成物が取れない。
  ここでは `InterchangeManager.import_asset`（同期）だけを使う
* **P-10**: 生成したアセットは**保存しないと参照が切れる**。取り込み直後に保存する
* **P-15**: enum 名 / プロパティ名は推測で断定しない。**候補名を順に試し**、
  どれが通ったかを記録する（`unreal.TextureCompressionSettings` は要検証）
* **P-18**: `void` を返す API（`post_edit_change` など）は戻り値で成否を判定しない
* **P-23**: 「設定したつもりで効いていない」は戻り値では分からない。
  書いたあと **`verify_texture_settings` で読み直して実測する**

**要検証（実機プローブ `tools/ue_plugin/probe_texture_api.py` で確定させる）**:

* `unreal.TextureCompressionSettings` の enum 名（`TC_DEFAULT` / `TC_NORMALMAP` / `TC_MASKS`）
* `Texture2D` の `compression_settings` / `srgb` というプロパティ名
* テクスチャ単体を `InterchangeManager.import_asset` に渡せるか（パイプラインの既定）
"""

import os
import re

try:  # 単体テストは `unreal` 無しで動かす（UE 外で import されうる）
    import unreal
except ImportError:  # pragma: no cover - UE 内では常に成功する
    unreal = None


#: `source.textureFolder` の既定値（docs/03 §5）
DEFAULT_TEXTURE_FOLDER = "textures"

#: Texture アセットのプレフィックス（docs/04 §1）
TEXTURE_PREFIX = "T_"

#: UE アセット名として許される形（docs/04 §1.1）
_ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

#: サフィックス -> 圧縮設定 / sRGB（**docs/02 §7.3 の表がこの辞書の出所**）
#:
#:   `_BC` / `_D` / `_E`  -> Default   / sRGB ON
#:   `_N`                 -> Normalmap / sRGB OFF
#:   それ以外             -> Masks     / sRGB OFF
#:
#: `compression` は `unreal.TextureCompressionSettings` の enum 名（文字列）。
#: 名前は**要検証**なので `COMPRESSION_ALIASES` の候補を順に試す（P-15）。
TEXTURE_SETTINGS = {
    "_BC": {"compression": "TC_DEFAULT", "srgb": True},
    "_D": {"compression": "TC_DEFAULT", "srgb": True},
    "_N": {"compression": "TC_NORMALMAP", "srgb": False},
    "_ORM": {"compression": "TC_MASKS", "srgb": False},
    "_M": {"compression": "TC_MASKS", "srgb": False},
    "_R": {"compression": "TC_MASKS", "srgb": False},
    "_AO": {"compression": "TC_MASKS", "srgb": False},
    "_E": {"compression": "TC_DEFAULT", "srgb": True},
    "_MSK": {"compression": "TC_MASKS", "srgb": False},
}

#: 圧縮設定の enum 名の候補（先頭が第1候補）。P-15 のとおり**推測で断定しない**
COMPRESSION_ALIASES = {
    "TC_DEFAULT": ("TC_DEFAULT", "TC_Default", "DEFAULT", "Default"),
    "TC_NORMALMAP": ("TC_NORMALMAP", "TC_Normalmap", "NORMALMAP", "Normalmap"),
    "TC_MASKS": ("TC_MASKS", "TC_Masks", "MASKS", "Masks"),
}

#: `srgb` / `compression_settings` のプロパティ名の候補（P-15）
_SRGB_NAMES = ("srgb", "SRGB", "s_rgb")
_COMPRESSION_NAMES = ("compression_settings", "CompressionSettings")


# --- 純粋ロジック -------------------------------------------------------------


def settings_for(suffix):
    """サフィックスから圧縮設定 / sRGB を引く。

    未知のサフィックスは **None**（呼び側が警告を積めるようにする。
    ここで例外を投げない）。
    """
    if not suffix:
        return None
    if suffix in TEXTURE_SETTINGS:
        return TEXTURE_SETTINGS[suffix]
    normalized = str(suffix).strip().upper()
    if normalized and not normalized.startswith("_"):
        normalized = "_" + normalized
    return TEXTURE_SETTINGS.get(normalized)


def compression_candidates(compression):
    """圧縮設定の enum 名の候補を優先順に返す（P-15）。"""
    if not compression:
        return ()
    aliases = COMPRESSION_ALIASES.get(compression)
    if aliases:
        return aliases
    return (compression,)


def asset_name_for(relative_path):
    """ファイル名から拡張子を落として UE のアセット名にする。

    `textures/T_Crate_01_BC.png` -> `T_Crate_01_BC`
    **リネームはしない**（規約違反は `naming_warnings` が警告にする）。
    """
    if not relative_path:
        return ""
    text = str(relative_path).replace("\\", "/").rstrip("/")
    name = text.rsplit("/", 1)[-1]
    stem, _, extension = name.rpartition(".")
    if stem and extension:
        return stem
    return name


def naming_warnings(asset_name):
    """命名規約（docs/04 §1）に反していたら警告のリストを返す。

    **勝手にリネームしない。**アーティストの意図を壊さないため、
    警告だけ出してそのままの名前で取り込む。
    """
    problems = []
    if not asset_name:
        problems.append("テクスチャのファイル名からアセット名を決められません")
        return problems
    if not asset_name.startswith(TEXTURE_PREFIX):
        problems.append(
            "テクスチャ名 '{0}' が命名規約に反しています。"
            "Texture は '{1}' プレフィックスが必要です（docs/04 §1）".format(
                asset_name, TEXTURE_PREFIX))
    if not _ASSET_NAME_PATTERN.match(asset_name):
        problems.append(
            "テクスチャ名 '{0}' に使えない文字が含まれています。"
            "先頭は英字、使用可能文字は A-Za-z0-9_ です（docs/04 §1.1）".format(
                asset_name))
    return problems


def _manifest_dirs(manifest_path):
    """(マニフェストのあるフォルダ, 推定したアセットフォルダ) を返す。

    docs/03 §5.1 の構成ではマニフェストは `<assetFolder>/fbx/onepochi.json`。
    したがってアセットフォルダは**マニフェストの1つ上**になる。
    """
    if not manifest_path:
        base = os.path.abspath(os.getcwd())
        return base, base
    manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
    asset_dir = os.path.dirname(manifest_dir) or manifest_dir
    return manifest_dir, asset_dir


def resolve_texture_folder(texture_folder, manifest_path):
    """テクスチャフォルダを絶対パスにする。

    `source.textureFolder` は **`source.assetFolder` からの相対パス**（docs/03 §5.1）。
    `assetFolder` が無いときはマニフェストの位置から推定する
    （`manifest_io.resolve_source_file` と同じ「マニフェストを基準にする」考え方）。
    実在するフォルダを優先し、どれも無ければ第1候補を返す
    （**存在しないことは例外にしない**。`plan_imports` が `exists: False` で返す）。
    """
    folder = str(texture_folder or DEFAULT_TEXTURE_FOLDER).replace("\\", "/").strip()
    if os.path.isabs(folder):
        return os.path.normpath(folder)

    manifest_dir, asset_dir = _manifest_dirs(manifest_path)
    candidates = []
    for base in (asset_dir, manifest_dir):
        candidate = os.path.normpath(os.path.join(base, folder))
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return candidates[0]


def texture_folder_of(manifest, manifest_path):
    """マニフェストの `source` からテクスチャフォルダの絶対パスを決める。

    `source.textureFolder` が**明示的に null** なら None（テクスチャを持たない宣言）。
    """
    source = (manifest or {}).get("source") or {}
    if "textureFolder" in source and source.get("textureFolder") is None:
        return None

    folder = str(source.get("textureFolder") or DEFAULT_TEXTURE_FOLDER)
    folder = folder.replace("\\", "/").strip()
    asset_folder = str(source.get("assetFolder") or "").replace("\\", "/").strip()
    if asset_folder and not os.path.isabs(folder) and os.path.isabs(asset_folder):
        return os.path.normpath(os.path.join(asset_folder, folder))
    return resolve_texture_folder(folder, manifest_path)


def plan_imports(textures, texture_folder, manifest_path, texture_root):
    """テクスチャのインポート計画を作る。**例外は投げない。**

    textures: `{"_BC": "T_Crate_01_BC.png", ...}`（textureFolder からの相対パス）
    texture_folder: `source.textureFolder`（相対でも絶対でもよい）
    manifest_path: マニフェストの位置（相対パスの基準を決めるのに使う）
    texture_root: UE 側のテクスチャ格納パス（例 `/Game/_Dev/Props/Crate_01/Textures`）

    戻り値: 計画の配列（サフィックス順に安定ソート）

        [{"suffix", "relativePath", "sourcePath", "assetName", "destination",
          "assetPath", "settings", "exists", "warnings"}, ...]

    `exists: False` は**呼び側がエラーにできる形**にするための情報。
    `settings: None` は未知のサフィックス（警告は `warnings` に入る）。
    """
    plans = []
    if not textures or not isinstance(textures, dict):
        # 型が違うものは**例外にしない**（文字列を `sorted()` に渡すと
        # 1文字ずつ回ってしまうので、ここで落とす）
        return plans

    folder = resolve_texture_folder(texture_folder, manifest_path)
    root = str(texture_root or "").rstrip("/")

    for suffix in sorted(textures):
        relative = textures.get(suffix)
        warnings = []

        if not relative or not isinstance(relative, str):
            plans.append({
                "suffix": suffix,
                "relativePath": relative,
                "sourcePath": None,
                "assetName": "",
                "destination": root,
                "assetPath": None,
                "settings": settings_for(suffix),
                "exists": False,
                "warnings": ["materialSlots[].textures['{0}'] のパスが空です".format(suffix)],
            })
            continue

        source_path = os.path.normpath(
            os.path.join(folder, str(relative).replace("\\", "/")))
        asset_name = asset_name_for(relative)
        warnings.extend(naming_warnings(asset_name))

        settings = settings_for(suffix)
        if settings is None:
            warnings.append(
                "テクスチャサフィックス '{0}' は docs/02 §7.3 の表にありません。"
                "圧縮設定 / sRGB は UE の既定のままになります".format(suffix))

        exists = False
        try:
            exists = os.path.isfile(source_path)
        except OSError as error:  # 長すぎるパス等。例外にせず警告にする
            warnings.append("テクスチャの存在を確認できません（{0}）: {1}".format(
                type(error).__name__, source_path))

        if not exists:
            warnings.append("テクスチャのファイルが見つかりません: {0}".format(source_path))

        plans.append({
            "suffix": suffix,
            "relativePath": relative,
            "sourcePath": source_path,
            "assetName": asset_name,
            "destination": root,
            "assetPath": "{0}/{1}".format(root, asset_name) if root else asset_name,
            "settings": settings,
            "exists": exists,
            "warnings": warnings,
        })

    return plans


# --- UE 依存 ------------------------------------------------------------------


def _record(sink, label, error):
    sink.append("{0}: {1}: {2}".format(label, type(error).__name__, error))


def _attempt(sink, label, func, *args, **kwargs):
    """func を呼び、失敗したら sink に記録して None を返す。

    `postprocess.attempt` と同じ方針だが、`postprocess` は `unreal` を
    無条件に import するため、**単体テスト可能にするためここに持つ**
    （`materials` / `importer` 側からの循環 import も避けられる）。
    """
    try:
        return func(*args, **kwargs)
    except Exception as error:  # noqa: BLE001 - 失敗理由を残して完走させたい
        _record(sink, label, error)
        return None


def _set_any(sink, target, names, value, label=None):
    """候補名を順に試してプロパティを設定する。成功した名前を返す（P-15）。"""
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


def _get_any(sink, target, names, label=None):
    """候補名を順に試してプロパティを読む。戻り値: (値, 通った名前)（P-15）。"""
    label = label or (names[0] if names else "?")
    if target is None:
        return None, None
    for name in names:
        try:
            value = target.get_editor_property(name)
        except Exception:  # noqa: BLE001
            continue
        return value, name
    sink.append("プロパティを読めませんでした: {0}（候補: {1}）".format(
        label, ", ".join(names)))
    return None, None


def _load(package_path):
    if unreal is None or not package_path:
        return None
    try:
        return unreal.EditorAssetLibrary.load_asset(package_path)
    except Exception:  # noqa: BLE001
        return None


def _normalize_object_path(path):
    """オブジェクト参照からパッケージパスを取り出す。

    `importer._normalize_object_path` と同じ処理。
    `importer` を import すると循環するのでここに持つ（P-6 の戻り値の形）。

        "/Script/Engine.Texture2D'/Game/X/T_A.T_A'" -> "/Game/X/T_A"
    """
    text = str(path)
    if "'" in text:
        start = text.find("'")
        end = text.rfind("'")
        if end > start:
            text = text[start + 1:end]
    if "." in text:
        head, _, _tail = text.rpartition(".")
        return head
    return text


def compression_value(compression, sink):
    """圧縮設定の enum 値を取得する。戻り値: (値, 通った名前)。

    **`is not None` で判定すること。**`TC_Default` は値 0 なので
    真偽値で見ると「取れなかった」と誤読する。
    """
    if unreal is None:
        sink.append("unreal モジュールが無いため圧縮設定を決められません")
        return None, None
    enum = getattr(unreal, "TextureCompressionSettings", None)
    if enum is None:
        sink.append("unreal.TextureCompressionSettings が存在しません。"
                    "圧縮設定は UE の既定のままにします")
        return None, None
    candidates = compression_candidates(compression)
    for candidate in candidates:
        value = getattr(enum, candidate, None)
        if value is not None:
            return value, candidate
    sink.append("TextureCompressionSettings に '{0}' がありません"
                "（試した候補: {1}）。圧縮設定は UE の既定のままにします".format(
                    compression, ", ".join(candidates) or "(なし)"))
    return None, None


def apply_texture_settings(texture, settings, sink):
    """テクスチャの `compression_settings` / `srgb` を設定する。

    enum / プロパティ名が取れなかったら**警告を積んで続行する**（P-15）。
    戻り値: 何を設定できたかの記録辞書。
    """
    record = {"compression": None, "compressionEnum": None, "srgb": None,
              "appliedBy": {}, "postEditChange": False}
    if texture is None:
        sink.append("テクスチャを読めなかったため設定を適用できません")
        return record
    if not settings:
        return record

    value, enum_name = compression_value(settings.get("compression"), sink)
    if value is not None:
        applied = _set_any(sink, texture, _COMPRESSION_NAMES, value,
                           "compression_settings")
        if applied:
            record["compression"] = settings.get("compression")
            record["compressionEnum"] = enum_name
            record["appliedBy"]["compression"] = applied

    srgb = bool(settings.get("srgb"))
    applied = _set_any(sink, texture, _SRGB_NAMES, srgb, "srgb")
    if applied:
        record["srgb"] = srgb
        record["appliedBy"]["srgb"] = applied

    # 圧縮設定の変更をテクスチャに反映させる。
    # **`post_edit_change()` は void を返すので戻り値で成否を判定しない**（P-18）。
    if hasattr(texture, "post_edit_change"):
        _attempt(sink, "post_edit_change", texture.post_edit_change)
        record["postEditChange"] = True
    return record


def observed_texture_settings(texture, sink):
    """テクスチャの `srgb` / `compression_settings` を**読み直す**。

    `materials.observed_slot_materials` と同じ思想。
    「設定したつもりで効いていない」を戻り値では検出できない（docs/99 P-23）。
    """
    record = {"srgb": None, "compression": None, "readBy": {}}
    if texture is None:
        return record
    srgb, name = _get_any(sink, texture, _SRGB_NAMES, "srgb")
    if name:
        record["srgb"] = bool(srgb)
        record["readBy"]["srgb"] = name
    compression, name = _get_any(sink, texture, _COMPRESSION_NAMES,
                                 "compression_settings")
    if name:
        record["compression"] = compression
        record["readBy"]["compression"] = name
    return record


def jsonable(value):
    """レポートにそのまま入れられる形にする。

    `compression_settings` を読み直すと **UE の enum オブジェクト**が返る。
    これを記録に入れると `json.dump` が
    `TypeError: Object of type TextureCompressionSettings is not JSON serializable`
    で落ち、**取り込みは成功しているのにレポートだけが消える**（2026-10-14 実測）。

    enum 名（`TC_NORMALMAP` など）が取れればそれを使う。**取り方は推測しない**
    （P-15）ので、`name` が無ければ `str()` に落とす。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(value)


def verify_texture_settings(texture, settings, sink, label=""):
    """設定が**実際に効いているか**を読み直して確かめる（docs/99 P-23）。

    戻り値:

        {"srgb": 実測値, "compression": 実測値, "readBy": {...},
         "checked": ["srgb", ...], "problems": [...], "matches": bool or None}

    **`matches: None` は「確かめられなかった」**（プロパティが読めない /
    enum が取れない）。確かめられないことを `True`（合格）にはしない。
    """
    observed = observed_texture_settings(texture, sink)
    # **比較は生の値（enum）で行い、記録は JSON 安全な形にする。**
    # `observed["compression"]` は UE の enum オブジェクト（`jsonable` の docstring 参照）
    result = {"srgb": observed["srgb"],
              "compression": jsonable(observed["compression"]),
              "readBy": observed["readBy"], "checked": [], "problems": [],
              "matches": None}
    if not settings:
        return result

    if observed["readBy"].get("srgb"):
        result["checked"].append("srgb")
        expected = bool(settings.get("srgb"))
        if bool(observed["srgb"]) is not expected:
            result["problems"].append(
                "sRGB が {0} のままです（{1} にしようとしました）".format(
                    observed["srgb"], expected))

    # 期待値の enum は**ここで取り直す**。取れなかったときの警告は
    # `apply_texture_settings` が既に積んでいるので捨てる（二重警告を避ける）
    expected_value, _enum_name = compression_value(settings.get("compression"), [])
    if expected_value is not None and observed["readBy"].get("compression"):
        result["checked"].append("compression")
        if observed["compression"] != expected_value:
            result["problems"].append(
                "圧縮設定が {0} のままです（{1} にしようとしました）".format(
                    jsonable(observed["compression"]), settings.get("compression")))

    if result["checked"]:
        result["matches"] = not result["problems"]
    for problem in result["problems"]:
        sink.append("{0}{1}".format(
            "テクスチャ '{0}': ".format(label) if label else "", problem))
    return result


def _interchange_manager(sink):
    """同期インポート用の InterchangeManager を取得する（P-6）。"""
    if unreal is None or not hasattr(unreal, "InterchangeManager"):
        sink.append("unreal.InterchangeManager が存在しないため"
                    "テクスチャを取り込めません")
        return None
    getter = getattr(unreal.InterchangeManager,
                     "get_interchange_manager_scripted", None)
    if getter is None:
        sink.append("InterchangeManager.get_interchange_manager_scripted が"
                    "存在しないためテクスチャを取り込めません")
        return None
    return _attempt(sink, "get_interchange_manager_scripted", getter)


def _import_parameters(sink, replace_existing=True):
    if unreal is None or not hasattr(unreal, "ImportAssetParameters"):
        sink.append("unreal.ImportAssetParameters が存在しないため"
                    "テクスチャを取り込めません")
        return None
    parameters = _attempt(sink, "ImportAssetParameters",
                          unreal.ImportAssetParameters)
    if parameters is None:
        return None
    # 存在するプロパティだけ設定する（UE バージョンで揺れるため）
    for name, value in (("is_automated", True),
                        ("replace_existing", bool(replace_existing)),
                        ("follow_redirectors", True)):
        try:
            parameters.set_editor_property(name, value)
        except Exception:  # noqa: BLE001
            pass
    return parameters


def _pick_texture_path(paths, asset_name):
    """取り込み結果から目的のテクスチャのパッケージパスを選ぶ。"""
    for path in paths:
        if path.rsplit("/", 1)[-1] == asset_name:
            return path
    return paths[0] if paths else None


def _import_one(plan, sink, manager, replace_existing=True):
    """テクスチャ1枚を `InterchangeManager.import_asset`（同期）で取り込む。

    **`AssetImportTask` は使わない。**Interchange が非同期に走り、
    生成物が取れないため（P-6）。
    """
    source_data = None
    creator = getattr(unreal.InterchangeManager, "create_source_data", None)
    if creator is None:
        sink.append("InterchangeManager.create_source_data が存在しません")
        return None
    source_data = _attempt(sink, "create_source_data", creator, plan["sourcePath"])
    if source_data is None:
        return None

    parameters = _import_parameters(sink, replace_existing)
    if parameters is None:
        return None

    result = _attempt(sink, "InterchangeManager.import_asset({0})".format(
        plan["assetName"]), manager.import_asset, plan["destination"],
        source_data, parameters)
    if result is None:
        return None

    # P-6 実測: 戻り値は bool ではなく参照文字列の配列（型は `unreal.Array`）。
    # `isinstance(result, list)` では判定できないので反復できるかで判断する。
    paths = None
    if not isinstance(result, bool):
        try:
            paths = [_normalize_object_path(entry) for entry in result]
        except TypeError:
            paths = None

    if paths:
        picked = _pick_texture_path(paths, plan["assetName"])
        if picked:
            return picked
        sink.append("取り込み結果に '{0}' が見つかりません（生成物: {1}）".format(
            plan["assetName"], paths))

    # 戻り値から特定できなくても、期待パスを直接ロードできれば成功とみなす
    # （保存前のパッケージはアセットレジストリに載らない / P-6）
    if _load(plan["assetPath"]) is not None:
        sink.append("取り込み結果を戻り値から特定できなかったため"
                    "期待パスで解決しました: {0}".format(plan["assetPath"]))
        return plan["assetPath"]

    sink.append("テクスチャを取り込めませんでした: {0}".format(plan["sourcePath"]))
    return None


def _save(package_path, asset, sink):
    """生成したアセットを保存する（P-10: 保存しないと参照が切れる）。"""
    if unreal is None:
        return False
    saver = getattr(unreal.EditorAssetLibrary, "save_loaded_asset", None)
    if saver is None:
        sink.append("EditorAssetLibrary.save_loaded_asset が存在しないため"
                    "テクスチャを保存できません: {0}".format(package_path))
        return False
    if _attempt(sink, "save_loaded_asset({0})".format(package_path),
                saver, asset, False) is None:
        sink.append("テクスチャを保存できませんでした（参照が切れる可能性があります）"
                    ": {0}".format(package_path))
        return False
    return True


def import_textures(plans, sink, done=None, replace_existing=True):
    """計画に従ってテクスチャを1枚ずつ取り込み、サフィックス別の設定を適用する。

    plans: `plan_imports()` の戻り値
    sink: 警告の受け皿（レポートの `warnings`）
    done: 同一レポート内で**すでに取り込んだ** `{assetPath: suffix}`。
          同じテクスチャを複数スロットが参照していても二重に取り込まない

    戻り値（**1件も取り込めなくても例外にしない**。呼び側が警告にする）:

        {"paths": [...],          # 生成されたアセットのパッケージパス
         "imported": [...],       # 取り込めたものの詳細
         "missing": [...],        # ソースファイルが無かったもの
         "failed": [...],         # 取り込みに失敗したもの
         "reused": [...],         # done により再利用したもの
         "saved": n,
         "verifyFailed": n}       # 設定が効いていなかった枚数（実測 / P-23）
    """
    record = {"paths": [], "imported": [], "missing": [], "failed": [],
              "reused": [], "saved": 0, "verifyFailed": 0}
    if not plans:
        return record

    done = done if done is not None else {}

    # 警告は計画段階のものも漏らさず上流に渡す
    for plan in plans:
        for warning in plan.get("warnings") or []:
            sink.append(warning)

    for plan in plans:
        if not plan.get("exists"):
            record["missing"].append({"suffix": plan["suffix"],
                                      "sourcePath": plan.get("sourcePath")})

    # **再利用の判定は InterchangeManager を取る前に済ませる。**
    # すでに取り込んだものは UE 側の取り込み機構を一切必要としないため、
    # ここを後回しにすると「取り込み済みなのに failed」になる
    pending = []
    for plan in plans:
        if not plan.get("exists"):
            continue
        asset_path = plan.get("assetPath")
        if asset_path and asset_path in done:
            record["reused"].append({"suffix": plan["suffix"],
                                     "assetPath": asset_path})
            if asset_path not in record["paths"]:
                record["paths"].append(asset_path)
            continue
        pending.append(plan)

    if not pending:
        return record

    manager = _interchange_manager(sink)
    if manager is None:
        record["failed"].extend({"suffix": p["suffix"],
                                 "sourcePath": p["sourcePath"]} for p in pending)
        return record

    for plan in pending:
        asset_path = plan.get("assetPath")
        package_path = _import_one(plan, sink, manager, replace_existing)
        if package_path is None:
            record["failed"].append({"suffix": plan["suffix"],
                                     "sourcePath": plan["sourcePath"]})
            continue

        texture = _load(package_path)
        applied = apply_texture_settings(texture, plan.get("settings"), sink)
        # **設定を書いたら読み直して確かめる**（docs/99 P-23）。
        # `set_editor_property` の戻り値だけでは「効いていない」が分からない
        verified = verify_texture_settings(texture, plan.get("settings"), sink,
                                           label=plan.get("assetName") or "")
        if verified.get("matches") is False:
            record["verifyFailed"] += 1
        saved = _save(package_path, texture, sink) if texture is not None else False
        if saved:
            record["saved"] += 1

        record["paths"].append(package_path)
        record["imported"].append({
            "suffix": plan["suffix"],
            "assetName": plan["assetName"],
            "assetPath": package_path,
            "settings": plan.get("settings"),
            "applied": applied,
            "verified": verified,
            "saved": saved,
        })
        if asset_path:
            done[asset_path] = plan["suffix"]
        done[package_path] = plan["suffix"]

    return record


# --- 往復検証のための比較（K-8: 存在確認だけで合格にしない） -------------------


def _declared_textures(asset):
    """`materialSlots[].textures` を (スロット index, サフィックス, 相対パス) で平らにする。"""
    declared = []
    for slot in (asset.get("materialSlots") or []):
        for suffix, relative in sorted((slot.get("textures") or {}).items()):
            declared.append((slot.get("index"), suffix, relative))
    return declared


def _imported_records(material_record):
    """`material["textures"]`（スロットごとのリスト）から取り込み記録を集める。

    戻り値: (imported の辞書のリスト, reused のリスト, missing のリスト, failed のリスト)
    """
    imported, reused, missing, failed = [], [], [], []
    for slot_record in (material_record or {}).get("textures") or []:
        if not isinstance(slot_record, dict):
            continue
        block = slot_record.get("imported") or {}
        imported.extend(x for x in (block.get("imported") or []) if isinstance(x, dict))
        reused.extend(block.get("reused") or [])
        missing.extend(block.get("missing") or [])
        failed.extend(block.get("failed") or [])
    return imported, reused, missing, failed


def compare_textures(asset, material_record, material_policy=None):
    """マニフェストの宣言と UE 側の実測を突き合わせる（P5 の DoD）。

    作る項目（`comparison` に入る / `core.verify.CONDITIONAL` が拾う）:

        textureCount       宣言した枚数が取り込めたか
        textureCompression サフィックス別の圧縮設定が**実際に効いているか**
        textureSrgb        サフィックス別の sRGB が**実際に効いているか**
        textureParameters  MI のパラメータに**実際に割り当たっているか**

    **テクスチャを宣言していないアセットでは空の辞書を返す**
    （S-01〜S-10 を落とさない / `CONDITIONAL` の趣旨）。

    「確かめられなかった」を合格にはしない（docs/99 K-8 / P-23）。
    プロパティが読めなかった場合は `matches: False` にして
    `unverified` に理由を残す。
    """
    declared = _declared_textures(asset)
    if not declared:
        return {}

    comparison = {}
    imported, reused, missing, failed = _imported_records(material_record)
    policy_imported = bool(((material_record or {}).get("textures")))

    comparison["textureCount"] = {
        "declared": len(declared),
        "imported": len(imported),
        "reused": len(reused),
        "missing": len(missing),
        "failed": len(failed),
        "matches": (len(imported) + len(reused)) == len(declared),
    }
    if not policy_imported:
        comparison["textureCount"]["note"] = \
            "material.importTextures が False か、取り込み経路に入っていません"

    # --- 圧縮設定 / sRGB は `verify_texture_settings` の実測を使う ---
    compression = {"expected": {}, "actual": {}, "unverified": []}
    srgb = {"expected": {}, "actual": {}, "unverified": []}
    for record in imported:
        suffix = record.get("suffix")
        wanted = record.get("settings") or settings_for(suffix) or {}
        verified = record.get("verified") or {}
        checked = verified.get("checked") or []

        compression["expected"][suffix] = wanted.get("compression")
        compression["actual"][suffix] = verified.get("compression")
        if "compression" not in checked:
            compression["unverified"].append(suffix)

        srgb["expected"][suffix] = wanted.get("srgb")
        srgb["actual"][suffix] = verified.get("srgb")
        if "srgb" not in checked:
            srgb["unverified"].append(suffix)

    problems = []
    for record in imported:
        problems.extend((record.get("verified") or {}).get("problems") or [])

    compression["matches"] = bool(imported) and not compression["unverified"] \
        and not problems
    srgb["matches"] = bool(imported) and not srgb["unverified"] and not problems
    if problems:
        compression["problems"] = problems
        srgb["problems"] = problems
    for block in (compression, srgb):
        if block["unverified"]:
            block["note"] = ("読み取れなかったサフィックスがあるため"
                             "合格にしません（docs/99 K-8）")
    comparison["textureCompression"] = compression
    comparison["textureSrgb"] = srgb

    # --- MI のパラメータ割当（`observed_texture_parameters` の実測） ---
    # `slot_record["assigned"]` は**件数（int）**、`observed` は
    # `{パラメータ名: テクスチャのパス or None}`（materials.py の実装）。
    # 期待するパラメータ名はマニフェストの宣言 × `textureParameterMap` から作る
    parameter_map = (material_policy or {}).get("textureParameterMap") or {}
    expected_parameters, unmapped = [], []
    for _index, suffix, _relative in declared:
        parameter = parameter_map.get(suffix)
        if not parameter:
            if suffix not in unmapped:
                unmapped.append(suffix)
            continue
        if parameter not in expected_parameters:
            expected_parameters.append(parameter)

    observed_parameters, unassigned, assigned_count = {}, [], 0
    for slot_record in (material_record or {}).get("textures") or []:
        if not isinstance(slot_record, dict):
            continue
        observed_parameters.update(slot_record.get("observed") or {})
        unassigned.extend(slot_record.get("unassigned") or [])
        count = slot_record.get("assigned")
        if isinstance(count, int):
            assigned_count += count

    # 値が None のキーは「読めたが何も乗っていない」。乗っているものだけ数える
    live = sorted(name for name, path in observed_parameters.items() if path)
    entry = {
        "expected": sorted(expected_parameters),
        "observed": live,
        "assignedCount": assigned_count,
        "unassigned": unassigned,
        # 読み取り API が無い UE では `observed` が空になる。
        # 空は「割当なし」ではなく**「確かめられなかった」**なので合格にしない
        "matches": bool(expected_parameters) and not unassigned
        and all(name in live for name in expected_parameters),
    }
    if unmapped:
        entry["unmappedSuffixes"] = unmapped
        entry["matches"] = False
        entry["note"] = ("textureParameterMap に対応が無いサフィックスがあります: "
                         "{0}".format(", ".join(unmapped)))
    elif not observed_parameters:
        entry["note"] = \
            "MI のパラメータを読み直せませんでした（確かめられないので合格にしません）"
    comparison["textureParameters"] = entry
    return comparison
