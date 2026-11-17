# 真实 ComfyUI 验收运行手册

本机（开发机）原本没有生产 ComfyUI，本手册最初用于编写验收步骤。用户明确授权后，已在**隔离 CPU 实例** `D:\Projects\ComfyUI-acceptance` 完成安装、真实 1×1、3×5、555/555、安全/立即暂停、定向取消（运行中+排队中）、强杀恢复、四组合导出、追加 5 次、安装/更新/代码回退和正常 Ctrl+C；另在**独立 GPU 实例** `D:\Projects\ComfyUI-gpu-acceptance` 用 DreamShaper8_LCM 完成 1×1/3×5/555/1×101，并在 2026-11-15 完成 HOST06 A—D 与 HOST13 UI01—UI06。结果见 2026-11-15 总报告（本地证据：`test-artifacts/next-20261115/FINAL-REPORT.md`） 与 TEST-REPORT 第 0 节。2026-11-15 PU01/PU02 补齐了证据检查器、完整源码快照与受控卷阶段同步协议；**用户生产 ComfyUI、实际工作流、真实 ENOSPC、目标安装仍未运行。**

以下命令用于在**用户生产 ComfyUI**（或另一台目标机器）重新完成验收；每一步都要求保存命令输出/截图，不能只写“通过”。隔离 CPU tiny 模型结果不能替代生产环境/GPU/真实模型验证。

## 0. 记录环境

```bash
# 用 ComfyUI 的 Python（不是系统 Python）
<COMFY_PYTHON> -c "import sys, folder_paths, comfyui_version; print(sys.executable); print(comfyui_version.__version__); print(folder_paths.get_user_directory()); print(folder_paths.get_input_directory()); print(folder_paths.get_output_directory())"
git -C <COMFY_ROOT> rev-parse HEAD
```

记录到 `docs/ENVIRONMENT.md`：COMFY_ROOT、COMFY_PYTHON、custom_nodes、Python/torch、前端版本、GPU、模型、启动方式/端口。

## 1. 安装

```bash
<COMFY_PYTHON> tools/install_plugin.py --comfy-root <COMFY_ROOT>
# 或
<COMFY_PYTHON> tools/install_plugin.py --comfy-root <COMFY_ROOT> --mode copy
```

确认 `<COMFY_ROOT>/custom_nodes/ComfyUI-Gacha-Master/__init__.py` 存在；不要覆盖未知同名目录。

## 2. 启动与加载证据

启动 ComfyUI（不中断用户生产任务；建议独立测试实例/端口）。保存启动日志中：

- 插件目录被加载；
- `GachaSaveImage` 出现在节点注册；
- `/gacha_master/v1/capabilities` 返回 200；
- 侧栏“抽卡大师”出现，打开无控制台错误。

```bash
curl -s http://127.0.0.1:<PORT>/gacha_master/v1/capabilities
curl -s http://127.0.0.1:<PORT>/gacha_master/v1/health
```

## 3. L1 最小真实工作流（1 张 × 1 次）

1. 用示例 `examples/workflow_img2img.json`，替换为实际可用模型，确认 LoadImage #1、SaveImage #8、KSampler.seed 直接数值。
2. 在 UI 创建任务：1 张原图、每张 1 次；预检通过。
3. 开始，等待完成。检查：
   - `GachaSaveImage` 在 ComfyUI 历史/节点中执行，不是原生 SaveImage；
   - `<results>/JOB-.../SRC-0001/DRAW-000001/ATTEMPT-001-*/OUT-0001.png` 存在；
   - `outputs.complete.json` 的 N、身份、哈希与文件一致；
   - `seed_allocations` 有该 DRAW 的种子；PNG 元数据含 attempt 身份；
   - ComfyUI 历史的终态为 success；任务中心计数 1/1、OUT 1；
   - 原图字节未变（前后 SHA-256）。
4. 把日志、截图、OUT/清单路径、SHA-256 记录进 TEST-REPORT（Q20/Q24 L1）。

## 4. 定向取消验证（只有通过后才能提升能力）

在**测试实例**上：

1. 放一个外部哨兵任务（不是本插件）在队列中；或先让本插件长任务运行，再把哨兵排在其后。
2. 本插件任务处于 running 或 pending 时立即暂停（API `mode=now`）。
3. 观察只取消本插件 prompt ID，外部任务保持并最终 success；running 场景插件应 PAUSED/INTERRUPTED；pending 场景 ComfyUI 不写 history，插件会在 cancel 端点确认后结算为 INTERRUPTED/PAUSED。
4. 保存外部任务 ID、队列前后快照、取消响应、最终历史。通过后才把 `targeted_cancel_verified` 设为 true 并重启；否则保持 false（安全降级）。隔离实例已按此流程通过并记录在 `test-artifacts/real-comfyui/Q04_*.json`。

