"""マニフェスト（onepochi.json）の構築と書き出し。

schema/onepochi_manifest.schema.json v0.3.1 に対応する（`SCHEMA_VERSION` が正）。
bpy に依存しない純粋な Python。
"""

import datetime
import hashlib
import json
import os
import random

SCHEMA_VERSION = "0.3.1"
TOOL_VERSION = "0.1.0"
#: バリデーション規則のバージョン（docs/05 の見出しと一致させる）。
#: V-050 で 0.3.0、**テクスチャの発見規則（V-110〜V-114 / V-092）で 0.3.1**。
#: 食い違いは
#: `test_core.py: test_ruleset_version_matches_docs` が機械的に検出する
RULESET_VERSION = "0.3.1"

MANIFEST_FILENAME = "onepochi.json"


def make_run_id(now=None, rng=None):
    """'20261010_013000_a1b2c3' 形式の実行 ID を作る。"""
    now = now or datetime.datetime.now()
    rng = rng or random
    suffix = "".join(rng.choice("0123456789abcdef") for _ in range(6))
    return "{0}_{1}".format(now.strftime("%Y%m%d_%H%M%S"), suffix)


def asset_id(index):
    """`assets[]` の id を 0 起点の index から作る（'a001' / 'a002' ...）。

    バッチ書き出し（複数アセットを1本のマニフェストにまとめる経路）で
    **id が衝突しないこと**がこの関数の責務。単一アセットの場合も
    同じ関数を通すので、従来どおり先頭は `a001` になる。
    """
    return "a{0:03d}".format(int(index) + 1)


def iso_now(now=None):
    """ローカルタイムゾーン付きの ISO8601 文字列。"""
    now = now or datetime.datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()
    return now.isoformat(timespec="seconds")


def bone_hierarchy_hash(hierarchy):
    """[(親 or None, ボーン名), ...] -> 'sha1:<hex>'。

    トランスフォーム値は含めない（階層構造の同一性のみを見る / docs/03 §7.1）。
    """
    parts = []
    for parent, bone in hierarchy:
        parts.append("{0}>{1}".format(parent if parent else "", bone))
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return "sha1:" + digest


def blender_coordinate_system(applied_scale=100.0, conversion_done_by="fbx_exporter",
                             fbx_settings=None, unit_in_meters=1.0):
    """Blender -> UE の座標系記録ブロックを作る。

    Blender: Z-up / -Y-forward / 右手系 / 1.0 = 1m
    UE:      Z-up / +X-forward / 左手系 / 1.0 = 1cm
    """
    block = {
        "sourceUpAxis": "Z",
        "sourceForwardAxis": "-Y",
        "sourceUnitInMeters": unit_in_meters,
        "targetUpAxis": "Z",
        "targetForwardAxis": "X",
        "appliedScale": applied_scale,
        "conversionDoneBy": conversion_done_by,
    }
    if fbx_settings:
        block["fbxExportSettings"] = dict(fbx_settings)
    return block


def build_manifest(run_id, generator, coordinate_system, source, destination,
                   assets, validation_status, validation_issues,
                   preset=None, material=None, limits=None, ran_at=None):
    """マニフェスト辞書を組み立てる。

    assets は既に組み立て済みのアセット辞書のリスト。
    """
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "preset": preset,
        "generator": generator,
        "coordinateSystem": coordinate_system,
        "source": source,
        "destination": destination,
        "validation": {
            "status": validation_status,
            "ranAt": ran_at or iso_now(),
            "ruleSetVersion": RULESET_VERSION,
            "issues": validation_issues,
        },
        "assets": assets,
    }
    if material is not None:
        manifest["material"] = material
    if limits is not None:
        manifest["limits"] = limits
    return manifest


def write_manifest(folder, manifest, filename=MANIFEST_FILENAME):
    """マニフェストを UTF-8 / LF / インデント2 で書き出し、パスを返す。"""
    if not os.path.isdir(folder):
        os.makedirs(folder)
    path = os.path.join(folder, filename)
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")
    return path


def read_manifest(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# --- 軽量な構造チェック -------------------------------------------------------
#
# Blender / UE の Python には jsonschema が同梱されていないため、
# JSON Schema による完全検証は行わず「致命的な欠落」だけを自前で見る。
# 厳密な検証は開発時に外部ツールで行う（docs/99 参照）。

_REQUIRED_TOP = (
    "schemaVersion", "runId", "generator", "coordinateSystem",
    "source", "destination", "validation", "assets",
)

_REQUIRED_BY_TYPE = {
    "StaticMesh": ("id", "assetType", "name", "destinationPath", "sourceFile", "staticMesh"),
    "SkeletalMesh": ("id", "assetType", "name", "destinationPath", "sourceFile", "skeleton", "skeletalMesh"),
    "AnimationSequence": ("id", "assetType", "name", "destinationPath", "sourceFile", "animationSequence"),
}


def check_manifest_shape(manifest):
    """必須キーの欠落を日本語メッセージのリストで返す。空なら形としてはOK。"""
    problems = []
    if not isinstance(manifest, dict):
        return ["マニフェストが JSON オブジェクトではありません"]

    for key in _REQUIRED_TOP:
        if key not in manifest:
            problems.append("トップレベルに必須キー '{0}' がありません".format(key))

    version = manifest.get("schemaVersion")
    if version:
        major = str(version).split(".")[0]
        expected_major = SCHEMA_VERSION.split(".")[0]
        if major != expected_major:
            problems.append(
                "schemaVersion のメジャーバージョンが異なります（マニフェスト: {0} / ツール: {1}）".format(
                    version, SCHEMA_VERSION
                )
            )

    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        problems.append("assets が 1件以上の配列ではありません")
        return problems

    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            problems.append("assets[{0}] がオブジェクトではありません".format(index))
            continue
        asset_type = asset.get("assetType")
        required = _REQUIRED_BY_TYPE.get(asset_type)
        if required is None:
            problems.append(
                "assets[{0}] の assetType が不明です: {1}".format(index, asset_type)
            )
            continue
        for key in required:
            if key not in asset:
                problems.append(
                    "assets[{0}] ({1}) に必須キー '{2}' がありません".format(index, asset_type, key)
                )
        problems.extend(_skeleton_shape_problems(index, asset_type, asset))
    return problems


#: `skeleton` ブロックのうち、**型まで**確かめるキー。
#: ここは「必須キーがあるか」だけでは足りない。スキーマが
#: `"type": "string"`（null 非許容）なのに、収集側が
#: `None` を入れてしまう事故が起きるため（実際に起きた）
_SKELETON_REQUIRED_STRINGS = ("rootBoneName",)


def _skeleton_shape_problems(index, asset_type, asset):
    """SkeletalMesh の `skeleton` ブロックの型チェック。

    **`force=True` の最後の砦。**検証（V-061 など）は `force` で飛ばせるが、
    ここは書き出しの直前で必ず通るため、スキーマ違反のマニフェストが
    ディスクに出ることを防げる。
    """
    if asset_type != "SkeletalMesh":
        return []
    skeleton = asset.get("skeleton")
    if not isinstance(skeleton, dict):
        if skeleton is not None:
            return ["assets[{0}] の skeleton がオブジェクトではありません".format(index)]
        return []

    problems = []
    for key in _SKELETON_REQUIRED_STRINGS:
        value = skeleton.get(key)
        if not isinstance(value, str) or not value:
            problems.append(
                "assets[{0}] の skeleton.{1} が空か文字列ではありません（{2!r}）。"
                "ルートボーンが特定できていない可能性があります".format(
                    index, key, value)
            )
    return problems
