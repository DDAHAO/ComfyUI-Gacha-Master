> 历史开发记录，归档于 2026-11-17。安装与当前版本说明请以 [项目首页](../README.md) 为准。文中的原始验收产物保留在开发机，不随公开仓库发布。

# ComfyUI 抽卡大师（ComfyUI Gacha Master）V1.0

把现有 ComfyUI 图生图工作流变成一台可控的“抽卡机”：一次导入多张原图，每张自动连续抽卡，后台单在途排队、暂停/恢复、断点核验，随后在原图对照界面筛选、追加抽卡并临时或正式导出。

> **2026-11-15 正式投用开发（PU01—PU06）结论：**PU01 证据勘误/候选可重建与 PU02 存储工具保护/阶段同步已在本轮**实现并验证**（证据检查器、完整源码快照与解包重建、写入入口自我保护、所有权/预算/卷外记录、DISK01—05 场景执行器的正常/拒绝/超时/模拟失败矩阵）。**PU03 真实 ENOSPC（DISK01—05）、PU04 用户实际工作流（HOST14）、PU05 目标安装/回退、PU06 三个日常周期仍为 NOT_RUN**：本机非管理员、只有 C:（系统）与 D:（项目）两个卷，没有可丢弃的受控卷，也没有用户目标实例/工作流资料。因此**不能声明任何生产组合正式可用**。本轮总报告见 PU01—PU02 完成报告（本地证据：`test-artifacts/production-use-20261115/20261115T113000-pu01-pu02/FINAL-REPORT.md`），机器汇总见同目录 `SUMMARY.json`。
>
> **上一轮（NP01—NP06，2026-11-15 早些时候，历史）：**NP01 候选冻结/可重复验收工具、NP02 真实宿主多页与日常交互、NP03 受控卷工具与故障注入完成；HOST06 A—D 与 HOST13 UI01—UI06 在隔离真实 ComfyUI 0.45.0 宿主取得 PASS 证据（1×101 OUT 为真实 GPU，5000 原图/10000 OUT 为明确标记的夹具）。报告见 2026-11-15 总报告（本地证据：`test-artifacts/next-20261115/FINAL-REPORT.md`），入口勘误见 ERRATA（本地证据：`test-artifacts/next-20261115/ERRATA.md`）。历史 2026-11-14 结论保留为 最终验收报告（本地证据：`test-artifacts/final-20261114/REPORT.md`）。

> **历史验证状态（2026-11-12/14，保留）**
>
> 用户授权后已在独立测试目录安装 ComfyUI 0.45.0（CPU torch 2.9.1）与极小测试模型，完成真实 L1 1×1、3×5、**555/555**、安全暂停、定向取消、强杀恢复、保存/DB 崩溃窗口、HTTP 丢响应恢复、四组合导出、导出失败续传、备份恢复、目标冲突保护、快照/引用校验、引用竞态隔离、快照复制竞态阻断、右键/键盘选择、双 KSampler 种子映射、双实例锁/并发幂等、DB BUSY/迁移失败回滚、合成旧事件不回退、1000 OUT 侧栏分页/服务重连、磁盘保留不足/只读/卷失联提交前阻断、XSS/编码防护、DB 只读停写、F12 备份轮转崩溃、浏览器关闭继续、追加 5 次、安装/更新/代码回退和正常 Ctrl+C 关停。
>
> - 后端、SQLite、文件保存、调度、恢复、导出、HTTP API 已通过 **L0** 自动化测试。
> - 真实 ComfyUI：插件 copy 安装到隔离实例，`Gacha Master routes registered`，真实侧栏可用；真实 KSampler/VAE/CLIP、`LoadImage`、`GachaSaveImage` 链路已执行。
> - 555 完成：555 ATTEMPT/DRAW/OUT、严格顺序、唯一 prompt_id/种子、全部文件哈希/PNG/清单/源图哈希核验通过。
> - 定向取消：运行中只中断插件 prompt → PAUSED；排队中只移除插件 pending → PAUSED/INTERRUPTED，外部哨兵任务均正常完成。
> - 强杀恢复：`taskkill /F` 后重启为 RECOVERY_REQUIRED/UNKNOWN，不自动重提；人工 retry 复用同一种子成功。
> - 独立 GPU 真实模型：RTX 3070 + DreamShaper8_LCM（真实 SD1.5 LCM checkpoint），1×1/3×5/555 全部 PASS（512×512、4 steps；555 约 19m34s）。
> - 2026-11-14 修复复验：真实宿主侧栏、四步创建、CREATING 暂停/继续、真实 3×5、四组合导出/双标签 409、强杀恢复、三启动周期、历史库 v1→v2 迁移、5000 原图 UI 分页、100000 计划响应性均取证；Python 97 项 96 PASS+1 SKIP、JS 37 PASS。
> - 真实模型是极小测试权重 + 随机 VAE，**不是画质模型**；CPU、常规抽卡 1 step（取消/强杀用 2000 step），不代表质量/GPU 性能。
> - **仍需**：用户生产 ComfyUI 及其真实工作流兼容；独立 GPU + DreamShaper8_LCM 已完成 1×1/3×5/555。
>
> 详见 [TEST-REPORT.md](../docs/TEST-REPORT.md)、[ENVIRONMENT.md](../docs/ENVIRONMENT.md)、[KNOWN-ISSUES.md](../docs/KNOWN-ISSUES.md)。

