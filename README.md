# ワンポチツール (OnePochi Tool)

DCCツールで作成したメッシュを **ボタン一つで Unreal Engine 5.8 の Static Mesh / Skeletal Mesh アセットとして取り込む** ためのパイプラインツール。

> **2026-10-13 現在、ボタン1回で UE にアセットが生成されるところまで実機で動いています。**
> Static Mesh / Skeletal Mesh（Skeleton の新規作成・既存流用・モーフターゲット）/
> コリジョン / ソケット / **LOD** / **再インポート時の設定保持** / **マテリアルインスタンスの自動生成**が
> 往復検証済みです。UE のメニューからも取り込めます。詳細は
> [docs/12_実装状況.md](docs/12_実装状況.md) / [docs/13_ロードマップ.md](docs/13_ロードマップ.md)。
>
> **いま止まっているもの**: UE の Remote Control の Python 実行許可が、エディタ再起動で
> 効かなくなっていました。**原因は設定の置き場所**で、`URemoteControlSettings` は
> `UCLASS(config = RemoteControl)` のため `DefaultEngine.ini` からは読まれません。
> `UEProject/OnePochiPlayground/Config/DefaultRemoteControl.ini` に置き直し済みです。
> **UE を一度再起動すれば効くはず**（→ [docs/13 §2.1](docs/13_ロードマップ.md) **A-19** / 所要 2 分。
> 確認は `python tools/check_rc_config.py`）。これが通ると UE 実機の検証が再開できます。

## スコープ

### 対象（In Scope）
- DCCツール側での書き出し（バリデーション → 中間ファイル出力）
- Unreal Engine 側でのインポート（アセット生成・インポート設定の自動適用）
- Static Mesh Asset の生成
- Skeletal Mesh Asset の生成（+ Skeleton アセット）
- LOD / コリジョン / ソケット / マテリアルスロットの取り回し
- 再インポート（更新）
- バリデーションとエラーレポート

### 対象外（Out of Scope）
- Unreal Engine 上での **GC（ジオメトリコレクション）化**
- Unreal Engine 上での **キャラクターセットアップ**（ABP/Control Rig/IK Rig/Retarget 等）
- アニメーションのオーサリング
- レベル配置・ゲームプレイロジック

> 本ツールは「DCCで書き出したデータを正しくインポートする」ところまでを責務とする。

## フォルダ構成

