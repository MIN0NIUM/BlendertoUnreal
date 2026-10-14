"""テクスチャの発見規則（R-4 / docs/04_命名規約.md §3）。

bpy に依存しない純粋な Python。`tools/dcc_blender/tests/test_texture_discovery.py`
から単体テストできる。

    純粋ロジック（FS に触らない）   SUFFIXES / EXTENSIONS / parse_texture_file /
                                    candidate_stems / discover
    ファイルシステムに触る          texture_folder_path / list_texture_files

**発見規則**（走査は `source.textureFolder` 直下のみ。**再帰しない**）:

| 優先 | パターン | 例（スロット `M_Crate_Body` / アセット `Crate_01`） |
|---|---|---|
| 1 | `T_<スロット名から M_ を外した名前>_<サフィックス>` | `T_Crate_Body_BC` |
| 2 | `T_<スロット名そのまま>_<サフィックス>` | `T_M_Crate_Body_BC` |
| 3 | `T_<アセット名>_<サフィックス>`（**単一スロットのときだけ**） | `T_Crate_01_BC` |

- 大文字小文字は区別しない
- 同じサフィックスで複数見つかったら **語幹の優先順 -> 拡張子の優先順**で 1 つ採用し、
  採用しなかったものを **V-111** に出す（黙って選ばない）
- 見つからなかったサフィックスは**何も出力しない**（空文字やダミーを入れない）
- **`importTextures` が false でも出力する。**「発見（DCC 側の事実）」と
  「取り込むか（UE 側の方針）」は別（設計方針 D-3 / D-5）

**再帰しない理由**: S-11 の `megascan/` のような元素材フォルダを巻き込まないため
（docs/20261013夜間作業プラン §2.1 の実測）。
"""

import os

from . import issues as issues_mod
from . import naming

#: テクスチャサフィックス（docs/04 §3 の表と**同じ順序**）。
#: 出力の `textures` のキー順もこの順になる（決定的にするため）。
#: この集合が docs/04 §3 の表と一致することは単体テストで突き合わせる（P-28）
SUFFIXES = ("_BC", "_D", "_N", "_ORM", "_M", "_R", "_AO", "_E", "_MSK")

#: 対象拡張子。**この並びが採用の優先順**（同じサフィックスで複数見つかったとき）
EXTENSIONS = (".png", ".tga", ".jpg", ".jpeg", ".tif", ".tiff", ".exr")

#: 非可逆圧縮の拡張子。法線マップに使うとブロックノイズが法線に乗る（V-113）
LOSSY_EXTENSIONS = (".jpg", ".jpeg")

#: 法線マップのサフィックス（V-113 / V-114 の対象）
NORMAL_SUFFIX = "_N"

#: UE のテクスチャアセットのプレフィックス（docs/04 §1）
TEXTURE_PREFIX = "T_"

_SUFFIX_BY_LOWER = dict((s.lower(), s) for s in SUFFIXES)
_EXTENSION_RANK = dict((e, i) for i, e in enumerate(EXTENSIONS))


class TextureFile(object):
    """テクスチャフォルダ直下の 1 ファイルを分解したもの。"""

    __slots__ = ("file_name", "base", "stem", "suffix", "extension")

    def __init__(self, file_name, base, stem, suffix, extension):
        self.file_name = file_name
        #: 拡張子を除いたファイル名（`T_M_Crate_Body_BC`）
        self.base = base
        #: サフィックスを除いた語幹（`T_M_Crate_Body`）。分解できなければ None
        self.stem = stem
        #: 正規化したサフィックス（`_BC`）。表に無ければ None
        self.suffix = suffix
        #: 小文字化した拡張子（`.jpg`）
        self.extension = extension

    def __repr__(self):
        return "<TextureFile {0} stem={1} suffix={2}>".format(
            self.file_name, self.stem, self.suffix)


def parse_texture_file(file_name):
    """ファイル名を (語幹, サフィックス, 拡張子) に分解する。

    対象拡張子でなければ None を返す（`.psd` などの作業データは無視する）。
    サフィックスが docs/04 §3 の表に無い場合は `suffix = None` で返す
    （「テクスチャらしいが規約外」として V-092 に出すため、ここでは捨てない）。
    """
    if not file_name:
        return None
    base, extension = os.path.splitext(file_name)
    extension = extension.lower()
    if extension not in _EXTENSION_RANK:
        return None
    if not base:
        return None

    stem, sep, tail = base.rpartition("_")
    if not sep or not stem:
        return TextureFile(file_name, base, None, None, extension)
    suffix = _SUFFIX_BY_LOWER.get(("_" + tail).lower())
    if suffix is None:
        return TextureFile(file_name, base, None, None, extension)
    return TextureFile(file_name, base, stem, suffix, extension)


def candidate_stems(slot_name, asset_name, single_slot=False):
    """照合する語幹の候補を**優先順**で返す。

    `single_slot` が True のときだけ、アセット名による語幹（優先 3）を足す。
    マルチスロットで足すと、どのスロットに割り当てるべきか決められない
    （S-03 の `M_Slot_A` / `M_Slot_B` / `M_Slot_C` で取り違える）。
    """
    stems = []

    def _add(stem):
        if not stem:
            return
        if any(stem.lower() == existing.lower() for existing in stems):
            return
        stems.append(stem)

    if slot_name:
        base = slot_name[2:] if slot_name.startswith("M_") else slot_name
        _add(TEXTURE_PREFIX + base)          # 優先 1
        _add(TEXTURE_PREFIX + slot_name)     # 優先 2
    if single_slot and asset_name:
        _add(TEXTURE_PREFIX + asset_name)    # 優先 3
    return stems