> **2026-11-17 存储清理（CL01—CL05）状态：**已实现按任务“清理未入选结果”（预览 → 明确确认 → 后台执行 → 进度/历史/部分失败）与改进后的缩略图缓存清理；schema 升级到 v4，新增独立留存状态、外部清理日志、持久维护占用、启动/旧备份对账。隔离 L0 全链路与浏览器操作已验收；**真实 ComfyUI 宿主/GPU 推理本轮 NOT_RUN**；用户亲测实例/图片未被触碰。逐项结果见 [TEST-REPORT](../docs/TEST-REPORT.md) 与 `test-artifacts/storage-cleanup-20261117/`。
>
## 已实现功能

- 多图点击/拖拽上传、尾部空白格、添加顺序冻结；后端受控文件夹根目录导入（不递归，自然排序，坏图显式列出）。
- 快照模式（默认，复制并 SHA-256/解码校验）与原路径引用模式（每次执行重验哈希）。
- 绑定一个现有 `LoadImage` 和一个最终 `SaveImage`；只改执行副本，原工作流/编辑器不变。
- 每张原图连续抽卡，自动逐项提交，全局单在途；555 DRAW 真实规模已通过（L0 模拟 + 真实 555/555）。
- 创建任务先返回 CREATING，由有界后台工作器按批次（默认 500，跨原图）生成 DRAW+种子+持久进度；暂停/放弃/继续有明确阶段，重启不自动继续。100000 计划注册 p95 < 20 ms，完整生成在本机 L0 约 62 s。
- 结果按稳定游标分页，每源缓存 3 页、全局 DTO ≤1000、详情挂载 OUT ≤300、缩略图并发 ≤8；原图按 40 行分页并可按稳定编号跳转，200/500/5000 原图均可到达。
- 预分配、持久化、任务内唯一的受控种子；重试复用原种子和新 ATTEMPT；支持多个标准采样器。
- SQLite（WAL/FULL/外键/quick_check）、一致性在线备份、JOB/正式冻结检查点、滚动保留、恢复流程。
- JOB/SRC/BLOCK/DRAW/ATTEMPT/OUT/EXPORT 追踪；无覆盖发布；完整清单/终态收据；部分输出只作证据。
- 安全暂停与立即暂停；不能验证定向取消时明确降级为“等待本次结束”，绝不全局中断。
- 启动恢复：本地收据+结果文件+远端 queue/history 核验；UNKNOWN 必须人工逐项处理。
- 存储清理：已确认筛选组内、未入选且未被导出的完整结果可预览后永久清理；入选/导出引用/原图/快照/清单/收据/DB/备份受保护；独立留存状态使“有意删除”不伪装为 MISSING/CORRUPT；清理日志在 DB 外持久化，恢复旧数据库后仍能区分有意清理与意外缺图。
- ComfyUI 侧栏任务中心：准确统计、分页缩略图、连接状态、骨架屏、有界通知、320—980 px 容器响应布局、深浅主题。
- 四步创建向导（工作流 → 原图 → 参数 → 检查创建）：真实上传/队列/失败重试、引用目录扫描、旧预检响应隔离、同 key/同载荷创建幂等。
- 对照筛选：并排默认，可切换滑动分割线；比例不同自动回退；方向键/空格/右键；共享入选控制器与 409 版本冲突回读。
- 连续筛选：按原图组在同一工作区预览、多选、保留或跳过并进入下一组；支持返回上一组、跨原图页/结果页、完成页。筛选会话、当前位置、结果页 offset 与确认边界持久在 SQLite，**完整关闭并重启 ComfyUI、清空站点数据或换浏览器后仍可“继续筛选”**；组确认与继续边界在同一事务，响应丢失有动作回执幂等。
- “查看结果→收起结果”在行级结果容器上完整收束（缩略图、网格、分页栏、错误、空态、骨架屏）；轮询/迟到响应不会重新展开已收起行。
- 临时/正式导出，平铺/按原图分组，冻结清单、逐文件校验、部分失败恢复、零图片/截止版本二次确认、预览过期 409 刷新。

