"""すべての自動テストをまとめて実行する。

    python tools/run_tests.py              # Blender 結合テストも含む（見つかれば）
    python tools/run_tests.py --no-blender # Python のみ（UE / Blender 不要）
    python tools/run_tests.py --with-ue    # UE を**起動して** E2E 往復テストも行う
                                           #   → UE エディタは閉じておくこと（P-16）
    python tools/run_tests.py --with-ue --via-rc
                                           # **起動中の UE エディタ**に Remote Control
                                           #   で依頼する。UE を閉じる必要がない
    python tools/run_tests.py --list-steps # **何も実行せず**、ステップ一覧と
                                           #   「実行する / skip する（理由つき）」を出す

内容:
    1. Blender 側コアの単体テスト（bpy 不要）
    2. バリデータ（`bl/validate.py`）の単体テスト（bpy をスタブ化して叩く）
    3. UE 側マニフェスト検証の単体テスト（unreal 不要）
    4. samples 配下の生成済みマニフェストを JSON Schema で検証
    5. Blender 実機での結合テスト（元シーンを汚さないことの担保）
       ※ blender.exe が見つかる場合のみ実行
    6. E2E 往復テスト（Blender → FBX → UE）
       ※ --with-ue を付けたときだけ実行（UE の起動に時間がかかるため）
    7. 真のワンポチ（ボタン経路）の結合テスト
       ※ --with-ue --via-rc のときだけ実行。UE が居なければ自動 skip

`--list-steps` について（M-8 / CI 整備）:
    CI に載せたとき「緑だが本当は何も検証していない」事故を防ぐための一覧表示。
    **テストは1つも実行しない。**Blender の有無は実際の検出関数（`find_blender()`）で
    判定するが、**起動はしない**（ファイルの存在確認だけ）。
    UE が実際に応答するかはここでは見ない（UE に触れずに一覧を出すため）。

BLENDER_EXE 環境変数で Blender の場所を指定できる。
"""
import glob
import os
import subprocess
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

#: 選択オブジェクト方式で回す結合テスト対象
INTEGRATION_SAMPLES = (
    "01_static_minimal",
    "02_static_collision",
    "03_static_multislot",
    "08_static_socket",
)

#: **コレクション方式**で回す結合テスト対象（M-5 / LOD）。
#: (サンプル名, アセット用コレクション名)
#:
#: LOD サンプルは選択モードだと **LOD0 しか集まらない**ため、
#: `test_integration_blender.py` に `--collection` を渡して回す。
#: これで `staticMesh.lod.levels[].triangleCount`（schema v0.3.1 / R-6）の
#: 不変条件が CI で守られる。
COLLECTION_INTEGRATION_SAMPLES = (
    ("07_static_lod_fixed", "Crate_01"),
)

#: Skeletal Mesh の結合テスト対象（P3）
SKELETAL_SAMPLES = (
    "04_skeletal_minimal",
    "05_skeletal_shared_skeleton",
    "06_skeletal_morph",
)

#: **`07_static_lod`（修正前）は未登録。**実測で LOD の表現が規約と違い
#: （オブジェクト名サフィックスであってコレクションではない）、
#: LOD1 / LOD2 の三角形数も入れ替わっている。未使用マテリアル
#: `Material` が残っていて V-003c でも止まる（→ docs/14 §9）。
#: 修正版 `07_static_lod_fixed` は上の `COLLECTION_INTEGRATION_SAMPLES` で回す。


def find_blender():
    candidate = os.environ.get("BLENDER_EXE")
    if candidate and os.path.isfile(candidate):
        return candidate
    patterns = (
        os.path.expanduser(r"~\Documents\Blender\stable\blender-*\blender.exe"),
        r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
    )
    for pattern in patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


def _step(label, command, skip_reason=None):
    """1 ステップを表す辞書。`skip_reason` が None のときだけ実行される。"""
    return {"label": label, "command": command, "skip": skip_reason}


def _reason(*parts):
    """skip 理由を結合する（None / 空は捨てる）。1つも無ければ None。"""
    kept = [p for p in parts if p]
    return " / ".join(kept) if kept else None


def _sample_blend(name):
    return os.path.join(ROOT, "samples", "blender", name, name + ".blend")


def _missing_blend_reason(name):
    if os.path.isfile(_sample_blend(name)):
        return None
    return "サンプルの .blend が見つからない（samples/blender/{0}/）".format(name)


