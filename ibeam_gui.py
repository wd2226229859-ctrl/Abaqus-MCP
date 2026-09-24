# -*- coding: utf-8 -*-
"""
简支工字钢梁建模演示脚本（在 Abaqus CAE 图形界面里执行）

模型：I20a 工字钢，跨度 2000 mm，简支，跨中 10 kN 集中力，Q235。
单位制：mm - N - MPa（E=206000 MPa，密度 7.85e-9 t/mm^3）

每一步完成后会弹一个对话框，点 OK 继续下一步 —— 方便看清建模过程。

用法：
    abaqus cae script=ibeam_gui.py
"""
import os
from abaqus import mdb, session
from abaqusConstants import *
import sketch
import part
import material
import section
import assembly
import mesh
import step
import interaction
import load
import job

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 参数
L = 2000.0      # 梁长 mm
h = 200.0       # 截面高 mm
b = 100.0       # 翼缘宽 mm
tw = 7.0        # 腹板厚 mm
tf = 11.4       # 翼缘厚 mm
P = 10000.0     # 跨中集中力 N (10 kN)
SEED = 12.0     # 网格尺寸 mm


def pause(msg):
    """弹出确认框，让用户看清当前这一步的结果。"""
    try:
        getInput(msg, 'OK')
    except Exception:
        pass


def fit(obj):
    try:
        vp = session.viewports['Viewport: 1']
        vp.setValues(displayedObject=obj)
        vp.view.fitView()
        vp.view.setValues(session.views['Iso'])
    except Exception:
        pass


# ---------------------------------------------------------------- 0 模型
mname = 'IBeam'
if mname in mdb.models.keys():
    m = mdb.models[mname]
else:
    m = mdb.Model(name=mname)

# ---------------------------------------------------------------- 1 几何
sk = m.ConstrainedSketch(name='I-section', sheetSize=600.0)
sk.Line((0.0, 0.0), (b, 0.0))
sk.Line((b, 0.0), (b, tf))
sk.Line((b, tf), (b / 2.0 + tw / 2.0, tf))
sk.Line((b / 2.0 + tw / 2.0, tf), (b / 2.0 + tw / 2.0, h - tf))
sk.Line((b / 2.0 + tw / 2.0, h - tf), (b, h - tf))
sk.Line((b, h - tf), (b, h))
sk.Line((b, h), (0.0, h))
sk.Line((0.0, h), (0.0, h - tf))
sk.Line((0.0, h - tf), (b / 2.0 - tw / 2.0, h - tf))
sk.Line((b / 2.0 - tw / 2.0, h - tf), (b / 2.0 - tw / 2.0, tf))
sk.Line((b / 2.0 - tw / 2.0, tf), (0.0, tf))
sk.Line((0.0, tf), (0.0, 0.0))

p = m.Part(name='Beam', dimensionality=THREE_D, type=DEFORMABLE_BODY)
p.BaseSolidExtrude(sketch=sk, depth=L)
fit(p)
pause('第 1 步：I20a 截面草图已拉伸成 2 m 长的梁\n'
      '(h=200 b=100 tw=7 tf=11.4)\n\n点 OK 继续 —— 下一步：Q235 材料')

# ---------------------------------------------------------------- 2 材料
m.Material(name='Q235')
m.materials['Q235'].Elastic(table=((206000.0, 0.3),))
m.materials['Q235'].Density(table=((7.85e-9,),))
m.HomogeneousSolidSection(name='Sec-Q235', material='Q235', thickness=None)
p.SectionAssignment(region=(p.cells,), sectionName='Sec-Q235')
pause('第 2 步：Q235 材料（E=206000 MPa, v=0.3）已赋给梁体\n\n'
      '点 OK 继续 —— 下一步：装配')

# ---------------------------------------------------------------- 3 分区
# 在跨中切一刀：这样跨中会出现一个内部面，集中力才能通过耦合面传进去
dp = p.DatumPlaneByPrincipalPlane(principalPlane=XYPLANE, offset=L / 2.0)
p.PartitionCellByDatumPlane(datumPlane=p.datums[dp.id], cells=p.cells[:])

