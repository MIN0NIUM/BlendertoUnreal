"""LOD の適用と実測（UE 側 / T-5 / docs/03 §11.4 の `staticMesh.lod`）。

## このモジュールの立場

**API 名とシグネチャは実機で確定済み**（2026-10-11 / UE 5.8.3 / GUI エディタ）。
生データは `docs/tmp/lod_probe.json` と `docs/tmp/lod_sig.json`。
確定したのは下記（すべて `StaticMeshEditorSubsystem` のメソッド）:

    import_lod(base_static_mesh, lod_index, source_filename) -> int32
    set_lod_screen_sizes(static_mesh, screen_sizes: Array[float]) -> bool
    get_lod_screen_sizes(static_mesh) -> Array[float]
    set_lods(static_mesh, reduction_options: StaticMeshReductionOptions) -> int32
    get_lod_count(static_mesh) -> int32
    remove_lods(static_mesh) -> bool
    set_lod_build_settings(static_mesh, lod_index, build_options) -> None  # void

## 設計の要点（踏んだ／踏みそうな落とし穴）

- **`StaticMeshEditorSubsystem` は commandlet では `None`**（docs/99 P-5）。
  LOD の適用は GUI エディタ / Remote Control 専用になる。
  **取れないときは LOD を適用せず warning を残して完走する**（例外にしない）。
  実測のうち `lodCount` / `lodTriangleCounts` は `StaticMesh` 自身の
  `get_num_lods()` / `get_num_triangles(i)` で取るので **commandlet でも動く**
- **`import_lod` は戻り値が意味を持つ**（LOD の index）。
  `set_lod_build_settings` のような void API と混同しないこと（docs/99 P-18）。
  ここでは「負値」「要求した index と違う値」を失敗として扱う
- **`mode = "none"` でも既存の LOD を黙って消さない。**
  UE 側で手で足した LOD を壊さないため（`reimportPolicy` と同じ思想）。
  実測値を返して、宣言と食い違うときだけ warning を積む
- **LOD ごとの三角形数は schema v0.3.1 から `staticMesh.lod.levels[].triangleCount`
  に入る**（v0.3.0 には無かった。`meshStats` は `additionalProperties: false` で
  LOD0 の値しか持たない）。宣言があれば**厳密一致**で比較する。
  **無い宣言値をでっち上げて比較項目を作らない。**宣言が無いマニフェスト（v0.3.0 以前や
  LOD なし）では、代わりに「段が進むごとに三角形数が減っている」という
  **自己完結した不変条件**だけを見る
  （DCC 側の V-047 と同じ条件。往復で保たれたことの証拠になる）

## `levels[]` に LOD0 が入らない件（実測）

docs/03 §11.4 の表は「`levels[].level` は LOD0 必須・欠番はエラー」と書いているが、
**Blender 側の書き出し（`bl/pipeline.py`）は `levels[]` に level 0 を入れない。**
LOD0 は本体の FBX（`assets[].sourceFile`）として書き出され、
`levels[]` は LOD1 以降だけになる。

そのためこのモジュールは**両方の表記を受ける**:

    levels = [{"level": 1, ...}, {"level": 2, ...}]  ← Blender の実際の出力
    levels = [{"level": 0, ...}, {"level": 1, ...}]  ← docs/03 の表の読み

段数はどちらでも `max(level) + 1` で求まる。LOD0 のエントリが無いことは
**エラーにせず note にする**（実運用がその形だから）。

`unreal` が無い環境でも import できる（純粋ロジックを単体テストするため）。
"""

import os

try:
    import unreal
except ImportError:  # 単体テスト環境（UE 外）
    unreal = None

try:
    from . import postprocess
except ImportError:  # postprocess は unreal を import するので連鎖して失敗する
    postprocess = None


#: Static Mesh の LOD スロット上限（UE の `MAX_STATIC_MESH_LODS` 相当）。
#: スキーマの `autoLodCount` も 1〜8 なのでそれに合わせる。**要検証**
MAX_STATIC_MESH_LODS = 8