## 安装

要求：ComfyUI 已自带 Pillow、aiohttp；本插件不新增第三方运行依赖，不升级 torch/CUDA/ComfyUI。

```bash
# 用 ComfyUI 自己的 Python 运行（示例）
python tools/install_plugin.py --comfy-root D:\ComfyUI

# 不支持 junction 时显式复制
python tools/install_plugin.py --comfy-root D:\ComfyUI --mode copy

# 更新已由本插件复制安装的代码：先暂存并逐文件校验，旧代码移到 user/gacha_master/install-backups
python tools/install_plugin.py --comfy-root D:\ComfyUI --mode copy --update

# 只核对源码与安装代码指纹
python tools/install_plugin.py --comfy-root D:\ComfyUI --verify

# 卸载（默认保留全部任务数据/结果/导出/备份）
python tools/install_plugin.py --comfy-root D:\ComfyUI --uninstall
```

安装目标：`<COMFY_ROOT>/custom_nodes/ComfyUI-Gacha-Master`。重启 ComfyUI 后打开侧栏“抽卡大师”。

已用 copy 模式安装到隔离测试实例 `D:\Projects\ComfyUI-acceptance\ComfyUI\custom_nodes\ComfyUI-Gacha-Master`，并完成真实加载/路由/L1/3×5；用户生产 ComfyUI 仍未安装（本机原本没有）。实际路径/版本/日志见 ENVIRONMENT.md。

## 使用流程

1. 在 ComfyUI 打开一个图生图工作流，确认包含标准 `LoadImage`、`SaveImage` 和 `KSampler.seed` 或 `KSamplerAdvanced.noise_seed`（直接数值，不接种子生成节点）。
2. 侧栏“新建任务” → 第 1 步捕获当前画布并选择输入/输出节点 → 第 2 步添加原图（拖拽/点击，或引用模式的后端目录）→ 第 3 步设置次数 → 第 4 步运行预检并创建。
3. 创建只进入任务详情，需要显式点击“开始 / 继续”；重连、切页和打开恢复面板都不会自动启动任务。
4. 任务运行时可“安全暂停”（完成当前项）或“立即暂停”（未验证定向取消时安全降级为等待当前项）。
5. 点击结果缩略图进入**连续筛选工作区**并定位到该结果；工作区内默认并排，可切换“滑动对比”；方向键切换、空格/按钮/右键入选；比例不同时自动提示返回并排。需要旧的单结果对照窗口时，点缩略图下方的“对照”按钮，或 Alt/Ctrl+点击缩略图。
6. “追加抽卡”选择优先或末尾；编号按已分配最大值继续。
7. “导出”依次选择类型/布局 → 审核预览 → 冻结确认 → 复制结果；正式导出需逐原图确认入选或明确跳过；有未完成 DRAW 时必须确认“只导出截止版本”；预览过期 409 需刷新并重新审核。
8. 重启 ComfyUI 后任务不会自动继续；在恢复面板逐项处理 UNKNOWN，再显式开始。
9. 结果多时使用“连续筛选”：同一工作区显示一张原图和本组全部结果，缩略图只切换预览，星标/当前结果按钮切换入选（同组可保留多张）；“保留并下一张”确认整组已保存的入选后进入下一张原图，“跳过并下一张”只在本组没有入选时可用。“上一张原图”可回到本轮上一组复查。
10. “查看结果 → 收起结果”会移除该行全部结果子区域（缩略图、分页栏、错误、骨架屏和占位）；收起后的轮询或迟到响应不会再次展开。

