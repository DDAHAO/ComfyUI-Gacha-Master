# ComfyUI 抽卡大师：正式投用开发与验收实施文档

编写日期：2026-11-15（Asia/Shanghai）
适用工程：`D:\Projects\ComfyUI Gacha Master`
文档性质：**下一轮实施规格，可交给开发与验收执行者使用。本文所列新增开发、故障实测、生产安装和试用均尚未执行。**
当前产品结论：**PARTIAL，尚不具备“用户目标环境正式可用”的验收结论。**

本文根据最新完成记录安排正式投用所需的剩余工作。原 PR、NP、HOST、DISK、G 编号继续用于需求追溯；本文 PU01—PU06 是本轮执行工作包。原数据不变量与验收要求继续有效。

> **执行状态（2026-11-15，run `20261115T113000-pu01-pu02`）：**
> - **PU01 已完成并验证**：证据入口勘误（ERRATA（本地证据：`test-artifacts/next-20261115/ERRATA.md`））、统一路径基准与引用检查器、交付包 + 完整源码包、空目录解包逐文件重建、从解包交付目录安装/更新/verify、从解包源码快照重跑回归。
> - **PU02 已完成并验证**：写入入口自带保护、真实路径/卷身份、独占目录与无覆盖创建、写入预算、卷外记录、强退清单恢复、精确清理、reserve 配置还原；阶段同步执行器与 DISK01—05 场景矩阵（12 组协议自检，`simulated=true`）。
> - **PU03 真实 ENOSPC、PU04 用户实际工作流、PU05 目标安装/回退、PU06 三个日常周期仍为 NOT_RUN**：本机非管理员、只有 C:（系统卷）与 D:（项目卷），无可丢弃受控卷；目标资料模板校验 50 项缺失。
> - 因此第 12 节门槛未满足，`production_ready=false`，结论继续 **PARTIAL**。本轮证据见 PU01—PU02 完成报告（本地证据：`test-artifacts/production-use-20261115/20261115T113000-pu01-pu02/FINAL-REPORT.md`） 与同目录 `SUMMARY.json`。下文保留原始规格与门槛，未因本轮执行而降低。

## 1. 最新完成记录与本次核对结论

### 1.1 依据与证据入口

| 材料 | 用途 |
| --- | --- |
| 2026-11-15 完成总报告（本地证据：`test-artifacts/next-20261115/FINAL-REPORT.md`） | 最近一次完成情况与放行门槛 |
| 最终机器可读汇总（本地证据：`test-artifacts/next-20261115/final-20261115/SUMMARY.json`） | 总体 PARTIAL、各包及 G 门槛结果 |
| 最终候选清单（本地证据：`test-artifacts/next-20261115/final-20261115/candidate/MANIFEST.json`） | 四类文件集合与逐文件 SHA-256 |
| 最终快照说明（本地证据：`test-artifacts/next-20261115/final-20261115/candidate/SNAPSHOT.json`） | 候选身份、交付包摘要与 Git 状态 |
| 最终回归记录（本地证据：`test-artifacts/next-20261115/final-20261115/regression/REGRESSION.json`） | Python / JS 实际发现及执行数量 |
| NP02 完成报告（本地证据：`test-artifacts/next-20261115/20261115T081500-np02/NP02-REPORT.md`） | 真实宿主多页、交互与三个 UI 缺陷修复 |
| NP03 记录（本地证据：`test-artifacts/next-20261115/20261115T090500-np03/NP03-REPORT.md`） | 真实写满未执行、受控卷条件与工具结果 |
| NP04 记录（本地证据：`test-artifacts/next-20261115/20261115T091000-np04/NP04-REPORT.md`） | 用户目标环境和工作流缺件 |
| NP05 记录（本地证据：`test-artifacts/next-20261115/20261115T091500-np05/NP05-REPORT.md`） | 目标安装、回退及试用未执行 |
| 最终清理记录（本地证据：`test-artifacts/next-20261115/final-20261115/cleanup.json`） | 上轮实例退出与数据保留情况 |

本次编写前已只读比较当前工作区与最终候选清单，`runtime`、`browser_assets`、`delivery`、`source` 四个 scope 的文件集合与内容全部一致。此检查证明候选身份一致，没有重新执行产品回归、GPU 推理、磁盘故障或生产验收。

### 1.2 已完成的基线

| 项目 | 最新记录 | 本轮处理 |
| --- | --- | --- |
| NP01 候选与验收工具 | PASS | 复用，补证据定位和完整源码保存要求 |
| NP02 / HOST06 A—D | PASS，隔离真实宿主 | 保留真实 GPU 1×101 与 5000 源 / 10000 OUT 夹具的来源区分 |
| HOST13 UI01—UI06 | PASS，隔离真实宿主 | 保留主题、缩放、键盘、原生文件选择、网络恢复、冻结后移除与追加的证据 |
| Python 回归 | 109 项：108 PASS + 1 SKIP | 历史基线；新候选报告实际数量与跳过原因 |
| JS 回归 | 37 PASS | 历史基线；改动后按影响复验 |
| 100000 DRAW 计划 | 既有分批创建与恢复验收通过 | 不增加“执行 100000 次 GPU 推理”的要求 |
| 10000 OUT / 60 分钟浏览器耐久 | 既有夹具证据通过 | 集合、池化、图片生命周期变化时重跑 |
| 真实 GPU 1×1 / 3×5 / 555 / 1×101 | 隔离 DreamShaper8_LCM 组合有 PASS 证据 | 按对应候选及时间使用，不转记为目标工作流通过 |
| NP03 实际写满 | DISK01—05 全部 NOT_RUN | 本轮必须补齐 |
| NP04 / HOST14 | NOT_RUN | 必须提供并验证用户实际组合 |
| NP05 目标交付 | NOT_RUN | 必须完成目标安装、回退和三个独立日常周期 |

