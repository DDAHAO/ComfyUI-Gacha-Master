# Changelog

## 存储清理与留存模型（CL01—CL05）— 2026-11-17

### Added
- schema v4（增量迁移，不改写 v1—v3 校验和）：`output_retention`、`cleanup_runs`、`cleanup_items`、`job_storage_state`、`maintenance_state`。旧库全部 OUT 默认 `RETAINED`；迁移不删除图片。
- `backend/cleanup.py`：按任务“清理未入选结果”的预览、资格校验、导出/草稿引用保护、路径/符号链接/junction 校验、SHA-256 与文件身份冻结、15 分钟 plan_token、幂等确认、后台执行、逐项 intent/result 日志、孤立缩略图处理、持久维护占用、启动对账与旧库日志导入。
- API：`POST /jobs/{id}/cleanup/preview`、`GET /jobs/{id}/cleanups`、`GET /storage-cleanups/{id}`、`GET /storage-cleanups/{id}/items`、`POST /storage-cleanups/{id}/confirm`；错误码 `CLEANUP_NOT_ELIGIBLE`、`CLEANUP_PREVIEW_STALE`、`CLEANUP_RECOVERY_REQUIRED`、`STORAGE_BUSY`、`OUTPUT_PURGED`（410）、`OUTPUT_CLEANING`。
- 前端：任务详情“存储清理”卡片、预览/候选分页/二次确认/后台进度/历史/失败详情；设置页互斥存储分项与缓存清理详细反馈；历史模式显示“已清理”并禁用入选/预览；`storage_revision` 变化自动失效结果页缓存。
- 测试：`tests/unit/test_cleanup.py`（32 项，含 5000 OUT 夹具与故障/重启/旧备份/Windows 占用与 junction）、`tests/integration/test_cleanup_api.py`（5 项 HTTP 契约）、`tests/js/cleanup_api.test.mjs`（4 项前端客户端契约）。

### Changed
- 全部可用性查询（结果列表/游标/offset/OUT 状态/统计/筛选 SQL/书签 offset/导出成员）统一排除 `CLEANING/PURGED`；`out_total` 保留历史记录，新增 `out_purged/out_cleaning/out_retained/out_purged_bytes`；`out_verified/out_abnormal/selected` 只统计留存项。
- `outputs.health` 保持生成事实；有意删除只记录在留存状态，`PURGED` 不再显示为可用图，也不进入异常缺图分支。
- `Runtime._start` 在普通恢复核验前执行清理日志对账；`verify_completed_outputs` 仅对“PURGED 且有 DELETED 凭据”跳过缺图，取消/失败旧意图不豁免意外缺图。
- 维护边界覆盖调度领取、导出冻结/复制/恢复、入选、组确认、追加、移除、备份恢复和设置迁移；清理执行期间返回 `STORAGE_BUSY`。
- `POST /cache/clear` 保持 `removed` 兼容，新增扫描/字节/失败/跳过/错误详情；只遍历受控 `sources`/`outputs` 缓存目录，不跟随链接。
- `GET /storage` 增加互斥分项与导出统计，明确 `results_bytes` 已包含缩略图。

### Verified
- 隔离 L0：Python 全量单元/集成/故障注入 + JS 全量回归；详见 `docs/TEST-REPORT.md` 与 `test-artifacts/storage-cleanup-20261117/`。
- 真实浏览器（L0 UI harness，FakeEngine）：预览、取消、二次确认、部分失败、重新预览补删、刷新/重启后历史恢复、设置页缓存清理。

### Not run
- 真实 ComfyUI 宿主/GPU 推理上的清理浏览器流程；用户生产实例与亲测数据未触碰。
- 断电级耐久性：故障注入验证的是进程强退/重启对账，不是物理断电承诺。


## 窄屏详情布局修复 — 2026-11-16

