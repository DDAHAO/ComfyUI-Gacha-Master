# 测试报告

## 0. 2026-11-17 存储清理 CL01—CL05（当前结论）

**环境与数据边界：** Windows 11 26300；Python 3.12.10 / aiohttp / Pillow；隔离 L0（真实 SQLite/文件/HTTP/前端，FakeEngine，**非 ComfyUI 宿主、无 GPU 推理**）。用户亲测实例（`127.0.0.1:8288`）与亲测图片**未触碰**。浏览器验收使用独立 L0 UI harness（端口 8799，FakeEngine），`data_origin=fixture`。

### 0.1 正式回归

| 项目 | 实际结果 | 证据 |
| --- | --- | --- |
| 完整正式回归（最终代码） | **PASS**：Python **223**（1 SKIP：Windows symlink 权限；0 FAIL/0 ERROR），JS **54 PASS**（0 fail） | `test-artifacts/storage-cleanup-20261117/20261117T160000-final4/regression/REGRESSION.json`、`python-tests.log`、`js-tests.log` |
| 定向清理单测 | 32 项：资格/确认/保护/路径/Windows 占用/junction/文件替换/取消/重启对账/旧库恢复/5000 OUT/缓存共享/并发生成/无日志运行/损坏日志 | `tests/unit/test_cleanup.py` |
| HTTP 契约 | 5 项：预览/分页/确认 202/幂等重放/同键异体冲突/历史/410/校验/storage/cache | `tests/integration/test_cleanup_api.py` |
| JS 客户端契约 | 4 项：预览/确认/分页/410 | `tests/js/cleanup_api.test.mjs` |

> 说明：一次中间全量运行（`20261117T143000-final2`）出现 1 项既有 `test_immediate_pause_degrades_without_targeted_cancel` 计时偶发失败（取消调用竞态）；该用例单独重跑与 `test_scheduler` 连续 5 次均 PASS，最终全量 `20261117T160000-final4` PASS。该失败与存储清理改动无直接关联，保留记录以免掩盖。

### 0.2 关键证据

| 证据 | 内容 | 路径 |
| --- | --- | --- |
| 聚焦验收 | 隔离 fixture：8 OUT/1 入选，预览 7 候选/3707 B；确认 202→COMPLETED；7 文件删除；入选文件 hash 不变；原图快照 hash 不变；journal 15 文件；PURGED image=410 | `test-artifacts/storage-cleanup-20261117/evidence/EVIDENCE.json` |
| 5000 OUT 规模 | `data_origin=fixture`：候选 5012；预览 11.363 s；删除 198.753 s；删除 5012/0 失败；tracemalloc 峰值约 74.4 MiB | `test-artifacts/storage-cleanup-20261117/CLT25-scale-metrics.json` |
| 真实浏览器（L0） | 预览弹窗/取消/二次确认/部分失败（只读文件）/重新预览补删/刷新与服务器重启后历史/设置页缓存清理 | `test-artifacts/storage-cleanup-20261117/20261117T102500-l0-ui/screenshots/*.png`、`20261117T131500-ui-final/screenshots/01-preview-final-code.png`、`ui-server*.log`、`ui-state.json` |
| 旧库恢复 | 单元测试：清理前备份→清理→恢复旧库→外部日志重建 PURGED→旧入选阻止导出→取消失效入选后可重新审核 | `tests/unit/test_cleanup.py` |

### 0.3 CLT01—CLT26 逐项结果

| 编号 | 结果 | 说明 / 证据 |
| --- | --- | --- |
| CLT01 | **PASS**（语义等价夹具） | 8 OUT/1 入选聚焦证据 + 12 OUT/1 入选单测：仅删 7/11 未入选，入选与快照 hash 不变，DRAW/计划不变。规格中的 100 OUT/8 入选具体数字 **NOT_RUN**，由 5012 OUT 规模用例补充。 |
| CLT02 | **PASS** | 未确认组（含只浏览过）`CLEANUP_NOT_ELIGIBLE` + `next_action=continue_review`；0 文件删除、0 cleanup run。 |
| CLT03 | **PASS** | 预览后改选/冻结导出 → 确认拒绝且 0 删除；过期预览 → `CLEANUP_PREVIEW_STALE` 并标记 EXPIRED。追加导致计划版本变化由代码路径与 pending 组检查覆盖。 |
| CLT04 | **PASS** | 全组 SKIPPED：预览 `all_skipped=true`、候选覆盖全部 OUT；确认后全删；组决定/筛选汇总仍为 SKIPPED/confirmed。 |
| CLT05 | **PASS** | CORRUPT/MISSING/PARTIAL/非接受 attempt 不入候选，文件和异常证据保留。 |
| CLT06 | **PASS** | 已完成 QUICK 导出成员即使后来取消入选仍受保护；损坏的 `preview_json` 使整任务清理 `CLEANUP_NOT_ELIGIBLE`；有效草稿/审核成员同集合逻辑。 |
| CLT07 | **PARTIAL** | 已测：存在 PENDING DRAW、FROZEN 导出时阻止且不发暂停/取消。UNKNOWN/RECONCILING attempt 专用用例 **NOT_RUN**（SQL 分支已实现）。 |
| CLT08 | **PARTIAL** | 已测：预览后改选/冻结导出 → stale 且 0 删除。真实双标签页并发操作未单独执行。 |
| CLT09 | **PARTIAL** | 维护占用单测验证入选/组确认/导出冻结/备份恢复均 `STORAGE_BUSY`；调度领取与导出冻结在 `BEGIN IMMEDIATE` 内复查。未做高争用压力竞态测试。 |
| CLT10 | **PASS** | 双击/同键同体重放返回同一运行与最新进度；同键异体 409；运行中再次确认返回同一 run。 |
| CLT11 | **PASS** | Windows 打开文件导致单文件删除失败 → `PARTIAL_FAILED`，成功项减少、失败文件保留可读；真实浏览器复现“成功 10/失败 1”。只读缓存文件报告失败。 |
| CLT12 | **PASS** | 预览后修改候选 → 确认 `CLEANUP_PREVIEW_STALE`，首张前拒绝；`confirm` 前复查身份/大小/SHA-256。预先删除的候选被排除且不计释放。 |
| CLT13 | **PASS** | `..`/跨根路径 `PATH_NOT_ALLOWED`，哨兵文件 hash 不变；Windows junction 用例拒绝且外部文件保留；无递归目录删除。 |
| CLT14 | **PASS**（进程级） | 模拟 intent 前 DB 失败（不删）、intent 后文件仍在（重启取消并保留）、unlink 后无收据（重启补记 PURGED）、收据已写 DB 未写（旧库恢复导入）。**物理断电 NOT_RUN**。 |
| CLT15 | **PARTIAL** | DB 意图失败 → 不 unlink、写入 CANCELLED 收据、运行部分失败；无持久意图不删除。真实日志写满/只读（ENOSPC）与收据损坏 **NOT_RUN**。 |
| CLT16 | **PASS** | 重启对账后 PURGED 不变 MISSING、字节不重计；失败/取消旧 intent 不豁免后续意外缺图（`missing` 检出）；重复确认/对账不重复删除。 |
| CLT17 | **PASS** | 恢复清理前备份：外部日志重建 PURGED；旧入选指向已清理图 → 导出 410 `OUTPUT_PURGED`；取消失效入选后可继续（需重新审核）；不恢复/重抽/重删文件。 |
| CLT18 | **PASS** | schema v1/v2/v3 → v4 增量迁移；v1 行/编号/接受关系保留；v2→v3 失败回滚不留半表；v1/v2/v3 历史校验和未改写；迁移 0 文件删除。 |
| CLT19 | **PARTIAL** | 已测：清理后书签指向 PURGED OUT 时 offset 定位到同组仍留存项、`offset/total` 一致、状态含 `retention_state`。规格中的跨 50 结果页/40 原图页具体组合 **NOT_RUN**。 |
| CLT20 | **PASS** | 清理后追加 1 次成功：新 DRAW/ATTEMPT/OUT 路径与身份，已清理行保持 PURGED，可用/入选统计正常；入选文件 hash 未变（CLT01）。 |
| CLT21 | **PASS** | 清理后 image/thumbnail 均 410 `OUTPUT_PURGED`；不生成新缩略图、不标 CORRUPT；历史模式 UI 显示“已清理”且禁用入选/预览。 |
| CLT22 | **PASS**（并发未极限压力） | 浏览器：删除 1 个缓存、释放 285 B，再次浏览重建；空缓存 0 结果；只读缓存失败可见；完整 OUT/原图 hash 不变。并发浏览/生成的极限竞态未单独压力测试。 |
| CLT23 | **PASS** | 两个 OUT 共享同一内容哈希键时，只清理其中一个不删除共享缓存；全局缓存清理后可重建。 |
| CLT24 | **PASS**（浏览器重载/服务器重启） | 部分失败后刷新页面、重启隔离服务器：同一 cleanup 记录/进度/失败详情恢复；再次清理需新预览；不重复删除。未执行真实“关闭浏览器标签再打开”以外的宿主组合。 |
| CLT25 | **PASS** | 5012 OUT fixture：候选分页 50/页、worker 有界；预览 11.363 s、删除 198.753 s、峰值内存约 74.4 MiB；明确 `data_origin=fixture`。 |
| CLT26 | **PASS** | 结果目录 `outputs.complete.json`、用户额外 `notes-user-added.txt` 保留；缩略图根非受控 `keep.txt` 保留；空间统计分项无重复相加。 |

### 0.4 未执行/不能声明

- **真实 ComfyUI 宿主 / GPU 推理**上的清理 UI 与磁盘流程 **NOT_RUN**；本轮浏览器宿主是 L0 harness（FakeEngine），不能当作真实宿主验收。
- 用户生产 ComfyUI、生产工作流/模型、用户亲测 150 OUT/18 入选数据未用于删除验收。
- 物理断电耐久性、真实 ENOSPC、真实双标签页/多进程争用压力测试 **NOT_RUN**。
- CLT07/08/09/15/19 标为 **PARTIAL**；其余 PASS 仅限上表列出的隔离范围。


