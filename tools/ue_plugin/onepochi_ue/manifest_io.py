"""マニフェストの読み込みと検証（UE 側 [C1]）。

UE の Python には jsonschema が無いため、ここでは「致命的な欠落」と
「座標系の矛盾」だけを自前で見る。厳密な JSON Schema 検証は
開発時に `tools/validate_manifest.py` で行う。

このモジュールは `unreal` を import しない（単体テスト可能にするため）。
"""

import json
import os

SUPPORTED_SCHEMA_MAJOR = "0"
SUPPORTED_SCHEMA_VERSION = "0.3.1"

_REQUIRED_TOP = (
    "schemaVersion", "runId", "generator", "coordinateSystem",
    "source", "destination", "validation", "assets",
)

_REQUIRED_BY_TYPE = {
    "StaticMesh": ("id", "assetType", "name", "destinationPath", "sourceFile", "staticMesh"),
    "SkeletalMesh": ("id", "assetType", "name", "destinationPath", "sourceFile",
                     "skeleton", "skeletalMesh"),
    "AnimationSequence": ("id", "assetType", "name", "destinationPath", "sourceFile",
                          "animationSequence"),
}

#: UE 側で実装済みのアセット種別
IMPLEMENTED_TYPES = ("StaticMesh", "SkeletalMesh")


class ManifestError(Exception):
    """マニフェストが受け入れられないときに投げる。"""