### Fixed
- 任务详情“原图与结果”在面板最窄处显示错乱（缩略图越出卡片右侧、文件名与计数被挤成窄列）：≤479px 断点把 `grid-row: 1 / span 2` 写在内层 `<img class="gm-source-thumb">` 上，而该行真正的网格项是外层 `.gm-source-thumb-host`，于是缩略图落入 `auto` 列（实测 25px），56px 图片向右溢出、文字被挤到同一窄列。现改用显式 `grid-template-areas`：缩略图跨两行、`SRC-xxxx` 与名称/尺寸/计数在右列、操作按钮独占一整行；无容器查询宿主的 `data-gm-size="narrow"` 回退规则同步修正（并补 56px 缩略图尺寸）。新增 `tests/js/responsive_css.test.mjs` 锁定该契约（回退到旧规则时两项均失败）。

### Verified
- L0 UI 测试台（真实 Chromium + 真实前端模块 + FakeEngine，**非 ComfyUI 宿主**）：面板宽度 240/300/360/460/480/600/700/860px 下，原图行、缩略图与展开后的结果网格横向溢出均为 0，窄屏缩略图稳定 56×56；JS 测试 50 项全部 PASS。

### Not run
- 用户真实 ComfyUI 侧栏最窄宽度的复测（需在宿主中拉伸侧栏确认）。

## 连续筛选、重启续筛与结果收起修复 — 2026-11-16（UXR01—UXR05）

### Added
- schema v3：`review_sessions`（每任务一个持久筛选会话：范围、会话版本、当前原图/结果/结果页 offset、历史、最近确认边界、完成时间）与 `review_session_actions`（组决定动作回执，含请求哈希与响应快照）。
- `backend/review.py`：`ReviewManager` 提供会话读取/创建、稳定 `source_order` 待筛导航、位置保存、组决定、返回上一组、整组状态与汇总；`decide()` 在一个 `BEGIN IMMEDIATE` 事务内校验会话版本和结果/选择/计划三个业务版本、校验整组健康入选数、写 `source_reviews`、推进会话边界并落动作回执。
- API：`GET/POST /jobs/{id}/review-session`、`GET /jobs/{id}/review-sources`、`GET /sources/{id}/review-state`、`PATCH /review-sessions/{id}/position`、`POST /review-sessions/{id}/decisions`、`POST /review-sessions/{id}/back`；`GET /sources/{id}/outputs` 新增 `offset`/`total`（供结果页恢复）。
- 前端：`web/review_session.js`（请求 token、位置 400 ms 节流、单在途决定、等待入选保存、冲突回读、下一组失败只重试导航、浏览器缓存不作权威）、`web/views/review.js`（连续筛选工作区：原图/当前结果/缩略图多选/跨页/返回/完成页/并排与滑动）、任务详情“连续筛选/继续筛选 · SRC-xxxx”入口和每行“筛选本组”。
- `ResultPager.restore({offset, outputId})` 与 offset 回退：恢复保存的结果页与当前结果身份；`SelectionController.whenIdle()/pendingCount`。
- 测试：`tests/js/review_session.test.mjs` 新增“从缩略图打开指定结果”定位用例；`tests/unit/test_review_sessions.py`（事务/版本/幂等/回滚/移除重定位/双任务/迁移兼容/备份、500 源夹具导航、1001 OUT offset）、`tests/integration/test_review_api.py`、`tests/js/review_session.test.mjs`（11 项，含缩略图定位）。
- 故障注入点 `R01`（组确认事务提交前）与 `R02`（提交后、响应前），用于真实进程强退验收。

### Fixed
- “查看结果→收起结果”残留：收起现在在行级结果容器上设置 `hidden/aria-hidden`、清空全部子节点（工具栏、网格、卡片、错误、空态、骨架屏）并回收图片引用；切换原图先收束旧容器；迟到分页/轮询/选择回调只在当前展开行且持有 active pager 时渲染。修复前 L0 复现为 6 张卡片+分页栏仍可见，修复后 0 可见结果节点。
- 组按钮“已选 N 张”曾在入选保存完成、整组统计尚未回读的窗口显示旧值；现在该窗口保持“保存中…”并禁用组按钮，直到服务端整组统计返回。
- `finished_at` 曾在每次组确认时写入，导致“已确认但还有下一组”的会话被误显示为已结束；现在只在本轮没有下一待处理组时标记完成，详情入口以服务器 `resume` 为准。
- 位置保存失败/冲突不会显示“已保存”；下一次组确认前必须重新保存或回读，旧位置请求不会在确认后把断点写回上一组。
- 详情页结果缩略图点击原先仍打开旧的“对照筛选”弹窗；现在点击进入连续筛选工作区并精确定位到所点的 OUT（服务器按 OUT 身份计算结果页 offset）。旧单结果对照保留在缩略图“对照”按钮和 Alt/Ctrl+点击。