555 等历史运行与最终候选之间的代码差异仍须保留说明；不能因运行时部分相同，就写成“最终候选完整重跑全部历史场景”。

### 1.3 历史候选身份

| 项目 | 2026-11-15 最终记录 |
| --- | --- |
| Git HEAD | `8e388a5d1064b0597c550114fe1373c93bd731f7`，含大量未提交及未跟踪文件 |
| 候选指纹 | `570cc39e58e1942d762a144e5a13fc092567ace401879011e504d318d4c03e19` |
| 运行时 | 55 文件，`821edc199243b0946119918f1d46f559b6bf2c014713fd8677344f48eafabf88` |
| 浏览器资产 | 27 文件，`973713b732435e6351cc55ae751dc5992c1c214fa6db58d3225ad8f99b1c08f4` |
| 历史交付包 SHA-256 | `be91bfb5ae5872b8efd00ba15c1e2931294730f5fb74f5f70e375f8b7ab2a6fe` |

这些是历史值。加入本文、修正文档或修改工具都会改变 `source` / `delivery` 指纹；修改产品代码还会改变相应运行时指纹。实施时必须重新冻结候选，不把上述文件数、摘要或旧 PASS 写成新候选的实测结果。

### 1.4 源码阅读发现的执行缺口

以下是本次静态核对发现的事实与后续要求，尚未通过新增测试验证修复。

| 位置 | 当前情况 | 本轮要求 |
| --- | --- | --- |
| 完成总报告入口 | 根目录 `SUMMARY.json` / `cleanup.json` 链接不存在；实际文件在 `final-20261115/` 内 | 修正文档入口；为 JSON 内路径定义统一解析基准并逐项检查 |
| `DELIVERY.md` 顶部 | 最终指纹仍写 `78dd6947…`；最终 MANIFEST / SUMMARY 为 `570cc39e…` | 以机器清单为准，记录勘误；避免文档和自身包摘要相互引用造成反复漂移 |
| [候选工具](tools/candidate_manifest.py) | 交付 ZIP 排除 `tests/`；快照中的补丁和未跟踪文件名清单不能单独还原未跟踪测试内容 | 额外保留完整源码/测试快照，或使用包含全部相关文件的已保存版本 |
| [存储工具](tools/storage_acceptance.py) | `preflight()` 拒绝系统/项目卷，但直接 `fill()` 未复用该拒绝逻辑 | 写入入口必须重新检查，不能依赖调用者先执行预检 |
| 同一存储工具 | `_final_path()` 已实现，但 `fill()` / `cleanup()` 的包含关系主要使用词法路径；已有 run-dir 可复用并以 `wb` 打开填充文件 | 补最终路径/实际卷校验、独占运行目录、无覆盖创建及清理所有权约束 |
| 同一存储工具 | `fill` 默认保留 64 MiB；达到 headroom 会返回 `FILLED`，不代表目标业务写入遇到 ENOSPC | 增加阶段同步与目标写入失败证据，不能用填充成功关闭 DISK 用例 |
| [故障点](comfyui_gacha_master/backend/util.py) | 现有 `fault_point()` 用于硬退出，不提供“等待填充后继续写入”的同步屏障 | 在测试工具或隔离构建中补屏障，不能仅用固定延时猜测窗口 |

## 2. 正式投用的目标与范围

最终应能在一个**明确记录并通过验收的 Windows 本地单用户目标组合**中，完成日常批量创建、推理、筛选、追加、导出、停启和异常恢复，并能核对安装版本及执行结果。

当前可继承的兼容基线为 Windows 11、ComfyUI 0.45.0、前端 1.58.5。隔离 GPU 证据使用 Python 3.12.10、torch 2.9.1+cu126、RTX 3070 Laptop 8 GiB、DreamShaper8_LCM、512×512 / 4 steps。用户目标的模型、参数、节点及版本必须另行记录和验证。

保留以下规则，任何修复都不得降低它们：

1. 原图只读；冻结工作流只修改执行副本；既有 OUT、收据与导出文件不覆盖。
2. DRAW 和种子先持久化；人工重试沿用 DRAW / seed，新增 ATTEMPT；每个 DRAW 最多接受一个成功 ATTEMPT，其完整 OUT 集合作为结果。
3. UNKNOWN 不自动重提；重启不自动继续创建或推理；连网恢复和打开侧栏不自动开始。
4. 只有完整终态证据、文件/清单核验与数据库提交一致，才能确认成功。
5. 创建与浏览资源保持有界；不能用加载全部结果解决分页或选择一致性问题。
6. 导出按服务端冻结成员复制；入选变化、移除、追加不改变已冻结批次。
7. 只管理本插件 prompt；不得使用全局中断或清空外部任务队列来实现暂停。
8. 代码回退与数据库回退分别处理；不通过删除数据、跳过 schema 检查或手工改状态取得 PASS。

本轮不扩展到多租户、远程鉴权、跨平台、任意 ComfyUI 版本、子图绑定或任意自定义保存/随机节点。公开分发许可证另行处理，不影响已授权范围内的本地开发验收。

## 3. 工作包、依赖与可立即推进的工作

