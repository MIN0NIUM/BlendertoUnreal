# CLAUDE.md — ワンポチツール開発ガイド

このファイルは Claude Code が本プロジェクトで作業する際の前提を記録したものです。

## プロジェクト概要

DCCツール（主に Blender）から Unreal Engine 5.8 へ Static Mesh / Skeletal Mesh を
ワンボタンで取り込むパイプラインツール「ワンポチツール」の設計・実装。

**スコープ外**: UE 上での GC 化、キャラクターセットアップ（ABP / Control Rig / IK Rig / Retarget）、
アニメーションのオーサリング、レベル配置。

## まず読むもの

1. **`docs/99_作業ルール.md` — 禁止事項と既知の落とし穴。最初に必ず読む**
2. `README.md` — フォルダ構成と現在のステータス
3. `docs/90_オープン課題.md` — 決定事項一覧と残課題
4. `docs/02_仕様書.md` — 仕様の中心

## 絶対禁止（詳細は docs/99）

- `git init` / `git commit` / `git push` / `gh pr create` を実行しない。**Git 操作はすべてユーザーが行う**
- ユーザーが開いている Blender セッションを勝手に変更・保存しない（`_for_cli` バリアントを使う）
- 同じタスクで **3回失敗したら中断**し、原因調査をサブエージェントに投げる（テキストベースのみ、スクリーンショット禁止）

## 作業ルール

- ドキュメントは `docs/` 配下に番号付きで管理する。新規追加時は `README.md` のフォルダ構成表も更新する
- 仕様を変更したら、該当ドキュメントの「最終更新」日付とバージョンを更新する
- **UE 5.8 固有の API / 挙動は推測で断定しない。**未検証のものは本文に「要検証」と明記する
- マニフェストの構造を変更したら `schema/onepochi_manifest.schema.json` も必ず同時に更新する
- UI 文言・エラーメッセージは日本語のみ
- **1KB を超える Markdown / JSON は `Write` ツールで書く**（Bash ヒアドキュメントは失敗する → docs/99 P-1）

## 設計方針（詳細は docs/02 §1.2）

| # | 方針 |
|---|---|
| D-1 | UE 側のコア処理は「マニフェストのパスを渡すと取り込む」純粋な関数。起動方法に依存させない |
| D-2 | 設定はコードに埋め込まず、プリセット + マニフェストで宣言する |
| D-3 | バリデーションは DCC 側（上流）で行う |
| D-4 | 破壊的なジオメトリ整形は複製オブジェクト上で行い、元シーンを汚さない |
| D-5 | DCC 非依存の中間層（マニフェスト）を介する |
| D-6 | UE 側の「お作法」はインポータ任せにせずポストプロセスに寄せる |

## 実装場所

- `tools/dcc_blender/` — Blender アドオン（Python / bpy）
- `tools/ue_plugin/` — UE 側 Python スクリプト（+ 必要なら C++）
- `tools/presets/` — プリセット JSON

## テストの実行（UE が起動しているかで変わる）

```bash
python tools/run_tests.py --with-ue --via-rc    # UE 起動中（推奨）
python tools/run_tests.py --with-ue             # UE を閉じている（commandlet）
python tools/run_tests.py                       # Blender の結合テストまで
python tools/run_tests.py --no-blender          # Python のみ
```

- **`--via-rc` を付け忘れると commandlet が起動して UE エディタと競合する**（docs/99 P-16）。
  UE が起動しているときは必ず付ける
- Git Bash から UE パスを引数で渡すときは **`MSYS_NO_PATHCONV=1`**（docs/99 P-4）
- 往復テスト単体: `python tools/run_roundtrip_test.py [サンプル名...] --via-rc`

## 利用可能なツール

本セッションには Blender MCP（`mcp__blender__*`）と Houdini MCP（`mcp__fxhoudini__*`）が接続されています。
Blender の API 調査には `search_api_docs` / `get_python_api_docs`、
シーン確認には `get_objects_summary` / `get_object_detail_summary` を使うと効率的です。
UE 5.8 には **Epic 公式の MCP プラグイン（Experimental）が同梱**されており、有効化すれば Claude から
エディタを直接操作できます（既定 `http://127.0.0.1:8000/mcp`）。ただしこれは **開発・検証の補助**であり、
**ワンポチツール本体の実行時依存にはしません**。本番経路は UE Python + Interchange。

詳細は `docs/91_ツールチェーン提案.md` 参照。

## 環境（確定済み）

- OS: Windows 11 / シェル: PowerShell（Bash も利用可）。`jq` は**無い**
- UE: **5.8.3**。検証プロジェクト `UEProject/OnePochiPlayground`（Blank / Blueprint、公式MCP有効）
- Blender: **5.2 LTS**
- 対象プラットフォーム: PC / PS5 / Switch / モバイル（AAA）。VR は対象外
- Git: ユーザーが管理（Claude は触らない）
