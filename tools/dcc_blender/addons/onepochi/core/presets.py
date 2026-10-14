"""プリセット（設定ファイル）の読み込みと合成（docs/02_仕様書.md §10）。

設計方針 D-2: 設定をコードに埋め込まず、プリセット + マニフェストで宣言する。

プリセットは `extends` で継承できる。合成は辞書の深いマージ。
値の優先順位（後ろが強い）:
    project_default -> extends 連鎖 -> 当該プリセット -> onepochi_asset.json -> UI の明示指定
"""

import copy
import json
import os

PRESET_DIR_NAME = "presets"
DEFAULT_PRESET = "project_default"

#: プリセット置き場を外から差し替えるための環境変数（R-1）
PRESET_DIR_ENV = "ONEPOCHI_PRESET_DIR"

#: アセットフォルダ直下の設定ファイル名（R-1。環境変数で差し替え可）
ASSET_CONFIG_NAME = "onepochi_asset.json"
ASSET_CONFIG_NAME_ENV = "ONEPOCHI_ASSET_CONFIG_NAME"

#: DCC データフォルダの構造（docs/02 §2-3 / R-1）。プリセットで上書きできる
DEFAULT_DCC_LAYOUT = {
    "fbxSubdir": "fbx",
    "texturesSubdir": "textures",
    "substanceSubdir": "substance",
    "houdiniSubdir": "houdini",
    "manifestName": "onepochi.json",
    "reportName": "ue_report.json",
}

#: 環境（Dev / 本番）の既定値。プリセットの destination.environments で上書きできる（R-2）
DEFAULT_ENVIRONMENTS = {
    "dev": {
        "contentRoot": "/Game/_Dev",
        "pathWhitelist": ["/Game/_Dev"],
    },
    "production": {
        "contentRoot": "/Game",
        "pathWhitelist": ["/Game"],
    },
}

DEFAULT_ENVIRONMENT = "dev"

#: extends の循環を検出するための上限
_MAX_EXTENDS_DEPTH = 8


def deep_merge(base, override):
    """辞書を深くマージした新しい辞書を返す。

    - 辞書同士は再帰的にマージ
    - それ以外（リスト・スカラー）は override が丸ごと勝つ
    - override の値が None の場合も「None を明示指定した」とみなして上書きする
    """
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def preset_dir(root=None):
    """プリセットの格納ディレクトリを返す。

    優先順位:
        1. 引数 root
        2. 環境変数 ONEPOCHI_PRESET_DIR（スタジオ共有のプリセットを差し替える用）
        3. **アドオンに同梱された `onepochi/presets/`**（ZIP 配布でインストールした場合）
        4. このファイルからの相対 `tools/presets/`（リポジトリから直接動かす場合）

    R-1: プリセットの場所をコードに固定せず、外から差し替えられるようにする。

    3 が必要な理由: ZIP でインストールすると `tools/` が存在しないため、
    4 の探索は空のディレクトリを指す。**プリセットが 0 件になり書き出しができなくなる**
    （`tools/build_addon_zip.py` が `onepochi/presets/` に同梱するのと対になっている）。
    """
    if root:
        return root
    env = os.environ.get(PRESET_DIR_ENV)
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    # core -> onepochi（パッケージ直下に同梱されていればそれを使う）
    bundled = os.path.join(os.path.dirname(here), PRESET_DIR_NAME)
    if os.path.isdir(bundled):
        return bundled
    # core -> onepochi -> addons -> dcc_blender -> tools
    tools = here
    for _ in range(4):
        tools = os.path.dirname(tools)
    return os.path.join(tools, PRESET_DIR_NAME)


def list_presets(root=None):
    """利用可能なプリセット名を返す（ファイル名から拡張子を除いたもの）。"""
    directory = preset_dir(root)
    if not os.path.isdir(directory):
        return []
    names = []
    for entry in sorted(os.listdir(directory)):
        if entry.lower().endswith(".json"):
            names.append(os.path.splitext(entry)[0])
    return names


def load_preset_file(name, root=None):
    """プリセット JSON を1枚読む（extends は解決しない）。"""
    path = os.path.join(preset_dir(root), name + ".json")
    if not os.path.isfile(path):
        raise IOError("プリセットが見つかりません: {0}".format(path))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_preset(name, root=None):
    """extends を解決した最終的なプリセット辞書を返す。

    `extends` は親プリセット名。親を先に適用し、自分で上書きする。
    """
    chain = []
    current = name
    seen = set()
    for _ in range(_MAX_EXTENDS_DEPTH):
        if current is None:
            break
        if current in seen:
            raise ValueError("プリセットの extends が循環しています: {0}".format(current))
        seen.add(current)
        data = load_preset_file(current, root)
        chain.append(data)
        current = data.get("extends")
    else:
        raise ValueError("プリセットの extends が深すぎます: {0}".format(name))

    merged = {}
    for data in reversed(chain):  # 親から順に適用
        merged = deep_merge(merged, data)
    merged.pop("extends", None)
    merged["name"] = name
    return merged


