# 开发状态

> **2026-11-16 当前状态（连续筛选 UXR01—UXR05）：**结果收起残留、按原图组连续筛选、服务端持久断点与“完整重启 ComfyUI 后继续”**已实现**；在隔离真实宿主（10 张真实风景照片、100 个真实 GPU 结果）通过 L0/真实宿主验收，Python 186（185 PASS + 1 SKIP）/ JS 48 回归 PASS。用户生产 ComfyUI、FLOW11 真实请求计数、COL04/COL06、真实双标签/移除/备份恢复/位置失败注入仍 **NOT_RUN**。报告与证据入口 `test-artifacts/review-ux-20261116/run-20261116T164231/EVIDENCE.md`；详细分层见 [连续筛选规格第 0 节](../RESULT-REVIEW-UX-SPEC-20261116.md#0-实施与验收状态2026-11-16)。

> **2026-11-15 状态（PU01—PU06）：**PU01 证据勘误/候选可重建与 PU02 存储工具保护/阶段同步**已实现并验证**；**PU03 真实 ENOSPC（DISK01—05）、PU04 用户实际工作流（HOST14）、PU05 目标安装/回退、PU06 三个日常周期仍为 NOT_RUN**。本机非管理员、只有 C:（系统卷）与 D:（项目卷），没有可丢弃的受控卷；也没有用户目标实例、工作流、模型与维护窗口。因此**正式可用结论仍为 PARTIAL**。本轮报告见 PU01—PU02 完成报告（本地证据：`test-artifacts/production-use-20261115/20261115T113000-pu01-pu02/FINAL-REPORT.md`）；NP01—NP06 报告见 2026-11-15 总报告（本地证据：`test-artifacts/next-20261115/FINAL-REPORT.md`）（入口勘误 ERRATA（本地证据：`test-artifacts/next-20261115/ERRATA.md`））；2026-11-14 及更早内容保留为历史记录。

更新日期：2026-11-15
当前版本：1.0.0 正式投用开发候选（PU01/PU02 开发与验证 PASS；PU03—PU06 NOT_RUN）
状态：隔离组合 ComfyUI 0.45.0 / RTX 3070 / DreamShaper8_LCM 已完成真实 1×1、3×5、555、1×101 与 HOST06/13 补验（历史证据，按候选/时间引用）；用户生产环境、实际生产工作流、真实 ENOSPC **NOT_RUN**。

## 2026-11-15 正式投用开发（PU01—PU06）

| 工作包 | 结果 | 说明 |
| --- | --- | --- |
| PU01 证据/候选/交付可复现 | **PASS（本轮）** | `tools/check_release_evidence.py` 新增（路径基准/引用/候选身份/门槛/数据来源/markdown 链接）；`candidate_manifest.py --verify-snapshot` 新增强制解包重建四个 scope；历史证据入口与 `DELIVERY.md` 指纹勘误，原始包与日志未改写；`ERRATA.md` 记录四类问题 |
| PU02 存储工具保护与阶段同步 | **PASS（本轮）** | `storage_acceptance.py` 写入入口自带卷/序列号/登记/所有权/预算/卷外记录/清理保护；`storage_scenarios.py` + `storage_writer.py` 提供 ready→填充→释放→失败→清空→恢复协议；DISK01—05 场景定义齐备；33 项存储工具测试（含真实 junction 重定向）+ 12 组全矩阵自检通过 |
| PU03 真实 ENOSPC | **NOT_RUN** | 无受控卷、非管理员（`environment/VOLUMES.json` 记录 C:/D: 与 diskpart 拒绝）；工具与协议已就绪，真实 fill 与五类故障未执行 |
| PU04 目标工作流 | **NOT_RUN** | 无用户实例/工作流/模型/原图；`tools/target_environment.py validate` 对模板返回 `MISSING_INPUTS`（50 项缺失） |
| PU05 目标安装/回退 | **NOT_RUN** | 前置 PU03/PU04 未满足，无维护窗口与生产操作范围 |
| PU06 三个日常周期 | **NOT_RUN** | 依赖 PU05 与目标实测 |
| 回归 | **PASS** | Python 167（166 PASS + 1 SKIP，Windows symlink 权限）/ JS 37 PASS；日志见本轮 `regression/` |

关键事实：本轮新增测试 58 项（证据检查 20、存储工具 33、目标资料 5）；历史 109 项基线仅作参考，不合并计数。

## 2026-11-15 下一阶段工作包（NP01—NP06，历史）

| 工作包 | 结果 | 说明 |
| --- | --- | --- |
| NP01 候选冻结与验收工具 | **PASS** | 候选指纹 `e6708d07…`（NP02 时点）；运行时 55 文件 `821edc19…`；dry-run 无副作用；真实 1×1 PASS；受控失败/强退/未知场景/多余运行时文件/空测试集均按预期失败；Python 109（108 PASS + 1 SKIP）/ JS 37 PASS |
| NP02 真实宿主多页与日常交互 | **PASS** | HOST06 A—D：真实 GPU 1×101（页 50/50/1）、后页入选+FINAL 导出独立核验、5000 源/10000 OUT 夹具、失败/迟到响应；HOST13 UI01—UI06：主题/缩放、键盘焦点、减少动态、原生文件选择、图片/断网失败、冻结/移除/追加 |
| NP03 受控卷与真实 ENOSPC | **PARTIAL** | 工具与 25 项可靠性/故障注入（24 PASS + 1 SKIP）通过；DISK01—05 **NOT_RUN**：无独立受控卷、非管理员、diskpart/fsutil 拒绝访问 |
| NP04 目标工作流 | **NOT_RUN** | 无用户实际 ComfyUI、工作流、模型、原图；HOST14 未执行 |
| NP05 目标安装/回退/试用 | **NOT_RUN** | 无目标实例、维护窗口与生产安装授权；隔离安装/更新/校验证据可复用 |
| NP06 文档与交付 | **PASS（本轮）** | 当前状态、支持范围、操作/异常说明、历史结论标注统一；未测项保留 NOT_RUN |

关键修复：原图翻页后 `aria-busy` 永久 busy、键盘 Space 入选后焦点被画布抢走、模态初始焦点可能被并发渲染移出。均已在最终运行时 `821edc19…` 复验。

## 2026-11-14 正式使用前修复与复验

本轮针对 [PRODUCTION-READINESS-DEVELOPMENT.md](../PRODUCTION-READINESS-DEVELOPMENT.md) 完成：

- **PR01/PR02/PR03**：结果分页改为有界 `ResultPager`，每源缓存 3 页、单游标单在途、失败按原游标重试；原图按稳定 `source_order` 分页，201/500/5000 张均可到达并可跳转；入选使用共享 `SelectionController` 与轻量 `POST /sources/{id}/outputs/state`，已有卡片按 OUT 身份更新星标/健康/版本，双标签 409 不自动补写。
- **PR04**：`POST /jobs` 先持久登记并返回 CREATING；单个后台工作器按 `plan_insert_batch`（默认 500，跨原图）分批提交 DRAW+全部采样器 seed+进度；暂停/放弃/继续使用持久意图与 generation 屏障；重启不自动继续；schema v1→v2 增量迁移。
- **PR05**：详情页原图窗口 ≤40 行、每源结果页 ≤3、全局 OUT DTO ≤1000、全页挂载 OUT 卡片 ≤300、缩略图并发 ≤8；有限页窗口提供首页/上一页/下一页/按编号定位。
- **PR06/PR07/PR08**：真实宿主侧栏、四步创建、CREATING 暂停/继续、真实 GPU 3×5、四组合导出、强杀恢复、三启动周期、历史库迁移、安装更新/回退、重连与窄侧栏均取得本轮证据。

本轮验证摘要：

```text
Python L0：97 tests, 96 PASS + 1 SKIP（Windows symlink 权限），0 FAIL
JS：37 PASS（新增 detail_pagination/SRC/PG/状态合并/预算用例）
真实 GPU：隔离组合 1×1、3×5、555 全 PASS（SQLite/文件/哈希/PNG/清单独立核验）
创建规模：1×100000 61.5s、200×500 37.5s、5000×20 266.8s；注册 p95 15.7ms
真实宿主：HOST01—HOST13 已取得证据；HOST14 用户生产工作流 NOT_RUN
可靠性：真实强杀→UNKNOWN→人工 retry（同种子、新 ATTEMPT、仅 1 个接受 OUT）PASS
迁移：真实历史 v1 库（5 JOB/7 SRC/23 DRAW/21 OUT/8 备份）升级后计数、哈希、外键与 integrity 全部保持
安装：copy 安装/更新备份/校验/junction/卸载保留数据 PASS
```

详细证据：最终验收报告（本地证据：`test-artifacts/final-20261114/REPORT.md`）。

## 当前阶段

| Phase | 状态 | 说明 |
| --- | --- | --- |
| 0 环境与关键路径 | 完成（隔离实例） | 授权前本机无 ComfyUI；用户授权后创建隔离 ComfyUI 0.45.0 实例并完成真实 1×1/3×5/555 |
| 1 可靠执行骨架 | 完成（L0 + 真实 1×1/3×5/555） | SQLite/快照/引用/工作流冻结/种子/块调度/单在途/受控保存 |
| 2 暂停、未知提交、恢复、备份 | 完成（L0；真实安全/立即暂停、强杀恢复 PASS） | 双暂停、F01—F12 子进程崩溃、收据、人工决策、备份/恢复；真实强杀+人工 retry PASS |
| 3 任务中心与筛选/追加 | 完成（L0 UI + 真实侧栏/追加） | 真实前端模块 + 真实后端 API；真实侧栏运行 555；真实追加 5 次 |
| 4 审核与导出 | 完成（L0 + 真实四组合/显式跳过） | 临时/正式、平铺/分组、审核/跳过、冻结、复制/恢复；真实导出 PASS |
| 5 集成、耐久、发布 | 部分（主验收通过） | CPU 隔离实例真实 L1/L2、555、取消、强杀、F07/F08、HTTP 丢响应、导出/续传、备份/旧库对账、浏览器关闭、追加通过；独立 GPU 真实模型 1×1/3×5/555 通过；用户生产环境仍未验收 |
| UI 改造 A—D | 完成（L0 浏览器 + 2026-11-15 NP02 真实宿主 UI01—UI06） | 容器响应/主题/骨架/通知/模态生命周期；四步创建/投放区/旧预检隔离/创建幂等；并排+滑动对照/共享选择/409；任务卡片/统计/连接状态/导出审核/恢复面板；真实宿主主题/缩放/键盘/文件选择/网络/冻结闭环 |

## 已实现（主要文件）

- 状态机/事务/数据不变量：`backend/states.py`、`schema.py`、`repository.py`、`db.py`
- 调度/提交/暂停/终态：`backend/scheduler.py`
- 受控保存/清单/收据/无覆盖：`backend/storage.py`、`nodes/save_image.py`
- 原图导入/快照/引用/文件夹扫描：`backend/inputs.py`、`images.py`
- 工作流预检/绑定/种子/执行副本：`backend/workflow.py`
- 恢复/UNKNOWN 人工决策：`backend/recovery.py`
- 备份/轮转/恢复：`backend/backups.py`
- 导出草稿/审核/冻结/复制：`backend/exports.py`
- HTTP API/幂等/错误：`backend/api.py`
- ComfyUI 接入/单实例/启动顺序：`backend/runtime.py`、`integration.py`、根 `__init__.py`
- 前端任务中心：`web/`（extension/app/api/store/views/styles）
- UI 共用组件：`web/components/`（stepper/dropzone/image_compare/skeleton/toast/tooltip）、`web/layout.js`、`web/dom.js`
- 创建草稿校验：`web/create_draft.js`；统一入选：`web/selection_controller.js`
- 安装/卸载：`tools/install_plugin.py`

## 最后验证

```text
.venv/Scripts/python.exe -m unittest tests.unit.test_core tests.unit.test_node tests.unit.test_scheduler tests.unit.test_exports tests.unit.test_reliability tests.unit.test_scale tests.integration.test_api tests.integration.test_install tests.integration.test_integration_register tests.integration.test_comfy_adapter tests.fault_injection.test_crashes -v
→ 85 tests run, 84 OK + 1 skipped（提交后复验：test-artifacts/l0-postcommit.txt）

node --test tests/js/api.test.mjs
→ 7 tests pass（提交后复验使用 ZCode 内置 Node v24.21.0：test-artifacts/js-postcommit.txt）

tools/production_acceptance.py（独立 GPU，1x1/3x5/555）
→ PASS；JOB-20261111-EB2A1E 1/1、JOB-20261111-74D01F 15/15、JOB-20261111-CB4D67 555/555（1170 s）
→ 证据：test-artifacts/gpu-acceptance-postcommit/SUMMARY.json
```

浏览器 L0 UI 测试台：创建/预检/文件夹导入/执行/对照/方向键/Space/按钮入选/临时导出；截图 `test-artifacts/ui-export.png`、`ui-compare.png`。
提交后复验（2026-11-12，基线 `744ff97`）：L0 85 项 84 PASS + 1 SKIP、JS 7 PASS；独立 GPU 重新完成 1×1、3×5、555/555（555 用时 1170 s），SQLite/文件/哈希/PNG/清单核验通过。

UI 改造复验（2026-11-12，工作区基线 `8e388a5` + 未提交改动）：

```text
node --test tests/js/api.test.mjs tests/js/ui_refresh.test.mjs
→ 23 tests PASS（7 基线 + 16 UI 专项）

85 项 Python L0 → 84 PASS + 1 SKIP（Windows symlink 权限）

浏览器 L0（真实前端 + FakeEngine，NOT 真实 ComfyUI）：
四步创建/上传失败重试/引用目录、旧预检隔离、创建丢响应同 key 重试只产生 1 任务、
并排+滑动/比例回退/滑块键盘、外部写者 409、入选保存失败、20 次开关无监听泄漏、
确认框焦点与单次结算、骨架/断线重连、安全暂停与立即暂停降级、重启 UNKNOWN 面板、
QUICK/FINAL 审核/跳过/零图/截止确认、部分失败→恢复、1000 OUT 分页、
320/480/720 容器与深浅主题、无外部资源与恶意文件名文本化。
截图与逐项边界：test-artifacts/ui-refresh/SUMMARY.md
```
真实 ComfyUI：L1 1×1、L2 3×5、555/555、安全暂停、定向取消（运行中+排队中）、强杀恢复、F07/F08、四组合导出/续传、备份恢复、浏览器关闭、追加 5 次、更新/代码回退、正常 Ctrl+C 均 PASS。

## 未完成/未验证（2026-11-14 历史；当前状态见顶部 2026-11-15 表）

- **用户生产 ComfyUI 及其实际生产工作流 NOT_RUN**：本轮已完成隔离组合的真实宿主 UI 与真实 GPU 1×1/3×5/555；未在用户声明之外的其他版本/自定义节点/生产工作流验收，不能发布“用户生产环境已通过”。
- **真实 ENOSPC NOT_RUN**：当前账户无管理员权限，无法创建容量受控的隔离卷/VHD；只验证了保留空间预检失败与 L0 故障注入。禁止写满系统/项目盘。
- **HOST14 实际用户工作流代表性连续运行 NOT_RUN**：与上一条同因；隔离 GPU 工作流结果单独记录，不共享 PASS 标签。
- **60 分钟 UI 耐久 PASS**：Chromium 60.0 分钟、891 次翻页、6 次对照、0 错误；挂载 1 行/50 卡、缓存 150 DTO、图片并发峰值 8；强制 GC 后堆中位 3.169→3.286 MB（+3.69%），Nodes/Listeners 1044/299 不变。IAB 另跑 93 分钟补充。
- Q16 操作系统原生文件选择器上传、冻结后逻辑移除仍仅 L0。
- 真实宿主 HOST06 的大清单分页使用 111 原图/555 OUT；201 原图/300 OUT 来自 L0 隔离夹具，5000 原图/10000 OUT 来自 L0 夹具；报告分别标注数据来源，不把夹具写成真实采样。

## 真实验收进度（2026-11-11 完成主验收）

用户授权后创建隔离实例 `D:\Projects\ComfyUI-acceptance`（ComfyUI 0.45.0、torch 2.9.1+cpu、CPU、端口 8199），插件 copy 安装并加载。真实完成：

- L1 1×1：PASS（GachaSaveImage、OUT/manifest、PNG 元数据、种子、历史成功；源图未变）；
- 3×5：PASS（15/15；严格 AAAAA→BBBBB→CCCCC；15 唯一种子/prompt_id）；
- 555：PASS（555/555；555 ATTEMPT/DRAW/OUT、唯一 prompt_id/种子、全文件哈希/PNG/清单/源图哈希核验）；
- 安全暂停 + 恢复：PASS；立即暂停：运行中 → PAUSED，排队中 → 只移除插件 pending，外部哨兵成功；
- 强杀：运行中 `taskkill /F`，重启 → RECOVERY_REQUIRED/UNKNOWN，不自动重提；人工 retry → 同种子 ATTEMPT-002 成功；
- 导出：QUICK/FINAL × FLAT/GROUPED 四组合全通过；正式导出显式跳过 SRC-0003 → 仅导出 2 项；预置冲突目录 confirm 409 且不覆盖，清理后重试成功；导出来源缺失→PARTIAL_FAILED→恢复后 resume 3/3；
- 备份恢复：真实一致性备份→新增任务→恢复；新任务回滚消失、555 与 6 个导出保留、pre-restore 库保留并校验；
- 崩溃窗口：真实 F07（收据已写/DB 未提交）重启补记 SUCCEEDED；真实 F08（DB 已提交）重启无重复；均只 1 ATTEMPT/1 OUT；
- HTTP 丢响应：代理确认 `/prompt` 200/263B 后丢弃；插件只提交 1 次，UNKNOWN→同一 history success 找回→确认 COMPLETED；
- 浏览器关闭：真实 60 DRAW 任务 8/60 时关闭唯一标签，后台继续到 60/60；重开页面侧栏同步 60/60，started_at 未变；
- 1000 OUT UI：真实 250×4=1000 OUT，初始 50 张+加载更多，19 次到 1000；关闭重开恢复 50 张与 1000 计数；服务停止/重启后 reload 仍恢复 250/250、OUT 1000；
- DB 只读：真实只读 DB 启动 503 DB_READ_ONLY，DB 哈希不变；恢复可写并回滚安全备份，555/1000/Q34 全部恢复；
- F12 轮转崩溃：临时备份后硬退出；重启旧 VERIFIED 备份保留，.tmp 未登记，无自动提交；
- 正常 Ctrl+C：独立 8200 与主数据 8199 均真实控制台验证；主实例 1.5 s 内 exit 0、端口关闭、日志 `Stopped server`；重启后 32 JOB、555/555、slot/queue 空；PASS；
- 多采样器：真实双 KSampler 2 DRAW=4 唯一整数种子，DB 与 ComfyUI history 节点 6/10 完全一致；
- 双实例/并发：第二实例 409 INSTANCE_LOCKED；6 并发 start+6 同 key append 只产生一个 block；并发 pause 后 15/15；
- DB BUSY：独立进程持锁时创建请求 5.51s 后 503 DB_BUSY，无半份 JOB；释放后同 key 重试成功；
- 只读结果根：ACL 拒写后提交前 READ_ONLY，0 ATTEMPT/OUT，无探针残留；恢复后主数据正常；
- 卷失联：结果根整体移走后提交前 READ_ONLY，0 ATTEMPT/OUT；恢复目录后主数据正常；
- 快照复制竞态：50MB PNG 复制中连续写源文件 → 409 INPUT_CHANGED，无 JOB/快照；
- XSS/编码：恶意原名在列表/详情/导出审核均为文本，xssFlag=null；导出目标名安全；
- 迁移失败：独立 data_root 冲突表使迁移中断，事务回滚无半套表，旧备份哈希不变；
- 事件：execution_events=false；并发幂等一次；旧版本选择 409；空 history→UNKNOWN；合成迟到/重复/旧状态事件不回退 COMPLETED/SUCCEEDED；
- 追加：SRC-0001 追加 5 次 → 20/20，DRAW 6–10 连续、旧选择保留、新种子唯一；
- 安装/更新/回退：卸载保留数据、copy 重装、原地更新、回退 HEAD `00b09d3`、恢复当前代码，全程 32 JOB、555/555 保留；
- 真实侧栏：PASS（截图 `test-artifacts/real-comfyui/real-ui.png`）。

555 任务 `JOB-20261111-BAB90A` 已 COMPLETED；定向取消能力在**该隔离实例**配置中启用为 `verified_atomic`，仓库默认仍为 false。详见 [TEST-REPORT.md](TEST-REPORT.md)、[ENVIRONMENT.md](ENVIRONMENT.md)。

需求到实现/测试的映射见 [TRACEABILITY.md](TRACEABILITY.md)。

## 剩余验收任务（不影响隔离实例主验收结论）

1. 在用户生产 ComfyUI 用真实模型/工作流重跑 1×1、3×5、555（或代表性规模），确认版本/节点/性能兼容。
2. 操作系统原生文件选择器上传、冻结后逻辑移除（其余 Q16 已真实通过）。
3. 真实 ENOSPC、Q34 校验后立即替换等剩余 L0 子项。
4. 生产安装、升级/回退/卸载与数据保留验证。

## 接续注意

- 不要把 L0、CPU tiny 或独立 GPU 结果写成用户生产环境验收；TEST-REPORT 中仍未运行的项目必须保持 NOT_RUN。
- 真实 ComfyUI 上发现接口不符时，改 `adapters/comfy.py`/`integration.py`，不要降低数据不变量。
- 卸载默认保留数据；安装/迁移前先备份 `data_root` 与结果目录。
