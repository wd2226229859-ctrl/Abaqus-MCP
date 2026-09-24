# -*- coding: utf-8 -*-
"""
Abaqus MCP Server
=================
让 AI 通过 MCP 协议驱动本机 Abaqus 2024 进行有限元建模 / 求解 / 后处理。

设计要点
--------
1. 本 Server 跑在本机 Python 3.13 上（与 WorkBuddy 一致），Abaqus 自带的
   Python 3.10 只用来执行内核脚本，两者通过 subprocess 解耦，互不污染。
2. 所有建模操作通过 `abaqus cae -noGUI=<script>` 执行。内核每次启动都会
   丢失内存状态，因此以 **.cae 文件作为会话载体**：脚本执行前 openMdb，
   执行后 saveAs。这样多次调用可以连续推进同一个模型。
3. 脚本内部的输出（print）与异常会被捕获并以 JSON 形式回传，AI 能直接
   看到建模过程中的反馈与报错。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import traceback
import uuid
from pathlib import Path

from mcp.server.mcpserver import MCPServer

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
ABAQUS_BAT = os.environ.get("ABAQUS_BAT", r"D:\SIMULIA\Commands\abaqus.bat")
HOME = Path(os.environ.get("ABAQUS_MCP_HOME", str(Path.home() / "abaqus-mcp")))
WORKDIR = HOME / "work"
WORKDIR.mkdir(parents=True, exist_ok=True)

# 关键修复: Abaqus 的临时目录若落在含单引号/特殊字符的路径下（例如 Windows 用户名
# 带单引号，默认会指向 C:\Users\<用户名>\AppData\Local\Temp），pre.exe 会静默失败，
# 表现为 "Analysis Input File Processor exited with an error" 且求解空转不产出结果帧。
# 这里强制把临时目录指向一个干净路径。
ABAQUS_TMPDIR = os.environ.get("ABAQUS_TMPDIR") or r"D:\AbaqusTmp"

mcp = MCPServer(
    "abaqus",
    instructions=(
        "本机 Abaqus 2024 建模驱动。典型流程: "
        "1) abaqus_status 体检 -> 2) abaqus_new_model 建会话 -> "
        "3) abaqus_run_script 分步建模(几何/材料/截面/装配/网格/分析步/载荷/Job) -> "
        "4) abaqus_inspect 自查 -> 5) abaqus_submit_job 求解 -> 6) abaqus_read_results 取结果。"
        "不确定 API 就先调 abaqus_cheatsheet。"
    ),
)


# --------------------------------------------------------------------------
# 底层：调用 Abaqus 内核
# --------------------------------------------------------------------------
_RUNNER = r'''
# -*- coding: utf-8 -*-
import json, os, sys, traceback

_OUT_PATH = __MCP_OUT__
_MODEL = __MCP_MODEL__
_SAVE = __MCP_SAVE__

_logs = []
_result = None
_code = 0


def _emit():
    try:
        with open(_OUT_PATH, "w", encoding="utf-8") as fh:
            json.dump({"exit": _code, "logs": _logs, "result": _result},
                      fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


class _Tee(object):
    """把用户脚本里的 print 收集起来回传给 AI。"""
    def __init__(self, sink):
        self._sink = sink
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._sink.append(line.rstrip("\r"))
        return len(s)

    def flush(self):
        if self._buf.strip():
            self._sink.append(self._buf.rstrip("\r"))
        self._buf = ""


def _save_as(mdb_obj, path):
    """保存 .cae。已存在时先改名归档（不用 os.remove —— 某些宿主环境会拦截删除）。"""
    import time as _t
    try:
        mdb_obj.saveAs(pathName=path)
        return True, "saved"
    except Exception as exc:
        if not os.path.exists(path):
            raise
        arch = path + ".bak_" + _t.strftime("%Y%m%d_%H%M%S")
        i = 0
        while os.path.exists(arch):
            i += 1
            arch = "%s.bak_%s_%d" % (path, _t.strftime("%Y%m%d_%H%M%S"), i)
        try:
            os.rename(path, arch)
        except Exception:
            raise exc
        mdb_obj.saveAs(pathName=path)
        return True, "saved (old file archived to %s)" % os.path.basename(arch)


try:
    from abaqus import mdb, session
    from abaqusConstants import *          # noqa: F401,F403  (暴露给用户脚本)

    _stdout = sys.stdout
    _stderr = sys.stderr
    sys.stdout = _Tee(_logs)
    sys.stderr = _Tee(_logs)

    _user_error = None
    try:
        if _MODEL and os.path.exists(_MODEL):
            mdb.close()
            openMdb(pathName=_MODEL)
            _logs.append("[kernel] opened existing model: %s" % _MODEL)
        elif _MODEL and not os.path.exists(_MODEL):
            _logs.append("[kernel] model not found, will create new: %s" % _MODEL)

        try:
            __user_src = __MCP_SRC__
            exec(compile(__user_src, "<mcp-script>", "exec"), globals())
        except Exception:
            # 脚本报错也保留模型现状，方便从断点继续改，而不是整段回滚。
            _user_error = traceback.format_exc()
            _logs.append(_user_error)
            _logs.append("[kernel] 脚本执行出错，但模型状态已尽力保留，可修正脚本后重跑。")

        if "__result__" in globals():
            try:
                json.dumps(globals()["__result__"])
                _result = globals()["__result__"]
            except Exception:
                _result = repr(globals()["__result__"])

        if _SAVE and _MODEL:
            _ok, _msg = _save_as(mdb, _MODEL)
            _logs.append("[kernel] %s: %s" % (_msg, _MODEL))

        if _user_error is not None:
            _code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stdout = _stdout
        sys.stderr = _stderr
        try:
            mdb.close()
        except Exception:
            pass

except Exception:
    _code = 1
    _logs.append(traceback.format_exc())

_emit()
'''


def _subprocess_env() -> dict:
    """构造子进程环境：把 Abaqus 临时目录指到无特殊字符的路径。"""
    try:
        Path(ABAQUS_TMPDIR).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    env = os.environ.copy()
    # 关键修复: Abaqus 的 job tmpdir（见 Job-1.env 里的 tmpdir=）由 Windows 的
    # TEMP/TMP 推导而来，只设 ABAQUS_TMPDIR 不够。默认 TEMP 是
    # C:\Users\<用户名>\AppData\Local\Temp，路径里的单引号会让 pre.exe 直接失败，
    # 表现为 "Analysis Input File Processor exited with an error" 且
    # .dat/.sta/.msg 都不完整。这里把临时目录整体搬到干净路径。
    env["ABAQUS_TMPDIR"] = ABAQUS_TMPDIR
    env["TMPDIR"] = ABAQUS_TMPDIR
    env["TEMP"] = ABAQUS_TMPDIR
    env["TMP"] = ABAQUS_TMPDIR
    env["PYTHONIOENCODING"] = "utf-8"
    # 关键: 宿主（WorkBuddy 等）常通过 PYTHONPATH 注入自己的 sitecustomize，
    # 而 Abaqus 的 driver 也是 Python 写的，会被这段注入污染，表现为求解器
    # 跑到 pre.exe 就静默退出。这里把这些注入变量全部摘掉。
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
                "PYTHONWARNINGS", "PYTHONOPTIMIZE", "PYTHONNOUSERSITE",
                "PYTHONDEBUG", "PYTHONVERBOSE", "PYTHONINSPECT", "PYTHONUSERBASE"):
        env.pop(key, None)
    return env


def _archive_job_files(job_name: str, keep: tuple = ()) -> list:
    """把同名的历史作业文件改名归档（不删除，只改名）。

    两处调用：
      1. 写 .inp 之前 —— 残留的 .lck / .sim / .odb_f 会让 writeInput 抛
         AbaqusShutdown 而不落盘；
      2. 求解之前（keep=(".inp",)）—— writeInput 自己会留下 Job-1.lck，
         带着这把锁去提交求解会被 Abaqus 直接拒绝（"Detected lock file"）。
    """
    arch = WORKDIR / "_old"
    arch.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    moved = []
    for f in sorted(WORKDIR.glob(job_name + ".*")):
        if f.suffix.lower() == ".cae":
            continue
        if f.suffix.lower() in keep:
            continue
        dst = arch / ("%s.%s" % (f.name, ts))
        i = 0
        while dst.exists():
            i += 1
            dst = arch / ("%s.%s_%d" % (f.name, ts, i))
        try:
            os.rename(str(f), str(dst))
            moved.append(f.name)
        except OSError:
            pass
    return moved


def _run_cae(src: str, model: str | None, save: bool, timeout: int) -> dict:
    """在 Abaqus CAE 内核中执行一段 Python，返回结构化结果。"""
    model_arg = None
    if model:
        model_arg = str(Path(model))
        if not model_arg.lower().endswith(".cae"):
            model_arg += ".cae"

    # 临时脚本放在 _tmp/ 下且不做删除（宿主环境会拦截批量删除，且这些脚本
    # 本身是可复现的建模记录，留着便于排查）。可定期手动清理该目录。
    tmpdir = WORKDIR / "_tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S") + "_" + uuid.uuid4().hex[:6]
    script_path = tmpdir / f"_run_{stamp}.py"
    out_path = tmpdir / f"_run_{stamp}.json"

    # 用占位符替换而不是 % 格式化：模板内含 %s 之类会被误解析。
    # src 必须最后替换，避免用户代码里出现同名 token 时被二次替换。
    full = (_RUNNER
            .replace("__MCP_OUT__", repr(str(out_path)))
            .replace("__MCP_MODEL__", repr(model_arg))
            .replace("__MCP_SAVE__", repr(bool(save)))
            .replace("__MCP_SRC__", repr(src)))
    script_path.write_text(full, encoding="utf-8")

    cmd = [ABAQUS_BAT, "cae", f"noGUI={script_path}"]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(WORKDIR),
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        elapsed = round(time.time() - t0, 1)
        tail = (proc.stdout or "")[-3000:]
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "elapsed_sec": round(time.time() - t0, 1),
            "logs": [],
            "result": None,
            "error": f"执行超时（超过 {timeout} 秒）。可能是求解/网格过大，请拆分步骤或调大 timeout。",
        }

    payload = {}
    if out_path.exists():
        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}

    logs = payload.get("logs", [])
    ok = payload.get("exit", 1) == 0
    return {
        "ok": ok,
        "elapsed_sec": elapsed,
        "logs": logs[-200:],
        "result": payload.get("result"),
        "error": None if ok else ("\n".join(logs[-40:]) or "未知错误"),
        "stderr_tail": tail[-1200:] if tail else "",
    }


# --------------------------------------------------------------------------
# 工具 1：环境体检
# --------------------------------------------------------------------------
def abaqus_status() -> str:
    """检查本机 Abaqus 是否可用：版本、安装路径、许可证、工作目录。排障第一步先调用它。"""
    info = {
        "abaqus_bat": ABAQUS_BAT,
        "bat_exists": os.path.exists(ABAQUS_BAT),
        "workdir": str(WORKDIR),
        "abaqus_tmpdir": ABAQUS_TMPDIR,
        "abaqus_tmpdir_exists": os.path.isdir(ABAQUS_TMPDIR),
    }
    try:
        p = subprocess.run(
            [ABAQUS_BAT, "information=release"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, env=_subprocess_env(),
        )
        out = (p.stdout or "") + (p.stderr or "")
        for line in out.splitlines():
            if line.startswith("Abaqus 20") or "Abaqus is located" in line:
                info.setdefault("release", line.strip())
        info["release_ok"] = "Abaqus 20" in out
    except Exception as exc:
        info["release_ok"] = False
        info["release_error"] = str(exc)

    info["cae_files"] = sorted(str(p) for p in WORKDIR.glob("*.cae"))
    info["odb_files"] = sorted(str(p) for p in WORKDIR.glob("*.odb"))
    return json.dumps(info, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 工具 2：核心 —— 执行建模脚本
# --------------------------------------------------------------------------
def abaqus_run_script(
    script: str,
    model_path: str | None = None,
    save: bool = True,
    timeout: int = 900,
) -> str:
    """
    在 Abaqus CAE 内核里执行 Python 建模脚本（最重要的工具）。

    执行环境已预先注入: mdb, session, 以及 abaqusConstants 的全部常量
    (如 TWO_D_PLANAR, DEFORMABLE_BODY, STRESS, ANALYSIS ...)，无需自己 import。

    会话机制:
      - 传入 model_path（相对路径基于工作目录，见 abaqus_status 的 workdir 字段）
        若文件存在则自动 openMdb，脚本结束后自动保存。
      - 不传 model_path 则操作临时内存模型，结束后不落盘（适合试验/查询 API 行为）。

    输出约定:
      - 用 print() 输出的信息会被收集并返回给你。
      - 若要返回结构化数据，把值赋给 __result__（需可 JSON 序列化）。

    典型脚本骨架:
        m = mdb.models['Model-1'] if 'Model-1' in mdb.models.keys() else mdb.Model(name='Model-1')
        m.Material(name='Steel'); m.materials['Steel'].Elastic(table=((210000.0, 0.3),))
        print(m.materials.keys())
        __result__ = {'materials': list(m.materials.keys())}

    Args:
        script: 要执行的 Python 代码（Abaqus 2024 内核，Python 3.10 语法）
        model_path: .cae 文件路径，作为跨调用的持久化会话
        save: 是否保存回 model_path
        timeout: 超时秒数，默认 900
    """
    return json.dumps(_run_cae(script, model_path, save, timeout),
                      ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 工具 3：新建模型
# --------------------------------------------------------------------------
def abaqus_new_model(model_path: str, overwrite: bool = False) -> str:
    """
    新建一个空白 .cae 模型文件，作为后续建模的会话载体。

    Args:
        model_path: 目标 .cae 路径（可用相对路径，基于工作目录）
        overwrite: 已存在时是否覆盖
    """
    p = Path(model_path)
    if not p.is_absolute():
        p = WORKDIR / p
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() != ".cae":
        p = p.with_suffix(".cae")
    if p.exists() and not overwrite:
        return json.dumps({"ok": False, "error": f"已存在 {p}，如要重建请设 overwrite=true"},
                          ensure_ascii=False)

    src = (
        "mdb.Model(name='Model-1')\n"
        "print('created Model-1')\n"
        "__result__ = list(mdb.models.keys())\n"
    )
    res = _run_cae(src, str(p), True, 300)
    res["model_path"] = str(p)
    return json.dumps(res, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 工具 4：模型结构体检
# --------------------------------------------------------------------------
_INSPECT_SRC = r'''
def _summary():
    out = {"models": []}
    for mname, m in mdb.models.items():
        entry = {"name": mname, "parts": [], "materials": [], "sections": [],
                 "profiles": [], "steps": [], "jobs": [], "instances": []}
        try:
            _mats = []
            _MAT_TRAITS = ("elastic", "density", "plastic", "damping", "expansion",
                           "conductivity", "specificHeat", "userMaterial", "hyperelastic",
                           "creep", "swelling", "viscoelastic", "damageEvolution")
            for _k, _v in m.materials.items():
                _mats.append({"name": _k,
                              "behaviors": [t for t in _MAT_TRAITS if hasattr(_v, t)]})
            entry["materials"] = _mats
        except Exception as e:
            entry["materials"] = ["<err> %s" % e]
        try:
            entry["sections"] = list(m.sections.keys())
        except Exception as e:
            entry["sections"] = ["<err> %s" % e]
        try:
            entry["profiles"] = list(m.profiles.keys())
        except Exception:
            pass
        try:
            entry["steps"] = [{"name": k, "procedure": type(v).__name__}
                              for k, v in m.steps.items()]
        except Exception as e:
            entry["steps"] = ["<err> %s" % e]
        try:
            entry["jobs"] = [k for k, _j in mdb.jobs.items()
                             if getattr(_j, "model", mname) == mname]
        except Exception:
            pass
        for pname, p in m.parts.items():
            pe = {"name": pname, "isMeshed": False, "nodes": 0, "elements": 0}
            try:
                pe["cells"] = len(p.cells)
            except Exception:
                pe["cells"] = 0
            try:
                pe["faces"] = len(p.faces)
            except Exception:
                pe["faces"] = 0
            try:
                pe["vertices"] = len(p.vertices)
            except Exception:
                pe["vertices"] = 0
            try:
                _bb = p.getBoundingBox()
                if isinstance(_bb, dict) and "low" in _bb:
                    pe["bbox"] = {k: [round(float(c), 4) for c in _bb[k]]
                                  for k in ("low", "high")}
                elif isinstance(_bb, (list, tuple)) and len(_bb) == 2:
                    pe["bbox"] = {"low": [round(float(c), 4) for c in _bb[0]],
                                  "high": [round(float(c), 4) for c in _bb[1]]}
                else:
                    pe["bbox"] = repr(_bb)
            except Exception as e:
                pe["bbox_error"] = str(e)
            try:
                pe["nodes"] = len(p.nodes)
                pe["elements"] = len(p.elements)
                pe["isMeshed"] = pe["elements"] > 0
            except Exception:
                pass
            try:
                pe["sets"] = list(p.sets.keys())
                pe["surfaces"] = list(p.surfaces.keys())
            except Exception:
                pass
            entry["parts"].append(pe)
        try:
            entry["instances"] = list(m.rootAssembly.instances.keys())
        except Exception:
            pass
        try:
            entry["bc_loads"] = {
                "boundaryConditions": list(m.boundaryConditions.keys()),
                "loads": list(m.loads.keys()),
                "interactions": list(m.interactions.keys()),
                "constraints": list(m.constraints.keys()),
            }
        except Exception:
            pass
        out["models"].append(entry)
    return out

__result__ = _summary()
'''


def abaqus_inspect(model_path: str) -> str:
    """
    读取一个 .cae 文件的结构化摘要：模型/部件/几何统计/包围盒/材料/截面/分析步/
    载荷边界条件/Instance/网格数量。建模后用来自查是否符合预期。

    Args:
        model_path: .cae 文件路径
    """
    return json.dumps(_run_cae(_INSPECT_SRC, model_path, False, 300),
                      ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 工具 5：提交求解
# --------------------------------------------------------------------------
_WRITE_INP_SRC = r'''
import os, traceback
_job = __MCP_JOB__
_logs = []
_inp = os.path.join(os.getcwd(), _job + ".inp")


def _log(s):
    _logs.append(s)


def _mtime():
    return os.path.getmtime(_inp) if os.path.exists(_inp) else None


_ok = False
try:
    # 注意: Job 挂在 mdb 上，不属于某个 Model
    if _job not in mdb.jobs.keys():
        _log("job %s not found" % _job)
        _log("available jobs: %s" % list(mdb.jobs.keys()))
        raise KeyError(_job)
    j = mdb.jobs[_job]
    # 实测: 默认参数最稳；consistencyChecking=OFF 会触发内核 AbaqusShutdown。
    # 依次尝试，只要 .inp 时间戳刷新就算成功。
    for _tag, _call in (("default", lambda: j.writeInput()),
                        ("ON", lambda: j.writeInput(consistencyChecking=ON)),
                        ("OFF", lambda: j.writeInput(consistencyChecking=OFF))):
        _before = _mtime()
        try:
            _call()
        except BaseException as _ce:
            _log("writeInput(%s) 抛出 %s: %s" % (_tag, type(_ce).__name__, _ce))
            continue
        if _mtime() != _before or _before is None:
            _ok = True
            _log("inp written (%s) for %s (model=%s)"
                 % (_tag, _job, getattr(j, "model", "?")))
            break
        _log("writeInput(%s) 返回但 .inp 未刷新" % _tag)
except BaseException as _e:
    # Abaqus 内核写完 inp / 提交完 job 后会触发 shutdown，这是它的正常收尾行为，
    # 不是真的失败。真正的判定交给外层：看 .inp 时间戳有没有刷新。
    if "AbaqusShutdown" in type(_e).__name__:
        _log("[kernel] writeInput 期间内核退出（AbaqusShutdown），"
             "以 .inp 是否落盘为准判断是否成功。")
    else:
        _log(traceback.format_exc())

__result__ = {"ok": _ok, "logs": _logs}
'''


def abaqus_submit_job(
    model_path: str,
    job_name: str,
    cpus: int = 4,
    save_first: bool = True,
    timeout: int = 3600,
) -> str:
    """
    求解一个 Abaqus 作业（作业需先由建模脚本用 mdb.Job(...) 创建）。

    实现方式：先用 CAE 内核把模型写成 .inp，再用命令行 abaqus job=... interactive
    真正求解 —— 比在 CAE 里 waitForCompletion 稳定（后者在 noGUI 下会抛
    AbaqusShutdown），且 cpus 参数真正生效。

    完成后用 abaqus_read_results 读取 .odb 结果。

    Args:
        model_path: .cae 路径
        job_name: 已在模型中创建的 job 名称
        cpus: CPU 核数
        save_first: 写 inp 前先保存 cae
        timeout: 超时秒数，默认 3600
    """
    res: dict = {"job": job_name, "stage": "write_inp"}
    res["archived_before"] = _archive_job_files(job_name)
    inp_file = WORKDIR / f"{job_name}.inp"
    old_mtime = inp_file.stat().st_mtime if inp_file.exists() else None

    src = _WRITE_INP_SRC.replace("__MCP_JOB__", repr(job_name))
    r1 = _run_cae(src, model_path, save_first, 900)
    res["write_inp"] = r1
    res["inp"] = str(inp_file)

    # 判定标准统一用 .inp 时间戳是否刷新：
    # CAE 写 inp 后往往会抛 AbaqusShutdown，不代表失败。
    inp_fresh = inp_file.exists() and inp_file.stat().st_mtime != old_mtime
    res["inp_fresh"] = inp_fresh

    if not inp_fresh:
        payload = r1.get("result") or {}
        res["ok"] = False
        res["error"] = "生成 .inp 失败：" + (
            "\n".join(payload.get("logs", [])[-20:])
            or r1.get("error") or "未知原因")
        return json.dumps(res, ensure_ascii=False, indent=2)

    # 命令行求解：与 CAE 解耦，更稳、可超时、可采日志
    # 先清掉 writeInput 留下的 .lck / .sim 等，否则 Abaqus 会拒绝提交。
    res["stage"] = "solve"
    res["archived_locks"] = _archive_job_files(job_name, keep=(".inp",))
    # 两套命令依次尝试。优先 mp_mode=threads：默认的 MPI 会 fork 子进程，父
    # abaqus.bat 可能提前返回；退回最朴素的写法再试一次。
    attempts = [
        [ABAQUS_BAT, f"job={job_name}", f"cpus={cpus}", "mp_mode=threads", "interactive"],
        [ABAQUS_BAT, f"job={job_name}", "interactive"],
    ]
    tried = []
    res["ok"] = False
    for cmd in attempts:
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=str(WORKDIR), env=_subprocess_env(),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            tried.append({"cmd": " ".join(cmd[1:]), "error": f"超时 {timeout}s"})
            continue
        text = (proc.stdout or "") + (proc.stderr or "")
        rec = {
            "cmd": " ".join(cmd[1:]),
            "elapsed_sec": round(time.time() - t0, 1),
            "returncode": proc.returncode,
            "completed": "COMPLETED" in text,
            "stdout_tail": (proc.stdout or "")[-2000:],
        }
        tried.append(rec)
        res["elapsed_sec"] = rec["elapsed_sec"]
        res["returncode"] = rec["returncode"]
        res["solve_stdout_tail"] = rec["stdout_tail"]
        if rec["completed"] and proc.returncode == 0:
            res["ok"] = True
            break
    res["attempts"] = tried
    if not res["ok"]:
        res["error"] = (
            "求解未正常完成（通常在 pre.exe 阶段报错）。按可能性排查：\n"
            "1) 作业临时目录含特殊字符：若系统用户名含单引号，Abaqus 会把 tmpdir 拼成 "
            "含单引号的路径，导致 pre.exe 静默失败。本 Server 已把 TEMP/TMP 指到 "
            f"{ABAQUS_TMPDIR}，若仍失败可在工作目录的 abaqus_v6.env 里自行指定；\n"
            "2) 残留锁文件 Job-1.lck：已自动归档到 work/_old，可手动清理；\n"
            "3) 宿主沙箱限制子进程：改用 manual_command 在系统终端里执行。"
        )
        res["manual_command"] = f'cd /d "{WORKDIR}" && abaqus job={job_name} interactive'

    # 附上关键日志尾部，便于判断收敛/报错
    extra = {}
    for ext in (".sta", ".msg", ".dat", ".log"):
        f = WORKDIR / f"{job_name}{ext}"
        if not f.exists():
            continue
        try:
            extra[ext] = f.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        except Exception:
            pass
    res["job_files"] = extra
    res["odb"] = str(WORKDIR / f"{job_name}.odb")
    res["odb_exists"] = os.path.exists(res["odb"])
    return json.dumps(res, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 工具 6：读取 ODB 结果
# --------------------------------------------------------------------------
_READ_ODB_SRC = r'''
from odbAccess import openOdb

_path = __MCP_ODB__
_step = __MCP_STEP__
_fields = __MCP_FIELDS__
_max_points = __MCP_MAXPOINTS__
_print_summary = __MCP_SUMMARY__

res = {"odb": _path, "steps": [], "frame_count": 0, "frames": []}
odb = openOdb(_path)
try:
    sa = odb.steps
    keys = list(sa.keys())
    res["steps"] = [{"name": k,
                     "procedure": getattr(sa[k], "procedure", ""),
                     "description": getattr(sa[k], "description", ""),
                     "numFrames": len(sa[k].frames)} for k in keys]
    if not keys:
        raise RuntimeError("这个 odb 里没有分析步")
    sname = _step if (_step and _step in keys) else keys[-1]
    res["active_step"] = sname
    frames = sa[sname].frames
    res["frame_count"] = len(frames)

    def _sample(fo):
        vals, worst, worst_label = [], 0.0, None
        comps = list(fo.componentLabels) if fo.componentLabels else []
        for v in fo.values:
            try:
                lbl = int(v.nodeLabel)
                data = [float(x) for x in v.data]
            except Exception:
                continue
            mag = max(abs(d) for d in data) if data else 0.0
            if mag > worst:
                worst, worst_label = mag, lbl
            vals.append({"label": lbl, "data": [round(d, 6) for d in data]})
        n = len(vals)
        if n > _max_points:
            k = max(1, n // _max_points)
            vals = vals[::k][:_max_points]
        return {"count": n,
                "components": comps,
                "abs_max_value": round(worst, 6),
                "abs_max_at_node": worst_label,
                "sample": vals}

    for idx, fr in enumerate(frames):
        info = {"frame": idx}
        try:
            raw = list(getattr(fr, "value", []) or [])
            info["frame_value"] = [round(float(v), 6) for v in raw]
        except Exception:
            info["frame_value"] = None
        try:
            avail = list(fr.fieldOutputs.keys())
        except Exception:
            avail = []
        if _print_summary:
            info["available_fields"] = avail
        if _summary:
            res["available_fields_last_frame"] = avail
            break
        for fname in _fields:
            try:
                info[fname] = _sample(fr.fieldOutputs[fname])
            except Exception as e:
                info[fname] = "<err> %s" % e
        res["frames"].append(info)
finally:
    try:
        odb.close()
    except Exception:
        pass

__result__ = res
'''


def abaqus_read_results(
    odb_path: str,
    step_name: str | None = None,
    fields: str = "U,RF,S",
    max_points: int = 200,
    summary_only: bool = False,
    timeout: int = 900,
) -> str:
    """
    读取分析结果 .odb：列出所有分析步/帧，并提取指定场变量（抽样，不会灌入全量节点）。

    场变量名示例: U(位移) U1 U2 U3 UR1(转角) RF(支反力) S(应力) E(应变) PEEQ IVOL EVOL GLME ...
    不确定有哪些变量时先用 summary_only=true 查看 available_fields。
    注意: 读 odb 每次会启动一次 Abaqus 内核，通常 20-60 秒。

    Args:
        odb_path: .odb 文件路径（相对路径基于工作目录）
        step_name: 分析步名，留空取最后一个
        fields: 逗号分隔的场变量名，如 "U,RF,S"
        max_points: 每个场变量抽样的节点数上限，防止输出爆炸
        summary_only: 只看有哪些分析步/场变量，不取数值
    """
    p = Path(odb_path)
    if not p.is_absolute():
        p = WORKDIR / p
    if not p.exists():
        return json.dumps({"ok": False, "error": f"找不到 ODB 文件: {p}"}, ensure_ascii=False)

    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    src = (_READ_ODB_SRC
           .replace("__MCP_ODB__", repr(str(p)))
           .replace("__MCP_STEP__", repr(step_name))
           .replace("__MCP_FIELDS__", repr(field_list))
           .replace("__MCP_MAXPOINTS__", repr(max_points))
           .replace("__MCP_SUMMARY__", repr(bool(summary_only))))
    return json.dumps(_run_cae(src, None, False, timeout), ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 工具 7：脚本文件方式执行（长脚本更好用）
# --------------------------------------------------------------------------
def abaqus_run_script_file(
    script_path: str,
    model_path: str | None = None,
    save: bool = True,
    timeout: int = 1800,
) -> str:
    """
    执行一个已存在于磁盘上的 .py 建模脚本（比内联传代码更适合长脚本）。

    Args:
        script_path: .py 文件路径（相对路径基于工作目录）
        model_path: .cae 会话文件
        save: 是否保存
        timeout: 超时秒数
    """
    p = Path(script_path)
    if not p.is_absolute():
        p = WORKDIR / p
    if not p.exists():
        return json.dumps({"ok": False, "error": f"找不到脚本: {p}"}, ensure_ascii=False)
    src = p.read_text(encoding="utf-8")
    return json.dumps(_run_cae(src, model_path, save, timeout),
                      ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 工具 8：建模 API 速查（不确定语法时先看这个）
# --------------------------------------------------------------------------
def abaqus_cheatsheet() -> str:
    """返回 Abaqus Python 建模常用 API 速查表。不确定某个 API 的写法时先调用它。"""
    return r"""