```
BlenderToUnreal/
├── README.md                  ← このファイル（プロジェクト索引）
├── CLAUDE.md                  Claude Code 用の前提（最初に docs/99 を読む）
├── docs/
│   ├── 01_要件定義.md           要件・ユースケース・成功基準
│   ├── 02_仕様書.md             ★ 仕様書（メイン / v0.3.1）
│   ├── 03_マニフェスト仕様.md     中間メタデータ (JSON) の仕様 — schema v0.3.1 と同期
│   ├── 04_命名規約.md            命名規約・サフィックス表
│   ├── 05_バリデーション規則.md   書き出し前チェック項目（V-001〜V-114 / ルールセット v0.3.1）
│   ├── 10_サンプル実測.md        S-01/02/03 の実測レポート（一次データ）
│   ├── 11_UE環境実測.md         UE 5.8.3 の API / Toolset 実測レポート
│   ├── 12_実装状況.md           ★ 実装したもの・使い方・動作確認記録
│   ├── 13_ロードマップ.md       ★ 全体ロードマップ（DoD 付き・進捗板）
│   ├── 14_サンプル指摘.md       ★ サンプル .blend の実測と修正依頼（S-07 の決着は §9.7）
│   ├── 20_アーティスト向け使い方.md ★ アーティスト向け手順書（スクショは A-17 待ち）
│   ├── 20261010夜間作業プラン.md  夜間作業の計画と進捗（N-1〜N-11）
│   ├── 20261013夜間作業プラン.md  夜間作業の計画と進捗（M-1〜M-9 / R-4・R-5・R-6 の実装）
│   ├── 20261014作業プラン.md      完遂までの作業計画（W-1〜W-18 / ユーザーの使用テスト UT-1〜UT-9 / マニュアル）
│   ├── 90_オープン課題.md        Q-01〜Q-19 / R-1〜R-6（解決）+ **R-7（R-7c だけ判断待ち）**
│   ├── 91_ツールチェーン提案.md   Task2: Claude/MCP/Skills 調査結果
│   ├── 92_CI案.md               GitHub Actions のたたき台（`.github/` は未作成 / 置き場所が未決）
│   ├── Remote Controlでつまったところ.md  ブログ記事の下書き素材（RC の概要・原因・対策・チェッカー）
│   ├── 98_寝る前作業.md          夜間に Claude へ投げる作業の指示書
│   └── 99_作業ルール.md          ★ 禁止事項・既知の落とし穴 P-0〜P-33（最初に読む）
├── dist/                      配布 ZIP の出力先（`build_addon_zip.py` が作る）
├── schema/
│   └── onepochi_manifest.schema.json   マニフェストの JSON Schema（v0.3.1）
├── samples/                   ※ Git 管理外（開発者の手元だけ。テクスチャに第三者素材を含む）
│   ├── README.md              用意してほしいサンプルデータの一覧
│   ├── blender/               .blend サンプル置き場
│   │                          （01/02/03/04/05/06/07/**07_fixed**/08/**10**/90）
│   │   └── 05_skeletal_shared_skeleton/
│   │       └── onepochi_asset.json   ★ アセット単位の設定（Skeleton 流用の宣言）
│   └── expected/              期待されるインポート結果（スクショ/メモ）
├── tools/
│   ├── dcc_blender/           Blender アドオン（Static Mesh / Skeletal Mesh）
│   │   ├── addons/onepochi/   ★ アドオン本体（Blender は addons/ の下しか探さない）
│   │   │   ├── core/          bpy 非依存（命名規約・プリセット・マニフェスト・UE 通信）
│   │   │   │   ├── ue_bridge.py   Remote Control API への依頼（P2）
│   │   │   │   ├── texture_discovery.py
│   │   │   │   │               ★ テクスチャの発見規則（R-4 / docs/04 §3.1）
│   │   │   │   └── verify.py      往復検証のポリシー（どの比較項目の一致を要求するか）
│   │   │   └── bl/            bpy 依存（収集・検証・整形・書き出し・UI）
│   │   │       └── skeletal.py    ボーン / スキン / Shape Key の収集（P3）
│   │   ├── bootstrap_addon.py インストール不要の読み込み
│   │   ├── run_headless.py    headless 実行（--environment / --asset-type）
│   │   └── tests/             コア 92 件 + バリデータ 104 件 + テクスチャ発見 46 件
│   │       │                  + 配布 9 件 / 実行ログ 12 件 + Blender 実機の結合テスト
│   │       ├── test_texture_discovery.py
│   │       │                  ★ 発見規則の単体テスト（R-4 / bpy 不要）
│   │       └── test_integration_onepochi_button.py
│   │                          ★ UI ボタンそのものを叩く結合テスト（P2 の DoD）
│   ├── ue_plugin/             UE 側 Python
│   │   ├── onepochi_ue/       取り込みコア（起動方法に依存しない / D-1）
│   │   │   ├── importer.py        import_from_manifest()
│   │   │   ├── skeletal.py        Skeletal 用 Interchange パイプライン + 実測
│   │   │   ├── postprocess.py     インポート後の強制適用と実測（方針 D-6）
│   │   │   ├── reimport.py    ★ 再インポート時の設定保持（T-7 / StaticMesh のみ）
│   │   │   ├── lod.py         ★ LOD の適用と実測（T-5 / GUI・RC 専用）
│   │   │   ├── textures.py    ★ テクスチャ取り込みと圧縮設定（UE 実機で検証済み）
│   │   │   ├── materials.py       マスターマテリアル / MI の生成と割当
│   │   │   ├── menu.py        ★ UE エディタのメニュー項目（P6-4）
│   │   │   └── skeleton_compat.py 既存 Skeleton との互換判定（exact / subset）
│   │   ├── onepochi_rc.py     ★ Remote Control からの受け口（P2）
│   │   ├── init_unreal.py     ★ エディタ起動時にメニューを登録する
│   │   ├── run_import.py      UE コンソール / commandlet からの起動
│   │   ├── probe_skeletal_api.py / probe_lod_api.py / probe_texture_api.py
│   │   │                      UE 側 API 名を実機確認するプローブ（読み取りのみ）
│   │   └── tests/             マニフェスト検証 21 件 / Skeleton 互換 35 件 / 受け口 10 件 /
│   │                          再インポート 36 件 / LOD 43 件 / テクスチャ 77 件
│   ├── presets/               プリセット JSON 9 種（dccLayout / environments を含む）
│   │                          `sample_texture.json` ★ テクスチャ検証用（importTextures: true）
│   ├── launch_blender.bat     アドオン読み込み済み Blender を起動
│   ├── deploy_to_ue.bat       UE プロジェクトへ配置（init_unreal.py も一緒に入る）
│   ├── build_addon_zip.py     ★ 配布 ZIP を作る（プリセット同梱 + 構造検証）
│   ├── import_via_ue_cmd.bat  UE headless インポート（**GUI を閉じてから**）
│   ├── run_tests.py           ★ テストをまとめて実行（--no-blender / --with-ue / --via-rc）
│   │                          **--list-steps** で実行 / skip とその理由を一覧できる（全 26 ステップ）
│   ├── run_roundtrip_test.py  ★ E2E 往復テスト（Blender → FBX → UE → comparison 検査）
│   ├── make_sample_lod.py     S-07 修正版 LOD サンプルの生成 / 検証（headless Blender）
│   ├── make_sample_batch.py   S-10 バッチ用サンプル（2 アセット）の生成 / 検証
│   ├── run_reimport_test.py   ★ 再インポート設定保持の実機検証（2パス / --via-rc）
│   ├── run_material_test.py   ★ マテリアル（マスター / MI）の実機検証（--via-rc）
│   ├── check_rc_config.py     ★ Remote Control 設定の診断（**UE を起動していなくても動く**）
│   │                          --ini-only で設定ファイルの点検だけ（docs/99 P-29 / A-19）
│   │                          --project "<.uproject>" で任意のプロジェクトを点検
│   ├── setup_ue_project.bat   ★ 自分の UE プロジェクトに Remote Control の設定を入れる
│   ├── setup_ue_project.py    （.uproject をドラッグ&ドロップするだけ / 既存の内容は残す /
│   │                          --dry-run で見るだけ / 書く前に .bak-日時 を残す）
│   ├── tests/                 test_setup_ue_project.py（上のツールの単体テスト 25 件）
│   └── validate_manifest.py   マニフェストの JSON Schema 検証
└── UEProject/                 ※ Git 管理外（開発者の手元だけ）。使う人は自分のプロジェクトに
    └── OnePochiPlayground/    　 setup_ue_project.bat で設定を入れる。検証用 UE プロジェクト（UE 5.8.3）
```