## 5. L2 真实采样

- Q01：3 源 × 5 次，检查严格 AAAAA → BBBBB → CCCCC。
- Q02：111 源 × 5 次 = 555 真实 DRAW；记录模型、分辨率、步数、GPU、耗时、全部 OUT/清单/种子/计数；不得用复制输入或模拟推理替代。隔离实例已用 CPU tiny 模型完成 555/555；生产环境仍需用真实模型重跑。
- 真实强杀：独立端口/数据目录的测试 ComfyUI，强杀后按恢复协议核验，确认不自动重提；隔离实例已通过，生产实例仍需验证。

## 6. 安装/更新/代码回退（Q33）

更新前停服并同时备份插件代码与数据库（含 WAL/SHM）：

1. 停止提交与计划工作器，备份 `data_root/db`（含 WAL/SHM）和当前代码指纹；
2. 复制安装用：`python tools/install_plugin.py --comfy-root <ROOT> --mode copy --update`。安装器先复制到暂存目录并逐文件校验，旧代码移到 `<COMFY_ROOT>/user/gacha_master/install-backups/`（不在 custom_nodes 内），再切换；失败自动恢复旧目录。`--verify` 输出源码/安装树哈希；启动并确认 `/health` 200、既有 555 任务仍 555/555；
3. 需要回退时停服，删除插件包目录后复制目标 commit 的代码，再启动核验同一数据；
4. **代码回退不等于数据库回退**：旧代码若只支持更低 schema，会拒绝打开升级后的库（`DB_READ_ONLY`/schema 保护）；此时必须使用升级前一致性备份恢复数据库，并保留升级后库、结果、导出与新增文件作为证据；
5. 数据库恢复前记录任务/选择/冻结导出的时间边界；恢复后重新核对任务、选择、冻结成员、接受关系与文件哈希；不得删除 `pre-restore-*` 或安装备份；
6. 恢复当前版本并再次核验；全程不得删除 `data_root`、结果、导出或备份。
   隔离实例证据：`Q33_uninstall_reinstall.json`、`Q33_update_rollback.json`；2026-11-15 安装/更新/校验证据见 `test-artifacts/next-20261115/20261115T081500-np02/install/`。

## 7. 正常 Ctrl+C 关停（Q35）

在**前台真实控制台**启动 ComfyUI，按 Ctrl+C，确认：

- 进程自行退出且 exit code 为 0；
- 监听端口关闭；
- 日志出现 ComfyUI 的 `Stopped server`，无 traceback；
- 重启后 job/备份/队列状态一致，无自动重提。

自动化宿主可能给子进程继承 Windows“忽略 Ctrl+C”标志；这类环境需在启动器里调用 `SetConsoleCtrlHandler(NULL, FALSE)` 后再启动，等同于正常终端。隔离实例证据：`Q35_ctrlc_console.json`、`Q35_ctrlc_console.log`。

## 8. 异步创建与进度（2026-11-14 起）

1. `POST /jobs` 返回 201 时只证明检查点已登记；轮询 `GET /jobs/{id}` 直到 `status=READY`（或 FAILED/PAUSED），再调用 `start`。不要用旧的“创建完立即 start”假设。
2. 检查 `creation.stage` 与 `draws_committed/draws_total`；`draws_committed` 只能来自已提交事务，不能把总需求当成已生成。
3. 在 CREATING 中测试 `POST /jobs/{id}/pause`：应返回 `pausing_creation` 或 `paused_creation`，等待阶段变为 PAUSED，再 `start` 请求继续；继续后仍需等 READY 再显式启动推理。
4. 强杀/重启后 CREATING 应为 PAUSED，不自动继续；数据库 schema 应为 2，旧 v1 库升级前后关键实体计数与文件哈希一致。
5. 规模验收：至少覆盖 1 源×100000、200 源×500、5000 源×20；记录注册 p95、状态查询 p95、计划完成时间；100000 计划不等于 100000 次 GPU 推理。

## 9. 更新状态