### 连续筛选与重启续筛

- 任务详情“原图与结果”标题旁显示“连续筛选”；已有未完成会话时显示“继续筛选 · SRC-xxxx”。从某一行点“筛选本组”可从该组开始。
- 筛选会话、当前原图/结果/结果页、筛选范围、会话版本和最近确认边界保存在后端 SQLite；浏览器 `localStorage` 不是进度来源。清空站点数据、换浏览器或完整关闭 ComfyUI 后，重新打开任务仍从服务器断点继续。
- 组内勾选立即保存单张入选；点击“保留并下一张/跳过并下一张”时，前端会等待本组所有入选保存完成，核对服务端整组统计与三个版本（结果/选择/计划），再在**同一事务**中写组审核和下一组继续边界。服务端未确认前不会显示“已保存”。
- 保存失败或版本冲突时停留在当前组并显示“读取最新进度/重试保存进度”；不会自动重发旧决定、不会自动取消入选、不会删除文件。
- 退出筛选只结束界面；未确认的当前组不会被当成已确认。若本组已确认但下一组尚未加载，重启后从下一组边界继续；若确认事务在提交前进程退出，审核与边界都不前进。
- 回到一个已确认组复查后再次确认属于同一组，已确认组数不会重复增加；结果或计划版本变化会使该组重新进入待复筛。
- 连续筛选用的是独立的组筛选会话，不改变正式导出的逐源审核、冻结和截止版本规则。

## 数据位置

真实安装时（ComfyUI 用户目录/输出目录存在）：

```text
<COMFY_USER>/gacha_master/          # 配置、SQLite、快照、收据、日志、备份
<COMFY_OUTPUT>/gacha_master/Jobs/   # 结果 OUT 与任务清单
<COMFY_OUTPUT>/gacha_master/Exports/# 默认导出根（可在设置中登记其他根）
```

未安装 ComfyUI 时默认退回 `~/.comfyui-gacha-master/`（仅独立运行/测试用途）。卸载默认不删除以上数据。

## 可靠性规则（摘要）

- 原图只读；已发布 OUT/导出绝不覆盖。
- 一个 DRAW 最多确认成功一次；重试保留种子和 DRAW，新增 ATTEMPT。
- 提交结果未知时不自动重 POST；保持 RECOVERY_REQUIRED / UNKNOWN。
- 文件存在、HTTP 200、进度 100% 都不单独等同于成功；必须终态证据 + 完整清单 + 全部文件校验 + DB 事务。
- 插件只操作自己的任务；不清空全局队列，不调用全局 `/interrupt`。
- 重启先核验、后由用户确认；关闭浏览器不影响后端继续运行。

## 兼容性与已知限制