## 現在のステータス

| 項目 | 状態 |
|---|---|
| 決定事項 | **Q-01〜Q-19 / R-1〜R-6 すべて解決**（R-4 テクスチャの発見規則 / R-5 RC 設定の固定 / R-6 LOD 三角形数 — いずれも 2026-10-13 に実装）。**未決は R-7c（`megascan/` をリポジトリに残すか）だけ** → `docs/90_オープン課題.md` |
| 仕様書 | **v0.3.1**（実装と同期） → `docs/02_仕様書.md` |
| マニフェスト仕様 | **v0.3.1** + JSON Schema（`lod.levels[].triangleCount` を追加 / R-6） |
| 命名規約 | 承認済み（Q-10） |
| バリデーション規則 | **V-001〜V-114 / ルールセット v0.3.1**（テクスチャの V-110〜V-114 を追加） |
| サンプルDCCデータ | **01/02/03/04/05/06/08/11 + 90_broken_all + ツール生成の 07_fixed（LOD）/ 10（バッチ）が使える状態。****S-11（テクスチャ付き）も受領・検証済み**（→ `docs/14` §9.8）。お願いしているサンプルは**残っていません** → `samples/README.md` |
| Unreal プロジェクト | `UEProject/OnePochiPlayground`（UE 5.8.3）。Remote Control の設定を **`Config/DefaultRemoteControl.ini`** に固定済み。**効くかの確認は UE の再起動待ち**（→ A-19 / `docs/99` P-29。点検は `python tools/check_rc_config.py`） |
| **P1 Static Mesh** | ✅ **完了**。Blender → UE の往復が実機で成功 |
| **P2 真のワンポチ化** | ✅ **完了**。**Blender の UI ボタン1回で UE にアセットが生成される**（実機確認・回帰テスト化済み） |
| **P3 Skeletal Mesh（Blender 側）** | ✅ **完了・実機検証済み**。FBX 読み戻しで 28 / 28 / 26 チェック成功 |
| **P3 Skeletal Mesh（UE 側）** | ✅ **完了・実機検証済み**。S-04 / S-05 / S-06 の往復で `boneCount` / `boneNames` / `materialSlots` / `morphTargets` が一致。Skeleton の新規作成・既存流用・モーフターゲットすべて動作 |
| **P4 ソケット** | ✅ **完了**。S-08 で位置・回転・スケールが差分 0 |
| **P4 LOD** | ✅ **完了・実機検証済み**。`lodCount` 3 / 三角形数 **[1280, 320, 80]** が一致（`07_static_lod_fixed`）。`mode = auto` は未検証 |
| **P4 再インポート設定保持** | ✅ **完了・実機検証済み**（11 チェック全成功）。UE 側の Nanite / ライトマップ / 手で足したソケットが維持され、`override: ["nanite"]` のときだけ上書きされる。**StaticMesh のみ** |
| **P4 バッチ書き出し** | ✅ **完了**（1 回の書き出しで `assets` 2 件）。UE 実機で `/Game/_Dev/Samples/S10_Batch/` に `SM_Crate_01` と `SM_Barrel_01` が生成され、往復テストが緑（2026-10-14） |
| **P5 マテリアル** | ✅ **マスターマテリアルと MI の生成・割当が実機検証済み**（17 チェック全成功） |
| **P5 テクスチャ** | ✅ **完了**（R-4）。S-11 で `_BC` / `_N` / `_ORM` の 3 枚を発見して `materialSlots[].textures` に出力し、**UE 実機で圧縮設定・sRGB・MI への割当を読み直して確認**（`TC_DEFAULT`/ON、`TC_NORMALMAP`/OFF、`TC_MASKS`/OFF / 2026-10-14）。警告 V-113（法線が JPG）/ 情報 V-114（`_N` の色空間）も実際に発火 |
| **P6 配布 / エディタボタン** | ✅ ZIP（プリセット同梱 / 構造検証 9 件）と **UE のメニュー項目**が完了。アーティスト向け手順書も作成（スクショは A-17） |
| **P6 CI** | ✅ **完了**。`--list-steps` で「何を実行し、何を・なぜ skip するか」が日本語で出る。GitHub Actions のたたき台は `docs/92_CI案.md`（**`.github/` は未作成** ← 置き場所が未決） |
| 座標系・単位 | **実測で確定**: Blender `(x,y,z)` m → UE `(x,-y,z)` cm / `appliedScale` 100 / Import Uniform Scale 1.0 |
| テスト | **単体 514 件**（489 + UE プロジェクト設定ツール 25 件）。`--no-blender` で **13 実行 / 13 skip**、Blender 結合まで **21 実行 / 5 skip**（いずれも 2026-10-14 実測）。**`--with-ue --via-rc` は 26 実行 / 0 skip の想定**で、UE 実機込みの全体実行は設定ツール追加後に**未再測定**（追加した 1 ステップは UE 不要。直前の実測は 25 実行 / 0 skip がすべて成功 / 2026-10-14）。<br>往復テストも `--via-rc` で **10 サンプルすべて緑**。<br>**skip も数えるようにしたのが今回の要点**です。以前は「N ステップすべて成功」が**全体の何分のいくつか分からない**形でした（`--list-steps` で一覧できます） |
| 詳細 | → `docs/12_実装状況.md` / `docs/13_ロードマップ.md` |