def python_steps():
    return [
        ("Blender コア単体テスト",
         [sys.executable, os.path.join(HERE, "dcc_blender", "tests", "test_core.py")]),
        ("バリデータ単体テスト",
         [sys.executable, os.path.join(HERE, "dcc_blender", "tests",
                                       "test_validate.py")]),
        ("テクスチャ発見規則テスト",
         [sys.executable, os.path.join(HERE, "dcc_blender", "tests",
                                       "test_texture_discovery.py")]),
        ("UE マニフェスト検証テスト",
         [sys.executable, os.path.join(HERE, "ue_plugin", "tests", "test_manifest_io.py")]),
        ("Skeleton 互換判定テスト",
         [sys.executable, os.path.join(HERE, "ue_plugin", "tests",
                                       "test_skeleton_compat.py")]),
        ("再インポート設定保持テスト",
         [sys.executable, os.path.join(HERE, "ue_plugin", "tests",
                                       "test_reimport.py")]),
        ("LOD 適用ロジックテスト",
         [sys.executable, os.path.join(HERE, "ue_plugin", "tests",
                                       "test_lod.py")]),
        ("テクスチャ取り込みロジックテスト",
         [sys.executable, os.path.join(HERE, "ue_plugin", "tests",
                                       "test_textures.py")]),
        ("配布 ZIP の構造テスト",
         [sys.executable, os.path.join(HERE, "dcc_blender", "tests",
                                       "test_distribution.py")]),
        ("実行ログ保持テスト",
         [sys.executable, os.path.join(HERE, "dcc_blender", "tests",
                                       "test_runs.py")]),
        ("UE プロジェクト設定ツールのテスト",
         [sys.executable, os.path.join(HERE, "tests",
                                       "test_setup_ue_project.py")]),
        ("Remote Control 受け口テスト",
         [sys.executable, os.path.join(HERE, "ue_plugin", "tests",
                                       "test_rc_receiver.py")]),
        ("マニフェストのスキーマ検証",
         [sys.executable, os.path.join(HERE, "validate_manifest.py"), "--all"]),
    ]


def blender_steps(blender_exe, blender_skip=None):
    """Blender 実機での結合テスト。

    `blender_exe` が None のとき（= Blender が無い / `--no-blender`）でも
    **ステップ自体は組み立てる**。`--list-steps` で「何が skip されるか」を
    出すために必要なため。実行されるのは `skip` が None のものだけ。
    """
    exe = blender_exe or "blender"
    static_script = os.path.join(HERE, "dcc_blender", "tests",
                                 "test_integration_blender.py")
    skeletal_script = os.path.join(HERE, "dcc_blender", "tests",
                                   "test_integration_skeletal.py")
    steps = []
    for name in INTEGRATION_SAMPLES:
        steps.append(_step(
            "Blender 結合テスト: " + name,
            [exe, "--background", _sample_blend(name), "--python", static_script],
            _reason(blender_skip, _missing_blend_reason(name)),
        ))
    for name, collection in COLLECTION_INTEGRATION_SAMPLES:
        steps.append(_step(
            "Blender 結合テスト（コレクション方式 / LOD）: {0} [{1}]".format(
                name, collection),
            [exe, "--background", _sample_blend(name), "--python", static_script,
             "--", "--collection", collection],
            _reason(blender_skip, _missing_blend_reason(name)),
        ))
    for name in SKELETAL_SAMPLES:
        steps.append(_step(
            "スケルタル結合テスト: " + name,
            [exe, "--background", _sample_blend(name), "--python", skeletal_script],
            _reason(blender_skip, _missing_blend_reason(name)),
        ))
    return steps


#: 真のワンポチ（ボタン経路）の結合テスト対象。
#: (サンプル名, UE の出力先, プリセット, assetType)
#: **出力先は往復テストと分ける**（同じアセットを別経路で上書きしないため / P-8）
ONECLICK_SAMPLES = (
    ("01_static_minimal", "/Game/_Dev/Samples/S01_OneClick",
     "sample_debug", "StaticMesh"),
    ("04_skeletal_minimal", "/Game/_Dev/Samples/S04_OneClick",
     "character", "SkeletalMesh"),
)


def oneclick_steps(blender_exe, skip_reason=None):
    """`bpy.ops.onepochi.export()` を実際に叩く結合テスト（P2 の DoD）。

    UE が起動していない場合、テスト側が自分で skip する（終了コード 0）。
    """
    exe = blender_exe or "blender"
    script = os.path.join(HERE, "dcc_blender", "tests",
                          "test_integration_onepochi_button.py")
    steps = []
    for name, ue_path, preset, asset_type in ONECLICK_SAMPLES:
        steps.append(_step(
            "真のワンポチ（ボタン経路）: " + name,
            [exe, "--background", _sample_blend(name), "--python", script, "--",
             "--ue-path", ue_path, "--preset", preset,
             "--asset-type", asset_type],
            _reason(skip_reason, _missing_blend_reason(name)),
        ))
    return steps