# 支座：两端下翼缘底边（模拟支座接触线，允许端部转动）
tol = 0.01
p.Set(name='Set-EndA',
      edges=p.edges.getByBoundingBox(yMin=-tol, yMax=tol, zMin=-tol, zMax=tol))
p.Set(name='Set-EndB',
      edges=p.edges.getByBoundingBox(yMin=-tol, yMax=tol,
                                     zMin=L - tol, zMax=L + tol))
fit(p)
pause('第 3 步：跨中已分区（切开一个内部面，用来传集中力）\n'
      '并已建好两端支座位置\n\n点 OK 继续 —— 下一步：装配')

# ---------------------------------------------------------------- 4 装配 + 加载点
a = m.rootAssembly
inst = a.Instance(name='Beam-1', part=p, dependent=ON)

# 跨中参考点 + 耦合，用于施加集中力
rp = a.ReferencePoint(point=(b / 2.0, h, L / 2.0))
a.Set(name='Set-RP', referencePoints=(a.referencePoints[rp.id],))
mid_faces = inst.faces.getByBoundingBox(zMin=L / 2.0 - 0.1, zMax=L / 2.0 + 0.1)
a.Set(name='Set-MidFace', faces=mid_faces)
m.Coupling(name='Coup-Mid',
           controlPoint=a.sets['Set-RP'],
           surface=a.sets['Set-MidFace'],
           influenceRadius=WHOLE_SURFACE,
           couplingType=KINEMATIC,
           u1=ON, u2=ON, u3=ON, ur1=ON, ur2=ON, ur3=ON)
fit(a)
pause('第 4 步：跨中已分区，并建好参考点（耦合到跨中截面）\n'
      '集中力将施加在这个参考点上\n\n点 OK 继续 —— 下一步：划分网格')

# ---------------------------------------------------------------- 5 网格
p.seedPart(size=SEED, deviationFactor=0.1, minSizeFactor=0.1)
p.setMeshControls(regions=p.cells[:], technique=SWEEP, algorithm=MEDIAL_AXIS)
p.setElementType(
    elemTypes=(mesh.ElemType(elemCode=C3D8R, elemLibrary=STANDARD),),
    regions=(p.cells[:],))
p.generateMesh()
fit(a)
pause('第 5 步：网格划分完成（C3D8R，种子 %g mm）\n\n'
      '点 OK 继续 —— 下一步：支座与荷载' % SEED)

# ---------------------------------------------------------------- 6 边界载荷
m.StaticStep(name='Load', previous='Initial', nlgeom=OFF, description='跨中 10kN')

# A 端铰支座：约束三个平动
m.DisplacementBC(name='Support-A', createStepName='Initial',
                 region=inst.sets['Set-EndA'],
                 u1=0.0, u2=0.0, u3=0.0)
# B 端滚动支座：释放轴向 U3
m.DisplacementBC(name='Support-B', createStepName='Initial',
                 region=inst.sets['Set-EndB'],
                 u1=0.0, u2=0.0)
# 跨中集中力，方向 -Y
m.ConcentratedForce(name='P-Mid', createStepName='Load',
                    region=a.sets['Set-RP'], cf2=-P)
fit(a)
pause('第 6 步：简支边界（左端铰、右端滚动）+ 跨中 10 kN 向下集中力\n\n'
      '点 OK 继续 —— 下一步：创建分析作业')

# ---------------------------------------------------------------- 7 作业
mdb.Job(name='Job-IBeam', model=mname,
        description='Simply supported I20a beam, 10kN at midspan',
        numCpus=4, numDomains=4, memory=80)
mdb.saveAs(pathName=os.path.join(BASE, 'ibeam.cae'))
pause('建模全部完成，已保存为 ibeam.cae（与脚本同目录）\n'
      '分析作业 Job-IBeam 已创建。\n\n'
      '点 OK 结束脚本。之后可在 Job Manager 里点 Submit 提交求解。')
