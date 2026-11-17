# 需求追溯矩阵（R01—R16 → 实现 → 测试）

状态口径：`L0` = 独立核心/真实文件/SQLite/HTTP mock/浏览器测试台通过；`L1/L2` = 隔离真实 ComfyUI 实例。真实 1×1、3×5、555/555、1×101、安全/立即暂停、定向取消、强杀恢复、四组合导出、追加 5 次、安装/更新/回退、正常 Ctrl+C 已 PASS；独立 GPU + DreamShaper8_LCM 又完成过 1×1、3×5、555；2026-11-15 NP02 完成 HOST06 A—D 与 HOST13 UI01—UI06。用户生产 ComfyUI、实际工作流、真实 ENOSPC 仍 NOT_RUN。实现位置均在本仓库。

正式投用编号与历史编号的对应关系（本轮 PU01—PU06 为主执行线，原编号继续用于追溯）：

| PU | 覆盖的历史门槛 | 本轮证据 |
| --- | --- | --- |
| PU01 证据/候选/交付可复现 | PR11（候选冻结）、G07/G08 | `tools/check_release_evidence.py`、`candidate_manifest --verify-snapshot`、`test-artifacts/next-20261115/ERRATA.md` |
| PU02 存储工具保护与阶段同步 | R16 磁盘部分、G06 工具前置、NP03 工具 | `tools/storage_acceptance.py`、`tools/storage_scenarios.py`、`tests/unit/test_storage_tools.py` |
| PU03 真实 ENOSPC | DISK01—05、G06、Q19/Q23 | **NOT_RUN**（无受控卷） |
| PU04 实际工作流与目标组合 | HOST14、G04/G05、NP04 | **NOT_RUN**（无目标资料） |
| PU05 目标安装/迁移/回退 | G07、NP05、Q33 | **NOT_RUN**（依赖 PU03/PU04） |
| PU06 三个日常周期与放行 | G01—G08 | **NOT_RUN**（依赖 PU05） |

2026-11-16 连续筛选工作包：

| UXR | 范围 | 实现位置 | 本轮证据 | 状态 |
| --- | --- | --- | --- | --- |
| UXR01 收起修复 | 行级结果容器/焦点/迟到响应 | `web/views/detail.js`、`web/styles.css` | `prefix-repro/`（修复前）、`l0/`、`real-host/real01*.json` | **PASS**（真实宿主 COL01/02/05） |
| UXR02 持久会话与事务 | schema v3、断点 API、组确认单事务、版本/幂等 | `backend/schema.py`、`backend/review.py`、`backend/repository.py`、`backend/api.py` | `tests/unit/test_review_sessions.py`、`tests/integration/test_review_api.py`、`real-host/res05/res06` | **PASS**（含真实 R01/R02） |
| UXR03 连续筛选工作区 | 同组预览/多选、两枚组按钮、跨原图导航 | `web/review_session.js`、`web/views/review.js` | `tests/js/review_session.test.mjs`、`real-host/real01*.json` | **PASS** |
| UXR04 恢复与大集合 | 继续入口、后页恢复、多任务、资源预算 | `web/views/detail.js`、`web/collections/result_pager.js` | `real-host/real02*.json`、`fixture-500/*` | **PASS（隔离宿主；500 夹具）** |
| UXR05 真实宿主与文档 | 真实实操、重启/强退、机器记录 | 规格与运行证据 | `test-artifacts/review-ux-20261116/run-20261116T164231/` | **PASS（用户生产 NOT_RUN）** |

