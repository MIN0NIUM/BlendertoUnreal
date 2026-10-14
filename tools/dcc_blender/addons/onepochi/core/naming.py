"""命名規約の判定（docs/04_命名規約.md の機械化）。

bpy に依存しない純粋な Python。`tools/dcc_blender/tests/` から単体テストできる。
"""

import re

# --- 基本パターン (docs/04 §0) -------------------------------------------------

#: 先頭英字・A-Za-z0-9_ のみ（N-1, N-2）
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

#: サンプルデータ用。先頭数字を許容する緩和版
NAME_RE_LOOSE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*$")

#: マテリアル名は M_<名前> 形式が必須（Q-19）
MATERIAL_RE = re.compile(r"^M_[A-Za-z][A-Za-z0-9_]*$")

#: UE パッケージパス
UE_PATH_RE = re.compile(r"^/[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*$")

#: UE アセット種別のプレフィックス
PREFIX = {
    "StaticMesh": "SM_",
    "SkeletalMesh": "SK_",
    "Skeleton": "SKEL_",
    "PhysicsAsset": "PHYS_",
    "Material": "M_",
    "MaterialInstance": "MI_",
    "Texture": "T_",
    "AnimationSequence": "A_",
}

#: コリジョンメッシュのプレフィックス -> マニフェスト上の形状名
COLLISION_PREFIXES = {
    "UCX_": "convex",
    "UBX_": "box",
    "USP_": "sphere",
    "UCP_": "capsule",
}

SOCKET_PREFIX = "SOCKET_"

#: 予約コレクション名（LOD<n> は別途正規表現で判定）
RESERVED_COLLECTIONS = ("Collision", "Sockets")

LOD_COLLECTION_RE = re.compile(r"^LOD(\d+)$")

#: Blender が重複名に自動で付けるサフィックス（`.001`）。
#:
#: **コレクション名も .blend 全体で一意**（docs/99 P-21 の実測をコレクションに
#: 拡張したもの / Blender 5.2.2 LTS で実測）。したがって
#: **1 ファイルに 2 つのアセットコレクションを置くと、2 つめの `LOD0` は
#: 必ず `LOD0.001` になる**（代入しても例外は出ず、黙って付く）。
#: バッチ書き出し（N-5）はこの状態が普通なので、
#: **予約コレクション名の判定ではこのサフィックスを剥がしてから見る。**
#: アセット名・オブジェクト名・マテリアル名ではこれを許さない
#: （`name_problem()` は従来どおりドットをエラーにする）
DUPLICATE_SUFFIX_RE = re.compile(r"\.\d{3}$")

#: Blender が自動で付ける既定名。命名規約違反として弾く対象
BLENDER_DEFAULT_MATERIAL_RE = re.compile(r"^Material(\.\d+)?$")


# --- 判定関数 -----------------------------------------------------------------


def name_problem(name, allow_leading_digit=False):
    """名前の規約違反理由を日本語で返す。問題なければ None。"""
    if not name:
        return "名前が空です"
    pattern = NAME_RE_LOOSE if allow_leading_digit else NAME_RE
    if pattern.match(name):
        return None

    # 具体的な理由を切り分けて返す（ユーザーが直せるように）
    if not name[0].isascii():
        return "名前に ASCII 以外の文字（日本語など）が含まれています"
    if any(not c.isascii() for c in name):
        return "名前に ASCII 以外の文字（日本語など）が含まれています"
    if " " in name:
        return "名前に空白が含まれています"
    if "." in name:
        return "名前にドットが含まれています（Blender の重複サフィックス .001 などは不可）"
    if "-" in name:
        return "名前にハイフンが含まれています（アンダースコアを使ってください）"
    if name[0].isdigit():
        return "名前が数字で始まっています（先頭は英字にしてください）"
    if not name[0].isalpha():
        return "名前が英字で始まっていません"
    return "名前に使用できない文字が含まれています（使えるのは A-Za-z0-9_ のみ）"


def is_valid_name(name, allow_leading_digit=False):
    return name_problem(name, allow_leading_digit) is None


def material_problem(name):
    """マテリアル名の規約違反理由を返す。問題なければ None。"""
    if not name:
        return "マテリアル名が空です"
    if BLENDER_DEFAULT_MATERIAL_RE.match(name):
        return (
            "Blender の既定マテリアル名のままです（'{0}'）。"
            "M_<名前> 形式に変更してください".format(name)
        )
    if MATERIAL_RE.match(name):
        return None
    base_problem = name_problem(name)
    if base_problem:
        return base_problem
    return "マテリアル名が M_<名前> 形式ではありません（'{0}'）".format(name)


def mi_name(material_name):
    """M_Crate_Body -> MI_Crate_Body。M_ で始まらない場合は None。"""
    if not material_name.startswith("M_"):
        return None
    return "MI_" + material_name[2:]


