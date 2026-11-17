# 架构与实现说明

## 1. 总览

```text
ComfyUI sidebar (web/) ──HTTP──> backend/api.py
                                      │
                               backend/service.py ── SQLite (backend/db.py, repository.py)
                                      │
                               backend/scheduler.py ── adapters/comfy.py ── ComfyUI /prompt
                                      │                         └─ /queue /history /api/jobs/{id}
                               backend/storage.py  <── nodes/save_image.py
                                      │
                               backend/recovery.py / backups.py / exports.py
```

- 后端运行在 ComfyUI Python 进程内，生命周期独立于浏览器。
- 调度器是唯一提交 `/prompt` 的组件；HTTP API 只改数据库状态并唤醒调度器。
- 文件保存发生在 `GachaSaveImage` 节点内；调度器独立复核清单/文件后才写成功事务。
- 所有数据库操作经 `Database` 单连接 + RLock 串行化，短事务；网络/哈希/复制不在写事务内。

## 2. 目录

```text
__init__.py                     ComfyUI 入口（节点、WEB_DIRECTORY、路由）
comfyui_gacha_master/
  integration.py                PromptServer 路由/生命周期接入
  nodes/save_image.py           GachaSaveImage（受控 SaveImage 替换）
  backend/
    config.py                   Settings、AppPaths、限值校验
    db.py                       SQLite 打开/PRAGMA/事务/完整性/在线备份
    schema.py                   版本化 DDL、约束、触发器
    repository.py               参数化 SQL 与领域事务
    states.py                   JOB/DRAW/ATTEMPT/OUT/EXPORT 状态机
    service.py                  创建、启动、暂停、追加、选择、审核、设置
    review.py                   持久筛选会话、待筛导航、位置、组确认事务、返回
    scheduler.py                单在途、块顺序、提交、暂停、终态核验
    storage.py                  独占目录、PNG 发送、清单、终态收据、暂存输入
    inputs.py                   上传 inbox、文件夹扫描、快照/引用校验
    images.py                   Pillow 解码、头部尺寸、缩略图
    workflow.py                 捕获校验、绑定、种子、执行副本
    recovery.py                 重启核验、UNKNOWN 决策
    exports.py                  草稿/审核/冻结/复制/恢复
    backups.py                  一致性备份、轮转、恢复
    api.py                      aiohttp 路由与错误/幂等
    runtime.py                  启动顺序、单实例锁、依赖装配
    adapters/comfy.py           真实 ComfyUI HTTP 适配
    adapters/fake.py            仅测试用注入引擎
  web/                          侧栏任务中心（ES modules，无外部 CDN）
tests/                          L0/故障/安装/API/JS 测试
examples/                       示例工作流与合成原图
tools/install_plugin.py         安装/卸载
```

## 3. 数据不变量在代码中的落点

| 不变量 | 实现 |
| --- | --- |
| 原图只读 | 上传/快照只读源字节；引用每次重验哈希；不写回原路径 |
| 结果/导出不覆盖 | `publish_noreplace`（Windows `MoveFileExW` 无替换，POSIX `link`+fsync）；目标存在即冲突 |
| 一个 DRAW 最多一次成功 | `draws.accepted_attempt_id` 触发器：必须指向同 DRAW 的 SUCCEEDED ATTEMPT，且不可改；`finalize_success` 冲突检查 |
| 一个未核实执行 | `execution_slot` 单行；UNKNOWN 不释放槽 |
| 未知提交不重 POST | `SubmitResult("unknown")` → RECOVERY_REQUIRED，调度器不再提交，只监测原 attempt |
| 完成证据链 | 执行终态 + `outputs.complete.json` + 全部文件哈希/尺寸/身份 + DB 事务 |
| 暂停先禁止新调度 | `run_intent` 落库；`claim_draw` 在同一 `BEGIN IMMEDIATE` 中检查 `RUNNING + RUN` |
| 重启等待确认 | `RecoveryManager.startup` 清除 run_intent，RUNNING/PAUSING → RECOVERY_REQUIRED；不自动 POST |
| 历史固定 | 工作流/快照/种子/编号落库；追加按最大 DRAW 编号 +1；不重排 |
| 导出冻结 | 冻结事务写 export_reviews/items；之后选择/新结果不改成员 |
| 异常保留证据 | 部分 OUT、临时文件、收据、终态历史保留；缺图标记 MISSING/CORRUPT，不自动重抽 |

## 4. 编号与目录

- 显示编号：`JOB-YYYYMMDD-XXXXXX`、`SRC-0001`、`DRAW-000001`、`ATTEMPT-001-<短id>`、`OUT-0001`。
- 内部结果路径包含 ATTEMPT 身份，重试不会覆盖：