| ID | 需求 | 实现位置 | 对应测试 / 证据 | 当前状态 |
| --- | --- | --- | --- | --- |
| R01 | 多图添加、拖拽、尾部空格、顺序、未执行输入移除 | `web/views/create.js`、`backend/inputs.py`、`backend/service.py::remove_source` | 浏览器 L0 UI（添加/尾部空格/←→重排/移除）；`test_scheduler::test_remove_unexecuted_source...`；`test_api` 上传/扫描 | L0 PASS；真实多文件上传/文件夹导入/箭头重排/移除/DOM 拖拽上传 PASS；操作系统原生文件选择器未验证 |
| R02 | 文件夹根目录、稳定排序、固定清单、不递归 | `backend/inputs.py::scan_folder`、`backend/service.py` 文件夹源创建 | `test_reliability::test_folder_scan...`、`test_core::test_natural_sort...`、`test_api::test_upload_scan...` | L0 PASS；真实扫描 2/10/中文/大小写/子目录/坏图 PASS（`Q17_folder_scan.json`） |
| R03 | 默认快照 + 引用模式、哈希验证、保护源文件 | `backend/inputs.py`、`backend/service.py::_materialize_source`、`backend/repository.py` | `test_scheduler::test_reference_mode...`、`test_core::test_png...`、`test_exports::test_changed_source...` | L0 PASS；真实 snapshot 移动原图继续；reference 改内容阻断/运行中使用 staged；快照复制中改源图 409 且无损坏快照 |
| R04 | 绑定原工作流副本、一个输入/最终输出、不改编辑器 | `backend/workflow.py`、`backend/scheduler.py::_claim_and_submit`、`nodes/save_image.py` | `test_core::test_transform_keeps_original...`、`test_integration_register`、`test_node` | L0 PASS；真实 1×1、MASK 路径、额外 SaveImage 警告、任务冻结工作流 PASS |
| R05 | 预分配持久种子、任务内唯一、重试复用、追加新种子 | `repository.allocate_seed`、`service._ensure_plan/append_block`、`schema.seed_allocations` | `test_scheduler::test_3x5...`、`test_multiple_samplers...`、`test_retry...` | L0 PASS；真实 3×5/555 唯一种子；强杀 retry 同种子；追加新种子；真实双 KSampler 映射 PASS |
| R06 | AAAAA→BBBBB→CCCCC、自动队列、全局单在途、浏览器独立 | `scheduler.py`、`repository.next_executable_draw`、`runtime.py` | `test_scheduler::test_3x5.../test_single_inflight...`；UI 后台关闭浏览器截图 `ui-background.png`；555 FakeEngine | L0 PASS；真实 3×5 严格顺序、555/555 自动完成 PASS；真实关闭浏览器后台继续/重开同步 PASS |
| R07 | 安全暂停、定向立即暂停或安全降级、失败停止 | `service.pause_job`、`scheduler._monitor_attempt/_settle_pause`、`adapters/comfy.py` | `test_safe_pause...`、`test_immediate_pause_targeted...`、`test_immediate_pause_degrades...`、`test_failure_blocks...` | L0 PASS；真实安全暂停/立即暂停、失败后同种子新 ATTEMPT 重试、执行错误首错即停均 PASS |
| R08 | JOB/SRC/DRAW/ATTEMPT/OUT、批量输出、不可覆盖 | `schema.py`、`repository.py`、`storage.py`、`nodes/save_image.py` | `test_multi_out...`、`test_publish_never_overwrites`、`test_target_conflict...`、F04/F05/F11 | L0 PASS；真实 1 OUT、1 DRAW 4 OUT、第 3 张保存失败（PARTIAL/CORRUPT，不认成功）、导出目标冲突不覆盖 PASS |
| R09 | SQLite 事务、单实例、完整性、滚动备份 | `db.py`、`schema.py`、`backups.py`、`runtime.FileLock` | `test_backup_rotation...`、`test_file_lock_is_cross_process`、`test_newer_schema...`、F01/F09/F12 | L0 PASS；真实第二实例 INSTANCE_LOCKED；备份/旧库/只读/DB BUSY/迁移失败/F12 轮转崩溃/正常 Ctrl+C 关停 PASS |
| R10 | 异常退出、提交未知、文件/DB 不一致恢复 | `scheduler.py`、`recovery.py`、`storage` 收据 | F01—F12 子进程；`test_dropped_response...`、`test_recovery_marks_missing...` | L0 PASS；真实强杀/F07/F08/HTTP 丢响应/空历史 UNKNOWN/合成旧事件不回退 PASS |
| R11 | ComfyUI 内任务中心、计数、重连、懒加载 | `web/`、`integration.py`、`api.py`、`runtime.py` | 浏览器 L0 UI；`test_integration_register`；1000 OUT 后端分页 | L0 PASS；真实侧栏/1000 OUT 初始 50 张+加载更多+重开恢复+服务重启 reload PASS |
| R12 | 原图/结果对照、切换、右键/按钮、多选持久化 | `web/views/compare.js`、`detail.js`、`api.py selection` | UI 对照/方向键/Space/按钮/右键；`test_api` PUT/版本冲突 | L0 PASS；真实方向键/空格/右键/刷新持久化 PASS |
| R13 | 任意原图追加、连续编号、新种子、优先/末尾 | `service.append_block`、`repository.next_executable_draw` | `test_append_priority_tail...`、`test_started_append...`、`test_api` 追加幂等 | L0 PASS；真实 tail 追加 5 次 PASS；真实 priority 追加与块中再追加 PASS（块连续、tail 在块后） |
| R14 | 运行中临时导出、不改调度、仅复制入选 | `exports.preview/confirm/copy`、`api.export_*` | `test_quick_flat...`、`test_background_pending...`、API 导出测试 | L0 PASS；真实 QUICK 导出 PASS；真实后台 pending cutoff 确认/冻结/后续结果不并入 PASS |
| R15 | 最终审核、明确跳过、冻结、平铺/分组、恢复 | `exports.py`、`web/views/export.js`、schema export 表 | `test_final_review...`、`test_all_skipped_zero...`、F10/F11、API 导出 | L0 PASS；真实 FINAL/FLAT/GROUPED、显式跳过、冻结、导出失败后 resume PASS |
| R16 | 路径限制、磁盘不足、损坏、备份清理、安装升级卸载 | `util/path`、`api.path`、`backups.py`、`install_plugin.py` | `test_path_containment`、`test_disk_low...`、`test_newer_schema...`、`test_install`、`test_cache` | L0 PASS；真实越界/junction/XSS/磁盘/只读/卷失联；真实隔离实例安装/卸载/重装/更新/回退与数据保留 PASS；生产安装未运行 |