#: `mode = "auto"` のときの `percent_triangles` の下限。
#: 半減を繰り返すと 8 段目で 0.0078 まで落ちるため、下限で丸める
AUTO_MIN_PERCENT_TRIANGLES = 0.01

#: `staticMesh.lod.mode` の有効値（schema の enum と一致させる）
VALID_MODES = ("none", "auto", "manual")


# --- 純粋ロジック（`unreal` 非依存 / 単体テスト対象） -------------------------


def normalize_mode(lod_block):
    """`staticMesh.lod.mode` を正規化する。

    戻り値: (mode, notes)。未知の値・欠落は **例外にせず** `"none"` + warning。
    """
    block = lod_block or {}
    if not block:
        return "none", []
    raw = block.get("mode")
    if raw is None:
        return "none", ['staticMesh.lod に mode がありません。'
                        '"none" として扱い LOD は適用しません']
    if raw in VALID_MODES:
        return raw, []
    return "none", [
        'staticMesh.lod.mode が不明な値です: {0!r}。"none" として扱い LOD は'
        '適用しません（有効な値: {1}）'.format(raw, ", ".join(VALID_MODES))
    ]


def normalize_levels(lod_block):
    """`levels[]` を `level` 昇順に正規化し、欠番・重複を検出する。

    戻り値:
        {
          "levels": [{level, sourceFile, sourceCollection, sourceNode, screenSize}, ...],
          "declaredCount": int,     # LOD スロットの総段数（本体 FBX の LOD0 を含む）
          "hasLod0": bool,          # levels[] に level 0 のエントリがあったか
          "missingLevels": [int],   # 欠番
          "problems": [str],        # エラー相当
          "notes": [str],           # warning 相当
        }

    `declaredCount` は `max(level) + 1`。`levels[]` が空なら **1**
    （本体の FBX が LOD0 なので、LOD スロットは必ず 1 段以上ある）。
    """
    raw_levels = (lod_block or {}).get("levels") or []
    problems = []
    notes = []
    levels = []
    seen = set()

    if not isinstance(raw_levels, list):
        return {
            "levels": [], "declaredCount": 1, "hasLod0": False,
            "missingLevels": [],
            "problems": ["staticMesh.lod.levels が配列ではありません"],
            "notes": [],
        }

    for index, entry in enumerate(raw_levels):
        if not isinstance(entry, dict):
            problems.append(
                "staticMesh.lod.levels[{0}] がオブジェクトではありません".format(index))
            continue
        level = entry.get("level")
        if isinstance(level, bool) or not isinstance(level, int):
            problems.append(
                "staticMesh.lod.levels[{0}].level が整数ではありません: {1!r}".format(
                    index, level))
            continue
        if level < 0:
            problems.append(
                "staticMesh.lod.levels[{0}].level が負の値です: {1}".format(index, level))
            continue
        if level in seen:
            problems.append("LOD{0} が levels[] に複数回宣言されています".format(level))
            continue
        seen.add(level)

        source = entry.get("sourceFile")
        if level >= 1 and not source:
            problems.append(
                "LOD{0} に sourceFile がありません（LOD ごとに FBX が必要です）".format(level))

        levels.append({
            "level": level,
            "sourceFile": source,
            "sourceCollection": entry.get("sourceCollection"),
            "sourceNode": entry.get("sourceNode"),
            "screenSize": entry.get("screenSize"),
        })

    levels.sort(key=lambda item: item["level"])
    present = [item["level"] for item in levels]
    has_lod0 = 0 in present
    declared_count = (max(present) + 1) if present else 1

    missing = [n for n in range(1, declared_count) if n not in present]
    if missing:
        problems.append(
            "LOD に欠番があります（宣言: {0} / 欠番: {1}）".format(present, missing))

    if present and not has_lod0:
        notes.append(
            "levels[] に LOD0 のエントリがありません。本体の FBX"
            "（assets[].sourceFile）を LOD0 として扱います"
            "（Blender 側の書き出しはこの形）")

    if declared_count > MAX_STATIC_MESH_LODS:
        problems.append(
            "宣言された LOD 段数 {0} が UE の上限 {1} を超えています".format(
                declared_count, MAX_STATIC_MESH_LODS))

    return {
        "levels": levels,
        "declaredCount": declared_count,
        "hasLod0": has_lod0,
        "missingLevels": missing,
        "problems": problems,
        "notes": notes,
    }


