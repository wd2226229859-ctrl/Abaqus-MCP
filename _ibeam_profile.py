# -*- coding: utf-8 -*-
"""沿梁轴提取挠度与翼缘应力分布，与理论解逐点对比。"""
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
    coords = {}
    elem_nodes = {}
    for iname, inst in odb.rootAssembly.instances.items():
        for n in inst.nodes:
            coords[n.label] = tuple(float(c) for c in n.coordinates)
        for e in inst.elements:
            elem_nodes[e.label] = tuple(e.connectivity)

    fr = odb.steps['Load'].frames[-1]

    # 节点 -> U2
    u2_by_node = {}
    for v in fr.fieldOutputs['U'].values:
        u2_by_node[v.nodeLabel] = float(v.data[1])

    # 单元中心 -> S33
    def center(el):
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

    s33_by_elem = {}
    for v in fr.fieldOutputs['S'].values:
        c = center(v.elementLabel)
        if c:
            s33_by_elem[v.elementLabel] = (float(v.data[2]), c)

    stations = [0, 250, 500, 750, 900, 1000, 1100, 1250, 1500, 1750, 2000]
    table = []
    for zs in stations:
        lo, hi = zs - 20.0, zs + 20.0
        # 中性轴附近腹板节点的竖向位移
        us = []
        for lbl, c in coords.items():
            if lo <= c[2] <= hi and 80.0 <= c[1] <= 120.0 and lbl in u2_by_node:
                us.append(u2_by_node[lbl])
        u2_avg = (sum(us) / len(us)) if us else None
        # 下翼缘单元轴向应力（取绝对值最大）
        best_bot = None
        best_top = None
        for el, (s33, c) in s33_by_elem.items():
            if not (lo <= c[2] <= hi):
                continue
            if c[1] < 20.0:
                if best_bot is None or abs(s33) > abs(best_bot):
                    best_bot = s33
            elif c[1] > 180.0:
                if best_top is None or abs(s33) > abs(best_top):
                    best_top = s33
        table.append({'z': zs, 'u2': u2_avg, 'n_u': len(us),
                      's33_bottom': best_bot, 's33_top': best_top})
    res['table'] = table
    res['n_nodes'] = len(coords)
    odb.close()
except Exception:
    res['error'] = traceback.format_exc()

__result__ = res
'''

r = json.loads(S.abaqus_run_script(SRC, None, False, 900))

P, L, E, nu = 10000.0, 2000.0, 206000.0, 0.3
h, b, tw, tf = 200.0, 100.0, 7.0, 11.4
I = (b * h ** 3 - (b - tw) * (h - 2 * tf) ** 3) / 12.0
G = E / (2.0 * (1.0 + nu))
A_web = tw * (h - 2 * tf)
y_unit = h / 2.0 - tf / 2.0   # 下翼缘单元中心到中性轴的距离


def theory_u2(z):
    zz = min(z, L - z)
    bend = P * zz * (3 * L ** 2 - 4 * zz ** 2) / (48.0 * E * I)
    shear = (P / 2.0) * zz / (G * A_web)
    return bend + shear


def theory_s33(z):
    zz = min(z, L - z)
    m = (P / 2.0) * zz
    return m * y_unit / I


rows = []
for row in (r.get("result") or {}).get("table", []):
    z = row["z"]
    tu = theory_u2(z)
    ts = theory_s33(z)
    rows.append({
        "z": z,
        "fem_u2": row["u2"],
        "theory_u2": tu,
        "diff_u2_pct": ((row["u2"] - tu) / tu * 100.0) if (row["u2"] is not None and tu) else None,
        "fem_s33_bottom": row["s33_bottom"],
        "theory_s33": ts,
        "diff_s33_pct": ((abs(row["s33_bottom"]) - ts) / ts * 100.0)
        if row["s33_bottom"] is not None and ts else None,
    })

out = {
    "section": {"I_mm4": I, "A_web_mm2": A_web, "y_unit_mm": y_unit,
                "G_MPa": G},
    "table": rows,
    "raw_error": (r.get("result") or {}).get("error"),
}
with open(os.path.join(BASE, "_ibeam_profile.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("DONE")