## Q/F 覆盖入口

- Q01—Q35、F01—F12 的逐项状态、证据与未覆盖子项：见 [TEST-REPORT.md](TEST-REPORT.md)。
- 真实环境阻塞与最小下一步：见 [DEVELOPMENT-STATUS.md](DEVELOPMENT-STATUS.md)、[REAL-ACCEPTANCE-RUNBOOK.md](REAL-ACCEPTANCE-RUNBOOK.md)。
- 数据/备份位置、安装位置模板：见 [README.md](../README.md)、[ENVIRONMENT.md](ENVIRONMENT.md)。

**注意**：本矩阵的 L0 通过不等于真实 ComfyUI/GPU 验收。隔离实例已通过 1×1、3×5、555/555、1×101、暂停/取消、强杀恢复、F07/F08、HTTP 丢响应、四组合导出/续传、备份/旧库对账、浏览器关闭、追加、安装/更新/回退与正常 Ctrl+C；独立 GPU + DreamShaper8_LCM 已完成 1×1、3×5、555；2026-11-15 HOST06/13 补验通过原生文件选择器、主题/缩放/键盘/减少动态/断网/冻结后移除等。用户生产 ComfyUI、实际工作流与真实 ENOSPC 仍未运行；不得把独立 GPU、夹具或隔离宿主结果写成用户生产环境验收。

## 正式使用前修复映射（2026-11-14）

| 规格项 | 实现 | 回归/证据 |
| --- | --- | --- |
| PR01 PG01—PG05 | `web/collections/result_pager.js`、`views/detail.js` | `tests/js/detail_pagination.test.mjs`；真实宿主分页失败重试 |
| PR02 SRC01—SRC05 | `web/collections/source_window.js`、`service.list_sources`、`views/detail.js` | 201/500/5000 原图；5000 源 UI 125 页 |
| PR03 SEL01—SEL06 | `api.output_states`、`selection_controller.js`、`views/detail.js` | 双标签 409、OUT 1/201/250 导出冻结 |
| PR04 PLAN01—PLAN07 | `backend/creation.py`、`schema v2`、`service.create_job/start/pause/abandon` | `tests/unit/test_creation.py`；100k/200×500/5000×20 |
| PR05 资源预算 | `source_window`、`result_pager`、`lazy_image.js`、卡片池化、`resources.js` | Chromium 60 分钟；100 次翻页元素 415→417 |
| PR06 HOST01—HOST14 | 真实侧栏/四步/暂停/详情/对照/导出/双标签/重连/强杀/窄视口 | HOST14 生产工作流 NOT_RUN |
| PR07 可靠性 | 强杀恢复、启动周期、迁移、重连；ENOSPC 未执行 | `crash-recovery.json`、`startup-cycles.json`、migration before/after |
| PR08 交付 | `install_plugin.py --update/--verify`、迁移、文档 | `install.txt`、最终报告 |