def plan_screen_sizes(levels, total_count):
    """`set_lod_screen_sizes` に渡す配列を組み立てる。

    **全 LOD スロット（0〜total_count-1）に `screenSize` が明示されていない限り
    None を返す。**一部だけ指定された状態で残りを勝手に埋めると、
    UE の自動計算より悪い値を押し込むことになる（推測で断定しない）。

    戻り値: (sizes or None, notes)
    """
    by_level = {}
    for entry in levels or []:
        level = entry.get("level")
        if isinstance(level, bool) or not isinstance(level, int):
            continue
        value = entry.get("screenSize")
        if value is not None:
            by_level[level] = value

    slots = list(range(total_count)) if total_count and total_count > 0 else []
    if not slots:
        return None, []

    specified = [n for n in slots if n in by_level]
    missing = [n for n in slots if n not in by_level]

    if not specified:
        return None, [
            "levels[].screenSize が1つも指定されていないため、LOD の画面占有率は"
            "UE の自動計算に任せます（set_lod_screen_sizes は呼びません）"
        ]
    if missing:
        return None, [
            "LOD {0} に screenSize がないため set_lod_screen_sizes を呼びません"
            "（UE の自動計算に任せます）。手動で指定するには **LOD0 を含む全段**に"
            "screenSize が必要です。指定済み: LOD {1}".format(missing, specified)
        ]

    try:
        sizes = [float(by_level[n]) for n in slots]
    except (TypeError, ValueError):
        return None, [
            "levels[].screenSize に数値でない値があります: {0}".format(
                [by_level[n] for n in slots])
        ]
    return sizes, []


def auto_reduction_percentages(count):
    """`mode = "auto"` の `percent_triangles` の配列を組み立てる（純粋）。

    LOD0 は 1.0、以降は半減（0.5 / 0.25 / ...）。`AUTO_MIN_PERCENT_TRIANGLES`
    で下限を丸める。`unreal` の型には依存しないので単体テストできる。

    戻り値: (percentages, notes)。**失敗時は ([], 理由)**。
    """
    notes = []
    if count is None:
        return [], ["staticMesh.lod.autoLodCount がありません"]
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return [], ["staticMesh.lod.autoLodCount が数値ではありません: {0!r}".format(count)]
    requested = int(count)
    if requested != count:
        notes.append(
            "staticMesh.lod.autoLodCount が整数ではないため {0} に丸めました: {1!r}".format(
                requested, count))
    if requested < 1:
        return [], ["staticMesh.lod.autoLodCount が 1 未満です: {0}".format(requested)]
    if requested > MAX_STATIC_MESH_LODS:
        notes.append(
            "autoLodCount {0} が UE の上限 {1} を超えるため {1} に丸めます".format(
                requested, MAX_STATIC_MESH_LODS))
        requested = MAX_STATIC_MESH_LODS

    percentages = []
    clamped = []
    for index in range(requested):
        value = 1.0 / float(2 ** index)
        if value < AUTO_MIN_PERCENT_TRIANGLES:
            value = AUTO_MIN_PERCENT_TRIANGLES
            clamped.append(index)
        percentages.append(value)
    if clamped:
        notes.append(
            "LOD {0} の percent_triangles を下限 {1} に丸めました".format(
                clamped, AUTO_MIN_PERCENT_TRIANGLES))
    return percentages, notes