### Verified
- Python 回归 **186 项（185 PASS + 1 SKIP）**，JS **48 PASS**，`result=PASS`；`test-artifacts/review-ux-20261116/run-20261116T164231/regression/REGRESSION.json`。
- 隔离真实宿主（ComfyUI 0.45.0 / RTX 3070 / DreamShaper8_LCM，10 张真实风景照片、100 个真实 GPU 结果）：收起、组内多选保留、跳过、返回、未确认断点、fresh browser 恢复、完整重启 4+ 次、强杀、`R01` 提交前崩溃、`R02` 提交后响应丢失/幂等重放均 PASS；真实任务原有 100 OUT 与 3 个入选保留，schema 2→3 迁移成功。
- 500 原图×2 结果夹具（`data_source=fixture`）：38→41、200→201、单组 60 结果第 50→51 张跨页恢复、跨页多选整组确认、重启后续筛 PASS。

### Not run
- 用户生产 ComfyUI/工作流/模型；FLOW11 的真实 `/prompt`/取消计数；COL04 错误/空态专项注入与 COL06 100 次循环；RES10 真实双标签、RES11 真实移除、RES12 真实备份恢复、RES13 真实位置保存失败注入；60 分钟工作区资源/堆复测。详见规格第 0.4 节。

## 正式投用开发 — 2026-11-15（PU01—PU02）

### Added
- `tools/check_release_evidence.py`：统一路径基准（`path_base` = `repo` 或唯一 `evidence_root`）、引用存在性、markdown 链接、候选四类清单/交付包/快照一致、指纹重算、回归非零、失败不得声明 PASS、`production_ready` 与门槛/工作包/阻塞项一致性、夹具不得计入真实执行、DISK PASS 必须 `enospc_observed`。
- `tools/candidate_manifest.py`：新增 `candidate-source.zip`（完整 source scope，含测试与工具）与 `--verify-snapshot`，在空目录解包并逐文件重建 runtime/browser_assets/delivery/source；`SNAPSHOT.json` 记录 `source_archive` 与 `verification`。
- `tools/storage_scenarios.py` + `tests/host_acceptance/storage_writer.py`：阶段同步协议（ready → 填充 → 释放写入 → 失败记录 → 清理 → 恢复断言）与 DISK01—05 场景矩阵（`tools/scenarios/storage-disk.json`），支持 `normal/refused/timeout/hard-kill/enospc` 与 `selftest`。
- `tools/target_environment.py`：`volumes` 采集卷/管理员/diskpart 状态，`template`/`validate` 校验 PU04 目标资料（缺失项逐条列出，`ready=true` 仅表示可开始隔离预验）。
- `tests/unit/test_release_evidence.py`（20）、`test_storage_tools.py`（33，含真实 junction 重定向）、`test_target_environment.py`（5）。

### Changed
- `tools/storage_acceptance.py`：`fill()` 自行执行卷/序列号/登记/所有权/预算检查，直接调用不能绕过 `preflight()`；独占 run-dir + 所有权标记；filler `'xb'` 无覆盖；写入前登记清单；report/manifest/log/stop-file 必须卷外；`cleanup()` 复核卷、最终路径、所有权与文件尺寸，只删本轮登记文件；新增 `register`/`recover`/`reserve-set`/`reserve-restore`。
- 证据入口勘误：`test-artifacts/next-20261115/FINAL-REPORT.md` 修正 `SUMMARY.json`/`cleanup.json` 链接并标注勘误；新增 `ERRATA.md`（路径基准、`DELIVERY.md` 指纹、交付包不含测试）。
- `DELIVERY.md` 不再自记最终包摘要，改以机器清单为准；修正 `78dd6947…` → `570cc39e…`。