运行日期：2026-11-11（主验收）/ 2026-11-12（Q33/Q35 补测、独立 GPU 真实模型复跑、提交后复验、UI 改造 L0 验收，UTC 时间见日志）
代码版本：基线提交 `744ff97`；提交后复验在产品代码不变的前提下执行，另加一个仅测试的时序确定性修复。交付状态见 [DELIVERY.md](../DELIVERY.md)。
**测试环境：Windows 11 26300。L0 使用项目 venv（Python 3.12.10、Pillow 13.0.0、aiohttp 3.14.5）；CPU 真实 L1/L2 使用隔离 ComfyUI 0.45.0 实例（torch 2.9.1+cpu、端口 8199）；GPU 真实模型复跑使用独立实例 `D:\Projects\ComfyUI-gpu-acceptance`（RTX 3070、torch 2.9.1+cu126、DreamShaper8_LCM、端口 8288）。用户生产 ComfyUI 未安装、未触碰。**

## 0. 2026-11-16 连续筛选、重启续筛与收起修复（当前结论）

| 项目 | 实际结果 | 证据 |
| --- | --- | --- |
| 完整回归 | **PASS**：Python 186（185 PASS + 1 SKIP，Windows symlink 权限），JS 48 PASS，0 FAIL/0 ERROR | `test-artifacts/review-ux-20261116/run-20261116T164231/regression/REGRESSION.json` |
| 新增 Python 测试 | 16 项 review session/导航夹具 + 1 项缩略图定位 + 2 项 API + 迁移 v3/失败回滚 + 备份含会话 | `tests/unit/test_review_sessions.py`、`tests/integration/test_review_api.py`、`tests/unit/test_schema_migration.py` |
| 新增 JS 测试 | 11 项：位置/决定/等待入选/双击/冲突/下一组失败/幂等重放/返回/offset 恢复/重试/缩略图定位 | `tests/js/review_session.test.mjs` |
| 修复前复现 | 旧安装副本详情 + 旧 CSS，L0：收起后 6 张卡片、分页栏仍可见，`visibleResultNodes=8`，轮询 5 s 后仍为 8 | `prefix-repro/prefix-repro.json`、`prefix-collapsed-residue.png` |
| 修复后 L0 | 收起后 `stripHidden=true`、子节点 0、可见结果节点 0；连续筛选组 1 多选保留、组 4 跳过、返回、完成页 PASS | `l0/l0-smoke.json`、`l0/*.png` |
| 真实宿主（隔离 GPU） | ComfyUI 0.45.0 / RTX 3070 / DreamShaper8_LCM，10 张真实风景照片、100 个真实 GPU 结果；schema 2→3 迁移保留 100 OUT/3 入选；收起 0 残留；组 1 多选+保留、组 4 跳过、返回组 2；组 5 未确认断点 | `real-host/real01*.json`、`real02*.json`、截图 |
| 完整重启恢复 | 真实宿主连续 4+ 次完整关闭/启动；fresh browser 恢复组 5（2 个 OUT ID 与当前结果完全一致），随后组 6 断点在 3 次连续启动中保持 | `real-host/real02.json`、`res14-cycle5/6.json`、`final-state.json` |
| 强退与事务 | `R01`（提交前）真实进程退出：无审核行/回执，session 仍 `IN_GROUP`；重启恢复原组。`R02`（提交后、响应前）：审核+边界+回执齐备，重启从下一组继续；同决定重放 `idempotent=true` 且版本不前进。另在独立 DB/结果副本（端口 8291）重复 R01/R02 | `real-host/real03-R01.json`、`real03-R02.json`、`res05-db-after-r01.json`、`res06-db-after-r02.json`、`res06-replay.json`、`real03-crashcopy-*.json`、`crashcopy-db-after-*.json` |
| 强杀未确认组 | 组 6 选中 2 后 `Stop-Process -Force`；重启恢复组 6，两个 OUT ID 完全一致 | `real-host/real04.json`、`res04-db-after-forcekill.json`、`real02-after-forcekill.out` |
| 详情缩略图入口 | 点击详情结果缩略图进入连续筛选工作区，并恢复到所点 OUT（D3/O1 → 工作区当前 OUT 相同）；旧“对照”按钮仍打开原对照弹窗 | `real-host/thumbnail-entry.json`、`thumbnail-entry-review.png`、`thumbnail-entry-compare.png` |
| 大集合夹具 | 500 原图×2 结果（`data_source=fixture`）：SRC-0038→41、重启后 SRC-0200→201；单组 60 结果第 50→51 张跨页/重启恢复；第 1/2 页各选 1，整组决定 `selected=2` | `fixture-500/fixture_nav_38_41.json`、`fixture_nav_200_201.json`、`big-group-first.json`、`big-group-verify.json` |
| 404 控制台噪声 | 6 条 404 全部是 ComfyUI 自身可选文件（`user.css`、`comfy.templates.json`、subgraphs）；本插件未产生 404 | `real01b.json.finalNotFound`、`real01b_label.cjs` |
| 未执行 | 用户生产 ComfyUI/工作流/模型；FLOW11 真实 `/prompt`/取消计数；COL04 错误/空态专项、COL06 100 次循环；真实双标签/移除/备份恢复/位置保存失败注入；60 分钟工作区堆复测 | 见规格第 0.4 节 |

真实数据与夹具分开记录：真实宿主证据使用用户 10 张风景照片的实际生成结果；500 原图/60 结果为 `fixture`，不计入真实 GPU 推理。**本轮的“真实宿主”是隔离实例，不是用户生产 ComfyUI。**

## 0. 2026-11-15 正式投用开发复验（PU01—PU02，当前结论）

| 项目 | 实际结果 | 证据 |
| --- | --- | --- |
| Python 回归 | **167 项：166 PASS + 1 SKIP**（SKIP 为 Windows 账户不允许创建 symlink），0 FAIL / 0 ERROR | `test-artifacts/production-use-20261115/20261115T113000-pu01-pu02/regression/REGRESSION.json` |
| JS 回归 | **37 PASS** | 同上 |
| 新增测试 | **58 项**：证据检查器 20、存储工具保护/协议 33（含真实 junction 重定向）、目标资料校验 5 | `tests/unit/test_release_evidence.py`、`test_storage_tools.py`、`test_target_environment.py` |
| 候选可重建 | 交付包 + 完整源码包；在空目录解包后四个 scope 逐文件一致 | 本轮 `candidate/VERIFY.json` |
| 证据引用 | 新汇总路径基准唯一、引用全部可解析、候选身份与门槛自查通过 | 本轮 `EVIDENCE-CHECK.json` |
| 存储工具负向保护 | 系统卷/项目卷/未登记卷/序列号不符/容量超限/run-dir 越界/复用目录/非法参数/卷内报告/日志失败/清理换卷/所有权缺失/尺寸不符/不完整清单 全部按预期拒绝 | `tests/unit/test_storage_tools.py` |
| 阶段同步协议 | DISK01—05 × 正常/拒绝 + 超时 + 模拟失败 = 12 组全部 PASS（**simulated，仅协议自检**） | 本轮 `storage/PROTOCOL-SELFTEST.json` |
| 真实 ENOSPC | **NOT_RUN**：`environment/VOLUMES.json` 显示仅 C:/D:，非管理员，diskpart 需提升 | 本轮 `storage/DISK0*-NOT-RUN.json` |
| 目标资料 | **MISSING_INPUTS**：模板校验列出 50 项缺失 | 本轮 `environment/TARGET-ENVIRONMENT.json` + `TARGET-ENVIRONMENT-CHECK.json` |
| PU03—PU06 | **NOT_RUN** | 本轮 `SUMMARY.json` |

协议自检使用注入的 ENOSPC 模拟错误，**不构成磁盘写满证据**；真实 fill、真实业务写入失败与恢复仍待受控卷。

## 0.1 2026-11-15 下一阶段复验（NP01—NP05，历史）

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| NP01 候选/工具 | **PASS** | 候选 `e6708d07…`（NP02 时点）、运行时 55 文件 `821edc19…`；dry-run 无副作用；真实 1×1；受控失败/强退/未知场景/多余运行时文件/空测试集按预期失败；`test-artifacts/next-20261115/20261115T080500-np01-final/` |
| NP02 HOST06 A—D | **PASS** | 真实 GPU 1×101 页 50/50/1；后页入选 FINAL 导出 2 项独立核验；5000 源/10000 OUT 夹具（`data_source=fixture`）125 页/末页 SRC-4961—5000/跳转 4321；失败/迟到响应；`20261115T081500-np02/host/H06-*-final.json` |
| NP02 HOST13 UI01—UI06 | **PASS** | 主题/缩放、键盘 Space ★→☆、模态焦点、减少动态、原生文件选择、缩略图失败/慢响应/断网、冻结/逻辑移除/追加；`20261115T081500-np02/host/HOST13-final.json`、`UI06-final.json` |
| Python 回归 | **109 项：108 PASS + 1 SKIP** | `20261115T081500-np02/regression/REGRESSION.json` |
| JS 回归 | **37 PASS** | 同上 |
| NP03 真实 ENOSPC | **NOT_RUN** | 非管理员、无独立受控卷；DISK01—05 未执行；工具预检与 25 项故障注入（24 PASS + 1 SKIP）；`20261115T090500-np03/` |
| NP04/NP05 | **NOT_RUN** | 无用户生产实例/工作流/模型/维护窗口；HOST14 未执行；`20261115T091000-np04/`、`20261115T091500-np05/` |

本轮修复了原图翻页后 `aria-busy` 永久 busy、键盘 Space 入选后焦点被画布抢走、模态初始焦点可能被并发渲染移出；三项均在最终运行时复验。**用户生产 ComfyUI、真实 ENOSPC、目标安装仍不能声明通过。**

## 0. 2026-11-11/12 历史结论

