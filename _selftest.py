# -*- coding: utf-8 -*-
"""离线自检：直接调用 server 里的核心函数，端到端验证建模 -> 求解 -> 后处理。"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import server as S  # noqa: E402

report = {"steps": []}


def step(name, fn):
    print(">>> %s ..." % name, flush=True)
    try:
        r = fn()
        report["steps"].append({"step": name, "ok": True, "output": r})
        print("    ok", flush=True)
    except Exception:
        report["steps"].append({"step": name, "ok": False, "err": __import__("traceback").format_exc()})
        print("    FAIL", flush=True)


step("status", lambda: json.loads(S.abaqus_status()))
step("new_model", lambda: json.loads(S.abaqus_new_model("selftest_beam.cae", overwrite=True)))

MODEL_CAE = S.WORKDIR / "selftest_beam.cae"

BUILD_SRC = r'''
m = mdb.models['Model-1'] if 'Model-1' in mdb.models.keys() else mdb.Model(name='Model-1')

# 几何：1000x100 mm 的梁
sk = m.ConstrainedSketch(name='prof', sheetSize=3000.0)
sk.rectangle((0.0, 0.0), (1000.0, 100.0))
p = m.Part(name='Beam', dimensionality=TWO_D_PLANAR, type=DEFORMABLE_BODY)
p.BaseShell(sketch=sk)

# 材料 + 截面
m.Material(name='Steel')
m.materials['Steel'].Elastic(table=((210000.0, 0.3),))
m.HomogeneousSolidSection(name='Sec', material='Steel', thickness=50.0)
p.SectionAssignment(region=(p.faces,), sectionName='Sec')

# 装配
a = m.rootAssembly
a.Instance(name='Beam-1', part=p, dependent=True)

# 几何集合（左端固支、右端加载）
edges = p.edges
p.Set(name='Set-Left', edges=edges.findAt(((0.0, 50.0, 0.0),)))
p.Set(name='Set-Right', vertices=p.vertices.findAt(((1000.0, 100.0, 0.0),),
                                                   ((1000.0, 0.0, 0.0),)))
p.Set(name='Set-All', faces=p.faces[:])

# 网格
p.seedPart(size=25.0, deviationFactor=0.1, minSizeFactor=0.1)
p.generateMesh()

# 分析步
m.StaticStep(name='Load', previous='Initial')

# 边界条件 + 载荷
inst = a.instances['Beam-1']
m.DisplacementBC(name='Fix', createStepName='Initial',
                 region=inst.sets['Set-Left'], u1=0.0, u2=0.0)
m.ConcentratedForce(name='TipLoad', createStepName='Load',
                    region=inst.sets['Set-Right'], cf2=-10000.0)

# 作业（Job 属于 mdb，不属于 Model）
mdb.Job(name='Job-1', model='Model-1', description='MCP selftest cantilever')

print('parts', m.parts.keys())
print('nodes', len(p.nodes), 'elements', len(p.elements))
__result__ = {'nodes': len(p.nodes), 'elements': len(p.elements),
              'jobs': list(mdb.jobs.keys()), 'steps': m.steps.keys()}
'''

step("build_model", lambda: json.loads(S.abaqus_run_script(BUILD_SRC, str(MODEL_CAE), True, 900)))
step("inspect", lambda: json.loads(S.abaqus_inspect(str(MODEL_CAE))))
step("solve", lambda: json.loads(S.abaqus_submit_job(str(MODEL_CAE), "Job-1", 4, True, 1800)))

step("read_odb", lambda: json.loads(
    S.abaqus_read_results(str(S.WORKDIR / "Job-1.odb"), None, "U,RF", 20, False, 900)))

with open(os.path.join(BASE, "_selftest_report.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print("REPORT_WRITTEN", flush=True)