# Abaqus Python 建模速查（本 Server 内核内已自动可用：mdb, session, 所有常量）

## 0. 拿模型对象（务必先做这步，兼容已存在/不存在）
if 'Model-1' in mdb.models.keys():
    m = mdb.models['Model-1']
else:
    m = mdb.Model(name='Model-1')

## 1. 草图 + 部件
# 2D 壳/平面
sk = m.ConstrainedSketch(name='prof', sheetSize=200.0)
sk.rectangle((0, 0), (100, 50))
sk.CircleByCenterPerimeter((50, 25), (60, 25))         # 圆心 + 圆周上一点
sk.Line((0, 0), (100, 0))
p = m.Part(name='P', dimensionality=TWO_D_PLANAR, type=DEFORMABLE_BODY)
p.BaseShell(sketch=sk)

# 3D 拉伸：先从 sketch 建立 base shell，再 extrude
p = m.Part(name='P3', dimensionality=THREE_D, type=DEFORMABLE_BODY)
p.BaseSolidExtrude(sketch=sk, depth=20.0)

## 2. 材料 + 截面 + 赋截面
m.Material(name='Steel')
m.materials['Steel'].Elastic(table=((210000.0, 0.3),))
m.materials['Steel'].Density(table=((7.85e-9,),))      # t/mm^3，模态/重力需要
m.HomogeneousSolidSection(name='Sec', material='Steel', thickness=10.0)
p.SectionAssignment(region=(p.faces,), sectionName='Sec')     # 2D
# p.SectionAssignment(region=(p.cells,), sectionName='Sec')   # 3D