把真实命令、时间、commit、环境指纹、结果（PASS/FAIL/NOT_RUN）、证据路径写入 `docs/TEST-REPORT.md`；更新 `docs/ENVIRONMENT.md`、`docs/DEVELOPMENT-STATUS.md`、`docs/KNOWN-ISSUES.md`。只有生产/GPU 相关项通过后，README 才能宣称生产环境全部验收；隔离实例结果须明确标注 CPU tiny 模型。

## 10. 受控卷与真实 ENOSPC（PU03，2026-11-15 新增）

真实写满只允许在**明确归本轮使用、可丢弃**的 NTFS 卷上执行。系统卷、项目卷、用途不明的小卷一律拒绝；工具在写入入口自行复核，跳过 `preflight` 也不能绕过。

```powershell
$ControlledRoot = '<可丢弃的受控 NTFS 目录>'
$Registry = Join-Path $Evidence 'storage\controlled-volumes.json'
# 1) 只读状态；2) 确认序列号后登记（容量上限防止误登记大卷）
python tools\storage_acceptance.py status --path $ControlledRoot
python tools\storage_acceptance.py register --path $ControlledRoot `
    --confirm-volume-serial <实际序列号> --capacity-limit-bytes <字节上限> `
    --registry $Registry --note '<归属与销毁说明>'
# 3) 预检（会写一个微小探针并删除）
python tools\storage_acceptance.py preflight --path $ControlledRoot `
    --registry $Registry --report (Join-Path $Evidence 'storage\PREFLIGHT.json')
```

每个 DISK 子项用阶段同步执行器驱动，不要用固定 `sleep` 猜测窗口：

```powershell
python tools\storage_scenarios.py run --scenario DISK02 --mode enospc `
    --controlled-root $ControlledRoot --registry $Registry --serial <序列号> `
    --target-root (Join-Path $ControlledRoot 'targets\disk02') `
    --work-dir (Join-Path $Evidence 'storage\DISK02-work') `
    --report (Join-Path $Evidence 'storage\DISK02.json') `
    --headroom-bytes 67108864
```

时序：业务进程先报 `ready` → 控制器填充并确认剩余空间 → 释放写入 → 记录真实失败位置/异常 → 控制器清理填充 → 释放恢复 → 业务进程重试并核对不变量。`enospc` 与 `hard-kill` 模式**必须**提供 `--controlled-root/--registry/--serial`。

通过标准：目标业务写入本身因空间不足失败（filler 自己 ENOSPC 不算），释放后原图/已发布 OUT/接受关系/种子/冻结成员/最后有效备份均符合预期。每项保留 BEFORE / FAILURE / AFTER 三组证据。

测试前如调整隔离实例 `disk_reserve`，必须用记录式命令并证明还原：

```powershell
python tools\storage_acceptance.py reserve-set --config <实例 config.json> --value 0 `
    --report (Join-Path $Evidence 'storage\RESERVE.json')
python tools\storage_acceptance.py reserve-restore --record (Join-Path $Evidence 'storage\RESERVE.json')
```

无受控卷时保持 NOT_RUN，不得改写结论。协议自检（`storage_scenarios.py selftest --scenario all`）只验证机制，报告固定标注 `simulated=true`。

## 11. 证据自检与目标资料（PU01/PU04，2026-11-15 新增）

```powershell
# 汇总自检：路径基准、引用、候选身份/包摘要、回归计数、门槛、数据来源、markdown 链接
python tools\check_release_evidence.py --summary (Join-Path $Evidence 'SUMMARY.json') `
    --markdown (Join-Path $Evidence 'FINAL-REPORT.md') `
    --report (Join-Path $Evidence 'EVIDENCE-CHECK.json')

# 候选可重建：在空目录解包交付包与完整源码包，逐文件重建四个 scope
python tools\candidate_manifest.py --verify-snapshot (Join-Path $Evidence 'candidate')

# 目标资料：先生成模板，填写后校验；ready=true 只表示可以开始隔离预验
python tools\target_environment.py template --output (Join-Path $Evidence 'environment\TARGET-ENVIRONMENT.json')
python tools\target_environment.py validate --file (Join-Path $Evidence 'environment\TARGET-ENVIRONMENT.json')
python tools\target_environment.py volumes --output (Join-Path $Evidence 'environment\VOLUMES.json')
```

汇总约定：`path_base` 必须是 `repo`（全部仓库相对）或 `evidence_root` + `evidence_root`（唯一目录），同一文件内不得混用。`DISK0x` 的 PASS 必须带 `enospc_observed=true` 与真实 `data_source`；夹具不得计入生产门槛。
