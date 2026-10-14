"""ワンポチツール — Blender アドオン本体。

DCC（Blender）から Unreal Engine 5.8 へ Static Mesh / Skeletal Mesh を
ワンボタンで取り込むパイプラインの第1層。

構成:
    core/ ... bpy に依存しないロジック（命名規約・プリセット・マニフェスト）
    bl/   ... bpy に依存する層（収集・検証・整形・書き出し・UI）

headless 実行は `tools/dcc_blender/run_headless.py` を使う。
"""

bl_info = {
    "name": "ワンポチツール (OnePochi)",
    "author": "OnePochi Project",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "3D ビュー > サイドバー > ワンポチ",
    "description": "メッシュを検証して FBX とマニフェストに書き出し、UE へ取り込む",
    "category": "Import-Export",
}

__version__ = "0.1.0"

_MODULES = []


def _load_modules():
    """bpy がある環境でのみ UI 層を読み込む。"""
    global _MODULES
    if _MODULES:
        return _MODULES
    from .bl import props, ui
    _MODULES = [props, ui]
    return _MODULES


def register():
    for module in _load_modules():
        module.register()


def unregister():
    for module in reversed(_load_modules()):
        module.unregister()


if __name__ == "__main__":
    register()