def load(manifest_path):
    """マニフェストを読み込んで (dict, 絶対パス) を返す。"""
    path = os.path.abspath(manifest_path)
    if not os.path.isfile(path):
        raise ManifestError("マニフェストが見つかりません: {0}".format(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError as error:
        raise ManifestError("マニフェストの JSON が壊れています: {0}".format(error))
    if not isinstance(data, dict):
        raise ManifestError("マニフェストが JSON オブジェクトではありません")
    return data, path


def check_schema_version(manifest):
    """戻り値: 警告メッセージのリスト。メジャー差異は ManifestError。"""
    version = str(manifest.get("schemaVersion", ""))
    if not version:
        raise ManifestError("schemaVersion がありません")
    major = version.split(".")[0]
    if major != SUPPORTED_SCHEMA_MAJOR:
        raise ManifestError(
            "schemaVersion のメジャーバージョンが非対応です"
            "（マニフェスト: {0} / UE 側の対応: {1}）".format(version, SUPPORTED_SCHEMA_VERSION)
        )
    if version != SUPPORTED_SCHEMA_VERSION:
        return ["schemaVersion が {0} です（UE 側の想定は {1}）。"
                "未知のフィールドは無視します".format(version, SUPPORTED_SCHEMA_VERSION)]
    return []


def check_shape(manifest):
    """必須キーの欠落を日本語メッセージのリストで返す。"""
    problems = []
    for key in _REQUIRED_TOP:
        if key not in manifest:
            problems.append("トップレベルに必須キー '{0}' がありません".format(key))

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
            problems.append("assets[{0}] の assetType が不明です: {1}".format(index, asset_type))
            continue
        for key in required:
            if key not in asset:
                problems.append("assets[{0}] ({1}) に必須キー '{2}' がありません".format(
                    index, asset_type, key))
    return problems


def check_validation_status(manifest, allow_failed=False):
    """DCC 側の検証に失敗しているマニフェストは原則拒否する。"""
    validation = manifest.get("validation") or {}
    status = validation.get("status")
    if status == "failed" and not allow_failed:
        errors = [i for i in validation.get("issues", [])
                  if i.get("severity") == "error"]
        detail = "; ".join("[{0}] {1}".format(i.get("ruleId"), i.get("message"))
                           for i in errors[:5])
        raise ManifestError(
            "DCC 側のバリデーションが失敗しているマニフェストです（status=failed）。"
            "先に DCC 側で修正してください。エラー: {0}".format(detail or "(詳細なし)")
        )
    if status not in ("passed", "passedWithWarnings", "failed"):
        return ["validation.status が不明な値です: {0}".format(status)]
    return []


def check_coordinate_system(manifest):
    """座標系の矛盾を検出する（最大の事故要因）。

    戻り値: (import_uniform_scale, 警告リスト)
    appliedScale が 100 なら UE 側の Import Uniform Scale は 1.0 でなければならない。
    """
    coordinate = manifest.get("coordinateSystem") or {}
    warnings = []

    applied = coordinate.get("appliedScale")
    if not applied or applied <= 0:
        raise ManifestError("coordinateSystem.appliedScale が不正です: {0}".format(applied))

    unit = coordinate.get("sourceUnitInMeters")
    if unit is not None and abs(unit - 1.0) > 1e-6:
        warnings.append(
            "DCC 側の 1単位が {0} m です。スケールが意図どおりか確認してください".format(unit)
        )

    # appliedScale * (UE 側の追加スケール) = 100 になるようにする
    expected_uniform_scale = 100.0 / float(applied)
    if abs(expected_uniform_scale - 1.0) > 1e-6:
        warnings.append(
            "appliedScale が {0} のため、UE 側の Import Uniform Scale を {1} にします".format(
                applied, expected_uniform_scale)
        )

    if coordinate.get("targetUpAxis") != "Z":
        warnings.append("targetUpAxis が Z ではありません: {0}".format(
            coordinate.get("targetUpAxis")))

    done_by = coordinate.get("conversionDoneBy")
    if done_by not in ("fbx_exporter", "tool", "none"):
        warnings.append("conversionDoneBy が不明な値です: {0}".format(done_by))

    return expected_uniform_scale, warnings


def resolve_source_file(manifest_path, asset):
    """assets[].sourceFile（マニフェストからの相対パス）を絶対パスにする。"""
    source = asset.get("sourceFile")
    if not source:
        raise ManifestError("assets[].sourceFile がありません: {0}".format(asset.get("name")))
    base = os.path.dirname(os.path.abspath(manifest_path))
    path = os.path.normpath(os.path.join(base, source))
    if not os.path.isfile(path):
        raise ManifestError("中間ファイルが見つかりません: {0}".format(path))
    return path


def check_destination(manifest, asset):
    """出力先がホワイトリスト配下か確認する（事故防止）。"""
    destination = manifest.get("destination") or {}
    whitelist = destination.get("pathWhitelist") or []
    path = asset.get("destinationPath")
    if not path:
        raise ManifestError("assets[].destinationPath がありません: {0}".format(asset.get("name")))
    if not path.startswith("/"):
        raise ManifestError("destinationPath が '/' で始まっていません: {0}".format(path))
    if whitelist:
        allowed = any(path == root or path.startswith(root.rstrip("/") + "/")
                      for root in whitelist)
        if not allowed:
            raise ManifestError(
                "出力先 '{0}' が許可されたルート {1} の配下ではありません。"
                "マニフェストの destination.pathWhitelist を確認してください".format(path, whitelist)
            )
    return path


def prepare(manifest_path, allow_failed=False):
    """読み込み + 全検証をまとめて行う。

    戻り値: dict(manifest, path, uniform_scale, warnings, skipped)
    """
    manifest, path = load(manifest_path)
    warnings = []
    warnings.extend(check_schema_version(manifest))

    problems = check_shape(manifest)
    if problems:
        raise ManifestError("マニフェストの構造に問題があります: " + "; ".join(problems))

    warnings.extend(check_validation_status(manifest, allow_failed))
    uniform_scale, scale_warnings = check_coordinate_system(manifest)
    warnings.extend(scale_warnings)

    skipped = []
    for asset in manifest["assets"]:
        if asset.get("assetType") not in IMPLEMENTED_TYPES:
            skipped.append(asset)
            warnings.append(
                "assetType '{0}' は UE 側で未実装のため skip します（{1}）".format(
                    asset.get("assetType"), asset.get("name"))
            )

    return {
        "manifest": manifest,
        "path": path,
        "uniformScale": uniform_scale,
        "warnings": warnings,
        "skipped": [a.get("id") for a in skipped],
    }