## 3. 装配 + 网格
a = m.rootAssembly
a.Instance(name='P-1', part=p, dependent=True)
p.seedPart(size=5.0, deviationFactor=0.1, minSizeFactor=0.1)
p.setMeshControls(regions=p.faces, technique=STRUCTURED)   # 结构化网格
p.setElementType(elemTypes=..., regions=...)               # 单元类型，按需
p.generateMesh()

## 4. 集合 / 表面（加载前必备）
p.Set(name='Set-Fix', vertices=p.vertices.findAt(((0,0,0),)))
p.Set(name='Set-All', faces=p.faces[:])
p.Surface(name='Surf-Top', side1Faces=p.faces.findAt(...))

## 5. 分析步
m.StaticStep(name='Load', previous='Initial', nlgeom=False)
m.FrequencyStep(name='Modal', previous='Initial', numEigen=10)

## 6. 边界条件 / 载荷
region = a.instances['P-1'].sets['Set-Fix']
m.DisplacementBC(name='Fix', createStepName='Initial', region=region, u1=0, u2=0, u3=0, ur1=0, ur2=0, ur3=0)
m.ConcentratedForce(name='F', createStepName='Load', region=(a.instances['P-1'].vertices[0],), cf2=-1000.0)
m.Pressure(name='P', createStepName='Load', region=(inst.surfaces['Surf-Top'],), magnitude=1.0)

## 7. 作业
mdb.Job(name='Job-1', model='Model-1', numCpus=4, memory=80)
# 然后调用 abaqus_submit_job(model_path=..., job_name='Job-1')

## 8. 常用坑
# - 单位自洽：推荐 mm-t-s 或 mm-t-MPa（力单位 N 时 E=210000 MPa，密度 7.85e-9 t/mm^3）
# - findAt 坐标必须精确落在几何点上，否则找不到对象 -> 多用 getBoundingBox() 辅助
# - 二维梁/分析刚体：ANALYTIC_RIGID_SURFACE / DEFORMABLE_BODY
# - saveAs 前确保没有其他 mdb 句柄占用
"""


# --------------------------------------------------------------------------
# 注册工具
# --------------------------------------------------------------------------
_TOOLS = (
    abaqus_status,
    abaqus_new_model,
    abaqus_run_script,
    abaqus_run_script_file,
    abaqus_inspect,
    abaqus_submit_job,
    abaqus_read_results,
    abaqus_cheatsheet,
)
for _fn in _TOOLS:
    mcp.add_tool(_fn)


if __name__ == "__main__":
    mcp.run("stdio")
