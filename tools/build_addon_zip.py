"""Blender アドオンの配布用 ZIP を作る（Q-15 / P6-3）。

    python tools/build_addon_zip.py
    python tools/build_addon_zip.py --out dist --verify-only dist/onepochi_addon_0.1.0.zip

ZIP の中身は Blender の「ディスクからインストール」が要求する形にする:

    onepochi/                 ← ZIP 直下にパッケージが1つだけ
    ├── __init__.py           ← bl_info を持つ
    ├── core/ ...
    ├── bl/ ...
    ├── presets/              ← ★ `tools/presets/*.json` を同梱する
    └── blender_manifest.toml ← 拡張機能（Extensions）としてのメタデータ

**プリセットを同梱する理由**: `core/presets.py` の既定の探索は
「このファイルから 4 階層上の `tools/presets/`」。インストール後は
`tools/` が存在しないため、**同梱した `onepochi/presets/` を見つけられないと
プリセットが 0 件になる**（`presets.preset_dir()` の同梱フォールバックと対になっている）。

`__pycache__` と `.pyc` は除外する（docs/99 P-13: 古い `.pyc` が残ると import が食い違う）。
"""

import argparse
import io
import json
import os
import re
import sys
import zipfile


def _force_utf8_stdout():
    """Windows の既定コンソール（cp932）で日本語を出すと死ぬのを防ぐ（docs/99 P-9）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


_force_utf8_stdout()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADDON_SRC = os.path.join(HERE, "dcc_blender", "addons", "onepochi")
PRESET_SRC = os.path.join(HERE, "presets")
PACKAGE_NAME = "onepochi"

#: ZIP に入れないもの
EXCLUDE_DIRS = ("__pycache__", ".pytest_cache", ".git")
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej")

#: ZIP に必ず入っていなければならないもの（構造検証に使う）
REQUIRED_ENTRIES = (
    "onepochi/__init__.py",
    "onepochi/core/__init__.py",
    "onepochi/core/presets.py",
    "onepochi/core/manifest.py",
    "onepochi/core/naming.py",
    "onepochi/core/ue_bridge.py",
    "onepochi/bl/__init__.py",
    "onepochi/bl/ui.py",
    "onepochi/bl/pipeline.py",
    "onepochi/presets/project_default.json",
)


def read_version(init_path=None):
    """`__init__.py` の `__version__` を読む（import せずに正規表現で）。

    import すると `bpy` が無い環境で落ちうるので、テキストとして読む。
    """
    path = init_path or os.path.join(ADDON_SRC, "__init__.py")
    with io.open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise ValueError("__version__ が見つかりません: {0}".format(path))
    return match.group(1)


def _should_skip(relative_path):
    parts = relative_path.replace("\\", "/").split("/")
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    return relative_path.lower().endswith(EXCLUDE_SUFFIXES)


def collect_files(addon_src=None, preset_src=None):
    """(ZIP 内のパス, 実ファイルのパス) の組を返す。"""
    addon_src = addon_src or ADDON_SRC
    preset_src = preset_src or PRESET_SRC
    entries = []

    for current, directories, files in os.walk(addon_src):
        directories[:] = [d for d in directories if d not in EXCLUDE_DIRS]
        for name in sorted(files):
            full = os.path.join(current, name)
            relative = os.path.relpath(full, addon_src).replace("\\", "/")
            if _should_skip(relative):
                continue
            entries.append(("{0}/{1}".format(PACKAGE_NAME, relative), full))

    if os.path.isdir(preset_src):
        for name in sorted(os.listdir(preset_src)):
            if not name.lower().endswith(".json"):
                continue
            entries.append(("{0}/presets/{1}".format(PACKAGE_NAME, name),
                            os.path.join(preset_src, name)))

    return sorted(entries)


def build_manifest_toml(version):
    """拡張機能（Extensions）用のメタデータ。

    **要検証**: Blender 5.2 で「ディスクからインストール」が
    従来形式（bl_info）と拡張形式（blender_manifest.toml）のどちらを要求するかは
    実機で未確認。両方入れておき、ユーザーのインストール結果で確定させる。
    """
    lines = [
        'schema_version = "1.0.0"',
        'id = "onepochi"',
        'version = "{0}"'.format(version),
        'name = "ワンポチツール (OnePochi)"',
        'tagline = "メッシュを検証して FBX とマニフェストに書き出し、UE へ取り込む"',
        'maintainer = "OnePochi Project"',
        'type = "add-on"',
        'license = ["SPDX:GPL-3.0-or-later"]',
        'blender_version_min = "4.2.0"',
        'tags = ["Import-Export", "Pipeline"]',
        "",
    ]
    return "\n".join(lines)


def build(out_dir=None, version=None, addon_src=None, preset_src=None):
    """ZIP を作って絶対パスを返す。"""
    version = version or read_version(
        os.path.join(addon_src or ADDON_SRC, "__init__.py"))
    out_dir = out_dir or os.path.join(ROOT, "dist")
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    zip_path = os.path.join(out_dir, "onepochi_addon_{0}.zip".format(version))
    entries = collect_files(addon_src, preset_src)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arc_name, full in entries:
            archive.write(full, arc_name)
        archive.writestr("{0}/blender_manifest.toml".format(PACKAGE_NAME),
                         build_manifest_toml(version))

    return zip_path


def verify(zip_path):
    """ZIP の構造を検証する。問題のリストを返す（空なら合格）。"""
    problems = []
    with zipfile.ZipFile(zip_path, "r") as archive:
        names = archive.namelist()

    for required in REQUIRED_ENTRIES:
        if required not in names:
            problems.append("必須エントリがありません: {0}".format(required))

    for name in names:
        if "__pycache__" in name or name.endswith((".pyc", ".pyo")):
            problems.append("除外すべきファイルが入っています: {0}".format(name))

    # ZIP 直下はパッケージ1つだけ（Blender はこの形を要求する）
    tops = sorted({name.split("/")[0] for name in names if name})
    if tops != [PACKAGE_NAME]:
        problems.append("ZIP 直下が単一パッケージになっていません: {0}".format(tops))

    presets = [n for n in names if n.startswith("onepochi/presets/")
               and n.endswith(".json")]
    if len(presets) < 2:
        problems.append(
            "プリセットが同梱されていません（{0} 件）。インストール後に"
            "プリセットが見つからなくなります".format(len(presets)))

    if "onepochi/blender_manifest.toml" not in names:
        problems.append("blender_manifest.toml がありません")

    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Blender アドオンの配布 ZIP を作る")
    parser.add_argument("--out", default=None, help="出力ディレクトリ（既定 dist/）")
    parser.add_argument("--verify-only", default=None,
                        help="既存の ZIP を検証するだけ")
    parser.add_argument("--report", default=None, help="結果 JSON の出力先")
    args = parser.parse_args(argv)

    if args.verify_only:
        zip_path = os.path.abspath(args.verify_only)
        built = False
    else:
        zip_path = build(args.out)
        built = True

    problems = verify(zip_path)
    with zipfile.ZipFile(zip_path, "r") as archive:
        count = len(archive.namelist())
        size = os.path.getsize(zip_path)

    print("ZIP: {0}".format(zip_path))
    print("エントリ数: {0} / サイズ: {1:,} バイト".format(count, size))
    if problems:
        print("問題 {0} 件:".format(len(problems)))
        for problem in problems:
            print("  - {0}".format(problem))
    else:
        print("構造検証: 合格")

    if args.report:
        report = {"zip": zip_path, "built": built, "entries": count,
                  "bytes": size, "problems": problems}
        directory = os.path.dirname(os.path.abspath(args.report))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with io.open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2))

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