def asset_config_name():
    """アセット設定ファイルの名前（環境変数で差し替え可。R-1）。"""
    return os.environ.get(ASSET_CONFIG_NAME_ENV) or ASSET_CONFIG_NAME


def load_asset_config(asset_folder):
    """アセットフォルダ直下の `onepochi_asset.json` を読む。無ければ空辞書。

    これは「人間が手で書く」設定ファイル（docs/90 §2-3）。
    """
    path = os.path.join(asset_folder, asset_config_name())
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def environments(settings):
    """利用可能な環境の辞書を返す（既定 + プリセットの上書き）。"""
    destination = settings.get("destination") or {}
    return deep_merge(DEFAULT_ENVIRONMENTS, destination.get("environments") or {})


def environment_names(settings):
    """環境名を安定した順序で返す（dev -> production -> その他をアルファベット順）。"""
    known = list(DEFAULT_ENVIRONMENTS)
    names = list(environments(settings))
    ordered = [n for n in known if n in names]
    ordered.extend(sorted(n for n in names if n not in known))
    return ordered


def apply_environment(settings, environment=None):
    """選択した環境（Dev / 本番）を `destination` に畳み込んだ設定を返す（R-2）。

    環境は `contentRoot` と `pathWhitelist` を決める。`pathTemplate` の
    `{contentRoot}` がここで決まった値に展開される。

    優先順位: 引数 environment > destination.environment > DEFAULT_ENVIRONMENT
    """
    available = environments(settings)
    destination = dict(settings.get("destination") or {})
    name = environment or destination.get("environment") or DEFAULT_ENVIRONMENT
    if name not in available:
        raise ValueError(
            "環境 '{0}' は定義されていません。使えるのは: {1}".format(
                name, ", ".join(environment_names(settings)))
        )
    resolved = dict(settings)
    destination = deep_merge(destination, available[name])
    destination["environment"] = name
    destination["environments"] = available
    resolved["destination"] = destination
    return resolved


def dcc_layout(settings):
    """DCC データフォルダの構造を返す（R-1）。プリセットの dccLayout で上書き可。"""
    return deep_merge(DEFAULT_DCC_LAYOUT, settings.get("dccLayout") or {})


def asset_subfolder(settings, asset_folder, key):
    """`fbxSubdir` などのキーからアセットフォルダ配下の絶対パスを作る。"""
    layout = dcc_layout(settings)
    subdir = layout.get(key) or DEFAULT_DCC_LAYOUT.get(key, "")
    if not asset_folder:
        return None
    return os.path.join(asset_folder, subdir) if subdir else asset_folder


def resolve_settings(preset_name, asset_folder=None, overrides=None, root=None,
                     environment=None):
    """プリセット + onepochi_asset.json + UI 指定を合成した最終設定を返す。

    最後に環境（Dev / 本番）を畳み込む。環境は後から来たものが強い:
        プリセット -> onepochi_asset.json -> overrides -> 引数 environment
    """
    settings = resolve_preset(preset_name, root)
    if asset_folder:
        settings = deep_merge(settings, load_asset_config(asset_folder))
    if overrides:
        settings = deep_merge(settings, overrides)
    return apply_environment(settings, environment)


def expand_path_template(template, **values):
    """'{contentRoot}/Props/{assetName}' のようなテンプレートを展開する。

    展開後に `//` が生じないよう正規化する（contentRoot が '/' 終わりでも安全）。
    """
    if not template:
        return template
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", str(value))
    while "//" in result:
        result = result.replace("//", "/")
    if len(result) > 1 and result.endswith("/"):
        result = result[:-1]
    return result


def resolve_destination_path(settings, asset_name, explicit=None):
    """UE の出力先パスを決める（R-1 / R-2）。

    explicit（UI やコマンドラインの明示指定）が最優先。
    次に `destination.pathTemplate` を環境の `contentRoot` で展開する。
    """
    if explicit:
        return explicit
    destination = settings.get("destination") or {}
    template = destination.get("pathTemplate")
    if template:
        return expand_path_template(
            template,
            contentRoot=destination.get("contentRoot", "/Game"),
            assetName=asset_name,
        )
    return destination.get("defaultPath")
