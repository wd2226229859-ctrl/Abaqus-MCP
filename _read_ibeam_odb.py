# -*- coding: utf-8 -*-
"""读取简支工字钢梁 ODB 结果，与材料力学理论解对比。"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import server as S  # noqa: E402

# 示例：下方 SRC 里 openOdb(r"Job-IBeam.odb") 读取项目根目录下的 ODB，
# 把文件名改成你自己的 .odb 即可。

SRC = r'''
from odbAccess import openOdb
import traceback

res = {}
try:
    odb = openOdb(r"Job-IBeam.odb")
    # 节点/单元可能分布在多个 instance 下，全部合并
    coords = {}
    elem_nodes = {}
    inst_info = {}
    for iname, inst in odb.rootAssembly.instances.items():
        try:
            nn = len(inst.nodes)
        except Exception:
            nn = 0
        try:
            ne = len(inst.elements)
        except Exception:
            ne = 0
        inst_info[iname] = {'nodes': nn, 'elements': ne}
        for n in inst.nodes:
            coords[n.label] = tuple(float(c) for c in n.coordinates)
        for e in inst.elements:
            elem_nodes[e.label] = tuple(e.connectivity)
    res['instances'] = inst_info
    res['n_nodes'] = len(coords)
    res['n_elems'] = len(elem_nodes)

    steps = list(odb.steps.keys())
    res['steps'] = steps
    fr = odb.steps[steps[-1]].frames[-1]
    try:
        res['frame_value'] = [float(x) for x in (fr.frameValue or [])]
    except Exception:
        res['frame_value'] = None

    # --- 最大竖向位移 U2 ---
    best = None
    for v in fr.fieldOutputs['U'].values:
        c = coords.get(v.nodeLabel)
        if not c:
            continue
        u2 = float(v.data[1])
        if best is None or abs(u2) > abs(best['u2']):
            best = {'node': v.nodeLabel, 'u2': u2,
                    'coord': [round(q, 2) for q in c]}
    res['max_U2'] = best

    # --- 轴向应力 S33（梁轴为 Z）---
    def center(el):
        # 注意: Abaqus 内核里 sum 被自身类型覆盖，不能用内置 sum，改用显式累加
        ns = elem_nodes.get(el)
        if not ns:
            return None
        pts = [coords[n] for n in ns if n in coords]
        if not pts:
            return None
        cx = cy = cz = 0.0
        for p in pts:
            cx += p[0]
            cy += p[1]
            cz += p[2]
        k = float(len(pts))
        return (cx / k, cy / k, cz / k)

    best_mid = None
    best_all = None
    for v in fr.fieldOutputs['S'].values:
        c = center(v.elementLabel)
        if not c:
            continue
        s33 = float(v.data[2])
        if best_all is None or abs(s33) > abs(best_all['s33']):
            best_all = {'elem': v.elementLabel, 's33': s33,
                        'coord': [round(q, 2) for q in c]}
        if 850.0 <= c[2] <= 1150.0 and c[1] < 25.0:
            if best_mid is None or abs(s33) > abs(best_mid['s33']):
                best_mid = {'elem': v.elementLabel, 's33': s33,
                            'coord': [round(q, 2) for q in c]}
    res['max_S33_all'] = best_all
    res['max_S33_midspan_bottom'] = best_mid

    # --- 支反力合力（应等于 10 kN）---
    try:
        tot = 0.0
        cnt = 0
        for v in fr.fieldOutputs['RF'].values:
            tot += float(v.data[1])
            cnt += 1
        res['sum_RF2'] = tot
        res['rf_nodes'] = cnt
    except Exception as e:
        res['rf_err'] = str(e)

    odb.close()
except Exception:
    res['error'] = traceback.format_exc()

__result__ = res
'''

r = json.loads(S.abaqus_run_script(SRC, None, False, 900))

# ---------------- 理论解（材料力学）----------------
P = 10000.0
L = 2000.0
E = 206000.0
nu = 0.3
h = 200.0
b = 100.0
tw = 7.0
tf = 11.4

# 建模用的理想化截面的实际惯性矩（与型钢表 I20a 略有差别）
I = (b * h ** 3 - (b - tw) * (h - 2 * tf) ** 3) / 12.0
W = I / (h / 2.0)
G = E / (2.0 * (1.0 + nu))
A_web = tw * (h - 2 * tf)

theory = {
    "I_model_mm4": I,
    "W_model_mm3": W,
    "I20a_table_mm4": 2.369e7,
    "moment_max_Nmm": P * L / 4.0,
    "stress_max_MPa": (P * L / 4.0) / W,
    "deflection_bending_mm": P * L ** 3 / (48.0 * E * I),
    "deflection_shear_mm": P * L / (4.0 * G * A_web),
    "reaction_N": P,
}
theory["deflection_total_mm"] = (theory["deflection_bending_mm"]
                                 + theory["deflection_shear_mm"])

out = {"theory": theory, "odb": r}
with open(os.path.join(BASE, "_ibeam_result.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE")
