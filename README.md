# Abaqus MCP

让 AI 通过 MCP 直接驱动本机 Abaqus 2024 建模、求解、读结果。

## 已验证的本机环境

| 项 | 值 |
| --- | --- |
| Abaqus | 2024（`D:\SIMULIA\Commands\abaqus.bat`，自带 Python 3.10.5） |
| 许可证 | 本机 Flexnet，Abaqus/Standard 可用 |
| Server 解释器 | 任意 Python 3.13 虚拟环境（需安装 `mcp` 2.2.0） |
| 工作目录 | 项目根目录下的 `work/`（首次运行自动创建） |
| Abaqus 临时目录 | `D:\AbaqusTmp` |

建模链路已端到端验证：草图 → 部件 → 材料/截面 → 装配 → 集合 → 网格 → 分析步 →
边界/载荷 → Job → 保存 `.cae`，每一步都通过。单次内核调用约 6 秒。

## 提供的工具

| 工具 | 用途 |
| --- | --- |
| `abaqus_status` | 环境体检：版本、路径、临时目录、现有 cae/odb |
| `abaqus_new_model` | 新建空白 `.cae` 作为建模会话 |
| `abaqus_run_script` | **核心**：在 CAE 内核里执行建模脚本 |
| `abaqus_run_script_file` | 执行磁盘上的长脚本 |
| `abaqus_inspect` | 读取模型结构摘要（部件/材料/步/载荷/网格数） |
| `abaqus_submit_job` | 写 INP + 命令行求解 |
| `abaqus_read_results` | 读 `.odb` 结果（分析步/帧/场变量抽样与最大值） |
| `abaqus_cheatsheet` | 建模 API 速查 |

## 典型流程

```
abaqus_status                                  # 体检
abaqus_new_model("beam.cae")                   # 建会话
abaqus_run_script(script=..., model_path="beam.cae")   # 分步建模
abaqus_inspect("beam.cae")                     # 自查
abaqus_submit_job("beam.cae", "Job-1")         # 求解
abaqus_read_results("Job-1.odb", fields="U,S") # 取结果
```

会话机制：内核每次调用都会丢内存状态，因此以 `.cae` 文件作为跨调用的载体 ——
脚本执行前 `openMdb`，执行后 `saveAs`。脚本报错也会保留模型现状，便于断点续改。

## 本机踩过的坑（已固化进 server.py）

1. **临时目录里的单引号**：若 Windows 用户名含单引号（例如 `o'brien`），Abaqus 把作业 tmpdir 拼成
   `…\o'brien_Job-1_13020`，`pre.exe` 会静默失败 —— 只报一句
   "Analysis Input File Processor exited with an error"，`.sta/.msg` 都不生成，
   `.odb` 里 0 个结果帧，极难排查。server 已把 `TEMP/TMP/ABAQUS_TMPDIR`
   指向 `D:\AbaqusTmp`。注意 `tmpdir` **不是** `abaqus_v6.env` 的合法关键字，
   只能通过环境变量/命令行控制。
2. **`writeInput(consistencyChecking=OFF)` 会触发内核 `AbaqusShutdown`** 且不落盘；
   用默认参数即可。内核写完 INP 后抛出 `AbaqusShutdown` 属正常收尾，
   server 以 `.inp` 时间戳是否刷新来判定成功。
3. **Job 挂在 `mdb` 上，不是 Model 的属性**（`mdb.jobs[name]`，不是 `m.jobs`）。
4. **求解前要归档残留文件**：`.lck` / `.sim` / `.odb_f` 会让 Abaqus 拒绝提交
   （"Detected lock file"）；server 会自动改名归档到 `work/_old`，不做删除。
5. **命令行并行**：默认 MPI 会让父 `abaqus.bat` 提前返回，用 `mp_mode=threads`。

## 已知限制

- 求解（`abaqus_submit_job`）在"由终端/脚本嵌套启动的 Python"下可能仍在
  `pre.exe` 阶段失败。若如此，工具会返回 `manual_command`，把它粘到系统终端里
  执行即可；求解完成后再用 `abaqus_read_results` 读结果。
- `work/_tmp`（每步执行的脚本记录）和 `work/_old`（历史作业归档）会持续增长，
  可定期手动清理。

## 手动验证

```powershell
cd <项目根目录>
python _selftest.py
```

## MCP 客户端配置

已写入 `~/.workbuddy/mcp.json`，在 WorkBuddy 的连接器管理页对新出现的
`abaqus` 条目点"信任"后生效。