```text
<results_root>/<JOB display>/
  SRC-0001/DRAW-000001/ATTEMPT-001-abcd1234/OUT-0001.png
                                            outputs.complete.json
  thumbnails/
  manifest.json
```

- 数据根：`<data_root>/db/gacha.sqlite3`、`backups/`、`inbox/`、`jobs/<uuid>/`、`logs/`。
- 任务目录保存冻结的 `workflow_ui.json`、`workflow_api.json`、`create_request.json`、`plan.json`、`sources/`、`receipts/<attempt>/terminal.json`、`execution_prompt.json`。

## 5. 计划与种子

- 每个 SRC 一个 INITIAL BLOCK；每个 DRAW 在创建计划时分配种子，写入 `seed_allocations`，`UNIQUE(job_id, seed)` 防止任务内重复。
- 种子范围 = 绑定节点允许范围 ∩ `[0, 2^53-1]`；JSON 层面以整数/十进制传输（不经过 JS number 精度损失；前端不重算种子）。
- 追加 BLOCK：`priority=0`（优先）或 `priority=2`（末尾），`enqueue_seq` FIFO；编号按 `MAX(draw_no)+1`。
- 重试：同一 DRAW 清回 PENDING，新 ATTEMPT 使用新 UUID，种子保持。

### 调度块顺序

`Repository.next_executable_draw` 的排序：

1. 已开始的 APPEND 块（状态 RUNNING）继续；
2. 最早的待执行优先 APPEND 块；
3. 最早的 INITIAL 块（原图顺序）；
4. 最早的末尾 APPEND 块。

这得到严格 `A1..A5 → B1..B5 → C1..C5`，并允许优先追加在当前项之后插入；已开始的追加块不被后来的优先块打断。

## 6. 提交与终态

1. 校验源图/哈希/工作流哈希/磁盘/绑定种子。
2. 复制/校验源字节为 attempt 唯一暂存输入，写入 ComfyUI `input/` 根（直接子文件，原生 `LoadImage` 可列出）。
3. 生成执行副本：替换 `LoadImage.image`、写入种子、把目标 `SaveImage` 原位替换为 `GachaSaveImage`（保留节点 ID、上游 images 连线、下游连接）。
4. 独占创建 ATTEMPT 结果目录；注册 attempt token 上下文。
5. 同一事务：创建 ATTEMPT、DRAW→DISPATCHING、占用 `execution_slot`、写提交意图。
6. 只发一次 POST `/prompt`；超时/重置/未知 5xx → UNKNOWN，保留槽。
7. 轮询 `/history/{id}` 与 `/queue`；成功后独立验证清单/文件；再写终态收据、最终事务。
8. 只有成功事务完成且远端离开运行/待执行集合后才释放槽。

`GachaSaveImage`：
- 输入 `images`、`gacha_token`；`gacha_token` 参与缓存键；只接受后端注册的 attempt token。
- 每个 OUT 先写同目录临时文件 → fsync → 解码/哈希 → 无覆盖发布；N 张全部成功后最后发布 `outputs.complete.json`。
- 零张或第二次调用直接失败；部分文件保留为 PARTIAL 证据。

## 7. 暂停

- 安全暂停：落库 `PAUSE_SAFE`，当前已投递项完成、核验、记账后转 PAUSED。
- 立即暂停：落库 `PAUSE_NOW`；仅当适配器**运行时验证过**定向取消才调用 `/api/jobs/{id}/cancel`。当前未验证 → `degraded_waiting_current`，等待当前结束；绝不调用全局 `/interrupt`。
- 暂停与领取在数据库事务中串行：暂停先落库则 claim 失败；claim 先成功则暂停等待该项。
- 取消请求成功只表示请求被接受，必须继续观察 attempt 离开队列/历史并核验，才显示“已暂停”。

## 8. 恢复

`RecoveryManager.startup`：
1. 新 boot_id、清除所有 run_intent、禁止新提交。
2. 找活动/UNKNOWN ATTEMPT；对每个 ATTEMPT：
   - 远端 queued/running → 保留槽，等待；
   - 终态成功 + 完整清单/文件 → 幂等补记成功（报告已找回）；
   - 终态成功但输出不完整 → DRAW 失败，保留证据；
   - 终态失败/中断 → 失败/中断，释放槽；
   - 有完整文件但无可靠终态 → UNKNOWN，保留槽；
   - queue/history 均无 → UNKNOWN，保留槽；
   - 已成功 DRAW 的文件丢失/哈希错 → 标记 MISSING/CORRUPT，保留完成事实。
