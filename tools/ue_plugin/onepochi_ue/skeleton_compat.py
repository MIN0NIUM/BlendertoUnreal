"""既存 Skeleton との互換判定（Q-09 / docs/02_仕様書.md §6.2）。

**`unreal` を import しない。**UE を起動せずに単体テストできるようにするため
（`manifest_io` と同じ方針）。UE から読み取ったボーン階層を引数で受け取る。

## 2つのモード（Q-09 でどちらも選べることになった）

| モード | 意味 | 想定 |
|---|---|---|
| `exact`  | ボーン名・親子関係・**並び順**まで完全一致 | 量産。事故を絶対に出したくない |
| `subset` | 取り込む側のボーンが既存 Skeleton の部分集合 | 試行錯誤。体の一部だけ差し替える |

`subset` でも **親子関係は一致していなければならない。**
名前が揃っていても親が違えば、UE 側でバインドが破綻するため。

## ハッシュについて

`bone_hierarchy_hash()` は Blender 側
（`onepochi/core/manifest.py`）と**同じアルゴリズムでなければならない。**
食い違うと「同じ階層なのに不一致」と誤判定する。
`tools/dcc_blender/tests/test_core.py::TestCrossPackageHash` が
両者の一致を機械的に検証している。
"""

import hashlib


def bone_hierarchy_hash(hierarchy):
    """[(親 or None, ボーン名), ...] -> 'sha1:<hex>'。

    **Blender 側の `manifest.bone_hierarchy_hash()` と同一実装にすること。**
    """
    parts = []
    for parent, bone in hierarchy:
        parts.append("{0}>{1}".format(parent if parent else "", bone))
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return "sha1:" + digest


def normalize_pairs(pairs):
    """[(親, 名前), ...] に正規化する。

    マニフェストから読むと `[[親, 名前], ...]`（リストのリスト）になるため、
    タプルに揃える。空文字の親は None として扱う。
    """
    normalized = []
    for entry in pairs or []:
        if isinstance(entry, dict):
            parent = entry.get("parent")
            name = entry.get("name")
        else:
            parent = entry[0] if len(entry) > 0 else None
            name = entry[1] if len(entry) > 1 else None
        if not name:
            continue
        normalized.append((parent or None, name))
    return normalized


def hierarchy_from_manifest(skeleton_block):
    """マニフェストの `skeleton` ブロックからボーン階層を取り出す。

    `boneHierarchy` があればそれを使い、無ければ `bones` から組み立てる。
    """
    block = skeleton_block or {}
    pairs = block.get("boneHierarchy")
    if pairs:
        return normalize_pairs(pairs)
    return normalize_pairs(block.get("bones"))


def _roots(pairs):
    return [name for parent, name in pairs if parent is None]


def compare_names_only(incoming, existing_names, mode="exact"):
    """既存 Skeleton の**ボーン名しか読めない**場合の互換判定。

    `USKeleton` から親子関係を読む手段（`SkeletonModifier`）が無い環境では
    名前の並びしか分からない。並びはリファレンススケルトンのインデックス順で
    **親が必ず子より前に来る**ため、並びの不一致は「階層が違う」ことの
    信頼できる否定材料になる（ただし並びの一致は階層の一致を証明しない）。

    その限界を `reasons` に明記して返す。黙って合格にしない。
    """
    incoming = normalize_pairs(incoming)
    incoming_names = [name for _parent, name in incoming]
    existing_names = [str(name) for name in existing_names or []]

    missing = [name for name in incoming_names if name not in existing_names]
    extra = [name for name in existing_names if name not in incoming_names]

    reasons = ["既存 Skeleton は**ボーン名しか読めなかった**ため、"
               "親子関係は検証できていません（要検証）"]
    compatible = True

    if not incoming_names:
        compatible = False
        reasons.append("取り込む側のボーン階層が空です")
    if not existing_names:
        compatible = False
        reasons.append("既存 Skeleton のボーン名を読み取れませんでした")

    if missing:
        compatible = False
        reasons.append(
            "既存 Skeleton に無いボーンがあります（{0}{1}）".format(
                ", ".join(missing[:8]),
                "" if len(missing) <= 8 else " ほか{0}本".format(len(missing) - 8)))

    if mode == "exact":
        if extra:
            compatible = False
            reasons.append(
                "既存 Skeleton に余分なボーンがあります（{0}{1}）。"
                "exact モードは完全一致を要求します".format(
                    ", ".join(extra[:8]),
                    "" if len(extra) <= 8 else " ほか{0}本".format(len(extra) - 8)))
        elif incoming_names != existing_names:
            compatible = False
            reasons.append(
                "ボーンの並び順が違います（exact モードでは並び順も一致が必要）")
    elif mode == "subset":
        if extra:
            reasons.append(
                "既存 Skeleton に {0} 本の余分なボーンがありますが、"
                "subset モードなので許容します".format(len(extra)))
    else:
        compatible = False
        reasons.append(
            "compatibilityMode が不正です: '{0}'（'exact' か 'subset'）".format(mode))

    return {
        "compatible": compatible,
        "mode": mode,
        "method": "namesOnly",
        "reasons": reasons,
        "incomingBoneCount": len(incoming_names),
        "existingBoneCount": len(existing_names),
        "missingInExisting": missing,
        "extraInExisting": extra,
        "parentMismatches": [],
        "orderDiffers": incoming_names != existing_names,
    }