### Verified
- Python 167（166 PASS + 1 SKIP）/ JS 37 PASS。
- 存储工具测试 33 项（负向保护 + 清单恢复 + reserve 还原 + junction）+ 全矩阵协议自检 12 组（`simulated=true`，非磁盘证据）。
- 候选解包重建四个 scope 一致；新汇总证据自检通过。

### Not run
- PU03 真实 ENOSPC（无受控卷）、PU04 目标工作流、PU05 目标安装/回退、PU06 三个日常周期。

## 下一阶段验收 — 2026-11-15（NP01—NP06）

### Added
- `tools/candidate_manifest.py`：runtime/browser/delivery/source 四类确定性清单、不可变交付 zip、重复校验、安装精确集合比较（含多余运行时文件）。
- `tools/production_acceptance.py`：参数化 `NxM`/别名、单源 `1x101`、场景文件、结构化失败、`PROGRESS.json`、dry-run 副作用检查、独立 SQL/文件/清单/接受关系核验、`data_source/environment/host` 三轴来源标签。
- `tools/acceptance_supervisor.py`、`tools/run_regression.py`（拒绝 0 项测试）、`tools/storage_acceptance.py`（受控卷预检/填充/清理）、`tools/host_acceptance/`（宿主启停、5000/10000 夹具、H06/H13 Playwright 场景与独立导出核验）。
- 安装器 marker v3 记录运行时文件数与树哈希；`--verify` 同时报告交付与运行时精确比较。
- `docs/OPERATIONS.md`：创建/开始区别、后页筛选、409、断网、UNKNOWN、导出部分失败、磁盘不足、备份/回退与 schema 边界。

### Fixed
- 原图翻页后 `aria-busy` 永久为 true；分页 state 事件现在按 loading 清除。
- 键盘 Space/Enter 入选后焦点被 ComfyUI 画布抢走，第二次 Space 无法取消；现在保留/恢复星标焦点。
- 对照/导出模态初始焦点可能被并发渲染移出对话框；增加 150/500 ms 焦点看门狗。

### Verified
- HOST06 A—D：真实 GPU 1×101（页 50/50/1）、后页入选 FINAL 导出 2 项独立核验、5000 源/10000 OUT 夹具（`data_source=fixture`）125 页/跳转 4321、失败/迟到响应。
- HOST13 UI01—UI06：主题/缩放、键盘焦点、减少动态、原生文件选择、缩略图/断网失败、冻结后移除+追加。
- Python 109（108 PASS + 1 SKIP）/ JS 37 PASS；NP01 dry-run/1×1/负向验证 PASS。
- NP03 真实 ENOSPC DISK01—05 **NOT_RUN**（非管理员、无独立受控卷）；NP04/NP05 **NOT_RUN**（无目标实例/工作流/维护窗口）。

## 正式使用前修复 — 2026-11-14（1.0.0 正式使用前修复候选）

### Added
- 异步创建：`POST /jobs` 先持久登记并返回 CREATING；`CreationManager` 单工作器按 `plan_insert_batch`（默认 500，可跨原图）分批提交 DRAW+全部采样器 seed+进度；`creation` 进度 DTO；暂停/放弃/继续使用持久 `control_intent` 与 `generation` 屏障；重启不自动继续。
- schema v2 增量迁移 `creation_progress`；迁移校验旧脚本 checksum；旧 CREATING 根据请求检查点与已提交事实建立 PAUSED 进度。
- `POST /sources/{id}/outputs/state` 轻量状态接口（≤200 ID、源图归属校验、missing_ids）；`GET /sources/{id}/outputs` 支持 `direction=backward`。
- 原图分页元数据：`next_after_order` 精确判断是否还有下一页，`total_sources/active_sources/removed_sources`。
- 前端 `ResultPager`/`SourceWindow`：每源最多 3 结果页、全局 DTO ≤1000、请求 token/AbortController、失败按原游标重试、稳定 ID 去重与顺序；原图 40 行有限窗口与编号跳转。
- 共享 `LazyImageLoader`：视口内加载、并发 ≤8、离屏释放、几何扫描回退；资源预算快照 `__gachaMasterResources()`。
- `tools/install_plugin.py --update/--verify`：暂存复制、逐文件校验、custom_nodes 外备份与失败回滚；marker v2 记录完整文件哈希。
- 测试：`tests/unit/test_creation.py`（PLAN01—07）、`tests/unit/test_schema_migration.py`、`tests/js/detail_pagination.test.mjs`（PG01—05/SRC/预算/状态合并）；JS 共 37 项。

