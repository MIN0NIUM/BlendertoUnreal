"""往復検証のポリシー — 「どの比較項目の一致を要求するか」。

UE 側のレポート（`ue_report.json`）の `assets[].comparison` には、
DCC 側の予測値と UE 側の実測値の突き合わせが入る。そのうち
**一致を要求してよい項目**と**記録だけの項目**をここで一元管理する。

分けて管理する理由（実測で分かったこと / docs/99）:

    - `vertexCount` は **FBX の往復で必ず増える**。UV シームと分割法線で
      頂点が割られるため（S-01: 13 → 40 / S-04: 96 → 216）。
      一致を要求すると永久に赤くなる
    - 末端ボーンの `lengthCm` は FBX が関節の位置しか持たないので
      保存されない（P-11）。そもそも比較項目に入れていない
    - `triangleCount` は Skeletal では**非同期ビルド中に 0 が返る**ことが
      あるため「要検証」扱い（docs/13 P3）

この方針をコードの2か所（往復テストとボタン経路の結合テスト）に
書くと必ずずれるので、**bpy にも unreal にも依存しないここに置く**。
"""

#: assetType -> 一致を要求する comparison のキー
REQUIRED_BY_TYPE = {
    "StaticMesh": ("dimensions", "triangleCount", "uvChannelCount",
                   "materialSlots", "collisionShapes", "sockets"),
    "SkeletalMesh": ("boneCount", "boneNames", "materialSlots", "morphTargets"),
}

#: 「その項目が出ていれば一致を要求するが、無くても落とさない」項目。
#: ソケットのトランスフォームはソケットのあるアセットでしか出ないため、
#: 必須にすると 01〜03 が落ちる。しかし出ているなら一致していなければ困る
#:
#: LOD の2項目（T-5）も条件付きにする理由:
#:   - `lodCount` は Static Mesh なら常に出る（`mode = "none"` でも 1 段を宣言する）。
#:     ただし **commandlet では `StaticMeshEditorSubsystem` が取れず LOD を適用できない**
#:     ため（docs/99 P-5）、必須にすると commandlet 経路が落ちる
#:   - `lodTriangleCountsDescending` は 2 段以上あるときだけ出る。
#:     マニフェストに LOD ごとの三角形数が無いので、宣言値との比較ではなく
#:     「段が進むごとに減っている」という自己完結した不変条件を見る
#: テクスチャの4項目（P5）も条件付き。`materialSlots[].textures` を宣言した
#: アセット（S-11）でしか出ないので、必須にすると S-01〜S-10 が落ちる。
#: 出ているなら一致していなければ困る（取り込めた枚数 / 圧縮設定 / sRGB /
#: MI のパラメータ割当）。**どれも「確かめられなかった」は不合格**にしてある
#: （`textures.compare_textures` / docs/99 K-8・P-23）
#: `lodTriangleCounts`（R-6 / schema v0.3.1）も条件付き。
#: マニフェストに `lod.levels[].triangleCount` がある**宣言値つきのアセットでだけ**出る。
#: **これを入れ忘れると、項目は作られているのに誰も見ない**状態になる
#: （2026-10-14 の実機検証で発覚。レポートには出ていたが検査されていなかった）
CONDITIONAL = ("socketTransforms", "lodCount", "lodTriangleCounts",
               "lodTriangleCountsDescending",
               "textureCount", "textureCompression", "textureSrgb",
               "textureParameters")

#: assetType が不明なときに使う既定（Static Mesh と同じ）
DEFAULT_REQUIRED = REQUIRED_BY_TYPE["StaticMesh"]

#: 記録はするが一致を要求しない項目（理由はモジュールの docstring 参照）
INFORMATIONAL = ("vertexCount",)


def required_keys(asset_type):
    """その assetType で一致を要求する比較項目を返す。"""
    return REQUIRED_BY_TYPE.get(asset_type, DEFAULT_REQUIRED)


def is_required(asset_type, key):
    return key in required_keys(asset_type)


def check_asset(asset):
    """レポートの assets[] 1件を検査する。

    戻り値: (ok, 行のリスト)。行はそのままログに出せる日本語。

    **「比較できなかった」は不合格**として扱う。一致の証拠が無いまま
    緑にすると、検証していない項目を検証済みと誤認するため。
    """
    lines = []
    ok = True
    asset_type = asset.get("assetType")
    comparison = asset.get("comparison") or {}

    for error in asset.get("errors") or []:
        ok = False
        lines.append("✖ {0}".format(error))

    for key in required_keys(asset_type):
        entry = comparison.get(key)
        if entry is None:
            ok = False
            lines.append("✖ {0}: 比較できませんでした（一致の証拠がありません）"
                         .format(key))
            continue
        matched = bool(entry.get("matches"))
        ok = ok and matched
        lines.append("{0} {1}: {2}".format(
            "✔" if matched else "✖", key,
            {k: v for k, v in entry.items() if k not in ("matches", "note")}))

    for key in CONDITIONAL:
        entry = comparison.get(key)
        if entry is None or "matches" not in entry:
            continue
        matched = bool(entry["matches"])
        ok = ok and matched
        lines.append("{0} {1}: {2}".format(
            "✔" if matched else "✖", key,
            {k: v for k, v in entry.items() if k not in ("matches", "note")}))

    for key in INFORMATIONAL:
        entry = comparison.get(key)
        if entry is not None:
            lines.append("‥ {0}（参考・一致は要求しない）: {1}".format(
                key, {k: v for k, v in entry.items() if k != "note"}))

    return ok, lines