| ID | 工作包 | 优先级 | 依赖 | 完成标志 |
| --- | --- | --- | --- | --- |
| PU01 | 证据勘误、候选冻结与可重建交付 | P1，首先推进 | 当前工作区和历史证据 | 引用可解析，候选与完整源码可重建，差异及复验范围明确 |
| PU02 | 存储工具保护与阶段同步 | P0，真实写满前必需 | PU01；工具负向测试可先做 | 直接调用不能绕过保护，强退可清理，阶段可确定复现 |
| PU03 | DISK01—05 真实 ENOSPC 与恢复 | P0，可靠性放行必需 | PU02、受控 NTFS 卷 | 五类位置真实撞到空间不足，前后数据与恢复断言通过 |
| PU04 | 实际工作流与目标组合验收 | P0，目标使用放行必需 | PU01、目标资料；可与 PU03 准备并行推进 | 目标组合隔离预验通过，目标安装后补实际 HOST14 与执行证据 |
| PU05 | 目标安装、迁移和回退 | P0，交付放行必需 | PU03、PU04 隔离预验、明确的生产操作范围 | 安装/实际加载一致，历史数据完整，回退及恢复边界验证通过 |
| PU06 | 三轮日常闭环、最终文档与放行 | P1 | PU05、PU04 目标实测 | 三轮通过，G01—G08 满足，支持声明与证据一致 |

实施主线：PU01 → PU02 → PU03；目标资料到位后推进 PU04 隔离预验；二者满足后执行 PU05 → PU04 目标补验 → PU06。

目标资料或受控卷暂缺时，先完成 PU01、PU02 和可独立完成的准备工作。按缺件记录真实用例 NOT_RUN；不得通过把隔离实例改称生产实例、降低负载或改写结论解除阻塞。

## 4. PU01：证据、候选与交付可复现

### 4.1 证据定位与状态勘误

1. 修正第 1.4 节列出的入口错误；历史候选包和原始日志保持原字节，必要时追加勘误说明。
2. 新汇总内所有路径采用仓库相对路径，或声明唯一 `evidence_root` 并以其为基准。不能让同一字段有时相对包目录、有时相对仓库。
3. 统一 README、DELIVERY、DEVELOPMENT-STATUS、TEST-REPORT、KNOWN-ISSUES、ENVIRONMENT、TRACEABILITY、REAL-ACCEPTANCE-RUNBOOK 和 OPERATIONS 的最新状态。
4. 原 NEXT 计划的“待实施”和原规格的历史门槛标注时间；当前状态指向最新有效汇总。历史 FAIL / INVALIDATED / NOT_RUN 继续保留。
5. 建议新增 `tools/check_release_evidence.py`，检查必要文件存在、JSON 可读、引用可解析、状态枚举、候选身份与门槛一致。此脚本**尚不存在，属于本轮开发任务**。

检查器至少拒绝：指向不存在的汇总、被改动的交付包、错误的运行时摘要、测试数为零、失败用例却声称整体 PASS、真实用例引用 fixture、必要门槛未通过却设置 `production_ready=true`。

### 4.2 冻结与重建

- 保存 HEAD、已修改文件、未跟踪源码/工具/测试的实际内容；不得只使用 `git archive HEAD` 打包当前产品。
- 生成 runtime、browser_assets、delivery、source 四个清单；另存完整源码/测试快照及 SHA-256。运行依赖、缓存、模型、用户数据和证据不打入源码包。
- 在新目录解包并核对文件集合；从完整源码快照运行回归，从交付目录运行安装器。交付 ZIP 不含测试，不能用其“发现零测试”当作回归通过。
- 最终安装使用冻结交付目录的 copy 模式。安装器对比的源码根应是该交付目录，避免与持续变化的开发工作区比较。
- 交付内文档引用包外 MANIFEST / SNAPSHOT；最终包摘要放在包外汇总或校验文件，避免把包自身摘要写进其包含的文档后继续改包。
- 冻结前后各做一次精确比较；测试工具变化也会改变 source scope，必须记录。

完整源码快照与交付目录的 runtime / browser_assets / delivery 应一致；source scope 因交付包不含测试而不同。验收器的 `--source-root` 指向已校验的完整源码快照，以保持总候选身份一致；安装器从冻结交付目录运行。不能把两种目录生成的顶层 fingerprint 混为同一个值。

### 4.3 差异与复验规则

| 变化 | 至少需要的复验 |
| --- | --- |
| 仅文档/引用 | 链接、状态、路径与包清单；重建交付摘要；运行时相同时可引用已有产品证据 |
| 候选/安装/验收工具 | 对应工具测试、负向用例、新目录重建、隔离安装 verify；已有产品证据按 scope 说明复用 |
| 保存/DB/调度/恢复/备份 | 全量 Python / JS 回归；受影响 DISK / 崩溃恢复；最终候选真实 1×1、3×5 和受影响规模 |
| 前端选择/导出/分页 | 对应 JS、真实宿主操作、后页导出；改变集合/图片生命周期时重跑 10000 OUT / 60 分钟耐久 |
| 目标模型/节点/参数或实例版本 | 重新预检与实际目标 1×1、3×5、代表性规模；不能沿用其他组合 PASS |

交付：候选四类清单、不可变交付包、完整源码包、差异/复验说明、引用检查结果及真实回归日志。计数来自本轮实际执行；既有 Windows symlink 跳过需保留原因，不能记成 PASS。

## 5. PU02：先补齐存储测试工具与阶段屏障

### 5.1 存储工具必须完成的开发

主要修改 [storage_acceptance.py](tools/storage_acceptance.py)，测试放入 [test_acceptance_tools.py](tests/unit/test_acceptance_tools.py) 或专门的工具测试文件。