| 项目 | 结果 |
| --- | --- |
| Python L0 自动化（单元/事务/调度/崩溃/API/安装/规模） | **84 项 PASS + 1 项 SKIP**（共 85；SKIP 为 Windows 账户不允许创建 symlink；最终输出 `test-artifacts/l0-full.txt`） |
| JS L0（API 客户端/事件桥/格式化） | **7 项 PASS**（`test-artifacts/js-tests.txt`） |
| 浏览器 L0 UI 测试台（真实前端模块 + 真实后端 + FakeEngine） | **PASS**：创建/预检/文件夹导入/执行/对照/键盘/按钮入选/临时导出；截图 `test-artifacts/ui-export.png`、`test-artifacts/ui-compare.png`、`test-artifacts/ui-background.png` |
| 真实 ComfyUI 集成 L1 | **PASS（测试实例）**：ComfyUI 0.45.0 / torch 2.9.1+cpu / port 8199；插件路由、侧栏、1×1 真实任务通过 |
| 真实采样 L2（3×5） | **PASS（CPU 测试模型）**：15/15，严格 AAAAA→BBBBB→CCCCC |
| Q02 555 真实采样 | **PASS（CPU 测试模型）**：555/555；555 ATTEMPT/DRAW/OUT，顺序、唯一种子、唯一 prompt_id、文件哈希、PNG 解码、清单和源图哈希全部核验 |
| 真实定向取消 | **PASS（隔离实例）**：运行中取消只中断插件 prompt；排队中取消只移除插件 prompt；两场景外部哨兵均正常完成 |
| 真实强杀恢复 | **PASS（隔离实例）**：运行中 `taskkill /F` 后重启为 `RECOVERY_REQUIRED`/`UNKNOWN`，不自动重提；人工 retry 后 ATTEMPT-002 复用同一种子成功 |
| 真实保存/DB 崩溃窗口 | **PASS（隔离实例）**：F07（收据已写、DB 未提交）重启补记 SUCCEEDED；F08（DB 已提交）重启无重复；均只 1 ATTEMPT/1 OUT |
| 真实 HTTP 丢响应 | **PASS（隔离实例）**：`/prompt` 被真实 ComfyUI 接受但代理丢弃响应；只 1 次提交，UNKNOWN→同一 history success 找回→确认后 COMPLETED |
| 真实导出 | **PASS（隔离实例）**：QUICK/FINAL × FLAT/GROUPED 四组合 + 正式导出显式跳过 1 个 SRC，文件/哈希/清单均核验 |
| 真实追加 | **PASS（隔离实例）**：已完成 3×5 后给 SRC-0001 追加 5 次，DRAW 6–10 连续、旧选择保留、新种子唯一 |
| 真实备份恢复 | **PASS（隔离实例）**：备份→新增任务→恢复；新任务回滚消失、555 与 6 个导出保留、pre-restore 库保留并校验；旧备份恢复后 555/3×5 保留、后建任务缺失并列为 orphan，当前库回滚恢复 PASS |
| 真实无覆盖冲突 | **PASS（隔离实例）**：预置导出批次目录导致 confirm 409，标记文件未变；删除冲突后重试成功 |
| 真实导出续传 | **PASS（隔离实例）**：移走一个 OUT → PARTIAL_FAILED 2/3；恢复后 resume → COMPLETED 3/3，不覆盖已复制文件 |
| 真实浏览器关闭 | **PASS（隔离实例）**：60 DRAW 任务 8/60 时关闭标签，后台继续到 60/60；重开侧栏同步 60/60 |
| 真实安装到 ComfyUI | **PASS（隔离测试实例）**：`D:\Projects\ComfyUI-acceptance\custom_nodes\ComfyUI-Gacha-Master`，copy 模式，加载日志 `Gacha Master routes registered` |
| **独立 GPU 真实模型 1×1** | **PASS**：RTX 3070 + DreamShaper8_LCM（SHA-256 已核验），1/1，1 唯一 seed/prompt_id，512×512 OUT |
| **独立 GPU 真实模型 3×5** | **PASS**：15/15，15 唯一 seed/prompt_id，15 OUT 全哈希/PNG/清单 |
| **独立 GPU 真实模型 555** | **PASS**：555/555，555 ATTEMPT/唯一 seed/prompt_id，555 OUT 全哈希/PNG/清单，约 19m34s |
| **提交后 GPU 复验 1×1/3×5/555** | **PASS**：在 `744ff97` 产品代码上重新执行，1/1、15/15、555/555；555 约 19m30s，独立 SQLite/文件/哈希/PNG/清单核验通过 |
| **UI 改造专项（L0 浏览器测试台）** | **23 JS PASS（7 基线 + 16 新增）+ 85 Python（84 PASS + 1 SKIP）**；20 项 UI 矩阵中 16 项 PASS、4 项 PARTIAL（见第 8 节）；**真实 ComfyUI 新 UI 复核当时 NOT_RUN；2026-11-15 HOST13 UI01—UI06 已补验 PASS，见第 0 节** |

**L0 与真实测试分开报告。** 真实 1×1、3×5、555、安全/立即暂停、定向取消、强杀恢复、四组合导出和追加均已在隔离 CPU 测试实例执行；独立 GPU + 真实模型又完成了 1×1、3×5、555。用户生产 ComfyUI 仍未安装/未触碰，其 production-specific 验收仍为 NOT_RUN。

### 0.1 独立 GPU 真实模型复跑（2026-11-12）

| 项目 | 实际值 |
| --- | --- |
| 实例根目录 | `D:\Projects\ComfyUI-gpu-acceptance`（独立实例，不是用户生产） |
| ComfyUI | 0.45.0 |
| Python | 3.12.10 |
| PyTorch / CUDA | 2.9.1+cu126 / CUDA 12.6；`cuda.is_available()=True` |
| GPU | NVIDIA GeForce RTX 3070 Laptop GPU，8192 MiB，驱动 546.30 |
| 模型 | `DreamShaper8_LCM.safetensors`，2,133,804,992 B，SHA-256 `a4f3e1526c5dc4fcbe342f5c410d83ae202c7a415fcefcbb92e0f93fcd0a87c3` |
| 参数 | 512×512，4 steps，cfg 1.5，lcm sampler |
| 1×1 | `JOB-20261111-EB34AB`，1/1，PASS |
| 3×5 | `JOB-20261111-CF7356`，15/15，PASS |
| 555 | `JOB-20261111-D6DB8B`，555/555，PASS，约 19m34s |
| 证据 | `test-artifacts/gpu-acceptance/1x1/SUMMARY.json`、`3x5/SUMMARY.json`、`555/SUMMARY.json`、`ENVIRONMENT.json` |

核验内容：DRAW/ATTEMPT 数、唯一 seed、唯一 prompt_id、OUT 文件存在/大小/SHA-256/PNG 解码/清单、源图哈希未变。**该实例为用户授权范围内新建的独立 GPU 测试环境，不是用户生产 ComfyUI。**

### 0.2 提交后复验（2026-11-12，基线 `744ff97`）

| 项目 | 实际结果 |
| --- | --- |
| Python L0 | 85 项：84 PASS + 1 SKIP（Windows 账户无法创建 symlink）；`test-artifacts/l0-postcommit.txt` |
| JS L0 | 7 PASS；使用 ZCode 内置 Node v24.21.0（系统 PATH 无 node）；`test-artifacts/js-postcommit.txt` |
| 独立 GPU 1×1 | `JOB-20261111-EB2A1E`，1/1，4.1 s，PASS |
| 独立 GPU 3×5 | `JOB-20261111-74D01F`，15/15，31.1 s，PASS |
| 独立 GPU 555 | `JOB-20261111-CB4D67`，555/555，1170 s，PASS |
| 独立核验 | 555 DRAW/ATTEMPT/OUT、555 唯一 actual_prompt_id、555 唯一种子；OUT 文件存在/大小/SHA-256/PNG/清单全通过；111 源图哈希不变 |
| 证据 | `test-artifacts/gpu-acceptance-postcommit/SUMMARY.json`、`run.log` |

说明：GPU 实例安装的插件源码在验收前已与 `744ff97` 逐文件比对一致（排除 `__pycache__`）。首次提交后 L0 出现 1 个偶发失败（`test_started_append_block_keeps_continuity`）：fake engine 在第二次追加提交前已完成整个运行块，追加按设计把 COMPLETED 任务置回 PAUSED；产品行为正确，测试未固定该竞态。已改为在追加期间暂停 fake engine 自动完成，并断言追加时任务仍为 RUNNING；单测连续 5 次及全量 L0 均通过。

## 1. 测试等级

| 等级 | 本轮做了什么 | 不能证明什么 |
| --- | --- | --- |
| L0 | 真实 SQLite/文件/进程/HTTP API；注入 `FakeEngine` 驱动调度与保存；子进程 `os._exit` 崩溃；浏览器测试台加载真实前端模块 | 不能证明真实 ComfyUI 路由/队列/取消/GPU/模型/真实 `LoadImage`/`SaveImage` 契约 |
| L1 | 已在隔离测试实例运行：ComfyUI 0.45.0、frontend 1.58.5、torch 2.9.1+cpu、端口 8199；真实安装/路由/强杀/定向取消/导出 | 不是用户生产实例，不是 GPU 性能验收 |
| L2 | 已运行真实 KSampler/VAE 采样：1×1、3×5、555；真实安全暂停、强杀重试、追加 5 次。另在独立 GPU 用 DreamShaper8_LCM 复跑 1×1、3×5、555 | CPU 实例为 tiny 模型；GPU 实例为真实 SD1.5 LCM checkpoint，但仍不是用户生产环境 |

## 2. 运行命令与证据

```bash
# 全量 Python L0（85 项，1 项 symlink 权限 SKIP）
.venv/Scripts/python.exe -m unittest tests.unit.test_core tests.unit.test_node tests.unit.test_scheduler tests.unit.test_exports tests.unit.test_reliability tests.unit.test_scale tests.integration.test_api tests.integration.test_install tests.integration.test_integration_register tests.integration.test_comfy_adapter tests.fault_injection.test_crashes -v
# 输出：test-artifacts/l0-full.txt

# JS L0（7 基线 + 16 UI 改造专项 = 23 项）
node --test tests/js/api.test.mjs tests/js/ui_refresh.test.mjs
# 输出：test-artifacts/js-tests.txt、test-artifacts/ui-refresh/SUMMARY.md

# UI 改造浏览器 L0（真实前端模块 + FakeEngine，不是 ComfyUI）
.venv/Scripts/python.exe tests/ui_harness/server.py
# 页面：http://127.0.0.1:8765/test
# 可选 GACHA_UI_ROOT=<固定目录> 保留截图会话数据；仅测试路由 /__harness/*
```