### テストの実行

**UE エディタを起動したまま**全部回す（推奨）:

```bash
python tools/run_tests.py --with-ue --via-rc
```

Blender の結合テストまで（UE 不要）:

```bash
python tools/run_tests.py
```

Python だけ（UE / Blender 不要）:

```bash
python tools/run_tests.py --no-blender
```

> **`--via-rc` を付け忘れると commandlet が起動して UE エディタと競合します。**
> UE が起動しているときは必ず付けてください。逆に UE を閉じているときは付けません
> （→ `docs/99_作業ルール.md` P-16）。

### 往復テストだけ回す

```bash
python tools/run_roundtrip_test.py --via-rc
```

```bash
python tools/run_roundtrip_test.py 04_skeletal_minimal --via-rc
```

## 次のアクション

**ユーザーにお願いしたいこと**（詳細は `docs/13_ロードマップ.md` §2.1）

| # | 内容 | 所要 | 解消すると |
|---|---|---|---|
| **A-19** | **UE を再起動するだけ**<br>① UE エディタを終了 → ② UE を起動 → ③ `python tools/check_rc_config.py` の出力を見せてください<br>**Project Settings では何も触らないでください**（`.ini` に固定した設定が効くかを見たいので） | 2分 | **UE 実機の検証が全部再開できます。**設定の置き場所が間違っていたのが原因でした（`DefaultEngine.ini` からは読まれない）。`Config/DefaultRemoteControl.ini` に置き直し済みで、`.ini` は**起動時にしか読まれない**ため再起動が要ります（→ `docs/99` P-29）。残っているのは**バッチ書き出し / テクスチャ / LOD auto の UE 取り込み確認**です |
| ~~A-18~~ | ~~Project Settings から Remote Control の設定を入れ直す~~ | — | **A-19 に置き換えてクローズ。**入れ直しても**再起動のたびに失われる**ため、プロジェクト側の `.ini` に固定する方針に変えました |
| ~~A-16~~ | ~~テクスチャ付きサンプル S-11~~ | — | **✅ 受領・検証済み。**`_BC` / `_N` / `_ORM` の 3 枚を発見し、**UE 実機の取り込みまで緑**（→ `docs/14` §9.8） |
| A-17 | **スクリーンショット 6 枚**（`docs/20_アーティスト向け使い方.md` の `【スクショ差し込み位置】`） | 10分 | アーティスト向け手順書の完成 |
| A-14 | **ZIP でのアドオンインストール確認**（任意） | 5分 | ZIP 配布の DoD。構造の自動検証は通っています |
| A-15 | **UE のメニューを押してみる**（任意）<br>`ツール > ワンポチツール > 最後に書き出したものを取り込む` | 1分 | 見た目の確認のみ（登録と実行は自動確認済み） |
| A-10 | **S-07 原本の扱いの判断**（任意） | 判断のみ | **ブロッカーではなくなりました**（ツールが修正版を生成） |
| ~~A-11~~ | ~~UE でソケットの向きを目視確認~~ | — | **完了** |
| ~~A-12~~ | ~~UE を閉じて commandlet で全テスト~~ | — | **完了。**14 ステップすべて成功 |
| ~~A-13~~ | ~~S-07 の LOD コレクションを子にする~~ | — | **不要になりました**（修正版を自動生成） |