| 要求 | 实现及验收要点 |
| --- | --- |
| 写入入口自带保护 | `fill` 自行执行卷和目录预检；系统卷、项目卷及未登记受控卷一律拒绝，不能通过跳过 `preflight` 绕过 |
| 以真实目标判断 | 同时核对最终绝对路径、卷标识、run-dir 与报告目录；拒绝使写入/清理跳到边界外的 junction / symlink / reparse point |
| 明确所有权 | 每轮新建独占目录和标记，拒绝非空或已存在的 run-dir；填充文件采用无覆盖创建，不能重写既有 filler 文件 |
| 有界写入 | 校验 headroom 非负、max-bytes 与 timeout 为正；每次分块都受剩余写入预算约束；记录实际写入和停止原因 |
| 卷外持久记录 | 控制器、日志、填充清单及最终报告均位于受控卷外；填充前先登记文件身份，正常结束和强退后都能定位半成品 |
| 可中止可恢复 | 提供卷外停止信号；超时/中断保留最后阶段和清单；控制器退出不能令填充进程无限继续 |
| 精确清理 | 重新核对实际卷、所有权和最终路径，只移除本轮登记且身份一致的 filler；不根据目录名递归清理；支持强退后的清单恢复 |
| 配置还原 | reserve 等测试参数有前值、测试值和恢复值；完成后证明恢复，不改变生产配置 |

负向验证至少覆盖：直接 fill 指向系统/项目卷、卷序列号不符、目录跳转、清理前换卷/换路径、复用 run-dir、预置同名 filler、非法/超限参数、控制器强退、日志写失败、清单不完整。保护测试使用模拟卷信息或微小临时文件，不写满系统/项目卷。

### 5.2 阶段同步执行器

新增测试执行器，建议位置 `tools/storage_scenarios.py`，配套场景与核验代码放在 `tests/host_acceptance/` 或工具专用目录。**名称是开发建议，当前没有可直接调用的实现或命令行。**

执行协议至少包含：

1. 以 run-id、用例 ID、目标路径、候选身份创建独立场景，保存原图/既有结果/库/备份摘要。
2. 业务进程在目标写入前报告 `ready`，并等待控制器释放；信号和日志写到卷外。
3. 控制器在校验后的受控卷上填充；确认真实剩余空间，然后释放目标业务写入。
4. 保存实际失败位置、系统异常或 SQLite 错误、提交次数、事务结果及进程状态。
5. 若操作在更早的预检/备份阶段失败，记录实际阶段，目标子项仍待测；调整隔离布局后重新执行。
6. 释放本轮填充文件，核对状态，再按恢复协议显式继续或人工处理 UNKNOWN。

同步钩子限定在测试执行器/隔离构建；不新增公开故障 API。不能以 `sleep(5)`、预先写满后无法进入目标流程、或 mock 抛错代替阶段实测。

交付：工具保护测试、屏障协议、单场景正常/失败/超时/强退示例、卷外记录与清理证明。PU02 通过前不执行现有工具的真实 fill。

## 6. PU03：真实 ENOSPC 五类验收

### 6.1 环境就绪条件

- 提供明确可丢弃、归本轮使用的 Windows NTFS 小容量卷或磁盘映像。不能写满 C: 系统卷、D: 项目卷或未说明用途的小卷。
- 记录卷序列号、最终挂载路径、容量、文件系统、空闲量和权限。管理员权限用于需要的创建/挂载；若已提供可写受控卷，不把管理员身份本身作为写入的唯一判断。
- 使用磁盘映像时，记录映像容量上限、宿主剩余空间及停止阈值，保证客体写满不会连带耗尽宿主卷。
- 只在隔离实例中调整 reserve；测试结束恢复原值。控制器、Python/ComfyUI 程序、模型、日志和报告位于卷外。
- 分别安排快照/检查点、结果、收据/DB、导出、备份的受控布局；避免一次填充先撞到无关写入，导致五项都只测到同一个预检。
- 每个故障子项独立复位，预先保留一组有效历史数据和最后有效备份，不能只对全新空库测试。

以上条件从执行当日探测取得。2026-11-15 的“非管理员、无受控卷”是历史状态，不作为永久结论。

### 6.2 必测矩阵

| 用例 | 必须实际触发的位置 | 最少通过断言 | 主要代码落点 |
| --- | --- | --- | --- |
| DISK01 | 创建检查点写入、原图快照复制；两处分别记录 | 原图/既有快照未变，半成品不进入 READY，无推理提交；恢复不重分配已提交 DRAW / seed | `backend/inputs.py`、`creation.py`、`service.py` |
| DISK02 | OUT 文件写入与完整清单发布；分别记录 | 半份文件不成为接受结果，不覆盖旧 OUT；证据不足保守阻断/UNKNOWN；恢复后同一 DRAW 只有一个接受 ATTEMPT | `backend/storage.py`、`scheduler.py`、`recovery.py` |
| DISK03 | 终态收据写入、SQLite 主库/WAL 的真实写入或提交失败；分别记录 | 严重错误停止新提交，不重复接受；重启不自动重提；已有有效库/备份可核验 | `backend/storage.py`、`db.py`、`repository.py`、`recovery.py` |
| DISK04 | 冻结批次中至少一项已成功后，后续导出复制失败 | 明确 PARTIAL_FAILED；原结果与已成功目标不变；resume 沿用冻结成员，不重新冻结、不覆盖冲突目标 | `backend/exports.py` |
| DISK05 | 新备份写入/校验/发布及轮转前后关键位置 | 半份备份不登记 VERIFIED，最后有效备份不被删除；恢复空间后可新建并独立恢复核验 | `backend/backups.py`、`db.py` |

“DISK01—05 五类 PASS”必须有内部子项明细；不能用其中一个写入失败替代同类其余关键位置。SQLite 记录实际主错误/扩展错误及事务行为，不预设一定返回某个错误码。

### 6.3 什么才算真实写满通过