def compare(incoming, existing, mode="exact"):
    """互換性を判定する。戻り値はレポートにそのまま載せられる dict。

    `incoming` / `existing` はどちらも [(親, 名前), ...]。
    `reasons` は**日本語**で、UI にそのまま出せる（Q-18）。
    """
    incoming = normalize_pairs(incoming)
    existing = normalize_pairs(existing)

    incoming_parents = {name: parent for parent, name in incoming}
    existing_parents = {name: parent for parent, name in existing}
    incoming_names = [name for _parent, name in incoming]
    existing_names = [name for _parent, name in existing]

    missing = [name for name in incoming_names if name not in existing_parents]
    extra = [name for name in existing_names if name not in incoming_parents]

    parent_mismatches = []
    for name in incoming_names:
        if name in missing:
            continue
        if incoming_parents[name] != existing_parents[name]:
            parent_mismatches.append({
                "bone": name,
                "incomingParent": incoming_parents[name],
                "existingParent": existing_parents[name],
            })

    # 並び順の違い（exact のときだけ問題にする）
    shared_incoming = [n for n in incoming_names if n not in missing]
    shared_existing = [n for n in existing_names if n not in extra]
    order_differs = shared_incoming != shared_existing

    incoming_hash = bone_hierarchy_hash(incoming)
    existing_hash = bone_hierarchy_hash(existing)

    reasons = []
    compatible = True

    if not incoming:
        compatible = False
        reasons.append("取り込む側のボーン階層が空です")
    if not existing:
        compatible = False
        reasons.append("既存 Skeleton のボーン階層を読み取れませんでした")

    if missing:
        compatible = False
        reasons.append(
            "既存 Skeleton に無いボーンがあります（{0}{1}）".format(
                ", ".join(missing[:8]),
                "" if len(missing) <= 8 else " ほか{0}本".format(len(missing) - 8)))

    for mismatch in parent_mismatches:
        compatible = False
        reasons.append(
            "ボーン '{0}' の親が違います（取り込む側: {1} / 既存: {2}）".format(
                mismatch["bone"],
                mismatch["incomingParent"] or "(ルート)",
                mismatch["existingParent"] or "(ルート)"))

    incoming_roots = _roots(incoming)
    existing_roots = _roots(existing)
    if incoming_roots and existing_roots and incoming_roots != existing_roots:
        compatible = False
        reasons.append(
            "ルートボーンが違います（取り込む側: {0} / 既存: {1}）".format(
                ", ".join(incoming_roots), ", ".join(existing_roots)))

    if mode == "exact":
        if extra:
            compatible = False
            reasons.append(
                "既存 Skeleton に余分なボーンがあります（{0}{1}）。"
                "exact モードは完全一致を要求します。"
                "部分集合で許したい場合は compatibilityMode を 'subset' に"
                "してください".format(
                    ", ".join(extra[:8]),
                    "" if len(extra) <= 8 else " ほか{0}本".format(len(extra) - 8)))
        if order_differs:
            compatible = False
            reasons.append(
                "ボーンの並び順が違います。exact モードでは並び順も一致が必要です")
    elif mode == "subset":
        if extra:
            reasons.append(
                "既存 Skeleton に {0} 本の余分なボーンがありますが、"
                "subset モードなので許容します".format(len(extra)))
    else:
        compatible = False
        reasons.append(
            "compatibilityMode が不正です: '{0}'（'exact' か 'subset'）".format(mode))

    if compatible and not reasons:
        reasons.append("ボーン階層は互換です（{0} 本）".format(len(incoming)))

    return {
        "compatible": compatible,
        "mode": mode,
        "reasons": reasons,
        "incomingBoneCount": len(incoming),
        "existingBoneCount": len(existing),
        "missingInExisting": missing,
        "extraInExisting": extra,
        "parentMismatches": parent_mismatches,
        "orderDiffers": order_differs,
        "incomingHash": incoming_hash,
        "existingHash": existing_hash,
        "hashMatches": incoming_hash == existing_hash,
    }