def resolve_level_source(manifest_path, source_file):
    """`levels[].sourceFile` を絶対パスにする（存在確認はしない）。

    **`manifest_io.resolve_source_file` と同じ規則**（マニフェストからの相対パス）。
    存在しないときに例外を投げるかどうかだけが違う。ここでは呼び出し側が
    「呼ばずにエラーを積む」判断をするため、解決だけして返す。
    """
    base = os.path.dirname(os.path.abspath(manifest_path))
    return os.path.normpath(os.path.join(base, source_file))


def declared_lod_count(asset):
    """マニフェストが宣言している LOD 段数。分からなければ None。

    出所:
      - `mode = "manual"` → `levels[]` から `max(level) + 1`
      - `mode = "auto"`   → `autoLodCount`（無ければ **None**。推測しない）
      - `mode = "none"`   → 1
    """
    block = (asset.get("staticMesh") or {}).get("lod") or {}
    mode, _ = normalize_mode(block)
    if mode == "manual":
        return normalize_levels(block)["declaredCount"]
    if mode == "auto":
        percentages, _ = auto_reduction_percentages(block.get("autoLodCount"))
        return len(percentages) or None
    return 1


def declared_lod_triangle_counts(asset):
    """マニフェスト側の「LOD ごとの三角形数」。無ければ None。

    **schema v0.3.1 で `levels[].triangleCount` が追加された**（R-6）。
    v0.3.0 までは置き場が無く、DCC 側のバリデータ（V-047）が LOD ごとの
    三角形数を**計算しているのにマニフェストに書いていない**状態だった。
    そのため **v0.3.0 以前のマニフェストでは引き続き None を返す**（後方互換）。

    2つの置き場を見る:
      - `staticMesh.lod.levels[].triangleCount`
      - `stats.lodTriangleCounts`
    どちらも無ければ None を返す（**無い宣言値を推測で作らない**）。
    """
    stats = asset.get("stats") or {}
    from_stats = stats.get("lodTriangleCounts")
    if isinstance(from_stats, list) and from_stats:
        return list(from_stats)

    block = (asset.get("staticMesh") or {}).get("lod") or {}
    normalized = normalize_levels(block)
    if not normalized["levels"]:
        return None
    counts = {}
    for raw in (block.get("levels") or []):
        if not isinstance(raw, dict):
            continue
        level = raw.get("level")
        value = raw.get("triangleCount")
        if isinstance(level, int) and not isinstance(level, bool) and value is not None:
            counts[level] = value
    if not counts:
        return None
    # LOD0 は本体 FBX 側（stats.triangleCount）にしか無いことがある
    if 0 not in counts and stats.get("triangleCount") is not None:
        counts[0] = stats["triangleCount"]
    total = normalized["declaredCount"]
    if any(n not in counts for n in range(total)):
        return None
    return [counts[n] for n in range(total)]