- `FILLED`、`ENOSPC_READY`、reserve 拒绝、ACL 拒写、卷失联、模拟 ENOSPC 都只能作为各自证据。
- 每个目标子项必须证明**业务目标写入**实际因空间不足失败，且对应不变量成立；仅 filler 自己遇到 ENOSPC 不算通过。
- 空间释放后完成恢复验证：原图、已发布 OUT、接受关系、种子、冻结导出成员、最后有效备份均符合预期。
- 已确认成功 DRAW 不重执行；UNKNOWN 由显式决策处理。对错误恢复的验证同时记录提交次数，不能只看最终任务变绿。
- 每项保存 BEFORE / FAILURE / AFTER 三组证据，包含卷容量、实际路径、配置、异常、SQL 检查、哈希和操作时间。

清理只处理本轮 filler 和临时故障配置。保留失败日志、数据库副本、收据和结果证据；结束后复核绝对目标及卷标识，记录空间恢复和残留进程。

## 7. PU04：用户实际工作流与目标组合

### 7.1 一次性收集的目标资料

| 类别 | 必填内容 |
| --- | --- |
| 实例 | ComfyUI 根目录、实际 Python、启动命令/工作目录、URL、有效 base-directory、用户/input/output/custom_nodes 路径 |
| 环境 | OS、ComfyUI commit/版本、前端版本、Python/torch/CUDA/驱动、GPU/显存、相关自定义节点版本 |
| 工作流 | 实际 UI JSON 与 API JSON；LoadImage / 最终 SaveImage 节点 ID；所有受控采样器种子节点 |
| 模型与原图 | 实际模型位置和 SHA-256；代表性原图副本和前后哈希；快照/引用模式 |
| 负载 | 常用分辨率、步数、batch、原图数×次数、每 DRAW 预期 OUT 数、导出布局、可接受时间与磁盘预算 |
| 数据与安装 | data_root、results_root、export roots、schema、当前插件来源/安装方式、历史数据与可验证备份 |
| 执行范围 | 可使用的隔离目录/端口、现有队列、维护窗口、允许的生产安装/启动/任务操作 |

保存为 `environment/TARGET-ENVIRONMENT.json`，缺项逐条列出。从给出的实例配置和路径读取资料；不把历史 CPU / GPU 验收根默认当作生产实例，不进行无边界磁盘扫描。

### 7.2 验证顺序与负载

1. 静态核对工作流、节点绑定、模型和路径，保存实际参数。目标尚未安装插件时，不能假设目标已有插件 API。
2. 复刻目标版本/节点/模型到隔离实例，安装冻结候选；单独使用数据根、结果根与原图副本。
3. 在该隔离组合执行预检与 dry-run；显式记录 `environment=isolated`、`host=real` 及真实数据来源。
4. 按 **1×1 → 3×5 → 一轮事先记录的日常代表性规模** 执行；低步数/低分辨率 smoke 另记，不能替代实际参数验收。
5. 在实际前端完成适用 HOST 矩阵，核对加载、创建、分页/选择、对照、追加、暂停继续、导出、重连与 HOST14 实际工作流连续运行。
6. PU05 目标安装后，在实际目标重跑 dry-run、1×1、3×5、代表性闭环及适用 HOST 操作，标记 `environment=target`。

如果日常规模或支持声明包含 555，就执行目标工作流 111×5，并核验完整结果。否则在执行前固定代表性规模和理由，保留隔离 555 的环境边界；不能运行后把 1×1 临时命名为代表性规模。

一 DRAW 多 OUT 的工作流必须在场景文件填写实际 `outputs_per_draw`。直接使用 `--scenarios NxM` 默认按每 DRAW 一个 OUT 核验；不能因总数不匹配而把合法多 OUT 当成产品失败，也不能跳过逐项身份检查。

### 7.3 独立核验与闭环

每组运行至少保存并核验：

- 冻结 UI/API 工作流与实际执行副本；只改绑定输入、指定输出和受控种子，编辑器原图不被改写。
- 源图顺序、每源 DRAW 编号、全部受控 sampler 的 seed 映射、ATTEMPT / prompt 身份、完整收据/清单和接受关系。
- 每个接受 OUT 的身份、字节数、SHA-256、PNG 解码/元数据；前后原图哈希一致。
- QUICK / FINAL × FLAT / GROUPED 四组合各至少一个有效批次；成员、manifest、DB 和目标文件一致。
- 后页入选、刷新/重连、后页追加、冻结后变更的语义；若日常工作流没有足够结果跨页，可用独立真实 1×101 补宿主分页，不冒充日常模型负载。
- 浏览器关闭后任务按授权继续，服务重启后不自动开始；UNKNOWN 决策在隔离副本验证。

独立核验可以复用 [verify_host06.py](tools/host_acceptance/verify_host06.py) 的方法，但必须覆盖本场景；只调用产品自身同一个校验函数不能构成独立证据。

超出现有支持范围的子图、自定义种子/保存节点或额外输出行为，记录具体不兼容点并停止对应场景。未经范围确认，不重写用户工作流或升级生产 ComfyUI / torch 来取得 PASS。

定向取消默认保持 `targeted_cancel_verified=false`。需要立即取消的目标实例，须按 [运行手册](docs/REAL-ACCEPTANCE-RUNBOOK.md) 验证 running / pending 两类取消和外部哨兵；通过后才开启。使用安全暂停的组合可以保留降级能力，并在支持声明中写明。

## 8. PU05：目标安装、旧数据与回退

生产操作按会话中明确的范围执行。实施前先完成只读检查、具体安装差异、可验证备份和回退方案；已有授权覆盖后继续执行，不重复索要同一权限。本文编写本身不表示生产安装已执行。

### 8.1 进入目标维护窗口前