### Fixed
- 详情“加载更多”成功后按钮永久禁用；分页请求现在按 token 结束，失败保留可重试入口。
- 201+ 原图被静默截断；现所有原图可按稳定 `source_order` 到达并跳转。
- 外部/双标签修改入选后已有卡片星标不更新；现按 OUT ID 应用权威状态，409 不自动补写。
- 任务列表/详情轮询每次重排 DOM 导致焦点/点击不稳定；现仅必要位置调整。
- 创建暂停错误触发“继续创建”；现持久停止并区分 PAUSING/PAUSED。
- 批量创建 finalize 逐源查询导致 100k 计划校验约 188 s；改为分组聚合后约 2 s。
- 5000 源请求体超过 aiohttp 默认 1 MiB；插件 JSON 端点独立读取上限 32 MiB。
- 原图整页 40/整页 50 精确整除时多发空页；服务端精确判断 has-more。

### Known limitations
- 用户生产 ComfyUI、实际生产工作流（HOST14）与真实 ENOSPC 仍 NOT_RUN。
- HOST06 大清单中的 201/5000 原图使用 L0 夹具；真实宿主使用 111 原图/555 OUT。
- 浏览器原生文件选择器上传、减少动态效果视觉评审仍仅 L0。

## UI 改造 — 2026-11-12（1.0.0 代码线，L0 浏览器验收）

按 UI-DEVELOPMENT.md 阶段 A—D 改造原生 ES modules 前端；无 React/Tailwind/CDN/新服务。

### Added
- 容器响应布局：命名 container query + `ResizeObserver` 数据属性回退，覆盖 320/480/720/980 px；深浅主题变量、`prefers-reduced-motion` 规则、`tabular-nums`、明确焦点框。
- 共用组件：步骤条、文件投放区、内容骨架、有界通知堆栈（最多 3 条、去重、悬停暂停计时）、图片滑动对比、轻量工具提示。
- 四步创建向导：共享草稿、捕获时间、原图步骤真实上传/校验/失败重试/并发 3、引用模式冲突、后端目录导入、计划上限、预检修订与迟到响应隔离、创建幂等（同 key/同 body 重试）。
- 并排/滑动对照：共用 OUT 身份、比例检查、完整 contain 显示、分割线键盘 1%/10%/Home/End、指针捕获拖动、右键仅结果区域。
- 共享 `SelectionController`：按 OUT 保存中禁用、乐观更新/失败回滚、409 只读回读、不同 OUT 可并发。
- 任务卡片/详情：成功抽卡/可用结果/已入选/需要处理优先，执行明细折叠；连接状态与最后同步时间；骨架首屏；结果分页增量追加。
- 导出：四阶段指示、逐 SRC 审核/跳过、0 图与截止版本二次确认、预览过期 409 刷新重审、部分失败清单与恢复、零图不再宣称“复制完成”。
- 恢复面板：证据/种子/尝试身份保留，明确区分“重新核验”和“确认继续”，无自动恢复。
- 测试：新增 `tests/js/ui_refresh.test.mjs`（16 项）；UI 测试台可选固定数据根与仅测试的 FakeEngine 场景控制；20 项 UI 矩阵与截图见 `test-artifacts/ui-refresh/SUMMARY.md`。