def _folder_issues(parsed_files, result):
    """フォルダ側の問題（スロットに依存しないもの）を出す。

    V-112 ... `T_` で始まらない / 使えない文字を含む
    V-092 ... サフィックスが docs/04 §3 の表にない（既存ルール / docs/05 §E）
    """
    for record in parsed_files:
        problem = naming.name_problem(record.base)
        if problem:
            result.warn(
                "V-112",
                "テクスチャフォルダのファイル名が規約違反です（{0}）。"
                "{1}<名前>_<サフィックス>.<拡張子> にしてください。"
                "このファイルは取り込み対象になりません".format(
                    problem, TEXTURE_PREFIX),
                record.file_name)
            continue
        if not record.base.startswith(TEXTURE_PREFIX):
            result.warn(
                "V-112",
                "テクスチャ名が '{0}' で始まっていません。"
                "このファイルは取り込み対象になりません".format(TEXTURE_PREFIX),
                record.file_name)
            continue
        if record.suffix is None:
            result.warn(
                "V-092",
                "テクスチャのサフィックスが規約（docs/04 §3）にありません。"
                "使えるのは {0} です。このファイルは取り込み対象になりません".format(
                    " / ".join(SUFFIXES)),
                record.file_name)


def discover(slot_names, asset_name, file_names):
    """マテリアルスロットごとのテクスチャを発見する。

    **ファイルシステムには触らない。**`file_names` はテクスチャフォルダ直下の
    ファイル名のリスト（`list_texture_files()` で取る）。

    戻り値は `(textures, issues)`:

        textures ... {スロット名: {"_BC": "T_....jpg", ...}}。
                     1 枚も見つからなかったスロットは**キーごと入らない**
        issues   ... `core.issues.IssueList`（V-110 / V-111 / V-112 / V-113 / V-092）
    """
    result = issues_mod.IssueList()
    slots = list(slot_names or [])
    parsed = [r for r in (parse_texture_file(n) for n in (file_names or []))
              if r is not None]

    _folder_issues(parsed, result)

    textures = {}
    single_slot = len(slots) == 1
    for slot_name in slots:
        stems = candidate_stems(slot_name, asset_name, single_slot)
        stem_rank = dict((s.lower(), i) for i, s in enumerate(stems))

        matches = {}
        for record in parsed:
            if record.suffix is None or record.stem is None:
                continue
            rank = stem_rank.get(record.stem.lower())
            if rank is None:
                continue
            matches.setdefault(record.suffix, []).append(
                (rank, _EXTENSION_RANK[record.extension], record.file_name))

        found = {}
        for suffix in SUFFIXES:
            candidates = matches.get(suffix)
            if not candidates:
                continue
            candidates.sort()
            chosen = candidates[0][2]
            found[suffix] = chosen
            if len(candidates) > 1:
                rejected = [c[2] for c in candidates[1:]]
                result.warn(
                    "V-111",
                    "スロット '{0}' のサフィックス {1} のテクスチャが {2} 枚見つかりました。"
                    "'{3}' を採用し、'{4}' は使いません".format(
                        slot_name, suffix, len(candidates), chosen,
                        "', '".join(rejected)),
                    chosen)
            if suffix == NORMAL_SUFFIX:
                extension = os.path.splitext(chosen)[1].lower()
                if extension in LOSSY_EXTENSIONS:
                    result.warn(
                        "V-113",
                        "法線マップ '{0}' が非可逆圧縮形式（{1}）です。"
                        "圧縮ノイズが法線に乗ります。{2} を推奨します".format(
                            chosen, extension, " / ".join(
                                e for e in EXTENSIONS
                                if e not in LOSSY_EXTENSIONS)),
                        chosen)

        if found:
            textures[slot_name] = found
        else:
            result.warn(
                "V-110",
                "スロット '{0}' に対応するテクスチャが 1 枚も見つかりませんでした。"
                "テクスチャフォルダ直下に {1} のいずれかの名前で置いてください".format(
                    slot_name,
                    " / ".join(s + "_<サフィックス>"
                               for s in (stems or ["T_<名前>"]))),
                slot_name)

    return textures, result


# --- ファイルシステム側（ここだけが実フォルダを見る） --------------------------


def texture_folder_path(asset_folder, texture_folder):
    """`assetFolder` + `textureFolder` を絶対パスにする。どちらか無ければ None。"""
    if not asset_folder or not texture_folder:
        return None
    return os.path.join(asset_folder, texture_folder)


def list_texture_files(folder):
    """フォルダ**直下**のファイル名を返す（**再帰しない**）。

    フォルダが無い / 読めない / 空でも**エラーにしない**（空リストを返す）。
    サブフォルダ（S-11 の `megascan/`）は名前も返さない。
    """
    if not folder:
        return []
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return []
    return [name for name in entries
            if os.path.isfile(os.path.join(folder, name))]


def has_texture_files(file_names):
    """対象拡張子のファイルが 1 つでもあるか。

    テクスチャを 1 枚も置いていないアセット（S-01〜S-10）で
    V-110 を出さないための判定に使う。
    """
    return any(parse_texture_file(name) is not None
               for name in (file_names or []))