**判断をお願いしたいこと**（`docs/90_オープン課題.md` §3.6）

| # | 内容 |
|---|---|
| **R-7c** | **`samples/blender/11_static_texture/megascan/`（`.psd` 1 枚 + 2K マップ 9 枚 + `.fbx`）をリポジトリに残すか。**① 残す（出所が追える / **推奨**）/ ② `samples/` 外へ移す（容量を減らす）。**走査は `textures/` 直下のみで再帰しない**ので、取り込みには影響しません |
| A-10 | `samples/blender/07_static_lod/07_static_lod.blend`（原本）を修正版に置き換えるか、両方残すか（① 両方残す = 現状維持を推奨） |

> **R-4 / R-5 / R-6 は回答済みで、2026-10-13 に実装しました**（R-4 = フォルダ走査 /
> R-5 = `Config/DefaultRemoteControl.ini` に固定 / R-6 = schema v0.3.1 に `triangleCount` を追加）。
> **R-7a（テクスチャ名に `M_` を含める綴りも許すか）と R-7b（`.jpg` を許すか）は推奨案で実装済み**です
> — どちらの綴りでも通り、法線が JPG のときだけ警告（V-113）を出します。
> 違う方針がよければ言ってください（切り替えは容易です）。

**この後の作業**（完了条件は `docs/13_ロードマップ.md`）

- ~~P0 環境検証~~ / ~~P1 Static Mesh MVP~~ / ~~P2 真のワンポチ化~~ / ~~P3 Skeletal Mesh~~ → **完了**
- ~~P4 再インポート設定保持~~ / ~~P4 LOD~~ / ~~P4 ソケット~~ / ~~P4 実行ログ~~ → **完了**
- ~~P5 マスターマテリアル / MI~~ → **完了**
- ~~P6 ZIP 配布~~ / ~~P6 UE のメニューボタン~~ / ~~P6 アーティスト向け手順書（本文）~~ → **完了**
- ~~P4 バッチ書き出し~~ → **完了**（UE 実機で 2 アセット生成を確認 / 2026-10-14）
- ~~P5 テクスチャ~~ → **完了**（UE 実機で圧縮設定・sRGB・MI 割当を確認 / 2026-10-14）
- P4 LOD の `mode = auto`（実装済み・**未検証** / 任意 / `mode="auto"` のサンプルが無い）
- ~~P6 CI~~ → **完了**（`--list-steps` / `docs/92_CI案.md`。GitHub Actions への実配置はリポジトリ未決）
- プリセットの運用ルール / SkeletalMesh の再インポート設定保持 / アーティスト向け手順書のスクショ（A-17）