3. 需要人工的 JOB → RECOVERY_REQUIRED / recovery_version+1。
4. 人工决策：重新核验、继续等待、放弃旧尝试并重试、标记失败、维持 UNKNOWN；逐项落库和事件。

## 9. 导出

- DRAFT（草稿）→ 审核记录（正式导出逐 SRC 确认入选或明确跳过）→ 版本对比 → 冻结 → 复制 → COMPLETED / PARTIAL_FAILED。
- 冻结检查预览后的 plan/result/selection 版本；变化返回 409。
- 目标批次目录独占创建；文件名只用 `SRC/DRAW/ATTEMPT/OUT` 编号；复制后哈希必须等于冻结哈希。
- 目标已存在：同 item 且哈希一致才认领；否则冲突，不覆盖。
- 源丢失但目标完整匹配：可核验完成并报告源缺失；两者都不可用 → 失败。
- 恢复只处理未验证项，不新增/删除冻结成员。

## 10. 前端

- 官方 `app.registerExtension` + `extensionManager.registerSidebarTab({type:"custom"})`；旧版无侧栏 API 时使用独立浮动启动器（不依赖脆弱 DOM 注入）。
- API 经 `api.fetchApi`，图片经 `api.apiURL`，不硬编码端口。
- 事件轮询按 `last_seq` 增量、缺口触发快照重取、页面隐藏暂停轮询、恢复可见后继续；后端不依赖页面。连接状态（同步中断/最后成功时间）与 JOB 状态分开显示；重连只重取快照，不自动 start/resume。
- 共用 UI 组件（`web/components/`）：步骤条、投放区、骨架、有界通知、分层图片对比、轻量 tooltip；全部幂等 `destroy()`，不保留全局 rAF。
- 容器布局用命名 `@container`（侧栏 `gm-app`、模态 `gm-modal`）+ `ResizeObserver` 数据属性回退；无 React/Tailwind/CDN。
- 模态统一生命周期：标题关闭、Esc、遮罩、切页、程序关闭都经 `close(reason)`；焦点陷阱与关闭还原；确认框 Promise 单次结算。
- 创建：四步共享草稿；上传并发 3、顺序 key 先分配；预检按工作流/绑定修订号丢弃迟到响应；创建请求冻结 body+idempotency key，丢响应时允许同 key 同 body 重试。
- 对照：默认左原图右结果，可切换滑动分割线；同 `source.id`/`output.id` 身份；比例不一致回退；方向键在**同 SRC** 结果内切换、首尾不跨原图；滑块聚焦时方向键只移动分割线；右键只作用于结果区域。
- 选择 PUT 目标布尔值 + `expected_version`；详情缩略图与对照共用 `SelectionController`，按 OUT ID 应用响应；乐观更新失败回滚，409 只读回读；刷新后服务端状态权威。

## 11. 安全

- 路径：`abspath` + `realpath` + `commonpath`；输入根/导出根登记；不跟随扫描目录链接；拒绝 NUL/`..`/越界；导出目录不得落入数据/结果目录。
- 图片：格式白名单（PNG/JPEG/WebP）、静态单帧、像素/字节上限、实际完整解码。
- HTTP：同源检查；错误结构含 code/message/trace_id/retryable；无通配 CORS、无 shell、无任意路径下载。
- 前端：所有用户文本走 `textContent`；不把文件名/错误插成 HTML。
- 节点：输出 token 只映射后端注册的 attempt，不接受任意路径。

## 12. 设计取舍

- **原生 LoadImage + attempt 唯一暂存文件**：保留 MASK/alpha/EXIF/尺寸语义，避免复制 LoadImage 实现；暂存文件每次 attempt 唯一，内容哈希由原生 `IS_CHANGED` 参与缓存。引用模式读取一次原始字节并校验后再暂存，不建立永久副本。
- **输出原位替换**：`GachaSaveImage` 保持 `SaveImage` 的 IMAGE 返回契约与节点 ID，避免破坏下游连接。
- **轮询而非浏览器 WebSocket**：调度器用独立线程/HTTP 客户端，不依赖页面连接；当前能力表 `execution_events=false`。
- **检查点备份**：JOB 进入 READY 后创建 `job_created` 一致性备份；正式导出冻结前创建 `final_export_freeze` 备份，失败则阻止冻结；普通运行仍按 DRAW/时间阈值滚动备份。
- **定向取消默认不启用**：隔离实例已完成运行中/排队中真实隔离验证；仓库仍默认关闭，需在目标实例验证后用 `targeted_cancel_verified` 显式开启，安全降级优先。

## 13. 异步创建与有界前端（2026-11-14）

