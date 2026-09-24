# -*- coding: utf-8 -*-
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import server as S  # noqa: E402

# 示例：检查一个已存在的 .cae 模型；把路径改成你自己的模型文件即可。
CAE_PATH = os.path.join(BASE, "ibeam.cae")
r = json.loads(S.abaqus_inspect(CAE_PATH))
with open(os.path.join(BASE, "_inspect.json"), "w", encoding="utf-8") as f:
    json.dump(r, f, ensure_ascii=False, indent=2)
print("DONE")