def compare_lod(asset, measured):
    """宣言と実測を突き合わせる（純粋 / `unreal` 非依存）。

    - `lodCount`: 宣言（`declared_lod_count`）と実測（`get_num_lods()`）
    - `lodTriangleCounts`: **宣言値がある場合だけ**項目を作り、**厳密一致**で比べる。
      schema v0.3.1 以降（`levels[].triangleCount`）で出るようになった。
      宣言が無いマニフェストでは従来どおり項目が出ず、下の降順チェックに落ちる
    - `lodTriangleCountsDescending`: 宣言値が不要な自己完結した不変条件。
      段が進むごとに三角形数が減っていること（DCC 側 V-047 と同じ）。
      2段以上あるときだけ項目を作る
    """
    block = (asset.get("staticMesh") or {}).get("lod") or {}
    mode, _ = normalize_mode(block)
    comparison = {}

    declared = declared_lod_count(asset)
    actual = measured.get("lodCount")
    if declared is not None and actual is not None:
        entry = {
            "mode": mode,
            "declared": declared,
            "measured": actual,
            "matches": int(declared) == int(actual),
        }
        if mode == "none":
            entry["note"] = (
                'mode = "none" は LOD 1 段を期待する。既存の LOD は自動削除しないため、'
                "UE 側に手作業の LOD があると一致しない（意図的な仕様）")
        elif mode == "auto":
            entry["note"] = (
                'mode = "auto" の段数は autoLodCount。UE 側の自動生成が'
                "要求どおりの段数になったかを見る")
        comparison["lodCount"] = entry

    triangles = measured.get("lodTriangleCounts") or []
    declared_triangles = declared_lod_triangle_counts(asset)
    if declared_triangles is not None and triangles:
        length = min(len(declared_triangles), len(triangles))
        comparison["lodTriangleCounts"] = {
            "declared": declared_triangles,
            "measured": triangles,
            "matches": (len(declared_triangles) == len(triangles)
                        and all(int(declared_triangles[i]) == int(triangles[i])
                                for i in range(length))),
        }

    usable = [t for t in triangles if isinstance(t, int) and not isinstance(t, bool)]
    if len(triangles) >= 2 and len(usable) == len(triangles):
        descending = all(usable[i] > usable[i + 1] for i in range(len(usable) - 1))
        comparison["lodTriangleCountsDescending"] = {
            "measured": triangles,
            "matches": descending,
            "note": "LOD の三角形数は段が進むごとに減っていなければならない"
                    "（DCC 側 V-047 と同じ不変条件）。宣言値が無くても成立する"
                    "自己完結したチェック。宣言値があるときは "
                    "`lodTriangleCounts` の厳密比較も別項目で行う",
        }

    return comparison


# --- UE 依存（適用） ----------------------------------------------------------

#: サブシステムが取れないときのメッセージ（docs/99 P-5）
SUBSYSTEM_UNAVAILABLE = (
    "StaticMeshEditorSubsystem が取得できないため LOD を適用できません。"
    "commandlet モード（-run=pythonscript）ではエディタサブシステムが初期化されません"
    "（docs/99 P-5）。GUI エディタまたは Remote Control 経由で実行してください"
)


def _num_lods(mesh, sink):
    """`StaticMesh.get_num_lods()`。commandlet でも動くので第一候補。"""
    if postprocess is None or not hasattr(mesh, "get_num_lods"):
        return None
    return postprocess.attempt(sink, "get_num_lods", mesh.get_num_lods)


def _subsystem_with(sink, *member_names):
    """必要なメソッドを持つ `StaticMeshEditorSubsystem` を返す。無ければ None。"""
    if postprocess is None:
        return None
    subsystem = postprocess.static_mesh_subsystem(sink)
    if subsystem is None:
        return None
    for name in member_names:
        if not hasattr(subsystem, name):
            sink.append(
                "StaticMeshEditorSubsystem に {0} がありません"
                "（この UE では使えません）".format(name))
            return None
    return subsystem


def _note(result, sink, message):
    sink.append(message)
    result["notes"].append(message)


def apply_lod(mesh, asset, manifest_path, sink):
    """`staticMesh.lod` を UE 側に適用する。

    戻り値（機械可読。レポートの `assets[].lod` に入れる）:
        {
          "mode": "none" | "auto" | "manual",
          "requested": int or None,     # 要求した LOD 段数
          "importedLevels": [int],      # import_lod が成功した LOD
          "screenSizes": [float] or None,
          "errors": [str],
          "skipped": [str],             # 「やらなかった理由」
          "notes": [str],
          "lodCountBefore": int or None,
          "lodCountAfter": int or None,
        }

    **例外を投げない。**失敗はすべて `errors` / `skipped` と `sink` に積んで完走する。
    """
    block = (asset.get("staticMesh") or {}).get("lod") or {}
    mode, mode_notes = normalize_mode(block)
    result = {
        "mode": mode,
        "declaredMode": block.get("mode"),
        "requested": None,
        "importedLevels": [],
        "screenSizes": None,
        "errors": [],
        "skipped": [],
        "notes": [],
    }
    for message in mode_notes:
        _note(result, sink, message)

    if postprocess is None or unreal is None:
        result["errors"].append(
            "unreal モジュールが無い環境では LOD を適用できません")
        return result

    result["lodCountBefore"] = _num_lods(mesh, sink)

    if mode == "none":
        _apply_none(result, sink)
    elif mode == "manual":
        _apply_manual(mesh, manifest_path, block, result, sink)
    elif mode == "auto":
        _apply_auto(mesh, block, result, sink)

    result["lodCountAfter"] = _num_lods(mesh, sink)
    return result


