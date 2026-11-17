# 接口与配置

前缀：`/gacha_master/v1`（由 ComfyUI 自定义路由注册）。所有响应带 `schema_version: 1`；成功为 `{"schema_version":1,"data":...,"event_seq":<n>}`，失败为：

```json
{
  "schema_version": 1,
  "error": {
    "code": "SUBMISSION_UNKNOWN",
    "message": "执行提交结果尚未确认，请先核验当前尝试。",
    "trace_id": "uuid",
    "retryable": false,
    "details": {"attempt_id": "uuid"}
  }
}
```

写操作（创建任务、启动/恢复、追加、导出预览/确认、人工 resolve）支持 `Idempotency-Key`；同 key 同内容返回原结果，不同内容 409。选择使用 `PUT` 目标布尔值。

## 主要接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /capabilities` | 实际能力、版本、限制、警告；未验证项不伪装 |
| `GET /health` | DB/PRAGMA、槽、备份、磁盘、调度器状态 |
| `GET/PUT /settings` | 配置；运行中拒绝改结果根 |
| `POST /uploads` | multipart 图片入受控 inbox，返回 upload token/尺寸/哈希 |
| `GET /uploads/{id}/image` | 上传预览（受控 inbox） |
| `POST /inputs/scan` | 已登记输入根下的直接图片、自然排序、无效项 |
| `POST /preflight` | 工作流/绑定/种子/节点预检 |
| `POST /jobs` | 校验并登记创建请求、冻结工作流，返回 201 + `status=CREATING`；源图复制和 DRAW/seed 由后台批次完成（幂等） |
| `GET /jobs` | 分页任务列表（cursor） |
| `GET /jobs/{id}` | 任务状态、持久事实计数与 `creation` 进度快照 |
| `POST /jobs/{id}/start` | READY/PAUSED 时显式启动；CREATING 时只请求“继续创建”，不会在计划完成后自动推理；恢复需 acknowledgement |
| `POST /jobs/{id}/pause` | `{"mode":"safe"\|"now"}`；CREATING 时持久停止计划工作；返回实际阶段 |
| `POST /jobs/{id}/abandon-creation` | 放弃未完成 CREATING 任务（保留检查点，不进入 READY） |
| `POST /jobs/{id}/resume` | 同 start |
| `GET /jobs/{id}/recovery` | 逐项证据与 UNKNOWN 列表 |
| `GET /jobs/{id}/sources` | 分页原图（`after_order`、`limit`）、有效/总原图数、DRAW/OUT/入选/审核汇总 |
| `POST /sources/{id}/outputs/state` | 轻量只读 OUT 状态：`{"output_ids":[...≤200]}`，返回 selected/version/health/result_revision；不存在或不属于该原图的 ID 放入 `missing_ids` |
| `POST /jobs/{id}/exports/preview` | 创建临时/正式导出草稿 |
| `GET /jobs/{id}/exports` | 该任务的导出批次 |
| `POST /attempts/{id}/reconcile` | 只核验，不提交 |
| `POST /attempts/{id}/resolve` | `decision=retry/mark_failed/abandon/keep_unknown/accept_success` |
| `POST /sources/{id}/remove` | 仅完全未执行原图；逻辑移除 |
| `POST /sources/{id}/relocate` | 引用模式同哈希重定位 |
| `POST /sources/{id}/append` | `{"count":n,"priority":"priority"\|"tail","expected_version":v}` |
| `POST /sources/{id}/review` | 源图审核决定 |
| `GET /sources/{id}/outputs` | 按 `(draw_no,out_no)` 稳定分页；`direction=forward\|backward` 支持反向页；返回 `next_cursor`/`prev_cursor`、`offset`、`total`；显式 `offset=N` 按过滤后序号定位（默认只列成功可用 OUT） |
| `GET /jobs/{id}/review-session` | 读取持久筛选会话、恢复上下文与全任务筛选汇总；没有会话时 `session=null`，不伪造位置 |
| `POST /jobs/{id}/review-session` | `{"scope":"pending\|all","start_source_id":?}`；幂等创建/取得会话，已有会话不重置断点 |
| `GET /jobs/{id}/review-sources` | 有界稳定 `source_order` 待筛导航：`scope=pending\|all`、`after_order`、`before_order`、`limit`；返回原图元数据、整组可用/已选统计、审核状态、`next_after_order`、`prev_before_order` 与汇总 |
| `GET /sources/{id}/review-state` | 整组状态：可用数、已选数/已选 OUT ID（≤200，超出 `selected_truncated`）、结果/选择/计划版本、待生成 DRAW |
| `PATCH /review-sessions/{id}/position` | `{"expected_version":v,"current_source_id":?,"current_output_id":?,"result_offset":n,"scope":?}`；只保存浏览位置，不确认原图、不改选择 |
| `POST /review-sessions/{id}/decisions` | 组确认（两枚组按钮唯一入口）；完整版本信封 + `Idempotency-Key`；单事务写审核、继续边界和动作回执 |
| `POST /review-sessions/{id}/back` | `{"expected_version":v}`；按会话历史返回上一组；不改已确认记录 |
| `GET /sources/{id}/image`、`/thumbnail` | 受控原图/缩略图 |
| `GET /outputs/{id}/image`、`/thumbnail` | 受控结果/缩略图 |
| `PUT /outputs/{id}/selection` | `{"selected":true,"expected_version":v}` |
| `POST /outputs/{id}/viewed` | 记录查看 |
| `PUT /export-drafts/{id}/reviews/{source_id}` | 正式审核：确认入选或明确跳过 |
| `POST /export-drafts/{id}/confirm` | 版本/截止确认、冻结并开始复制（202） |
| `GET /exports/{id}` | 冻结成员、逐项进度、错误 |
| `POST /exports/{id}/resume` | 原冻结清单下继续 |
| `GET /events?after=seq` | 增量事件（含缺口由前端重取快照） |
| `GET/POST /backups` | 列出/创建一致性备份 |
| `POST /backups/{id}/restore` | 显式恢复；保留原库，清除运行授权 |
| `GET /storage` | 数据/结果/备份/缩略图占用与磁盘可用 |
| `POST /cache/clear` | 仅清可重建缩略图缓存 |

