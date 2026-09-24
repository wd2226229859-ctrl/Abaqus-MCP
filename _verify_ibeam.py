# -*- coding: utf-8 -*-
"""离线验证 ibeam_gui.py 的建模逻辑（noGUI，不弹窗）。"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import server as S  # noqa: E402

# 示例：用 noGUI 跑一遍建模脚本，把路径改成你自己的脚本即可。
SCRIPT_PATH = os.path.join(BASE, "ibeam_gui.py")
r = json.loads(S.abaqus_run_script_file(SCRIPT_PATH, None, False, 900))

brief = {
    "ok": r.get("ok"),
    "elapsed_sec": r.get("elapsed_sec"),
    "logs": r.get("logs", [])[-25:],
    "error": (r.get("error") or "")[:1500],
}
with open(os.path.join(BASE, "_verify.json"), "w", encoding="utf-8") as f:
    json.dump(brief, f, ensure_ascii=False, indent=2)
print("DONE")