1. PU03 真实存储可靠性和 PU04 目标组合隔离预验通过，冻结最终候选；修改产品后按第 4.3 节重新验证。
2. 核对目标队列、单在途槽、UNKNOWN、创建工作器和导出复制；安排停止提交并正常停服。
3. 保存一致性 DB 备份并做独立完整性/外键/schema 检查；在线使用 SQLite backup 能力，停服后再保留原库目录及残留 WAL/SHM。不能只复制运行中的主库文件作为唯一备份。
4. 保存配置、代码、工作流和数据摘要，核对快照/结果/导出实际位置与卷。DB 备份不等于结果与原图快照的备份。
5. 在隔离副本演练旧 v1→当前 schema、部分 CREATING、UNKNOWN、选择状态、冻结导出和有效备份的保留与恢复；不在生产构造强杀或写满故障。

### 8.2 安装与实际加载

- 使用冻结包 copy 安装；已有受本插件管理的副本才使用 `--update`，未知同名目录保持现场并报告所有权问题。
- 核对安装器预测目的地与启动命令的实际 custom_nodes 路径一致；使用 `--base-directory` 时尤其要检查，不能仅从 ComfyUI 源码根推断加载路径。
- 安装备份位于 custom_nodes 外，避免第二个插件入口；如果已有 link 安装，先查清所有权与数据位置，再制定 copy 切换步骤。
- `--verify` 同时检查交付文件与精确运行时集合：缺失、多余、变更文件均需解释并处理。文件数相同不能代替哈希与集合比较。
- 重启后保存真实加载路径、唯一插件入口、GachaSaveImage 注册、health/capabilities、前端资产与控制台证据；排查浏览器缓存旧 JS 的混装情况。
- 启动后核对历史实体、接受关系、选择、冻结成员、文件哈希及备份，确认没有自动创建/推理，再进入目标工作流补验。

### 8.3 回退及恢复的通过标准

| 场景 | 必须验证的行为 |
| --- | --- |
| 安装切换失败 | 旧代码可恢复，原数据不变，无额外入口 |
| 代码回退且 schema 兼容 | 停服切回已验证代码，启动后旧数据和文件身份一致 |
| 旧代码不支持新 schema | 明确拒绝启动/写入；不能手改 schema 版本或删除迁移记录 |
| 数据库回退 | 保留升级后 DB/结果/导出，使用升级前一致性备份；记录任务、选择、冻结批次恢复到哪个时间点 |
| 恢复到新候选 | 重新校验安装和数据，新增文件不被删除；备份时间点以后的文件/记录单列核对，不自动接受孤立文件 |
| 卸载保留规则 | 在目标配置隔离副本核验默认保留 data_root、结果、导出、备份；不要求为测试临时卸载生产插件 |

记录回退和恢复的实际耗时、恢复点、未恢复内容与操作日志。PU05 需要可验证演练；只写“切回旧代码即可”不算完成。破坏性演练在目标配置副本执行，生产实例只执行维护范围内的必要操作，证据分别标记环境。

## 9. PU06：三个日常周期与最终交付

每周期均包含实际工作流的创建 → 执行 → 筛选 → 导出，使用事先记录的日常参数和负载。三个周期独立创建 JOB，分开保存日志与核验结果。

| 周期 | 特别覆盖 | 必须记录 |
| --- | --- | --- |
| CYCLE01 | 正常完整闭环、四组合导出 | 负载/参数、执行与导出耗时、接受数、文件校验、磁盘增长、错误 |
| CYCLE02 | 关闭浏览器后返回、后页选择与追加 | 后台持续执行、重新同步、星标与冻结批次一致、无旧意图补写 |
| CYCLE03 | 正常服务重启后显式恢复、备份核验 | 停服/启动日志、无自动开始、历史实体与文件一致、显式继续与最终导出 |

三个周期是短期日常闭环证据，不等于长期稳定性证明。性能阈值使用目标资料中事先确定的可接受时间、磁盘预算和已有资源上限；不能在失败后放宽门槛。

发生原图变化、错绑、重复接受、UNKNOWN 自动重提、导出漏成员/覆盖、库损坏或不可恢复写失败时，立即停止放量并保留现场。先修复和复验，再重新完成受影响周期；原失败报告保留。

交付文档至少说明：支持的确切组合、安装与验证入口、创建/开始的区别、后页选择、409、断网重连、UNKNOWN 决策、导出续传、磁盘不足恢复、备份与回退时间边界。复用并更新 [OPERATIONS.md](docs/OPERATIONS.md)，不再建立内容重复且状态不一致的第二套用户手册。

结束时确认目标队列、单在途槽、工作器、导出状态及磁盘余量；隔离实例正常停止，保存清理记录。生产实例是否保持运行按维护范围及用户日常需求记录。

## 10. 现有工具的 PowerShell 命令模板

以下模板经现有参数定义核对；本次撰写未执行模板中的回归、安装或推理。所有步骤使用新证据目录。新增的证据检查器和存储屏障执行器需要先开发，本文不提供假定已存在的调用命令。

### 10.1 初始化、候选快照与回归

```powershell
$Repo = 'D:\Projects\ComfyUI Gacha Master'
$DevPython = Join-Path $Repo '.venv\Scripts\python.exe'
$RunId = [DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(8)).ToString('yyyyMMddTHHmmss')
$Evidence = Join-Path $Repo "test-artifacts\production-use-20261115\$RunId"
if (Test-Path -LiteralPath $Evidence) { throw '证据目录已存在，请使用新 run-id。' }
New-Item -ItemType Directory -Path $Evidence -ErrorAction Stop | Out-Null

& $DevPython (Join-Path $Repo 'tools\candidate_manifest.py') --source-root $Repo --snapshot-dir (Join-Path $Evidence 'candidate')
if ($LASTEXITCODE -ne 0) { throw '候选冻结失败。' }

& $DevPython (Join-Path $Repo 'tools\run_regression.py') --root $Repo --python $DevPython --output-dir (Join-Path $Evidence 'regression')
if ($LASTEXITCODE -ne 0) { throw '回归失败，停止后续放行。' }

& $DevPython (Join-Path $Repo 'tools\candidate_manifest.py') --source-root $Repo --check (Join-Path $Evidence 'candidate\MANIFEST.json')
if ($LASTEXITCODE -ne 0) { throw '执行期间候选漂移，需重新冻结并评估复验。' }
```