## 异步创建与进度

`POST /jobs` 的确认只表示“创建请求已安全登记”，不表示计划已 READY。响应与 `GET /jobs/{id}` 中的 `creation` 来自持久表 `creation_progress`（schema v2）：

```json
{
  "stage": "PLANNING",
  "control_intent": "CONTINUE",
  "sources_total": 200,
  "sources_committed": 200,
  "draws_total": 100000,
  "draws_committed": 3500,
  "progress_version": 12,
  "generation": 1,
  "error_code": null,
  "error_message": null,
  "updated_at": "2026-11-14T10:00:00Z"
}
```

- `stage`：`WAITING/COPYING/PLANNING/FINALIZING/PAUSING/PAUSED/DONE/FAILED/ABANDONED`，与推理状态分开。
- `draws_committed` 只统计已提交事务的 DRAW；UI 必须同时显示 `draws_total` 与已生成数量。
- 重启或断电后 CREATING 计划变为 `PAUSED`，不会自动继续；用户显式 `start` 后才取得新 `generation` 从已提交事实续作。
- `POST /jobs/{id}/pause` 在 CREATING 时写入停止意图，不再错误地触发继续创建。
- CREATING 时禁止追加、移除原图、重定位和开始推理；`abandon` 后任务保持不可执行。

新增能力字段：`async_creation`、`creation_progress`、`output_state_api`、`output_backward_pagination`、`output_offset_pagination`、`resumable_review_sessions`、`review_decision_transaction`；限制字段增加 `plan_insert_batch`、`creation_batch_max`、`output_state_max_ids`。

## 连续筛选会话与断点

筛选进度以任务为单位保存在 SQLite（schema v3 的 `review_sessions`/`review_session_actions`），浏览器缓存不是权威来源。

### 会话 DTO 与恢复

`GET /jobs/{id}/review-session` 返回：