UI 测试台：`tests/ui_harness/server.py` 启动 `127.0.0.1:8765`，页面 `/test`；使用真实浏览器操作，后端为 `FakeEngine`，不是 ComfyUI。测试数据根/结果/导出均位于临时目录，并在结束时清理；截图与 `ui-harness.json` 保存在 `test-artifacts/`。

## 3. Q01—Q35

“整体”按规格要求的等级判定；L0 子项通过不等于整体通过。

| ID | 规格等级 | 整体 | L0 子项与证据 | L1/L2 |
| --- | --- | --- | --- | --- |
| Q01 | L2 | **PASS** | 真实 ComfyUI 3 源×5 次：15/15；数据库顺序严格 `(1,1..5)(2,1..5)(3,1..5)`，15 唯一任务内种子、15 个不同 prompt_id；`test-artifacts/real-comfyui/L2_3x5.json` | 真实 L2 PASS（CPU tiny 测试模型，1 step） |
| Q02 | L2 | **PASS** | 真实 555/555 COMPLETED；555 ATTEMPT、555 DRAW、555 OUT，顺序严格 1..111×1..5，555 唯一 prompt_id/种子，555 文件哈希+PNG 解码+manifest 全部通过，111 张源图哈希未变；`test-artifacts/real-comfyui/L2_555_completed.json`、`L2_555_verify.json`、`L2_555_resume.log` | 真实 L2 PASS（CPU tiny 测试模型，1 step） |
| Q03 | L1+L2 | **PASS（真实）** | 555 真实任务运行中安全暂停：完成当前项后 `PAUSED`、inflight=0、槽空；成功 357→358；恢复后继续；L0 测试同样通过 | 真实 L2 安全暂停 PASS |
| Q04 | L1+L2 | **PASS（真实）** | 运行中定向取消：插件 prompt 在 KSampler 被 `execution_interrupted`，job PAUSED/ATTEMPT INTERRUPTED；同队列外部哨兵随后 success。排队中定向取消：只移除插件 pending prompt，外部哨兵 success；插件凭 cancel 确认+absent 结算为 INTERRUPTED/PAUSED（`remote_state=cancel_acknowledged`）。`Q04_targeted_cancel.json`、`Q04_queued_cancel_after_fix.json`；能力表 `verified_atomic` | 真实 ComfyUI 运行中+排队中定向取消 PASS |
| Q05 | L1+L2 | **PASS（真实）** | 2000 步真实 prompt 运行中 `taskkill /F` 杀 ComfyUI；重启后 job `RECOVERY_REQUIRED`、ATTEMPT `UNKNOWN`、slot 保留、queue/history 空，仍只有 1 个 ATTEMPT（不自动重提）；人工 retry 后 ATTEMPT-002 复用同一种子并 COMPLETED；`Q05_kill_pre.json`、`Q05_kill_post_restart.json`、`Q05_kill_after_retry.json` | 真实 L1/L2 强杀恢复 PASS（隔离实例） |
| Q06 | L1 | **PASS（真实）** | 本地代理把 `/prompt` 转发给真实 ComfyUI，收到 200/263B 后丢弃响应；插件只发出 1 次 `/prompt`，进入 RECOVERY_REQUIRED/ATTEMPT UNKNOWN，随后按 expected prompt ID 找到同一 history success，补记 SUCCEEDED（1 ATTEMPT/1 OUT），确认后 COMPLETED；`Q06_dropped_response.json`、`Q06_proxy.log`、`Q06_proxy.py` | 真实 HTTP 丢响应 PASS |
| Q07 | L1 | **PASS（真实 F07/F08）** | 真实 F07：OUT+manifest+terminal receipt 已写、DB 未提交时 os._exit；重启后 ATTEMPT SUCCEEDED/OUT VERIFIED/1 attempt，确认后 COMPLETED。真实 F08：DB finalize 后 os._exit；重启后仍 1 attempt/1 OUT，无重复。`Q07_F07_*.json`、`Q07_F08_*.json`；L0 另覆盖文件不全/哈希错/UNKNOWN | 真实保存/DB 崩溃窗口 PASS |
| Q08 | L0+L2 | **PASS（真实）** | L0 PASS；真实安全暂停/恢复（555）；真实强杀 UNKNOWN → 人工 retry 同种子 ATTEMPT-002（Q05）；真实保存失败 → `resolve retry` 后同 DRAW、同种子 `3545235195995082` 生成 ATTEMPT-002，4 个新 OUT VERIFIED，ATTEMPT-001 的 OUT1/2 哈希不变；真实 tail/priority 追加均产生新唯一种子（Q09/Q29）；`Q05_kill_after_retry.json`、`Q08_retry_after_save_failure.json`、`Q09_append5.json`、`Q29_priority_append.json` | 真实暂停/失败重试/追加种子与 ATTEMPT 分离 PASS |
| Q09 | L1+L2 | **PASS（真实）** | 已完成 3×5 后给 SRC-0001 追加 5 次：DRAW 6–10 连续、Task 20/20、旧 15 结果保留、3 个选择保留、5 个新种子唯一；`Q09_append5.json` | 真实 L2 追加 PASS（tail） |
| Q10 | L1 | **PASS（真实）** | 真实 ComfyUI 侧栏任务详情→对照：方向键 1/11→2/11，空格/右键结果图均切换入选；右键选中后刷新 4（SRC-0001 2、D2 ★），再次右键取消后刷新 3（D2 ☆）；`Q10_compare_selection.png`、`Q10_right_click.json` | 真实对照/方向键/空格/右键/持久化 PASS |
| Q11 | L1 | **PASS（真实）** | 真实四组合：QUICK/FINAL × FLAT/GROUPED 均 COMPLETED、3 项 VERIFIED、文件哈希/布局/manifest 全通过；正式导出显式跳过 SRC-0003 后仅导出 2 项并记录 skipped_sources；源结果未改；`Q11_exports.json`、`Q11_export_skip.json` | 真实 L1 导出 PASS（隔离实例） |
| Q12 | L1 | **PASS（真实主场景）** | 外部哨兵运行 + 插件 pending：取消仅移除插件 pending，插件 PAUSED/INTERRUPTED，外部 success；插件运行 + 外部 pending：取消仅中断插件，外部 success；队列最终空；`Q04_queued_cancel_after_fix.json`、`Q04_targeted_cancel.json` | 主场景 PASS；“取消目标恰在此刻结束”的竞态未单独注入 |
| Q13 | L0+L1 | **PASS（真实）** | L0 PASS；真实恢复 14:40 旧备份（VERIFIED）：555 仍 555/555、3×5 20/20；F08 回到 READY 未自动运行；Q06/Q18 后建任务在 DB 中缺失、结果目录保留并列为 orphan；pre-restore 当前库保留且 SHA-256 校验通过；随后用 pre-restore 恢复当前库，Q06/Q18/555/3×5=21 全部回来。`Q13_older_restore.json`、`Q13_restore_current_verify.json`、`Q13_restore_paths.json` | 真实旧库恢复/对账/原库保留/回滚 PASS |
| Q14 | L0+L1 | **PASS（真实）** | L0 PASS：重复保存/导出冲突不覆盖；真实导出目标冲突：预置批次目录+标记文件 → 409 `EXPORT_FILE_CONFLICT`、仍 DRAFT、标记不变，清理后 3/3；真实同名源：两个不同 PNG 均以 `same.png` 上传 → 2 SRC、原名均 same.png、哈希/快照路径不同、2/2 COMPLETED、本地原图哈希不变；真实失败重试新 ATTEMPT 且旧 OUT 不变（Q08）；GachaSaveImage 二次调用由 L0 覆盖；`Q14_export_conflict.json`、`Q14_same_name_sources.json`、`Q08_retry_after_save_failure.json` | 真实同名源/导出冲突/重试隔离 PASS |
| Q15 | L1+L2 | **PASS（真实）** | 真实 60 DRAW 任务运行到 8/60 时关闭唯一浏览器标签；关闭 8 s 后 API 显示 27/60（后台继续，started_at 未变）；重开 8199 页面，侧栏显示“已完成 60/60”、可用 OUT 60，未隐式重启；`Q15_while_closed.json`、`Q15_final.json`、`Q15_browser_closed_reopened.png` | 真实浏览器关闭/重开 PASS（CPU tiny 模型） |
| Q16 | L1 | **PASS（真实）** | 真实 multipart 一次上传 3 文件；真实侧栏文件夹扫描 4 张（坏图列出）、箭头重排、移除 UPPER、编号 0001–0003；真实 DOM drop（DataTransfer 两个 File）→ 0001 dragA/0002 dragB 上传完成并显示 SHA；`Q16_upload_reorder.json`、`Q16_drag_upload.json`、`Q16_drag_upload.png`；冻结后逻辑移除由 L0 覆盖 | 真实上传/拖拽/重排/移除 PASS |
| Q17 | L0+L1 | **PASS（真实）** | 真实扫描 `scanroot`：2/10 自然序、中文/大小写/大写扩展名、子目录不递归、坏 PNG 显式 invalid；order `bad,img2,Img10,UPPER,测试`，valid 4 / invalid 1；`Q17_folder_scan.json` | 真实 L1 文件夹扫描 PASS |
| Q18 | L1 | **PASS（真实）** | 真实 reference 模式：完成 1 次后改原图内容，追加并 start → 409 `INPUT_CHANGED`，attempt 数仍 1；恢复原图后哈希复原。真实 snapshot 模式：移动原图 a.png 后追加 1 次并 start → COMPLETED 21/21，快照继续可用；`Q18_reference_hash_change.json`、`Q18_snapshot_original_moved.json` | 真实引用阻断/快照继续 PASS |
| Q19 | L0+L1 | **PASS（真实）** | 真实坏 PNG/像素上限；reserve 800GB>free 736GB → 提交前 `DISK_LOW`；结果根 ACL 拒写 → `READ_ONLY`；结果根整体移走（卷失联）→ 路径不存在，提交前 `READ_ONLY`；三种场景均 0 ATTEMPT、0 OUT、无探针残留，job BLOCKED；`Q19_disk_low.json`、`Q19_readonly_results.json`、`Q19_volume_loss.json` | 真实磁盘不足/只读卷/卷失联提交前阻断 PASS |
| Q20 | L1+L2 | **PASS（真实）** | L0 PASS；真实 1×1 单 `LoadImage`→`GachaSaveImage`；真实 1 DRAW 4 OUT；真实 MASK 路径：`LoadImage.MASK → MaskToImage → ImageCompositeMasked → GachaSaveImage`，两张不同 alpha PNG 输出均为 64×64 灰度且 64 级、哈希不同，原图不变；真实额外 SaveImage → 预检 `EXTRA_SAVE_IMAGE` 警告、不接管；任务创建后客户端工作流改动不改变冻结 `workflow_hash`；`L1_tiny.json`、`Q24_multi_out.json`、`Q20_mask_extra_save.json` | 真实 MASK/批次/额外输出/冻结工作流 PASS |
| Q21 | L0+L1 | **PASS（真实）** | 真实双 KSampler 串联（#6→#10）：预检生成 2 个 seed binding + MULTIPLE_SAMPLERS 警告；2 DRAW 分配 4 个唯一整数种子，DB 映射与 ComfyUI history 中 node6/node10 完全一致，2/2 COMPLETED、2 OUT VERIFIED；`Q21_multi_sampler_seeds.json` | 真实多采样器种子映射 PASS |
| Q22 | L0+L1 | **PASS（真实）** | 第二 ComfyUI 实例共享 data_root 启动：插件 409 `INSTANCE_LOCKED`，第一实例健康；6 个并发 start（同 key）全 200/RUNNING；6 个并发 append（同 key）返回同一 block_id，effective 15、appended 5；2 个并发 safe pause → PAUSED；恢复后 COMPLETED 15/15、15 ATTEMPT、15 唯一种子，无双调度；`Q22_concurrency.json`、`Q22_instance2.log` | 真实双实例 + 多客户端并发 PASS |
| Q23 | L1 | **PASS（真实）** | 真实缺失 checkpoint 被 `/prompt` 拒绝；真实排队后删除 staged LoadImage 输入 → ComfyUI `execution_error` at node 1，job BLOCKED、ATTEMPT FAILED、0 OUT、无自动重试；`Q23_failure_stop.json`、`Q23_execution_error.json`；OOM/迟到取消由 L0/Q04 覆盖 | 真实验证拒绝 + 执行中异常首错即停 PASS |
| Q24 | L1+L2 | **PASS（真实）** | 真实 `RepeatLatentBatch(amount=4)`：1 DRAW→4 OUT 全部 VERIFIED；真实预置只读 `OUT-0003.png` → OUT1/2 PARTIAL、OUT3 CORRUPT、ATTEMPT FAILED、job BLOCKED、无 manifest/不认成功；`Q24_multi_out.json`、`Q24_partial_save.json` | 真实 4 OUT + 第 3 张保存失败 PASS |
| Q25 | L0+L1 | **PASS（真实 BUSY+迁移）** | 真实 `BEGIN EXCLUSIVE`：创建 5.51s 后 503 `DB_BUSY` retryable，无半份 JOB，释放后同 key 重试成功；真实迁移中途失败（冲突表）：启动 503 `DB_CORRUPT`，事务回滚后只剩冲突表，无 `jobs`/`schema_migrations` 半套表，旧备份 SHA-256 不变；`Q25_db_busy.json`、`Q25_migration_failure.json`；L0 覆盖事务/新 schema/备份失败 | 真实 BUSY + 迁移中途失败 PASS |
| Q26 | L0+L1 | **PASS（真实+合成注入）** | 插件 `execution_events=false`，状态只靠 queue/history 轮询；Q22 并发幂等只产生一次初始 JOB_STARTED/一个 BLOCK_APPENDED；旧版本选择 409；真实 COMPLETED job 注入迟到 `ATTEMPT_DISPATCHING`、重复 `ATTEMPT_SUCCEEDED`、旧 `JOB_STATUS(COMPLETED→RUNNING)`、重复 `JOB_COMPLETED` 后，job 仍 COMPLETED、ATTEMPT 仍 SUCCEEDED；events 表 DELETE/UPDATE 均被 append-only 触发器拒绝；Q05 空 history/queue → UNKNOWN；`Q26_events.json`、`Q26_selection.json`、`Q26_injection.json` | 真实幂等/合成乱序重复事件/空历史 PASS |
| Q27 | L1 | **PASS（真实）** | 真实 250 DRAW×4=1000 OUT COMPLETED，250 唯一种子/prompt_id、1000 文件哈希/PNG 通过；详情初始 50 张 + 加载更多，19 次到 1000；关闭重开恢复 50 张/1000 计数；停止 ComfyUI 4s 后重启并 reload，Q27 恢复 250/250、OUT 1000、首屏 50；`Q27_1000_*.json/png`、`Q27_reconnect.json` | 真实 1000 OUT/分页/重开/服务重连 PASS；IAB 无法切物理离线 |
| Q28 | L1 | **PASS（真实）** | L0 PASS；真实 UI 单选/取消/刷新持久化；两个并发客户端分别选择两个输出均成功；同一输出同版本并发切换一个 200、一个 409，状态未被抵消；旧版本选择 409；源缺失导出由 Q30 真实覆盖；`Q28_selection_concurrency.json`、`Q26_selection.json`、`Q30_export_resume.json` | 真实并发选择/版本冲突 PASS |
| Q29 | L0+L1 | **PASS（真实）** | L0 PASS；真实 tail 追加（Q09）DRAW 6–10 连续；真实 priority：暂停在 2/6 后追加 1 次 priority，恢复后执行顺序 1,2,7,3,4,5,6；真实嵌套：priority 块 7–11 执行中（已完成 3 个）追加 tail 1 次，最终顺序 1,2,3,7,8,9,10,11,4,5,6,12，块连续且 tail 在块后；`Q09_append5.json`、`Q29_priority_append.json`、`Q29_nested_append.json` | 真实 tail + priority + 块中再追加 PASS |
| Q30 | L1 | **PASS（真实）** | 真实导出前临时移走 SRC-0002 的 OUT：冻结后复制为 PARTIAL_FAILED（2 VERIFIED / 1 OUTPUT_MISSING）；恢复原文件后 resume → COMPLETED 3/3，未覆盖已复制文件；`Q30_export_resume.json` | 真实导出中断/恢复 PASS |
| Q31 | L1 | **PASS（真实）** | L0 PASS；真实全 SKIPPED → 422 confirm_zero，二次确认后零图 COMPLETED；真实预览后改变选择 → confirm 409、导出仍 DRAFT 无批次；真实后台未完成：1 源 6 DRAW 在 2/6 时安全暂停（4 pending），QUICK 预览入选 1 项，confirm 不带 cutoff → 422 `confirm_cutoff_required=true` 且仍 DRAFT；带 `confirm_cutoff=true` → COMPLETED 1 项；恢复任务到 6/6 后导出仍 1 项，未暗中加入后续结果；`Q31_zero_confirm.json`、`Q31_version_conflict.json`、`Q31_pending_cutoff.json` | 真实零图确认 + 版本冲突 + pending cutoff PASS |
| Q32 | L0+L1 | **PASS（真实）** | 真实路径越界/junction/上传名清理 PASS；恶意原名（HTML/script/percent/Unicode RTL）在侧栏列表、详情、对照、导出审核均只作为文本，`xssFlag=null`、无注入 img/svg；真实导出 4/4，manifest 保留原名 JSON，目标文件名仅 `SRC-####_DRAW-...`，无 payload；`Q32_security.json`、`Q32_uploads.json`、`Q32_export.json`、`Q32_xss_export.png` | 真实 XSS/编码/导出目标 PASS |
| Q33 | L1 | **PASS（真实）** | 安装器 L0 PASS；真实 copy 安装/重启加载 PASS；真实 `--uninstall` → `removed_copy`、data_retained=true，插件目录删除但 DB/备份保留；copy 重装并重启，health 200、capabilities 0.45.0/verified_atomic、32 JOB、555/1000 任务和备份保留；真实原地更新（停服后以工作树覆盖代码）→ health/32 JOB/555/555；回退到 HEAD `00b09d3`（删除包后复制）→ health/32 JOB/555/555；恢复当前代码 → health/32 JOB/555/555；`Q33_uninstall_reinstall.json`、`Q33_update_rollback.json` | 真实安装/重载/卸载/重装/更新/代码回退 PASS |
| Q34 | L0+L1 | **PASS（真实）** | reference 运行中改原图仍使用已校验 staged 字节；snapshot 创建 50MB PNG 时连续写入源文件，`_read_stable` 检测 stat 变化 → 409 `INPUT_CHANGED` “读取过程中源文件发生变化”，未创建 JOB、无快照/无损坏文件；`Q34_reference_race.json`、`Q34_snapshot_copy_race.json` | 真实引用竞态 + 快照复制竞态 PASS |
| Q35 | L0+L1 | **PASS（真实）** | 真实只读 DB：503 `DB_READ_ONLY`，scheduler 不启动，DB SHA-256 不变，恢复安全备份后主数据完整；真实 F12：备份临时文件后 os._exit，重启后 5 个 VERIFIED 旧备份保留、`.tmp` 未登记、queue/slot 空、无新提交；真实正常 Ctrl+C：先以独立 8200 实例验证控制台事件，再在主数据实例 8199（32 JOB、555/555）复测；均以 `CREATE_NEW_CONSOLE` 启动，启动器清除自动化宿主继承的“忽略 Ctrl+C”标志（正常前台终端无此标志），`GenerateConsoleCtrlEvent(CTRL_C_EVENT)` 后主实例 1.5 s 内 exit code 0、端口关闭、日志出现 `Stopped server`、无 traceback；重启后 health 200、32 JOB、555 COMPLETED 555/555、OUT 555、slot 空、queue 空；`Q35_db_readonly.json`、`Q35_f12_*.json`、`Q35_ctrlc_console.json`、`Q35_ctrlc_main.json`、`Q35_after_ctrlc_restart.json`、对应日志；L0 覆盖缓存清理 | DB 只读 + F12 + 正常 Ctrl+C 关停 PASS |