def _apply_none(result, sink):
    """`mode = "none"`: 何もしない。**既存の LOD を黙って消さない。**

    UE 側で手で足した LOD を壊さないため（`reimportPolicy` と同じ思想）。
    実測値だけ返し、宣言（1 段）と食い違うときは warning を積む。
    """
    result["requested"] = 1
    result["skipped"].append(
        'staticMesh.lod.mode = "none" のため LOD を適用しません')
    count = result.get("lodCountBefore")
    if isinstance(count, int) and not isinstance(count, bool) and count > 1:
        _note(result, sink,
              'staticMesh.lod.mode = "none" ですが UE 側に LOD が {0} 段あります。'
              "既存の LOD を自動では削除しません（UE 側の手作業を壊さないため / "
              "reimportPolicy と同じ思想）。意図しない LOD なら UE 側で削除してください"
              "（StaticMeshEditorSubsystem.remove_lods）".format(count))


def _apply_manual(mesh, manifest_path, block, result, sink):
    """`mode = "manual"`: `levels[]` の FBX を LOD スロットに読み込む。"""
    normalized = normalize_levels(block)
    result["requested"] = normalized["declaredCount"]
    result["hasLod0Entry"] = normalized["hasLod0"]
    result["missingLevels"] = normalized["missingLevels"]
    for problem in normalized["problems"]:
        result["errors"].append(problem)
        sink.append(problem)
    for message in normalized["notes"]:
        _note(result, sink, message)

    targets = [e for e in normalized["levels"] if e["level"] >= 1 and e["sourceFile"]]
    if any(e["level"] == 0 for e in normalized["levels"]):
        result["skipped"].append(
            "LOD0 は本体の FBX として取り込み済みのため import_lod は呼びません")

    if not normalized["levels"]:
        message = ('staticMesh.lod.mode = "manual" ですが levels[] が空です。'
                   "LOD0（本体 FBX）だけになります")
        result["skipped"].append(message)
        sink.append(message)
        return
    if not targets:
        message = ('staticMesh.lod.mode = "manual" ですが LOD1 以降の '
                   "sourceFile がありません")
        result["skipped"].append(message)
        sink.append(message)
        return

    subsystem = _subsystem_with(sink, "import_lod")
    result["subsystemAvailable"] = subsystem is not None
    if subsystem is None:
        result["skipped"].append(SUBSYSTEM_UNAVAILABLE)
        sink.append(SUBSYSTEM_UNAVAILABLE)
        return

    sources = {}
    for entry in targets:
        level = entry["level"]
        path = resolve_level_source(manifest_path, entry["sourceFile"])
        if not os.path.isfile(path):
            # **呼ばずにエラーを積む。**存在しないパスを渡すと UE 側の
            # ログだけにエラーが出て Python からは分からなくなる
            result["errors"].append(
                "LOD{0} の中間ファイルが見つかりません: {1}".format(level, path))
            continue
        sources[str(level)] = path

        # **`import_lod` は戻り値（LOD の index）が意味を持つ**（P-18 の逆）。
        # `attempt()` の None は「例外が出た」の意味なので先に分ける
        returned = postprocess.attempt(
            sink, "import_lod(LOD{0})".format(level),
            subsystem.import_lod, mesh, level, path)
        if returned is None:
            result["errors"].append(
                "LOD{0} の import_lod が例外で失敗しました: {1}".format(level, path))
            continue
        try:
            index = int(returned)
        except (TypeError, ValueError):
            result["errors"].append(
                "LOD{0} の import_lod が整数でない値を返しました: {1!r}".format(
                    level, returned))
            continue
        if index < 0:
            result["errors"].append(
                "LOD{0} の import_lod が負の値 {1} を返しました（失敗）。"
                "UE の Output Log を確認してください: {2}".format(level, index, path))
            continue
        if index != level:
            result["errors"].append(
                "LOD{0} を要求したのに import_lod が index {1} を返しました: {2}".format(
                    level, index, path))
            continue
        result["importedLevels"].append(level)

    result["sourceFiles"] = sources

    # --- 画面占有率 -----------------------------------------------------------
    count_now = _num_lods(mesh, sink)
    total = count_now if (isinstance(count_now, int)
                          and not isinstance(count_now, bool)
                          and count_now > 0) else normalized["declaredCount"]
    sizes, notes = plan_screen_sizes(normalized["levels"], total)
    for message in notes:
        _note(result, sink, message)
    if sizes is None:
        return

    subsystem = _subsystem_with(sink, "set_lod_screen_sizes")
    if subsystem is None:
        result["skipped"].append(SUBSYSTEM_UNAVAILABLE)
        return
    # `set_lod_screen_sizes` は **bool を返す**（void ではない / P-18）
    returned = postprocess.attempt(
        sink, "set_lod_screen_sizes", subsystem.set_lod_screen_sizes, mesh, sizes)
    if returned is None:
        result["errors"].append(
            "set_lod_screen_sizes が例外で失敗しました: {0}".format(sizes))
    elif not returned:
        result["errors"].append(
            "set_lod_screen_sizes が False を返しました（適用されていません）: "
            "{0}".format(sizes))
    else:
        result["screenSizes"] = sizes