def parse_collision_name(name, known_bodies=None):
    """UCX_Crate_01_01 -> ('convex', 'Crate_01', 1)。該当しなければ None。

    形式: `<Prefix>_<対象メッシュ名>[_<連番>]`。**連番は省略できる**
    （UE の規約でも省略可）。対象メッシュ名にアンダースコアを含みうるため、
    末尾の連番から逆算する。

    **ここには文字列だけでは解けない曖昧さがある。**`UCX_Crate_01` は
      (a) 本体 `Crate_01` の連番なし
      (b) 本体 `Crate` の 1 番
    のどちらにも読める。しかも `<名前>_<2桁>` は本プロジェクトの標準的な
    メッシュ名（`Crate_01`）なので、**(b) と誤読すると V-041 で止まる**。
    実測: S-08 `08_static_socket` がこれを踏んだ（本体は `Crate_01`）。

    `known_bodies` に**書き出し対象の本体メッシュ名**を渡すと、
    そちらに一致する読み方を優先する。渡さない場合は従来どおり
    「末尾の連番を剥がす」規則で読む（後方互換）。
    """
    for prefix, shape in COLLISION_PREFIXES.items():
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix):]
        head, sep, tail = rest.rpartition("_")
        has_serial = bool(sep) and tail.isdigit()

        if known_bodies:
            bodies = set(known_bodies)
            # 連番なしの読み方が実在するならそちらを採る（曖昧さの解消）
            if rest in bodies:
                return (shape, rest, None)
            if has_serial and head in bodies:
                return (shape, head, int(tail))
            # どちらも一致しない。従来の規則で返して V-041 に判定させる

        if has_serial:
            return (shape, head, int(tail))
        # 連番が無い形式も受け付ける（対象名のみ）
        return (shape, rest, None)
    return None


def is_collision_name(name):
    return parse_collision_name(name) is not None


def parse_socket_name(name):
    """SOCKET_weapon_r -> 'weapon_r'。該当しなければ None。

    **ここは「切り出すだけ」。**規約に合っているかは
    `socket_name_problem()` で判定する（D-3 / バリデーションは上流で行う）。
    """
    if not name.startswith(SOCKET_PREFIX):
        return None
    socket = name[len(SOCKET_PREFIX):]
    return socket or None


def socket_name_problem(object_name):
    """ソケット用 Empty の名前の規約違反理由を返す。問題なければ None。

    `SOCKET_` を剥がした残りが**そのまま UE のソケット名**になり、
    マニフェストのスキーマでは `ueAssetName`（`^[A-Za-z][A-Za-z0-9_]*$`）
    として検証される。つまり `SOCKET_1_weapon` は UE 側に届く前に
    スキーマ検証で落ちる。**それを DCC 側で止めるのがここ**（方針 D-3）。

    先頭数字を許容する緩和版（`NAME_RE_LOOSE`）は使わない。
    ソケット名は UE のアセット名と同じ厳しさが必要。
    """
    if not object_name or not object_name.startswith(SOCKET_PREFIX):
        return None                      # ソケットではない。ここでは何も言わない
    socket = object_name[len(SOCKET_PREFIX):]
    if not socket:
        return "'{0}' の後ろにソケット名がありません".format(SOCKET_PREFIX)
    problem = name_problem(socket)
    if problem:
        return ("ソケット名 '{0}' が規約に合いません（{1}）。"
                "{2}<英字で始まる名前> にしてください".format(
                    socket, problem, SOCKET_PREFIX))
    return None


def is_socket_name(name):
    return parse_socket_name(name) is not None


def strip_duplicate_suffix(name):
    """'LOD0.001' -> 'LOD0'。Blender の重複サフィックスだけを剥がす。

    **コレクション名専用。**オブジェクト名・マテリアル名に使ってはいけない
    （`Crate_01.001` を `Crate_01` と同一視すると、複製し忘れを見逃す）。
    """
    if not name:
        return name
    return DUPLICATE_SUFFIX_RE.sub("", name)


def has_duplicate_suffix(name):
    """Blender の重複サフィックス（`.001`）が付いているか。"""
    return bool(name) and bool(DUPLICATE_SUFFIX_RE.search(name))


def parse_lod_collection(name):
    """'LOD0' -> 0。LOD コレクションでなければ None。

    `LOD0.001`（2 つめのアセットで Blender が自動的に付ける名前）も
    LOD0 として扱う（`DUPLICATE_SUFFIX_RE` の説明を参照）。
    """
    m = LOD_COLLECTION_RE.match(strip_duplicate_suffix(name))
    if not m:
        return None
    return int(m.group(1))


def is_reserved_collection(name):
    base = strip_duplicate_suffix(name)
    return base in RESERVED_COLLECTIONS or parse_lod_collection(base) is not None


def ue_asset_name(asset_type, base_name):
    """('StaticMesh', 'Crate_01') -> 'SM_Crate_01'。

    base_name が既に正しいプレフィックスを持つ場合は二重付与しない。
    """
    prefix = PREFIX.get(asset_type)
    if prefix is None:
        return base_name
    if base_name.startswith(prefix):
        return base_name
    return prefix + base_name


def strip_known_prefix(name):
    """'SM_Crate_01' -> 'Crate_01'。既知プレフィックスが無ければそのまま返す。"""
    for prefix in sorted(PREFIX.values(), key=len, reverse=True):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def is_valid_ue_path(path):
    return bool(path) and bool(UE_PATH_RE.match(path))


def ue_path_problem(path):
    if not path:
        return "UE の出力先パスが空です"
    if not path.startswith("/"):
        return "UE の出力先パスは '/' で始めてください（例: /Game/_Dev/Props）"
    if path.endswith("/"):
        return "UE の出力先パスは '/' で終わらせないでください"
    if UE_PATH_RE.match(path):
        return None
    return (
        "UE の出力先パスに使用できない文字が含まれています（'{0}'）。"
        "各階層は英字または _ で始まり、A-Za-z0-9_ のみ使用できます".format(path)
    )


def find_case_collisions(names):
    """大文字小文字のみ異なる名前の組を返す（Windows で衝突する / N-4）。"""
    seen = {}
    collisions = []
    for name in names:
        key = name.lower()
        if key in seen and seen[key] != name:
            collisions.append((seen[key], name))
        else:
            seen.setdefault(key, name)
    return collisions
