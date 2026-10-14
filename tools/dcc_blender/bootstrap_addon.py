"""開発用ブートストラップ。

アドオンを Blender に「インストール」せずに読み込む。
ユーザーの Blender 設定（scripts/addons）を汚さないため、開発中はこちらを使う。

    blender.exe --python tools/dcc_blender/bootstrap_addon.py

リロードしたい場合は Blender の Python コンソールで:
    import bootstrap_addon; bootstrap_addon.reload_addon()
"""

import importlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
#: Blender がアドオンを探すのと同じ場所（Script Directories の規約: <dir>/addons/）
ADDONS_DIR = os.path.join(HERE, "addons")

PACKAGE = "onepochi"


def ensure_path():
    if ADDONS_DIR not in sys.path:
        sys.path.insert(0, ADDONS_DIR)


def register_addon():
    ensure_path()
    module = importlib.import_module(PACKAGE)
    try:
        module.unregister()
    except Exception:
        pass
    module.register()
    print("[ワンポチ] アドオンを読み込みました: {0} v{1}".format(
        PACKAGE, ".".join(str(v) for v in module.bl_info["version"])))
    print("[ワンポチ] 3D ビュー > サイドバー(N) > ワンポチ タブを開いてください")
    return module


def reload_addon():
    """コード変更を反映する（サブモジュールも含めて再読み込み）。"""
    ensure_path()
    module = sys.modules.get(PACKAGE)
    if module is not None:
        try:
            module.unregister()
        except Exception:
            pass
    for name in [n for n in list(sys.modules) if n == PACKAGE or n.startswith(PACKAGE + ".")]:
        del sys.modules[name]
    return register_addon()


if __name__ == "__main__":
    register_addon()