创建计划与推理执行现在是两个独立状态机：

- `POST /jobs` 在写锁内完成结构预检、工作流冻结、精确请求检查点和 `creation_progress` 行后返回 201；源图快照、DRAW/seed 生成由 Runtime 管理的**单个** `CreationManager` 线程按批次完成，不为每个 JOB 创建线程/Future。
- 每批在一个短事务中提交 DRAW、该 DRAW 的全部采样器 seed 与 `draws_committed/progress_version`；文件读取、解码、哈希和快照复制在事务外。批次默认 500 且可跨原图，超过约 200 ms 时自动减半。
- `control_intent`（CONTINUE/STOP/ABANDON）与 `generation` 构成屏障：暂停/放弃/继续/接管都会持久化意图；每批提交前和 READY 转换前校验 generation，迟到工作不能覆盖停止意图。
- 启动恢复把仍在活动阶段的计划改为 PAUSED；旧 schema v1 的 CREATING 任务用 `create_request.json` 和已提交事实建立 PAUSED 进度，无法核验时显式阻断，不按数量猜齐。
- READY 转换在一个事务中同时写 job 状态和 `stage=DONE`；若在最终校验后关闭进程，重启后可安全续作或保持不可执行。

前端不再保存无界结果数组：

- `web/collections/result_pager.js`：每 `(source, filter)` 一个单游标、单在途请求的 pager；最多缓存 3 页，页面起始游标轻量保留，失败按原游标重试；全局 DTO 预算 1000，跨 pager LRU 回收。
- `web/collections/source_window.js`：只保留一页 40 行原图 DTO 和页面起始 `source_order`；精确定位和前后翻页不依赖数组下标，逻辑移除留下编号空洞。
- `web/components/lazy_image.js`：共享图片队列，最多 8 个并发；IntersectionObserver 不可靠时用 700 ms 几何回退扫描，只对可见/近可见图片设置 src；卸载时释放观察器与 src。
- `web/resources.js`：集中预算与 `__gachaMasterResources()` 只读快照，供长时验收测量挂载行/卡、DTO、pager、图片并发。
- 详情页一次只展开一个原图的结果页，挂载 OUT 卡片固定在当前 50 张；切换原图时旧 pager 最多保留 5 个来源的 DTO，不保留 DOM。对照弹窗直接读取同一个 pager，不复制结果数组。

## 14. 持久筛选会话与组确认事务（2026-11-16）

连续筛选把“浏览位置”和“组决定”从浏览器状态提升为后端事实：

```text
GET /jobs/{id}/review-session ──> review_sessions（每任务一行）
        │                              │
        ├─ PATCH position ─────────────┤ version+1；只写位置，不确认
        │                              │
        └─ POST decisions ── BEGIN IMMEDIATE
              1. 校验 expected_session_version + result/selection/plan 三版本
              2. 校验整组健康入选数（SELECTED≥1 / SKIPPED=0）
              3. INSERT/UPDATE source_reviews
              4. UPDATE review_sessions: AFTER_CONFIRMED、确认边界、finished_at
              5. INSERT review_session_actions（request_hash + response_json）
             COMMIT ──> 审核、边界、动作回执同时可见
```

- `review_sessions` 的 `version` 单调递增；位置写入和决定都带 `expected_version`，旧请求迟到只能 409，不会把断点写回上一组。
- `break_type=IN_GROUP` 表示当前组；`AFTER_CONFIRMED` 表示本组已提交，恢复时由 `confirmed_source_order` 之后按稳定 `source_order` 找下一待处理组。`finished_at` 只在本轮确实没有下一组时设置。
- 组确认和继续边界在同一事务；`R01` 故障点位于提交前（回滚），`R02` 位于提交后、响应前（两者都保留，客户端回读）。动作回执使 HTTP 响应丢失后的重试幂等，不会推进两组。
- 待筛导航由 SQL 的 `EXISTS(健康可用 OUT) + NOT EXISTS(当前版本 source_reviews)` 直接筛选，按 `(job_id, source_order)` 有界分页；不逐页扫描、不用数组下标当身份。
- 结果页恢复使用 `outputs?offset=`：会话保存当前 OUT 身份与过滤后序号，`ResultPager.restore({offset, outputId})` 直接取对应页；页码分页仍按 `(draw_no,out_no)` 游标，跨页多选不改变整组统计。
- 前端 `review_session.js` 是唯一决定状态机：等待 `SelectionController.whenIdle()`、位置 flush、服务端整组统计与版本，再提交；单在途锁阻止双击。`views/review.js` 只渲染，不持有权威进度。
- 详情行展开/收起在结果容器层完成：`hidden`/`aria-hidden`、移除全部结果子节点、回收图片引用；pager 回调只对当前展开行渲染，迟到响应不能重开已收起行。