```json
{
  "session": {
    "id": "uuid", "job_id": "uuid", "scope": "pending", "version": 7,
    "break_type": "IN_GROUP",
    "current": {"source_id": "uuid", "source_order": 12, "output_id": "uuid", "result_offset": 50},
    "confirmed": {"source_id": "uuid", "source_order": 11, "decision": "SELECTED",
                  "result_revision": 10, "selection_version": 4, "plan_version": 2,
                  "confirmed_at": "..."},
    "history": [{"source_id": "uuid", "source_order": 10}],
    "finished": false, "created_at": "...", "updated_at": "..."
  },
  "resume": {"mode": "IN_GROUP", "reason": "current_group", "source": { /* 原图 DTO */ },
             "next_after_order": 12, "requeue_available": false},
  "summary": {"pending_sources": 289, "confirmed_sources": 11, "selected_groups": 8,
              "skipped_groups": 3, "no_result_sources": 4, "selected_outputs": 17,
              "available_outputs": 900, "plan_version": 2},
  "plan_version": 2, "job_id": "uuid"
}
```

- `break_type=IN_GROUP`：当前组未完成或正在复查；重启后回到 `current`（即使该组已被确认，用于显式返回复查）。
- `break_type=AFTER_CONFIRMED`：组确认已提交；重启后从 `confirmed_source_order` 之后的下一个待处理组继续。
- `resume.mode`：`IN_GROUP` / `NEXT_PENDING` / `FINISHED` / `NONE`。`FINISHED` 可带 `requeue_source`（更早组因新结果重新待筛时）。
- `finished` 只在本轮没有下一待处理组时为 true；不是“某组刚确认”的同义词。

### 位置保存

```json
PATCH /review-sessions/{id}/position
{"expected_version": 7, "current_source_id": "uuid", "current_output_id": "uuid",
 "result_offset": 50, "scope": "pending"}
```

- 每次成功保存 `version+1`；版本不一致返回 409 `VERSION_CONFLICT`，附当前 session/汇总，不自动覆盖。
- `current_output_id` 必须属于 `current_source_id` 且是健康可用结果；否则 409，位置不写。
- 切到新原图时服务器把旧当前组压入有界历史；`POST /review-sessions/{id}/back` 弹回上一组并设 `IN_GROUP`。
- 位置保存**不会**创建 `source_reviews`、不会修改入选、不会启动/暂停任务。

### 组确认事务

```json
POST /review-sessions/{id}/decisions
Idempotency-Key: uuid
{"source_id": "uuid", "decision": "SELECTED",
 "expected_session_version": 7,
 "expected_result_revision": 12,
 "expected_selection_version": 4,
 "expected_plan_version": 2,
 "reason": null}
```

- 四个预期版本都必须提交；`0` 是合法值（结果为 0/选择版本 0），漏传/部分提交返回 400。
- `SELECTED` 要求该原图整组至少 1 个健康、被接受且已保存的入选 OUT；`SKIPPED` 要求整组 0 入选。统计覆盖所有结果页，不只当前 50 张。
- 服务器在同一 `BEGIN IMMEDIATE` 事务内：校验会话版本和三个业务版本 → 校验整组入选数 → `INSERT/UPDATE source_reviews` → 更新会话 `AFTER_CONFIRMED`/确认边界/完成标记 → 写动作回执。任一步失败全部回滚。
- 成功响应包含 `session`、`review`（实际决定与 `selected_output_ids`）、`next`（`NEXT_PENDING` 或 `FINISHED`）和汇总。
- 同一 `Idempotency-Key` 同内容重试返回原结果（`idempotent=true`），不会再次推进；同 key 不同内容 409 `IDEMPOTENCY_CONFLICT`。响应丢失后，同一原图/版本/决定的新动作也会返回幂等结果而不推进。
- 过期会话/业务版本且不匹配已提交记录时返回 409；客户端必须回读，不得自动重发旧决定。
- 组确认不删除原图/结果、不取消入选、不触发导出；正式导出仍使用独立的导出草稿/逐源审核/冻结规则。

### 一致性与备份

- 会话按任务唯一（`UNIQUE(job_id)`），任务之间互不影响；逻辑移除原图保留编号，恢复时按稳定 `source_order` 定位下一组。
- 会话表随主数据库一起进入一致性备份/恢复；恢复旧备份后从备份时点的断点继续。
- 迁移为 schema v3 增量迁移；失败回滚不留半张表，旧任务无会话时 UI 显示“连续筛选”。

## 存储清理（按任务删除未入选结果）

### 目标与不可逆性

- 只处理当前任务中 **已完成筛选组** 内、**未入选**、**未被任何导出/草稿引用**、健康且被接受的成功 OUT。
- 删除是**永久删除**，不提供撤销；数据库备份只保存记录，不包含结果图片，不能恢复被删除文件。
- 原图/快照、入选 OUT、导出成品、清单/收据、工作流、种子、DB、备份和日志都不在本功能删除范围。