现有快照命令只生成交付 ZIP；PU01 要求的完整源码/测试快照仍须另行保存并验证。回归记录以新 `REGRESSION.json` 为准，不能复制旧计数。

### 10.2 安装计划与目标核对

先根据 TARGET-ENVIRONMENT 填写真实绝对路径。`$ComfyRoot` 是安装器目标根，必须与实际加载路径核对。

```powershell
$DeliveryRoot = '<已核验的冻结交付目录>'
$FrozenSourceRoot = '<已核验且包含源码工具和测试的冻结源码目录>'
$ComfyRoot = '<目标或隔离ComfyUI根目录>'
$ComfyPython = '<该实例实际Python绝对路径>'
$InstalledPluginRoot = '<该实例实际加载的ComfyUI-Gacha-Master目录>'
foreach ($TaskPath in @($DeliveryRoot, $FrozenSourceRoot, $ComfyRoot, $ComfyPython)) {
    if ($TaskPath.Contains('<') -or -not (Test-Path -LiteralPath $TaskPath)) {
        throw "请先填写并核验路径：$TaskPath"
    }
}

& $DevPython (Join-Path $Repo 'tools\candidate_manifest.py') --source-root $FrozenSourceRoot --check (Join-Path $Evidence 'candidate\MANIFEST.json')
if ($LASTEXITCODE -ne 0) { throw '完整源码快照与冻结候选不一致。' }

# 只输出计划，不执行安装；已有 copy 安装的更新计划再追加 --update。
& $ComfyPython (Join-Path $DeliveryRoot 'tools\install_plugin.py') --comfy-root $ComfyRoot --mode copy --dry-run |
    Out-File -LiteralPath (Join-Path $Evidence 'INSTALL-PLAN.json') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw '安装预检失败。' }

# 实际安装须满足 PU05 门槛；新装使用 copy，已归属本插件的 copy 更新才追加 --update。
# 安装完成后执行下列只读比较，并核对输出目的地等于实际加载目录。
& $ComfyPython (Join-Path $DeliveryRoot 'tools\install_plugin.py') --comfy-root $ComfyRoot --verify |
    Out-File -LiteralPath (Join-Path $Evidence 'INSTALL-VERIFY.json') -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw '安装文件核验失败。' }
```

实际新装命令是在已核准计划中去掉 `--dry-run`；更新按上述条件同时添加 `--update`。目标尚未安装时 `--verify` 失败是预期状态，不能当作已安装通过，也不应因此自动覆盖未知目录。

### 10.3 工作流 dry-run 与逐级真实执行

```powershell
$BaseUrl = '<该轮隔离或目标实例URL>'
$DataRoot = '<该实例插件data_root>'
$ResultsRoot = '<该实例插件results_root>'
$WorkflowApi = '<实际API工作流JSON绝对路径>'
$WorkflowUi = '<实际UI工作流JSON绝对路径>'
$InputRoot = '<代表性原图副本目录>'
$LoadNode = '<实际LoadImage节点ID>'
$SaveNode = '<实际最终SaveImage节点ID>'
$Environment = 'isolated'  # PU05 后在真实目标执行时，显式改为 target。

foreach ($TaskValue in @($BaseUrl, $DataRoot, $ResultsRoot, $WorkflowApi, $WorkflowUi, $InputRoot, $LoadNode, $SaveNode, $InstalledPluginRoot)) {
    if ($TaskValue.Contains('<')) { throw '必须先填写并核验所有实例与工作流参数。' }
}
$AcceptTool = Join-Path $FrozenSourceRoot 'tools\production_acceptance.py'
$Common = @(
    '--base-url', $BaseUrl, '--data-root', $DataRoot, '--results-root', $ResultsRoot,
    '--workflow-api', $WorkflowApi, '--workflow-ui', $WorkflowUi, '--input-root', $InputRoot,
    '--load-node', $LoadNode, '--save-node', $SaveNode, '--comfy-root', $ComfyRoot,
    '--source-root', $FrozenSourceRoot, '--installed-plugin-root', $InstalledPluginRoot,
    '--environment', $Environment, '--data-source', 'real_gpu', '--host', 'real'
)

& $DevPython $AcceptTool @Common --scenarios 1x1 --dry-run --output-dir (Join-Path $Evidence 'workflow\dry-run')
if ($LASTEXITCODE -ne 0) { throw '工作流预检失败。' }

# 以下开始真实创建和推理，仅在对应执行范围与资源窗口已明确后运行。
& $DevPython $AcceptTool @Common --scenarios 1x1 --output-dir (Join-Path $Evidence 'workflow\1x1')
if ($LASTEXITCODE -ne 0) { throw '1x1 失败，停止放量。' }
& $DevPython $AcceptTool @Common --scenarios 3x5 --output-dir (Join-Path $Evidence 'workflow\3x5')
if ($LASTEXITCODE -ne 0) { throw '3x5 失败，停止放量。' }
```

此示例适用每 DRAW 一个 OUT 的 GPU 工作流；CPU 实测改 `--data-source real_cpu`。多 OUT 时改用含相应 1×1、3×5 定义的 `--scenario-file`，并移除重复的 `--scenarios`。代表性规模另建场景文件，不能直接使用仓库示例 `representative-5x10` 作为用户负载结论。