## 3.1 真实 ComfyUI 测试实例结果（2026-11-11）

用户明确授权后，在 `D:\Projects\ComfyUI-acceptance` 创建隔离实例；未触碰生产 ComfyUI（本机原本无生产安装）。

| 项目 | 实际事实 |
| --- | --- |
| ComfyUI | 0.45.0，源码复制自 commit `4d64a8bc…`；端口 8199，仅监听 127.0.0.1 |
| Python/依赖 | `D:\Projects\ComfyUI-acceptance\venv\Scripts\python.exe`，3.12.10；torch 2.9.1+cpu、torchvision 0.24.1+cpu；frontend 1.58.5 |
| 插件安装 | copy 到 `custom_nodes/ComfyUI-Gacha-Master`；加载日志含 `Gacha Master routes registered`，`/gacha_master/v1/health` 200 |
| 能力表 | `/capabilities`：ComfyUI 0.45.0、frontend 1.58.5、queue/history lookup true、no-replace publish true、`targeted_cancel: verified_atomic`（该隔离实例配置） |
| 侧栏 | 真实 ComfyUI 页面出现“抽卡大师”侧栏按钮，面板显示任务/111 原图/运行进度；截图 `test-artifacts/real-comfyui/real-ui.png` |
| 模型 | `tiny-sdxs.ckpt`（347 MB）：tiny UNet/CLIP 测试权重 + 随机初始化标准 SD1 VAE；**真实可执行，但不是预训练画质模型**，分辨率 128、CPU；常规抽卡 1 step，取消/强杀测试用 2000 step 延长执行 |
| L1 1×1 | job `JOB-20261111-63861E`：COMPLETED 1/1；`GachaSaveImage` 在 `/history` 中，OUT-0001 128×128、manifest、PNG 元数据含 attempt/source/execution hash、种子 798030767378825；原始源图 SHA-256 未变 |
| 3×5 | job `JOB-20261111-F2DA44`：初始 COMPLETED 15/15，耗时约 46.5 s；顺序严格 AAAAA→BBBBB→CCCCC；15 唯一 prompt_id、15 唯一任务种子；后续真实追加 5 次后 20/20 |
| 555 | job `JOB-20261111-BAB90A`：**COMPLETED 555/555**（13:23:43Z）；555 ATTEMPT、555 DRAW、555 OUT、555 唯一 prompt_id、555 唯一种子；顺序、文件 SHA-256、PNG 解码、manifest、111 源图哈希全部核验；`L2_555_completed.json`、`L2_555_verify.json`、`L2_555_resume.log` |
| 安全暂停 | 真实通过：完成当前项后 PAUSED、inflight=0、槽空；恢复后继续 |
| 定向取消 | running job `JOB-20261111-34011F`：`Cancelling running prompt <插件ID>`，插件 ATTEMPT INTERRUPTED/job PAUSED；外部哨兵 success。queued job `JOB-20261111-2FAFB1`：只移除插件 pending，外部哨兵 success；插件凭 cancel 确认+absent 结算为 INTERRUPTED/PAUSED（cancel_acknowledged）；`Q04_targeted_cancel.json`、`Q04_queued_cancel_after_fix.json` |
| 强杀恢复 | job `JOB-20261111-7CA874`：2000 步任务运行中 `taskkill /F`；重启后 `RECOVERY_REQUIRED`/UNKNOWN/slot 保留，queue/history 空，ATTEMPT 数仍为 1（不重提）；人工 retry 后 ATTEMPT-002 复用同一种子成功；`Q05_kill_pre.json`、`Q05_kill_post_restart.json`、`Q05_kill_after_retry.json` |
| 保存/DB 崩溃窗口 | F07 job `JOB-20261111-E3BCFA`：收据已写/DB 未提交时退出，重启补记 SUCCEEDED（1 attempt/1 OUT）并确认完成；F08 job `JOB-20261111-6FA836`：DB finalize 后退出，重启仍 1 attempt/1 OUT，无重复；`Q07_F07_*.json`、`Q07_F08_*.json` |
| 导出 | 3×5 job 上 QUICK/FINAL × FLAT/GROUPED 四组合全部 COMPLETED/3 项 VERIFIED，文件哈希、布局、manifest 通过；正式导出显式跳过 SRC-0003 → 仅 2 项；`Q11_exports.json`、`Q11_export_skip.json` |
| 追加 | 3×5 job 完成后给 SRC-0001 追加 5 次 → 20/20，DRAW 6–10 连续，旧 3 个选择保留，新种子唯一；`Q09_append5.json` |
| 备份恢复 | 3.16 MB 一致性备份（VERIFIED）→新增 `POST-BACKUP-ROLLBACK` READY 任务→恢复同一备份；新任务回滚消失，555 仍 555/555，6 个导出保留，`pre-restore_*.sqlite3` 保留并校验；`Q13_backup_restore.json` |
| 旧库对账 | 恢复 14:40 旧备份：555/3×5 保留，F08 回 READY 未自动运行；Q06/Q18 缺失并列为 orphan 目录；pre-restore 当前库保留并校验；再回滚当前库，Q06/Q18/555/3×5=21 恢复；`Q13_older_restore.json` |
| 无覆盖冲突 | 预置 `FINAL-...` 批次目录及标记文件后 confirm → 409 `EXPORT_FILE_CONFLICT`，导出仍 DRAFT、标记内容未变；删除冲突后重试 3/3 COMPLETED；`Q14_export_conflict.json` |
| 失败停止 | 缺失 checkpoint 的真实工作流被 ComfyUI `/prompt` 拒绝：job BLOCKED、仅 1 个 FAILED ATTEMPT、第二个 DRAW 保持 pending，无自动重试；`Q23_failure_stop.json` |
| 多输出 | 真实 `RepeatLatentBatch(amount=4)`：1 DRAW→4 OUT，全部 VERIFIED，DRAW SUCCEEDED；`Q24_multi_out.json` |
| 浏览器关闭 | 真实 60 DRAW 任务 8/60 时关闭唯一标签，8 s 后 API 27/60（后台继续，started_at 未变）；重开页面侧栏显示 60/60，未隐式重启；`Q15_browser_closed_reopened.png` |
| 路径安全 | 真实 `subdir=../` 与输入根内 junction→外部目录均 403；上传名 `../evil.png` 规范为 `evil.png`；`Q32_security.json` |
| XSS/编码 | 恶意/百分号/Unicode 原名在列表/详情/导出审核均为文本，xssFlag=null，无注入节点；导出 manifest JSON 原名、目标名安全；`Q32_export.json`、`Q32_xss_export.png` |
| 导出恢复 | 临时移走一个 OUT 使导出 PARTIAL_FAILED（2/3）；恢复源文件后 resume → COMPLETED 3/3，已复制文件未覆盖；`Q30_export_resume.json` |
| 快照/引用 | reference 模式改原图后 start → 409 `INPUT_CHANGED`（attempt 数仍 1）；snapshot 模式原图移走后追加/启动 → COMPLETED 21/21；`Q18_*.json` |
| 草稿上传/重排 | 真实 multipart 上传 3 文件；侧栏扫描 4 张、箭头重排、移除 1 张、编号 0001–0003；`Q16_upload_reorder.json`、`Q16_draft_reorder_remove.png`、`Q16_drag_upload.json`、`Q16_drag_upload.png` |
| 1000 OUT UI | 真实 250 DRAW×4=1000 OUT；详情初始 50 张+加载更多，19 次到 1000；关闭重开恢复 50 张/1000 计数；服务重启后 reload 恢复；`Q27_1000_*.json/png`、`Q27_reconnect.json` |
| 多采样器种子 | 真实双 KSampler：2 DRAW×2 seed binding=4 唯一整数，DB 与 history node6/node10 完全一致；`Q21_multi_sampler_seeds.json` |
| 安装/卸载/重装/更新/回退 | 真实 uninstall `removed_copy` 且数据保留；copy 重装重启 health 200，32 JOB/555/1000 保留；真实原地更新与回退到 HEAD `00b09d3` 后均 health 200、32 JOB、555/555，恢复当前代码后数据不变；`Q33_uninstall_reinstall.json`、`Q33_update_rollback.json` |
| 双实例/并发 | 第二实例 409 INSTANCE_LOCKED；6 并发 start+6 并发 append 同 key 只产生 1 个 block；并发 pause 后 15/15、15 ATTEMPT/种子；`Q22_concurrency.json` |
| DB BUSY | 独立进程 BEGIN EXCLUSIVE 持锁，创建请求 5.51s 后 503 DB_BUSY retryable，无半份 JOB；释放后同 key 重试 201→COMPLETED 1/1；`Q25_db_busy.json` |
| 迁移中途失败 | 第二实例独立 data_root 注入冲突表：503 DB_CORRUPT；事务回滚后仅冲突表，无 jobs/schema_migrations；旧备份哈希不变；`Q25_migration_failure.json` |
| 合成事件流 | 对 COMPLETED job 注入迟到/重复/旧状态事件后仍 COMPLETED/SUCCEEDED；events DELETE/UPDATE 被 append-only 拒绝；`Q26_injection.json` |
| 引用竞态 | 真实 reference 任务运行中改原图：staged 哈希仍为登记值，任务 COMPLETED，ATTEMPT source_hash 不变，staged 完成后删除，原图恢复；`Q34_reference_race.json` |
| 快照复制竞态 | 50MB PNG 创建期间连续写源文件 → 409 INPUT_CHANGED，无 JOB/快照；`Q34_snapshot_copy_race.json` |
| DB 只读 | 真实只读 DB 启动 503 `DB_READ_ONLY`，scheduler 不启动，DB SHA-256 不变；恢复可写并从安全备份恢复，555/1000/Q34 任务全部回来；`Q35_db_readonly.json` |
| F12 轮转崩溃 | 备份临时文件后硬退出；重启后旧 VERIFIED 备份保留、`.tmp` 未登记、queue/slot 空；`Q35_f12_crash.json` / `Q35_f12_after_restart.json` |
| Ctrl+C 关停 | 独立 8200 实例与主数据 8199 实例均真实控制台验证；主实例 1.5 s 内 exit 0、端口关闭、日志 `Stopped server`；重启后 32 JOB、555 COMPLETED 555/555、OUT 555、slot/queue 空；`Q35_ctrlc_main.json`、`Q35_after_ctrlc_restart.json`、`Q35_ctrlc_console.json` |
| 磁盘保留不足 | 真实 reserve=800GB > free 736GB；start 后提交前 `DISK_LOW`，job BLOCKED、0 ATTEMPT、OUT 数不变；`Q19_disk_low.json` |
| 只读结果根 | ACL 拒绝写后 start → 提交前 READ_ONLY，0 ATTEMPT/OUT，探针无残留；恢复 ACL/原输出根后 555/1000/Q21 全部正常；`Q19_readonly_results.json` |
| 卷失联 | 结果根整体移走后 start → 路径不存在，提交前 READ_ONLY，0 ATTEMPT/OUT；恢复目录/主输出根后正常；`Q19_volume_loss.json` |
| HTTP 丢响应 | 代理转发 `/prompt` 并记录真实 200/263B 后丢弃响应；插件仅 1 次 `/prompt`，RECOVERY_REQUIRED/UNKNOWN → 同一 expected prompt ID 的 history success → ATTEMPT SUCCEEDED，确认后 COMPLETED；`Q06_dropped_response.json`、`Q06_proxy.log` |
| 未执行 | 用户生产 ComfyUI 兼容、alpha/EXIF 特殊语义、浏览器原生文件选择器上传、真实 ENOSPC、Q34 逐字节替换窗口等；见第 7 节 |