### Fixed
- `confirmDialog` 原先先 `resolve(false)` 再 `resolve(true)`，确认按钮永远返回 false；现 Promise 单次结算，确认 true、取消/遮罩/Esc false。
- 任务列表轮询曾推进分页 cursor 并不断追加下一页；现轮询只合并首页并保留已加载页。
- 对照模态每次打开都可能在关闭后残留 keydown 监听；现统一关闭生命周期，20 次开关监听净增 0。
- 入选保存曾由详情与对照各自实现乐观更新；现统一控制器，响应按 OUT ID 应用，不串图。
- 任务列表/详情全量重绘会丢滚动/焦点；现按 ID 原地更新，加载更多保留现有卡片与焦点。
- 详情在安全暂停完成、状态已为 PAUSED 后仍显示“等待当前项”；现状态变化时清理临时阶段提示。
- 连接成功回调被旧 store 状态覆盖，顶部一直显示“已连接”而非最后同步时间；现同步更新连接对象。

### Known limitations
- 新 UI 仅在 L0 浏览器测试台（stub 宿主 + FakeEngine）验收；真实 ComfyUI 0.45.0 侧栏/工作流捕获/暂停/UNKNOWN/导出复核 **NOT_RUN**。
- 20 项矩阵：16 PASS、4 PARTIAL；浏览器拖入文件夹、真实双标签、图片加载失败/异常 OUT 专属 UI、200% 缩放、减少动态效果未执行。
- C07 指针跟随聚光未实现（可选 P3）；引用模式创建前路径原图仅显示 `SRC` 占位。

## 1.0.0 — 2026-11-11

首个 V1.0 实现版本（L0 验证；真实 ComfyUI/GPU 验收 NOT_RUN）。

### Added
- ComfyUI 侧栏任务中心（官方 `registerExtension` / `registerSidebarTab`；旧版浮动启动器降级）。
- 多图上传、拖拽、尾部空白格；后端已登记根目录扫描（不递归、自然排序、坏图显式列出）。
- 快照/引用两种原图保护模式；SHA-256/尺寸/解码校验；引用模式每次执行重验。
- 一个 `LoadImage` + 一个最终 `SaveImage` 绑定；只改执行副本；额外输出警告。
- `GachaSaveImage`：独占 ATTEMPT 目录、临时文件 + 无覆盖发布、完整 `outputs.complete.json`、终态收据。
- 单在途调度器、块顺序（初始/优先追加/末尾追加）、任务内唯一持久种子、重试复用种子/新 ATTEMPT。
- 安全暂停与立即暂停；未验证定向取消时明确降级；绝不全局中断。
- SQLite schema v1、WAL/FULL/外键/quick_check、执行槽、事件、幂等请求、备份表。
- 一致性在线备份、校验、滚动保留、恢复保留原库、恢复后清运行授权。
- 重启核验、F01—F12 崩溃窗口、UNKNOWN 逐项人工处理（重试/失败/放弃/继续等待）。
- 临时/正式导出、平铺/按原图分组、逐 SRC 审核或明确跳过、零图二次确认、冻结、复制哈希校验、断点恢复。
- 原图/结果对照、方向键同 SRC 切换、Space/按钮/右键入选、多选持久化、`expected_version` 冲突。
- HTTP API（`/gacha_master/v1`）、统一错误结构、同源检查、上传/扫描/预检/恢复/导出/备份/存储/缓存接口。
- 安装/卸载脚本（junction 或受控复制；未知目录拒删；数据保留）。
- 示例 img2img 工作流与合成测试图；中文 README、环境、架构、状态、测试、已知问题、API 文档。
- L0 测试：81 Python（80 PASS + 1 symlink 权限 SKIP）+ 7 JS + 浏览器 UI 测试台；F01—F12 子进程故障注入。

### Known limitations
- 无真实 ComfyUI/模型环境：L1/L2、真实 555、真实强杀、真实安装 NOT_RUN。
- 定向取消未运行时验证；立即暂停默认安全降级。
- 不支持子图绑定、多最终保存节点、动画/多帧、列表映射多调用输出。