### 预览

```json
POST /jobs/{job_id}/cleanup/preview
Idempotency-Key: optional
{}
```

成功时 `data` 包含：

| 字段 | 含义 |
| --- | --- |
| `id` | cleanup_id |
| `status` | `PREVIEW` |
| `candidate_count` / `estimated_bytes` | 已核验文件身份/哈希的候选数量与实际文件字节 |
| `estimated_thumbnail_bytes` | 删除后会成为孤立的缩略图缓存字节（可选清理的缓存，不计入结果文件释放量） |
| `protected_selected_count` | 当前入选且被保护的 OUT 数 |
| `protected_export_count` | 未入选但被历史导出成员/有效草稿引用的 OUT 数 |
| `excluded` | 异常、未确认、已清理、文件缺失/变化等排除计数 |
| `overlap_note` | 保护与排除口径说明；分类可重叠，不相加为总数 |
| `all_skipped` | 全部适用原图组都被明确跳过（不能从 0 入选推导） |
| `plan_token` | 仅返回一次的确认令牌；服务器只保存其哈希 |
| `expires_at` | 预览有效期（15 分钟） |
| `storage_revision` | 留存版本；变化后必须重新预览 |

阻止条件使用 `CLEANUP_NOT_ELIGIBLE`（409），`details.next_action` 为 `continue_review` 时表示仍有原图组未按当前 `result_revision + selection_version + plan_version` 确认的原图组，界面应回到“继续筛选”。其余阻止原因包括任务非 COMPLETED/仍有运行意图、PENDING/活动/UNKNOWN/RECONCILING 执行、占用执行槽、导出冻结/复制/待恢复、已有清理占用、导出引用数据无法解析。

### 确认

```json
POST /storage-cleanups/{cleanup_id}/confirm
Idempotency-Key: required by UI
{
  "plan_token": "server-issued-opaque-token",
  "expected_candidate_count": 92,
  "confirm_permanent_delete": true
}
```

- `confirm_permanent_delete` 必须是严格布尔 `true`；缺失/false 返回 400。
- 令牌/候选数/计划哈希/选择/组确认/导出引用/文件身份/`storage_revision` 任一变化返回 `CLEANUP_PREVIEW_STALE`（409），**在删除第一张前拒绝**。
- 成功后返回 202；同一 `Idempotency-Key` 同内容重试返回**同一运行的最新状态**；同 key 不同内容返回 409 `IDEMPOTENCY_CONFLICT`。
- 一个实例一次只允许一个结果清理执行；占用是持久记录，进程重启由启动对账解除。清理与实例内生成投递、导出冻结/复制串行；清理期间目标任务的启动/追加/重选/组确认以及导出冻结、备份恢复、设置迁移返回 `STORAGE_BUSY`（409）。

### 进度、历史与逐项结果

- `GET /storage-cleanups/{id}`：`status` 为 `PREVIEW/RUNNING/COMPLETED/PARTIAL_FAILED/RECOVERY_REQUIRED/EXPIRED/CANCELLED`；`progress.total/deleted/failed/skipped` 由逐项记录实时聚合；`errors` 最多返回 10 条失败项。
- `GET /storage-cleanups/{id}/items?limit=50&cursor=...`：按 `(source_order, draw_no, out_no, output_id)` keyset 分页，单页上限 200；返回每项状态、错误、图片/缩略图 URL。
- `GET /jobs/{id}/cleanups?limit=20&cursor=...`：页面重进后可恢复历史；关闭浏览器不影响后台执行。
- 部分删除失败是 `PARTIAL_FAILED`，不是请求整体失败；已成功的删除不回滚，失败项保留文件且可读取。失败重试必须重新预览，成功项自然排除。

### 留存语义