证据文件：`test-artifacts/real-comfyui/` 下的 `SUMMARY.json`、`L1_tiny.json`、`L1_OUT-0001.png`、`L1_outputs.complete.json`、`L2_3x5.json`、`L2_555_completed.json`、`L2_555_verify.json`、`L2_555_resume.log`、`Q04_targeted_cancel.json`、`Q04_queued_cancel_after_fix.json`、`Q05_kill_*.json`、`Q06_dropped_response.json`、`Q06_proxy.log`、`Q06_proxy.py`、`Q07_F07_*.json`、`Q07_F08_*.json`、`Q08_retry_after_save_failure.json`、`Q09_append5.json`、`Q10_compare_selection.png`、`Q11_exports.json`、`Q11_export_skip.json`、`Q13_backup_restore.json`、`Q13_older_restore.json`、`Q13_restore_current_verify.json`、`Q14_export_conflict.json`、`Q14_same_name_sources.json`、`Q15_*.json/png`、`Q16_upload_reorder.json`、`Q16_draft_reorder_remove.png`、`Q16_drag_upload.json`、`Q16_drag_upload.png`、`Q17_folder_scan.json`、`Q19_disk_low.json`、`Q19_volume_loss.json`、`Q21_multi_sampler_seeds.json`、`Q27_1000_verify.json`、`Q27_ui_1000.json`、`Q27_1000_ui.png`、`Q27_reconnect.json`、`Q27_reconnect_1000.png`、`Q18_*.json`、`Q23_failure_stop.json`、`Q23_execution_error.json`、`Q24_multi_out.json`、`Q24_partial_save.json`、`Q20_mask_extra_save.json`、`Q28_selection_concurrency.json`、`Q29_priority_append.json`、`Q29_nested_append.json`、`Q30_export_resume.json`、`Q31_zero_confirm.json`、`Q31_version_conflict.json`、`Q31_pending_cutoff.json`、`Q32_security.json`、`Q33_uninstall_reinstall.json`、`Q33_update_rollback.json`、`Q34_reference_race.json`、`Q35_db_readonly.json`、`Q35_ctrlc_console.json`、`Q35_ctrlc_main.json`、`Q35_after_ctrlc_restart.json`、`real-ui.png`；运行时数据在 `D:\Projects\ComfyUI-acceptance\user\gacha_master` 与 `output\gacha_master\Jobs`。