def build_steps(argv):
    """オプションの組み合わせから全ステップ（skip 分を含む）を組み立てる。

    戻り値: (steps, context)。`context` は `--list-steps` の見出し用。
    **副作用は Blender の存在確認だけ**（起動もテスト実行もしない）。
    """
    no_blender = "--no-blender" in argv
    with_ue = "--with-ue" in argv
    via_rc = "--via-rc" in argv

    blender_exe = None if no_blender else find_blender()

    if no_blender:
        blender_skip = "`--no-blender` が指定された"
    elif blender_exe is None:
        blender_skip = ("Blender が見つからない"
                        "（BLENDER_EXE を設定すると実行されます）")
    else:
        blender_skip = None

    if not with_ue:
        ue_skip = "UE が必要（`--with-ue` が未指定）"
    else:
        ue_skip = None

    if ue_skip:
        rc_skip = ue_skip
    elif not via_rc:
        rc_skip = ("起動中の UE に Remote Control で依頼する経路が必要"
                   "（`--via-rc` が未指定）")
    else:
        rc_skip = None

    steps = [_step(label, command) for label, command in python_steps()]
    steps.extend(blender_steps(blender_exe, blender_skip))

    # --- E2E 往復テスト ---
    roundtrip = [sys.executable, os.path.join(HERE, "run_roundtrip_test.py")]
    if with_ue and via_rc:
        roundtrip.append("--via-rc")
        roundtrip_label = "E2E 往復テスト（起動中の UE に Remote Control で依頼）"
    else:
        roundtrip_label = "E2E 往復テスト（UE 起動 / commandlet）"
    steps.append(_step(roundtrip_label, roundtrip, ue_skip))

    # 再インポート時の設定保持（T-7 / N-2）。
    # UE 側の設定を書き換えてから再取り込みするので **RC 専用**
    steps.append(_step(
        "再インポート設定保持の実機検証（preserveSettings / override）",
        [sys.executable, os.path.join(HERE, "run_reimport_test.py"), "--via-rc"],
        rc_skip,
    ))
    # マテリアル（P5）。MI の生成と**割当の実測**まで見る
    steps.append(_step(
        "マテリアルの実機検証（マスターマテリアル / MI の生成と割当）",
        [sys.executable, os.path.join(HERE, "run_material_test.py"), "--via-rc"],
        rc_skip,
    ))
    steps.extend(oneclick_steps(blender_exe, _reason(rc_skip, blender_skip)))

    context = {
        "noBlender": no_blender,
        "withUe": with_ue,
        "viaRc": via_rc,
        "blenderExe": blender_exe,
        "blenderSkip": blender_skip,
        "ueSkip": ue_skip,
        "rcSkip": rc_skip,
    }
    return steps, context


def print_step_list(steps, context, argv):
    """`--list-steps`: **何も実行せず**に一覧と skip 理由を日本語で出す。"""
    options = [a for a in argv if a != "--list-steps"]
    print("=" * 68)
    print("テストステップ一覧（--list-steps / **何も実行しません**）")
    print("=" * 68)
    print("オプション: {0}".format(" ".join(options) if options else "（なし）"))
    if context["blenderExe"]:
        print("Blender: {0}".format(context["blenderExe"]))
    elif context["noBlender"]:
        print("Blender: 使いません（`--no-blender` が指定された）")
    else:
        print("Blender: 見つかりません"
              "（BLENDER_EXE を設定すると結合テストが実行されます）")
    if context["withUe"]:
        print("UE: 使います（{0}）".format(
            "Remote Control 経由 / `--via-rc`" if context["viaRc"]
            else "commandlet 起動 / エディタは閉じておくこと（docs/99 P-16）"))
    else:
        print("UE: 使いません（`--with-ue` が未指定）")
    print("※ UE が実際に応答するかはここでは確認しません"
          "（一覧表示で UE に触れないため）")
    print("")

    width = len(str(len(steps)))
    runnable = 0
    for index, step in enumerate(steps, start=1):
        mark = "skip" if step["skip"] else "実行"
        if not step["skip"]:
            runnable += 1
        print("{0:>{1}}. [{2}] {3}".format(index, width, mark, step["label"]))
        if step["skip"]:
            print("{0}理由: {1}".format(" " * (width + 8), step["skip"]))

    print("")
    print("-" * 68)
    print("合計 {0} ステップ / 実行 {1} / skip {2}".format(
        len(steps), runnable, len(steps) - runnable))
    if runnable == 0:
        print("**実行されるステップが 1 つもありません。**"
              "この構成では何も検証されません")
    return 0


def main(argv):
    steps, context = build_steps(argv)

    if "--list-steps" in argv:
        return print_step_list(steps, context, argv)

    if context["blenderExe"]:
        print("Blender: {0}".format(context["blenderExe"]))

    skipped = [s for s in steps if s["skip"]]
    if skipped:
        print("skip するステップ {0} 件"
              "（`--list-steps` で一覧を出せます）:".format(len(skipped)))
        for step in skipped:
            print("  - {0}\n      理由: {1}".format(step["label"], step["skip"]))

    todo = [s for s in steps if not s["skip"]]
    failures = []
    for step in todo:
        print("\n" + "=" * 68)
        print("[{0}]".format(step["label"]))
        print("=" * 68)
        result = subprocess.run(step["command"], cwd=ROOT)
        if result.returncode != 0:
            failures.append(step["label"])

    print("\n" + "=" * 68)
    if failures:
        print("失敗 {0} 件: {1}".format(len(failures), ", ".join(failures)))
        return 1
    print("{0} ステップすべて成功".format(len(todo)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