- 代码与接口按 ComfyUI `0.45.0`（源码 commit `4d64a8b`）核对；已在隔离 CPU 测试实例真实运行 1×1/3×5/555、取消、强杀恢复、导出、追加、安装/更新/回退与 Ctrl+C；另在独立 GPU 实例用 DreamShaper8_LCM 完成 1×1/3×5/555 与 1×101，**未在用户生产 ComfyUI 上验收**。
- 2026-11-15 真实宿主补验：HOST06 A—D（单源 101 OUT、多页、后页入选/导出、5000 源/10000 OUT 夹具、迟到响应）与 HOST13 UI01—UI06（视口/缩放/主题、键盘焦点、减少动态效果、原生文件选择、图片/断网失败、冻结/移除/追加）均 PASS；数据来源在每条证据中独立标注。
- 定向取消已在隔离 ComfyUI 0.45.0 实例对运行中和排队中任务验证：只处理本插件 prompt，外部哨兵任务不受影响。仓库默认 `targeted_cancel_verified=false`，安装后需按 [REAL-ACCEPTANCE-RUNBOOK.md](../docs/REAL-ACCEPTANCE-RUNBOOK.md) 在目标实例重新验证后由用户显式开启。
- 仅支持一个 `LoadImage`、一个最终 `SaveImage`；额外 `SaveImage` 保留原行为并警告；不支持子图绑定。
- 不支持动画/多帧图片、列表映射式多次输出调用、任意随机自定义节点。
- 不支持远程多用户签名/多租户；按本地单用户设计。
- **真实 ENOSPC、用户生产安装与 HOST14 仍未运行**；最新隔离证据不改变这三个结论。日常操作与异常处理见 [docs/OPERATIONS.md](../docs/OPERATIONS.md)。
- 完整清单见 [KNOWN-ISSUES.md](../docs/KNOWN-ISSUES.md)。

## 测试与文档

- [用户实操反馈：连续筛选、重启续筛与收起修复规格（2026-11-16；仅规格，待实施）](../RESULT-REVIEW-UX-SPEC-20261116.md)
- [正式投用开发与验收实施文档（2026-11-15，最新执行入口）](../PRODUCTION-USE-IMPLEMENTATION-20261115.md)
- [上一阶段开发计划（2026-11-15，实施结果见完成总报告）](../NEXT-DEVELOPMENT-PLAN-20261115.md)
- 2026-11-15 下一阶段验收总报告（本地证据：`test-artifacts/next-20261115/FINAL-REPORT.md`）
- [日常操作与异常处理](../docs/OPERATIONS.md)
- 正式使用前修复最终验收报告（2026-11-14/15，历史）（本地证据：`test-artifacts/final-20261114/REPORT.md`）
- [正式使用前开发与验收规格（2026-11-14）](../PRODUCTION-READINESS-DEVELOPMENT.md)
- 修复前投入使用验收报告（2026-11-14，历史问题基线）（本地证据：`test-artifacts/readiness-audit-20261114/REPORT.md`）
- [UI 组件选型与界面改造开发规格](../UI-DEVELOPMENT.md)
- [环境事实](../docs/ENVIRONMENT.md)
- [架构与不变量](../docs/ARCHITECTURE.md)
- [开发状态](../docs/DEVELOPMENT-STATUS.md)
- [交付报告（状态、证据、生产阻塞）](../DELIVERY.md)
- [生产验收执行器](../tools/production_acceptance.py)
- [证据一致性检查器](../tools/check_release_evidence.py)、[候选清单/解包重建](../tools/candidate_manifest.py)
- [受控卷工具（写入保护/清理）](../tools/storage_acceptance.py)、[DISK 阶段同步执行器](../tools/storage_scenarios.py)、[目标资料校验](../tools/target_environment.py)
- [生产 GPU 验收授权范围](../docs/PRODUCTION-AUTHORIZATION.md)
- [测试报告（Q01—Q35 / F01—F12）](../docs/TEST-REPORT.md)
- [已知问题](../docs/KNOWN-ISSUES.md)
- [API 与配置](../docs/API.md)
- [需求追溯矩阵 R01—R16](../docs/TRACEABILITY.md)
- [示例工作流](../examples/README.md)
- [真实 ComfyUI 验收运行手册](../docs/REAL-ACCEPTANCE-RUNBOOK.md)
- UI 改造截图与逐项边界：test-artifacts/ui-refresh/SUMMARY.md（本地证据：`test-artifacts/ui-refresh/SUMMARY.md`）
- 测试证据：`test-artifacts/l0-full.txt`、`test-artifacts/js-tests.txt`、`test-artifacts/ui-export.png`、`test-artifacts/ui-compare.png`、`test-artifacts/ui-background.png`

## 许可证

本仓库尚未选择公开发布许可证；不应假定已获授权再分发。第三方组件说明见 [LICENSE-NOTICE.md](../LICENSE-NOTICE.md)。