**限制**：CPU 实例使用极小测试模型；独立 GPU 实例使用 DreamShaper8_LCM 512×512/4 steps。两者都不代表用户自己的生产模型质量或工作流兼容性。定向取消能力在隔离 CPU 实例已设为 `verified_atomic`；仓库默认仍为 false，需在完成同类验证后由用户显式开启。

## 4. L0 自动化清单

- `tests/unit/test_core.py`：路径/自然排序/无覆盖发布/图片格式/损坏/过度像素/动画拒绝；工作流预检/种子/变换/下游保留/UI 映射。
- `tests/unit/test_node.py`：`GachaSaveImage` 的 batch 保存、manifest、PNG 元数据、UI 输出、零张拒绝、令牌校验、重复保存拒绝（FakeTensor + numpy，不依赖真实 torch/ComfyUI）。
- `tests/unit/test_scheduler.py`：3×5 顺序、种子、多 OUT、部分保存失败、安全/立即暂停、定向取消隔离/降级、丢响应不重提、失败停止、重试种子、单在途、优先/末尾追加、追加块连续、移除未执行 SRC、引用变化。
- `tests/unit/test_exports.py`：临时/正式、平铺/分组、冻结后选择不变、审核必填、全跳过零图、版本冲突、目标冲突、源缺失。
- `tests/unit/test_reliability.py`：多采样器种子、单实例、跨进程文件锁、备份轮转/恢复、坏图扫描、新 schema 拒写、只读 DB 分类、迁移中途失败原子回滚、只读结果目录提交前阻断、状态不可回退、磁盘不足阻断、CREATING 恢复/放弃、已完成输出 MISSING/CORRUPT 核验、symlink 扫描（权限不足时 SKIP）。
- `tests/unit/test_scale.py`：555 FakeEngine、1000 OUT 分页、10000 DRAW 计划（0.39 s）。
- `tests/integration/test_api.py`：能力/健康/设置/同源、上传/扫描/预检、任务生命周期、选择/事件、幂等、追加/暂停/恢复报告、导出/备份/存储/缓存。
- `tests/integration/test_install.py`：复制/链接安装、重复安装、卸载、未知目录拒删、数据保留、安装副本导入。
- `tests/integration/test_integration_register.py`：用伪 `PromptServer`/`folder_paths`/`comfy.cli_args` 验证根入口注册节点、WEB_DIRECTORY、全部路由、重复注册不产生第二实例（L0，不是真实 ComfyUI）。
- `tests/integration/test_comfy_adapter.py`：用本地 mock HTTP 服务验证真实 `ComfyAdapter` 的能力探测、提交、客户端 prompt_id 回显、queue/history 解析、丢弃响应（只提交一次）、拒绝、定向取消能力开关与中断历史区分；不是真实 ComfyUI。
- `tests/fault_injection/test_crashes.py`：F01—F12 子进程强杀/恢复。
- `tests/js/api.test.mjs`：API 客户端、错误映射、PUT 选择、multipart、事件缺口、格式化。

## 5. F01—F12 故障窗口

F01—F12 全部 L0 PASS。真实 ComfyUI 另执行：F07（收据已持久、DB 未提交）与 F08（DB 已提交）均 PASS；真实强杀恢复见 Q05/Q07。其余 F 点的真实注入未运行。

| ID | 注入点 | 重启后观察到 | 结论 |
| --- | --- | --- | --- |
| F01 | 领取事务提交前 | 无 ATTEMPT、槽空、DRAW PENDING、JOB RECOVERY_REQUIRED；确认后可继续 | PASS (L0) |
| F02 | DISPATCHING 落库、HTTP 未明确接收 | ATTEMPT UNKNOWN、槽占用、0 次新提交；人工 retry 后完成，种子不变 | PASS (L0) |
| F03 | 远端已接收、客户端丢响应 | 只提交 1 次；按原 ID 找回成功；UNKNOWN 不重提；**真实 ComfyUI Q06 同样 PASS**（1 次提交、same prompt history、COMPLETED） | PASS (L0 + 真实 Q06) |
| F04 | PNG 写到一半 | 无完整 OUT/清单；ATTEMPT UNKNOWN；保留临时证据 | PASS (L0) |
| F05 | 全部 OUT 发布、清单未发布 | OUT 存在但不是成功；UNKNOWN；不自动重抽 | PASS (L0) |
| F06 | 完整清单已发布、终态未持久 | `remote_state=files_found_terminal_unknown`，UNKNOWN；不自动成功 | PASS (L0) |
| F07 | 终态收据已持久、DB 完成事务未提交 | 重启幂等补记成功，0 次新提交，槽释放；**真实 ComfyUI F07 同样 PASS**（1 ATTEMPT/1 OUT） | PASS (L0 + 真实 F07) |
| F08 | 完成事务已提交、槽/界面未收尾 | 成功只记一次，槽已释放，无活动 ATTEMPT；**真实 ComfyUI F08 同样 PASS**（无重复） | PASS (L0 + 真实 F08) |
| F09 | 追加/种子事务中途 | 计划版本仍 1、无 APPEND 块、DRAW 数不变；重放不重复 | PASS (L0) |
| F10 | 导出复制中途 | 半成品不 VERIFIED；恢复只处理未验证项；源结果不变 | PASS (L0) |
| F11 | 导出文件已发布、item 状态未提交 | 同批次重新核验后认领目标，不覆盖、不新增成员 | PASS (L0) |
| F12 | 备份临时文件/轮转中 | 半份备份不入库；重启后另建备份成功 | PASS (L0) |

代表性证据：第 2 节测试日志；截图 `test-artifacts/ui-*.png`。子进程故障数据根是临时目录，测试结束清理；日志中的 PASS/断言是运行证据。

## 6. 规模与性能

