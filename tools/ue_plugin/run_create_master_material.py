"""仮マスターマテリアル `M_OnePochi_Base` を生成する（Q-19）。

UE のコンソールから:
    py "C:/.../tools/ue_plugin/run_create_master_material.py"

既定の出力先は `/Game/_Dev/Materials/M_OnePochi_Base`。
別の場所に作りたい場合はジョブファイルで指定する:
    %LOCALAPPDATA%/OnePochi/material_job.json
    { "path": "/Game/_Dev/Materials/M_OnePochi_Base", "force": false,
      "report": "C:/.../material_report.json" }

構成（最小）:
    TextureSampleParameter2D "BaseColor" -> Base Color
    TextureSampleParameter2D "Normal"    -> Normal
    TextureSampleParameter2D "Emissive"  -> Emissive Color
    TextureSampleParameter2D "ORM"       -> R:AO / G:Roughness / B:Metallic
"""

import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEFAULT_PATH = "/Game/_Dev/Materials/M_OnePochi_Base"
JOB_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "OnePochi", "material_job.json",
)


def read_job():
    job = {}
    args = sys.argv[1:] if len(sys.argv) > 1 else []
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("--path", "--report") and index + 1 < len(args):
            job[token[2:]] = args[index + 1]
            index += 2
            continue
        if token == "--force":
            job["force"] = True
        index += 1

    if not job.get("path") and os.path.isfile(JOB_PATH):
        try:
            with open(JOB_PATH, "r", encoding="utf-8") as handle:
                job.update(json.load(handle))
        except Exception as error:  # noqa: BLE001
            print("[OnePochi] ジョブファイルを読めません: {0}".format(error))
    return job


def main():
    import unreal

    from onepochi_ue import materials

    job = read_job()
    path = job.get("path") or DEFAULT_PATH
    force = bool(job.get("force"))

    sink = []
    material, created = materials.create_master_material(path, sink, force=force)

    report = {
        "startedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "path": path,
        "created": bool(created),
        "exists": material is not None,
        "warnings": sink,
    }

    if material is None:
        unreal.log_error("[OnePochi] マスターマテリアルを生成できませんでした: {0}".format(path))
        for warning in sink:
            unreal.log_error("[OnePochi]   {0}".format(warning))
    else:
        state = "生成しました" if created else "既に存在します"
        unreal.log("[OnePochi] マスターマテリアルを{0}: {1}".format(state, path))
        for warning in sink:
            unreal.log_warning("[OnePochi]   {0}".format(warning))

    report_path = job.get("report")
    if report_path:
        directory = os.path.dirname(os.path.abspath(report_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        unreal.log("[OnePochi] レポート: {0}".format(report_path))

    print("[OnePochi] result={0} path={1} warnings={2}".format(
        "ok" if material is not None else "failed", path, len(sink)))


if __name__ == "__main__":
    main()