- `outputs.health` 仍记录生成时事实（`VERIFIED/PARTIAL/MISSING/CORRUPT/...`）。额外 `output_retention.state`：`RETAINED`（无记录等价）、`CLEANING`、`PURGED`。
- 列表/分页/统计/筛选/比较/导出默认只把 `RETAINED` 且健康、接受条件满足的 OUT 当作可用；`include_unhealthy=1` 的历史模式会返回 `retention_state` 与“已清理”记录。
- 任务/原图汇总新增 `out_purged`、`out_cleaning`、`out_purged_bytes`、`storage_revision`；`out_available/out_verified/out_abnormal/selected` 已排除正在清理/已清理项。
- `GET /outputs/{id}/image` 与 `.../thumbnail` 对 `PURGED` 返回 **410 `OUTPUT_PURGED`**，对 `CLEANING` 返回 409 `OUTPUT_CLEANING`；不会把有意删除标成 MISSING/CORRUPT，也不会从残留缩略图伪装原图。
- 恢复清理前的旧数据库后，启动对账会把外部清理日志重新导入；旧备份中若仍有指向已清理图片的入选，导出返回 `OUTPUT_PURGED`，用户可取消该失效入选（允许 `selected=false`），但不能再把这个 OUT 设为入选。
- 旧备份恢复、数据迁移必须同时保留 `<data_root>/storage-identity.json` 与 `<data_root>/jobs/<job>/cleanup/`；只迁移 SQLite 不足以保留“有意删除”的语义。

### 缩略图缓存

`POST /cache/clear` 继续返回兼容字段 `removed`，并新增：

| 字段 | 含义 |
| --- | --- |
| `scanned` | 受控目录（`thumbnails/sources`、`thumbnails/outputs`）中检查过的 JPG 数 |
| `bytes` | 实际删除文件字节 |
| `failed` / `errors` | 被占用/只读等失败数量与最多 20 条详情 |
| `skipped` | 非受控内容、链接、并发消失/重建等跳过数 |
| `note` | “再次浏览会重新生成，当前缓存占用可能增加” |

该接口不删除 OUT、清单、原图、快照、数据库或备份；内容哈希共享的缓存键仍被其他留存 OUT 引用时，任务级清理不会误删。

## 默认配置

| 配置 | 默认 |
| --- | --- |
| protection_mode | `snapshot` |
| max_inflight / max_active_jobs | 1 / 1 |
| auto_resume_on_restart / auto_retry_draw | false / false |
| seed_domain_max | 9007199254740991 |
| plan_limit | 100000 DRAW/JOB |
| plan_insert_batch | 500 |
| upload_max_file | 100 MiB |
| decode_max_pixels | 100 MP |
| thumbnail_edge / page_size | 320 / 50 |
| submit_timeout / status_poll | 30 s / 1 s（测试可降至 0.01） |
| long_execution_warning | 1800 s |
| backup_draws / backup_interval / retention | 20 DRAW / 600 s / 5 份 |
| disk_reserve | 1 GiB/写入卷 |
| thumbnail_workers / export_workers | 2 / 1 |
| sqlite_busy_timeout | 5000 ms |
| export_layout | `FLAT`（每批次可改 `GROUPED`） |
| targeted_cancel_verified | false（只有人工在真实 ComfyUI 验证后才应开启；默认降级） |

配置保存在 `<data_root>/config.json`。`data_root`/`results_root` 运行中不可迁移；结果根变更必须停机、备份、核验后处理。

## 错误码（部分）

`WORKFLOW_UNSUPPORTED`、`NODE_MISSING`、`SEED_BINDING_INVALID`、`INPUT_CHANGED`、`INPUT_MISSING`、`SNAPSHOT_FAILED`、`SUBMISSION_UNKNOWN`、`EXECUTION_FAILED`、`CANCEL_UNSUPPORTED`、`OUTPUT_INCOMPLETE`、`OUTPUT_CONFLICT`、`DISK_LOW`、`DB_BUSY`、`DB_CORRUPT`、`INSTANCE_LOCKED`、`VERSION_CONFLICT`、`EXPORT_REVIEW_REQUIRED`、`EXPORT_FILE_CONFLICT`、`PATH_NOT_ALLOWED`、`CLEANUP_NOT_ELIGIBLE`、`CLEANUP_PREVIEW_STALE`、`CLEANUP_RECOVERY_REQUIRED`、`STORAGE_BUSY`、`OUTPUT_PURGED`、`OUTPUT_CLEANING`、`IDEMPOTENCY_CONFLICT`、`CREATION_CHECKPOINT_INVALID`、`CREATION_CHECKPOINT_MISSING`、`PAYLOAD_TOO_LARGE`。

`SUBMISSION_UNKNOWN` 不可直接重试；必须核验/人工 resolve。