`--scenario-file` 会载入文件内的全部场景；逐级放量应拆成分级文件或明确的分次运行。每次使用新输出目录。预检涉及模型外置目录时，额外核对实例实际模型搜索路径，不能仅依赖 `<COMFY_ROOT>/models/checkpoints` 的静态检查。

dry-run 不创建任务或推理；仍需保存前后 JOB / ATTEMPT / 队列摘要，不能仅因脚本退出 0 就认定无副作用。真实运行还需 PU04 所列独立核验和宿主操作。

### 10.4 受控卷只读状态与预检

```powershell
$ControlledRoot = '<已提供并确认归本轮的受控NTFS目录>'
if ($ControlledRoot.Contains('<') -or -not (Test-Path -LiteralPath $ControlledRoot)) {
    throw '尚无已核验的受控卷，真实 DISK 用例保持 NOT_RUN。'
}
& $DevPython (Join-Path $Repo 'tools\storage_acceptance.py') status --path $ControlledRoot
if ($LASTEXITCODE -ne 0) { throw '卷信息读取失败。' }
& $DevPython (Join-Path $Repo 'tools\storage_acceptance.py') preflight --path $ControlledRoot --report (Join-Path $Evidence 'STORAGE-PREFLIGHT.json')
if ($LASTEXITCODE -ne 0) { throw '受控卷预检未就绪。' }
```

`preflight` 会创建并删除一个微小写入探针。后续 fill / 屏障 / 恢复 / cleanup 必须使用完成 PU02 后的工具和场景方案；保留 64 MiB 的填充命令本身不能完成 PU03。

## 11. 证据目录与结果格式

```text
test-artifacts/production-use-20261115/<run-id>/
  environment/      # 实例、版本、目标负载、路径/卷/权限、操作范围
  candidate/        # 四类清单、交付包、完整源码/测试快照、差异及校验
  regression/       # 实际发现与执行数、原始日志、跳过原因
  storage/          # DISK01—05 各子项：before/failure/after、配置与清理
  workflow/         # 隔离和 target 分开；UI/API、各规模、独立核验
  host/             # HOST 子项、步骤、截图、请求/控制台日志、数据来源
  install/          # 预检、前后摘要、加载路径、备份、迁移、回退
  pilot/            # CYCLE01—03；真实负载、耗时、错误、导出与恢复
  SUMMARY.json
  FINAL-REPORT.md
  cleanup.json
```

最少结果字段：run-id、用例 ID、起止时间/时区、候选及 scope 摘要、环境身份、数据来源、命令/步骤、预期与实际、错误、证据路径、恢复/清理情况。新汇总的路径基准必须明确。

状态含义：PASS 表示本用例断言和证据齐全；FAIL 表示执行后断言失败；NOT_RUN 表示未执行；PARTIAL 表示只有部分子项完成；STALE 表示证据与当前候选/范围不匹配。外部缺件可另记录阻塞原因，不把未执行写成 FAIL 或 PASS。

最终汇总至少包含 `production_ready`、目标身份、四类候选摘要、回归计数、PU / HOST / DISK / CYCLE 子项、G01—G08、支持范围、未测范围、遗留问题和证据入口。失败与作废尝试独立保留，不能覆写成通过。

## 12. 最终放行标准

| 门槛 | 最新历史状态 | 本轮正式投用通过条件 |
| --- | --- | --- |
| G01 功能 | PASS，隔离基线 | 新候选相关回归通过，选择/分页/导出等核心流程无未解决阻断问题 |
| G02 创建 | PASS，隔离基线 | 有界分批创建与暂停/继续/重启语义保持，100000 计划证据可追溯 |
| G03 规模 | PASS，隔离基线 | 既有资源预算保持；发生相关生命周期变化时补齐耐久复验 |
| G04 真实宿主 | PARTIAL | 隔离已通过项按差异复用；目标适用 HOST 操作与 HOST14 有实际证据 |
| G05 真实执行 | PARTIAL | 目标实际参数 1×1、3×5、预先约定代表规模通过，全部接受结果及导出独立核验 |
| G06 可靠性 | PARTIAL | DISK01—05 子项、空间释放后恢复及相关强杀/UNKNOWN/备份证据齐全，无重复接受或自动重提 |
| G07 交付 | PARTIAL | 冻结包、实际加载、旧数据、迁移/回退及保留规则可核验；三个日常周期通过 |
| G08 文档 | 上轮 PASS | 新文档、机器汇总、支持声明、操作/恢复手册、引用及安装摘要一致 |

只有必要门槛全部满足，且不存在未解决的数据完整性/恢复阻断问题，才能设置 `production_ready=true` 并声明**已记录的目标组合**可正式投入使用。缺受控卷、实际工作流、目标安装或日常周期中的任何一项，最终结论继续 PARTIAL，并明确缺件与下一步。

最终报告必须把以下三件事分别写清：本轮开发完成了什么、哪些场景实际验证通过、允许声明支持哪个目标组合。不得把“文档写完”“工具通过”或“隔离实例通过”合并成“用户生产已通过”。

## 13. 执行交接要求

执行者先读本文和第 1.1 节最新证据，再检查当前 Git 状态与适用 AGENTS 指令。保留现有未提交改动，不重置工作区，不盲目重做已经通过的产品功能。

按 PU01—PU06 依赖推进；工具缺口先实现并验证，遇到真实产品缺陷时做局部修复并补有意义的回归。目标/卷条件缺失时完成其余独立工作，最后集中报告具体缺件，不自行假设生产路径或替换实际模型。

每个工作包提交结果、证据和遗留项；最终更新本轮 SUMMARY / FINAL-REPORT 及项目入口。产品未达成第 12 节条件时保留 PARTIAL，不以完成代码或编写报告作为正式放行。