## 21. 存储清理与留存模型（2026-11-17）

### 21.1 数据模型（schema v4）

- `output_retention(output_id PK, state ∈ RETAINED/CLEANING/PURGED, cleanup_id, original_rel_path, sha256, size, reason, updated_at, purged_at)`：无记录等价 `RETAINED`。生成健康枚举不被改写；有意删除单独记录。
- `cleanup_runs`：不可变预览摘要（`plan_hash`、`plan_token_hash`、保护/排除计数、预计字节）、状态、确认/起止时间、失败/跳过/删除计数、`storage_revision`。
- `cleanup_items`：逐项 `(source_order, draw_no, out_no)` 身份、相对路径、原 sha256/size、文件身份、状态 `PENDING/INTENT/DELETED/FAILED/SKIPPED`、错误与孤立缩略图字节。
- `job_storage_state`：`storage_revision`，用于页面/分页/预览/草稿缓存失效；不改 `plan_version/result_revision/selection_version`。
- `maintenance_state`：单例持久维护占用；条件 `UPDATE ... WHERE holder IS NULL` 在 `BEGIN IMMEDIATE` 中取得，跨进程唯一。启动对账先解除旧占用，再服务可变 API。

### 21.2 外部清理日志（数据库外的权威）

```text
<data_root>/storage-identity.json
<data_root>/jobs/<job_uuid>/cleanup/<cleanup_uuid>/
  operation.json                  # 版本、实例身份、任务/路径/计划摘要、确认
  items/<output_uuid>.intent.json # unlink 前 fsync
  items/<output_uuid>.result.json # unlink 后 fsync（DELETED/FAILED/CANCELLED）
```

- 存储实例身份在首次清理前独立持久化；恢复旧 DB 不会更换身份。身份不匹配/未知 version/日志损坏时不导入、不授权删除。
- 删除顺序：校验边界与文件身份 → 写 intent → DB `CLEANING` → 重新核验 → unlink → 写 result → DB `PURGED`。没有持久 intent 不删除；result 写失败或 DB 更新失败进入 `RECOVERY_REQUIRED`，不谎报成功。
- 逐项只 unlink 精确 OUT 文件；不递归删目录，不删清单/收据/未知用户文件。
- 任务级孤立缩略图仅在该 32 位内容哈希键不再被任何留存 OUT 使用时删除；原图缩略图不动。

### 21.3 启动与恢复顺序

`Runtime._start` 在数据库/文件锁之后、`RecoveryManager.startup()` 之前调用 `StorageCleanupManager.reconcile_startup()`：

1. 读取/采纳实例身份；扫描 journal（损坏/身份不符只记冲突）。
2. 导入缺失的 cleanup run/items；有 `DELETED` 收据的 OUT 恢复为 `PURGED` 并提升 `storage_revision`。
3. 未闭合 intent：目标文件不存在 → 补记 `DELETED` 收据并 `PURGED`；文件仍存在 → 核验身份后写 `CANCELLED` 收据并保持 `RETAINED`，**绝不在重启后继续删除尚存在的图片**。
4. 已以 `FAILED/CANCELLED` 闭合的旧 intent 不豁免未来缺图：`verify_completed_outputs()` 只对“`PURGED` 且有 `cleanup_items.status=DELETED` 凭据”的 OUT 跳过缺图告警；其余缺失仍标 `MISSING`。
5. 清除遗留维护占用；DB 中仍为 `RUNNING` 的清理标记 `RECOVERY_REQUIRED`。

`verify_completed_outputs()` 对 `PURGED` 路径重又出现文件的情况只报告 `reappeared_conflicts`，不自动覆盖、不再次删除。备份恢复在替换 DB 后立即执行同一对账，再把清理事实导入旧库。

### 21.4 并发边界

- 结果清理取得维护占用后，调度领取（`Repository.claim_draw` 的 `BEGIN IMMEDIATE` 内）、导出冻结事务、入选/组确认/追加/移除/结果清理/备份恢复/设置迁移都会拒绝，返回 `STORAGE_BUSY`。
- 清理入口在存在活动/UNKNOWN/RECONCILING 执行、FROZEN/COPYING/RECOVERY_REQUIRED 导出或另一个清理占用时阻止；不会暂停/取消任务或中断 ComfyUI 执行。
- 哈希/复制/删除都在 DB 事务外；短事务只做状态与计数。HTTP 只启动后台线程，浏览器关闭不取消执行。