def _apply_auto(mesh, block, result, sink):
    """`mode = "auto"`: `autoLodCount` 段を `set_lods` で自動生成する。"""
    percentages, notes = auto_reduction_percentages(block.get("autoLodCount"))
    if not percentages:
        for message in notes:
            result["errors"].append(message)
            sink.append(message)
        message = ('staticMesh.lod.mode = "auto" ですが段数が決まらないため '
                   "LOD を生成しません（段数を推測しません）。"
                   "プリセットに autoLodCount（1〜8）を書いてください")
        result["skipped"].append(message)
        sink.append(message)
        return
    for message in notes:
        _note(result, sink, message)

    result["requested"] = len(percentages)
    result["autoPercentTriangles"] = percentages

    subsystem = _subsystem_with(sink, "set_lods")
    result["subsystemAvailable"] = subsystem is not None
    if subsystem is None:
        result["skipped"].append(SUBSYSTEM_UNAVAILABLE)
        sink.append(SUBSYSTEM_UNAVAILABLE)
        return

    options = _build_reduction_options(percentages, sink)
    if options is None:
        result["errors"].append(
            "StaticMeshReductionOptions を組み立てられませんでした")
        return

    # `set_lods` は int32 を返す。**戻り値の意味はドキュメントに無い（要検証）**ので、
    # 「負値は失敗」だけを判定して生値をレポートに残す
    returned = postprocess.attempt(sink, "set_lods", subsystem.set_lods, mesh, options)
    if returned is None:
        result["errors"].append("set_lods が例外で失敗しました")
        return
    result["setLodsReturned"] = returned
    try:
        index = int(returned)
    except (TypeError, ValueError):
        result["errors"].append(
            "set_lods が整数でない値を返しました: {0!r}".format(returned))
        return
    if index < 0:
        result["errors"].append(
            "set_lods が負の値 {0} を返しました（失敗）。"
            "UE の Output Log を確認してください".format(index))


