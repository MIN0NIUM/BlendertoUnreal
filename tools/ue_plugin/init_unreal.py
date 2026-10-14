"""UE エディタ起動時に自動で走るスクリプト（P6-4）。

UE は **Python のパスにある `init_unreal.py` をエディタ起動時に実行する**。
`tools/deploy_to_ue.bat` は `tools/ue_plugin/` を
`<プロジェクト>/Content/Python/` へコピーするので、このファイルも一緒に配置され、
次回のエディタ起動で**ワンポチのメニュー項目が生える**。

ここでやるのは**メニューの登録だけ**。取り込みの本体は
`onepochi_ue.importer`（起動方法に依存しない純粋な関数 / 方針 D-1）。

**エディタの起動を妨げないこと**が最優先なので、何があっても例外を外に出さない。
失敗したら Output Log に日本語で理由を残す。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main():
    try:
        import unreal
    except ImportError:
        print("[OnePochi] unreal モジュールが無いため init_unreal.py は何もしません")
        return

    try:
        from onepochi_ue import menu
    except Exception as error:  # noqa: BLE001
        unreal.log_error(
            "[OnePochi] メニュー登録モジュールを読み込めませんでした: {0}: {1}".format(
                type(error).__name__, error))
        return

    ok, message = menu.register_menu()
    if ok:
        unreal.log("[OnePochi] {0}".format(message))
    else:
        # 登録できなくても取り込み自体は Remote Control / コンソールから使える
        unreal.log_warning(
            "[OnePochi] {0}（Remote Control とコンソールからの取り込みは使えます）"
            .format(message))


main()
