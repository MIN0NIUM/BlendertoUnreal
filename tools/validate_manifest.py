"""マニフェストを JSON Schema で検証する開発用スクリプト。

Blender / UE の Python には jsonschema が無いため、開発時にこちらで検証する。

使い方:
    python tools/validate_manifest.py <onepochi.json> [...]
    python tools/validate_manifest.py --all        # samples 配下を全部
"""
import glob
import json
import os
import sys


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）でも日本語を出せるようにする。

    サブプロセスとして起動されると stdout が cp932 になり、`✔` などの文字で
    UnicodeEncodeError を出して**何も表示されずに死ぬ**。呼び出し側に
    `-X utf8` を要求せずに済むよう、ここで自衛する（docs/99 の既知の落とし穴を参照）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdout()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMA_PATH = os.path.join(ROOT, "schema", "onepochi_manifest.schema.json")


#: assetType -> スキーマ定義名。`assets[]` の oneOf を解きほぐすために使う
ASSET_BRANCHES = {
    "StaticMesh": "staticMeshAsset",
    "SkeletalMesh": "skeletalMeshAsset",
    "AnimationSequence": "animationSequenceAsset",
}


def _branch_validator(schema, definition_name, validator_class):
    """`$defs/<name>` 単体を検証するバリデータを作る。

    `$ref` を解決するために `$defs` を丸ごと持ち回る必要がある。
    """
    sub_schema = {
        "$schema": schema.get("$schema"),
        "$defs": schema.get("$defs", {}),
        "$ref": "#/$defs/{0}".format(definition_name),
    }
    return validator_class(sub_schema)


def explain(error, schema, data, validator_class):
    """エラーを (場所, メッセージ) のリストに展開する。

    `assets[]` の oneOf が落ちた場合は、assetType に対応する分岐で
    **再検証して本当の原因を特定する。**そうしないとアセット全体が
    ダンプされるだけで、どのプロパティが悪いのか分からない。
    """
    path = list(error.absolute_path)
    is_asset_oneof = (error.validator == "oneOf"
                      and len(path) == 2 and path[0] == "assets")
    if not is_asset_oneof:
        where = "/".join(str(p) for p in path) or "(root)"
        return [(where, error.message)]

    asset = data["assets"][path[1]]
    asset_type = asset.get("assetType")
    definition = ASSET_BRANCHES.get(asset_type)
    if definition is None:
        return [("assets/{0}".format(path[1]),
                 "assetType が不明です: {0!r}（対応: {1}）".format(
                     asset_type, ", ".join(sorted(ASSET_BRANCHES))))]

    branch = _branch_validator(schema, definition, validator_class)
    inner = sorted(branch.iter_errors(asset), key=lambda e: list(e.absolute_path))
    if not inner:
        # 分岐単体では通るのに oneOf が落ちるのは、複数の分岐に当てはまる場合
        return [("assets/{0}".format(path[1]),
                 "複数のアセット定義に当てはまってしまいます（assetType を確認）")]
    results = []
    for item in inner:
        where = "assets/{0}".format(path[1])
        if item.absolute_path:
            where += "/" + "/".join(str(p) for p in item.absolute_path)
        results.append((where, item.message))
    return results


def main(argv):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("jsonschema が見つかりません。pip install jsonschema を実行してください")
        return 2

    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    validator = Draft202012Validator(schema)

    if not argv or argv[0] == "--all":
        targets = sorted(glob.glob(os.path.join(ROOT, "samples", "**", "onepochi.json"),
                                   recursive=True))
    else:
        targets = argv

    if not targets:
        print("検証対象のマニフェストが見つかりません")
        return 1

    failed = 0
    for path in targets:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
        label = os.path.relpath(path, ROOT)
        if not errors:
            print("OK    {0}".format(label))
            continue
        failed += 1
        reported = []
        for error in errors:
            reported.extend(explain(error, schema, data, Draft202012Validator))
        print("NG    {0}  ({1} 件)".format(label, len(reported)))
        for where, message in reported:
            print("        {0}: {1}".format(where, message))
    print("\n{0} 件検証 / 失敗 {1} 件".format(len(targets), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