| 项目 | 结果 | 说明 |
| --- | --- | --- |
| 10,000 DRAW 计划创建 | 0.39 s | 单源、1 种子/ DRAW，SQLite 本地；L0 |
| 真实 3×5 CPU 采样 | 初始 15/15，约 46.5 s；追加后 21/21 | tiny 测试模型、1 step；不是 GPU 性能 |
| 真实 555 CPU 采样 | 555/555，wall 约 24m40s（含中途暂停约 8m24s） | tiny 测试模型、1 step；不是 GPU 性能 |
| 真实 555 GPU 采样 | 555/555，wall 约 19m34s | DreamShaper8_LCM、512×512、4 steps、LCM；RTX 3070 8GB |
| 555 FakeEngine DRAW | 约 59.5 s | 111 源×5；**不是真实推理**；L0 |
| 1000 OUT 分页 | 250 DRAW×4；20 页×50 | 后端分页/计数；L0 |
| UI 全量解码 | 未测量 | 真实 ComfyUI/1000 结果浏览器性能 NOT_RUN |

## 7. 未运行/未覆盖（不得声称通过）

- 真实页面内 `app.graphToPrompt` 捕获并创建任务未系统验证；浏览器测试通过真实侧栏/API 完成。
- `LoadImage` 单输入、`GachaSaveImage` 单/多输出、MASK 路径、额外 SaveImage 警告、任务创建后客户端改动不影响冻结工作流已真实执行；alpha/EXIF 特殊语义、编辑器 UI 内继续操作的可视化未验证。
- 定向取消已真实通过（运行中与排队中）；“取消目标恰在完成瞬间结束”的竞态未单独注入；仓库默认 `targeted_cancel_verified=false`。
- 独立 GPU + DreamShaper8_LCM 已真实执行 1×1、3×5、555；用户生产 ComfyUI、用户自己的真实模型/工作流兼容仍未验证。
- 用户生产 ComfyUI 实例的强杀恢复未验证；强杀只在隔离 CPU 实例执行。
- 用户生产安装/升级/卸载未验证；隔离 CPU 实例的 copy 安装、原地更新、回退到 HEAD、卸载/重装已通过（更新为停服后文件覆盖，安装器本身没有 update 子命令）。
- 导出四组合、显式跳过、目标冲突、源缺失后 resume 已完成；导出期间其他写者并发修改的竞态未真实验证。
- Q16 真实 multipart、DOM `DataTransfer` 拖拽、文件夹扫描、重排/移除已通过；操作系统原生文件选择器上传、冻结后逻辑移除仅 L0 覆盖。
- Q19 真实 reserve 磁盘不足、ACL 只读结果根、结果卷整体移走已通过；真实磁盘写满（ENOSPC）未注入。
- Q23 真实 `/prompt` 拒绝与执行中 `execution_error` 已通过；OOM、迟到取消仅 L0/Q04 覆盖。
- Q24 真实 4 OUT 与第 3 张保存失败已通过；其他保存异常组合仅 L0。
- Q26 合成乱序/重复/旧事件与 append-only 拒绝已通过；真实网络层事件丢失未单独注入。
- Q32 路径越界/junction/上传名清理/XSS 文本化/导出目标已通过；超大上传、浏览器端恶意文件未系统测试。
- Q34 reference 改图与 snapshot 复制中改写已通过；“校验后立即替换”的逐字节窗口仅 L0。
- Q35 正常 Ctrl+C、DB 只读、F12 轮转崩溃已通过；Ctrl+C 为真实控制台事件（非物理键盘），DB “不可写”以只读卷覆盖。

## 8. UI 改造专项（2026-11-12）

规格与阶段门槛见 [UI-DEVELOPMENT.md](../UI-DEVELOPMENT.md)；完整截图、模拟说明与逐项证据见
`test-artifacts/ui-refresh/SUMMARY.md`（本地证据：`test-artifacts/ui-refresh/SUMMARY.md`）。

**环境边界：** 真实前端 ES modules + 真实插件 API/SQLite/调度 + `FakeEngine`；宿主侧栏与
`graphToPrompt` 是 stub，后端端口 8765。未运行真实 ComfyUI 0.45.0、真实队列/取消/GPU/模型。
测试台新增 `/__harness/engine`、`/__harness/create` 仅用于设置 FakeEngine 开关和夹具任务，
不进入插件产物。

| 检查 | 结果 |
| --- | --- |
| Python L0 回归 | 85 项：84 PASS + 1 SKIP（Windows symlink 权限） |
| JS L0 | 23 PASS（7 基线 + 16 UI 专项） |
| 浏览器行为检查 | 四步草稿、真实上传/重试、引用目录、旧预检隔离、创建幂等、对照/滑块/比例回退、统一入选、409、部分失败/恢复、UNKNOWN、暂停降级、断线重连、1000 OUT、320/480/720 容器、浅/深主题、XSS/外部资源，均见 SUMMARY |

| ID | 状态 | 主要边界 |
| --- | --- | --- |
| UI01—UI02 | PASS (L0) | 步骤往返、真实上传与单项重试 |
| UI03 | PARTIAL | 引用目录 PASS；浏览器拖入文件夹未执行 |
| UI04—UI06 | PASS (L0) | 旧预检丢弃、创建幂等、比例回退 |
| UI07 | PARTIAL | 409/串图 PASS；图片加载失败/异常 OUT 专属 UI 未触发 |
| UI08—UI14 | PASS (L0) | 滑块键盘、并发选择、20 次开关无监听泄漏、确认框、骨架/断线、暂停、1000 OUT |
| UI15—UI16 | PASS | 四组合导出、跳过/零图/截止、409 刷新、部分失败/恢复 |
| UI17 | PASS (L0) | UNKNOWN 人工决策、重连不自动启动 |
| UI18—UI19 | PARTIAL | 200% 缩放、减少动态效果未模拟 |
| UI20 | PASS (L0) | 无第三方资源请求；恶意文件名文本化 |

**结论：** UI-DEVELOPMENT.md 阶段 A—D 的 L0 交付与自动化检查已完成；20 项中 16 PASS、4 PARTIAL。
真实 ComfyUI 侧栏、真实工作流捕获、真实暂停/UNKNOWN/导出与 200% 缩放仍需在目标实例复核，
不得用本节的 L0 结果替代。

## 9. 正式使用前修复复验（2026-11-14）

环境：Windows 11；隔离真实 ComfyUI 0.45.0 / 官方前端 1.58.5；Python 3.12.10；torch 2.9.1+cu126；RTX 3070 Laptop 8 GiB；DreamShaper8_LCM、512×512、4 steps。插件为工作区候选源码的逐文件复制（55 个运行时文件哈希与源码一致）。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| Python L0（含新增 PLAN/迁移） | 97 tests：96 PASS + 1 SKIP，0 FAIL | `test-artifacts/final-20261114/python-tests.log` |
| JS L0（新增 PG/SRC/预算/状态合并） | 37 PASS，0 FAIL | `test-artifacts/final-20261114/js-tests.log` |
| 真实 GPU 1×1 / 3×5 / 555 | 全部 PASS；555 = 555 DRAW/ATTEMPT/OUT、555 唯一 seed/prompt_id、哈希/PNG/清单全通过；最终安装包另跑 1×1 PASS | `test-artifacts/real-acceptance-20261114/{run2,run5}/SUMMARY.json`（run3 为更早中间运行，不再是最终包引用） |
| 真实宿主 HOST01—HOST13 | 已取得证据（侧栏单入口、graphToPrompt、目录扫描、四步创建、CREATING 暂停/继续、3×5、详情、对照、四组合导出、重连、强杀恢复、窄视口） | `test-artifacts/real-host-20261114/`、截图目录 |
| 真实宿主双标签 | PASS：A 写入选，B 产生 409、恢复原值、回读服务器；清除阻断后两端收敛，无自动补写 | `test-artifacts/soak-playwright/host_double_tab.json` |
| 计划规模 | 1×100000：注册 p95 15.7 ms、完成 61.5 s；200×500：37.5 s；5000×20：266.8 s；计数/顺序/种子唯一性全通过 | `test-artifacts/perf-20261114/*.json` |
| 5000 原图 UI 分页 | 125 页×40，末页 SRC-4961…5000；跳转 SRC-4321；单次 limit 40；未读取结果 | `test-artifacts/soak-playwright/check_5000_ui.json` |
| 真实强杀恢复 | PASS：RUNNING 时强杀 → RECOVERY_REQUIRED/1 UNKNOWN，不自动重提；人工 retry → ATTEMPT-2 同种子成功，仅 1 个接受 OUT | `test-artifacts/real-host-20261114/crash-recovery.json` |
| 三启动周期 | 3/3 PASS：每次加载节点/路由/前端资产，真实 1×1 推理，正常 Ctrl+C 等价的 SIGINT 关停 exit 0；无 torchvision 异常 | `test-artifacts/real-host-20261114/startup-cycles.json` |
| 历史 v1 库迁移 | v1→v2；5 JOB/7 SRC/23 DRAW/21 OUT/4 选择/5 导出/8 备份全部保留；21 个输出哈希不变；integrity/外键 OK | `test-artifacts/migration-20261114/runtime/after.json` |
| 安装/更新/回退 | copy 安装、`--update` 暂存校验+外部备份+替换、junction、verify、卸载保留数据；损坏安装拒绝覆盖 | `test-artifacts/final-20261114/install.txt` |
| UI 长时导航 | Chromium 60.0 分钟：891 次翻页、6 次对照、0 JS 错误；挂载 1 行/50 卡、缓存 150 DTO、图片并发峰值 8；强制 GC 后堆中位 3.169→3.286 MB（+3.69%，≤20%），Nodes/Listeners 1044/299 不变。IAB 93 分钟补充 1022 次翻页；修复前游离 DOM 增长已由卡片池化解决 | `test-artifacts/soak-playwright/{soak-playwright.json,soak-playwright-pre-pooling.json}` |

**明确 NOT_RUN：** 用户生产 ComfyUI 与实际生产工作流（HOST14）、真实 ENOSPC（当前账户无管理员权限创建受控卷）、浏览器原生文件选择器上传。不得把隔离宿主结果写成用户生产环境通过。