def _build_reduction_options(percentages, sink):
    """`StaticMeshReductionOptions` を組み立てる。失敗したら None。"""
    options = postprocess.attempt(sink, "StaticMeshReductionOptions()",
                                  unreal.StaticMeshReductionOptions) \
        if hasattr(unreal, "StaticMeshReductionOptions") else None
    if options is None:
        sink.append("unreal.StaticMeshReductionOptions がこの UE に存在しません")
        return None
    if not hasattr(unreal, "StaticMeshReductionSettings"):
        sink.append("unreal.StaticMeshReductionSettings がこの UE に存在しません")
        return None

    settings = []
    for index, percent in enumerate(percentages):
        item = postprocess.attempt(sink, "StaticMeshReductionSettings()",
                                   unreal.StaticMeshReductionSettings)
        if item is None:
            return None
        postprocess.set_property(sink, item, "percent_triangles", float(percent))
        settings.append(item)

    if not postprocess.set_property(sink, options, "reduction_settings", settings):
        return None
    # `screen_size` は指定せず UE に自動計算させる（宣言が無いものを推測しない）
    postprocess.set_property(sink, options, "auto_compute_lod_screen_size", True)
    return options


# --- UE 依存（実測） ----------------------------------------------------------


def measure_lod(mesh, sink):
    """LOD の実測。

    戻り値:
        {
          "lodCount": int,
          "lodTriangleCounts": [int],
          "lodVertexCounts": [int],
          "lodScreenSizes": [float],            # サブシステムが無ければキーを作らない
          "lodScreenSizeAutoComputed": bool,    # 取れなければキーを作らない
          "lodCountSubsystem": int,             # 同上（食い違いの検出用）
        }

    `lodCount` / 三角形数 / 頂点数は **`StaticMesh` 自身のメソッド**で取るので
    commandlet でも動く（docs/99 P-5）。サブシステムは補助。
    """
    measured = {}
    if postprocess is None:
        return measured

    count = _num_lods(mesh, sink)
    if count is not None:
        measured["lodCount"] = count

    subsystem = postprocess.static_mesh_subsystem(sink)

    # サブシステム側の値も記録し、食い違いを warning にする
    if subsystem is not None and hasattr(subsystem, "get_lod_count"):
        sub_count = postprocess.attempt(sink, "get_lod_count",
                                        subsystem.get_lod_count, mesh)
        if sub_count is not None:
            measured["lodCountSubsystem"] = sub_count
            try:
                differs = (count is not None and int(sub_count) >= 0
                           and int(sub_count) != int(count))
            except (TypeError, ValueError):
                differs = False
            if differs:
                sink.append(
                    "LOD 数が食い違っています: StaticMesh.get_num_lods() = {0} / "
                    "StaticMeshEditorSubsystem.get_lod_count() = {1}。"
                    "前者を正とします".format(count, sub_count))

    slots = count if (isinstance(count, int) and not isinstance(count, bool)
                      and count > 0) else 0
    if slots:
        triangles = []
        vertices = []
        for index in range(slots):
            if hasattr(mesh, "get_num_triangles"):
                triangles.append(postprocess.attempt(
                    sink, "get_num_triangles({0})".format(index),
                    mesh.get_num_triangles, index))
            if hasattr(mesh, "get_num_vertices"):
                vertices.append(postprocess.attempt(
                    sink, "get_num_vertices({0})".format(index),
                    mesh.get_num_vertices, index))
        if triangles:
            measured["lodTriangleCounts"] = triangles
        if vertices:
            measured["lodVertexCounts"] = vertices

    # 画面占有率はサブシステムしか読めない。**無ければキーを作らない**
    if subsystem is not None and hasattr(subsystem, "get_lod_screen_sizes"):
        sizes = postprocess.attempt(sink, "get_lod_screen_sizes",
                                    subsystem.get_lod_screen_sizes, mesh)
        if sizes is not None:
            try:
                measured["lodScreenSizes"] = [round(float(s), 6) for s in sizes]
            except (TypeError, ValueError) as error:
                sink.append("get_lod_screen_sizes の値を読めませんでした: {0}".format(error))

    if hasattr(mesh, "is_lod_screen_size_auto_computed"):
        auto = postprocess.attempt(sink, "is_lod_screen_size_auto_computed",
                                   mesh.is_lod_screen_size_auto_computed)
        if auto is not None:
            measured["lodScreenSizeAutoComputed"] = bool(auto)

    return measured
