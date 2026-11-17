# ComfyUI Gacha Master V1.0 交付报告

日期：2026-11-12（基线）；2026-11-15（下一阶段验收与正式投用开发追加）
代码：基线提交 `744ff97`；2026-11-15 候选含未提交改动，运行时 55 文件 `821edc19…`。**候选身份只从本轮 `candidate/MANIFEST.json` / `SNAPSHOT.json` 读取**，本文件不再自行记录最终包摘要（文档与自身包摘要互相引用会反复漂移）。
详细逐项测试：见 [docs/TEST-REPORT.md](docs/TEST-REPORT.md)、2026-11-15 总报告（本地证据：`test-artifacts/next-20261115/FINAL-REPORT.md`）、PU01—PU02 完成报告（本地证据：`test-artifacts/production-use-20261115/20261115T113000-pu01-pu02/FINAL-REPORT.md`）

> **勘误（2026-11-15，PU01）：**本文件原写“最终候选 `78dd6947…`”，该值不来自任何已保存清单；最终 `MANIFEST.json` / `SUMMARY.json` 的实际候选指纹是 `570cc39e58e1942d762a144e5a13fc092567ace401879011e504d318d4c03e19`。详见 test-artifacts/next-20261115/ERRATA.md（本地证据：`test-artifacts/next-20261115/ERRATA.md`）。以机器清单为准。

## 0. 2026-11-15 正式投用开发（PU01—PU06）

- **PU01 PASS（本轮开发）**：新增 `tools/check_release_evidence.py`（路径基准、引用、候选身份、门槛、数据来源、markdown 链接检查）与 `candidate_manifest.py --verify-snapshot`（交付包 + 完整源码包，空目录解包逐文件重建四个 scope）。历史证据入口与 `DELIVERY.md` 指纹已勘误，原始候选包/日志未改写。
- **PU02 PASS（本轮开发）**：`tools/storage_acceptance.py` 写入入口自带卷/序列号/登记/所有权/预算/卷外记录保护；新增 `tools/storage_scenarios.py` + `tests/host_acceptance/storage_writer.py` 阶段同步协议，DISK01—05 五类目标写入均有场景定义，正常/拒绝/超时/模拟失败矩阵通过。
- **PU03/PU04/PU05/PU06 NOT_RUN**：无可丢弃受控卷（本机非管理员，仅 C: 系统卷与 D: 项目卷），无用户目标实例、工作流、模型与维护窗口。
- 因此：**本轮交付工具与证据机制；生产可用结论仍为 PARTIAL。**

## 0.1 2026-11-15 下一阶段验收追加结论（历史）

- **NP01 PASS**：候选快照/确定性清单/不可变交付包、安装运行时精确比较、dry-run 无副作用、真实 1×1、结构化失败与负向验证；Python 109（108 PASS + 1 SKIP）/ JS 37 PASS。
- **NP02 PASS**：真实宿主 HOST06 A—D（真实 GPU 1×101、后页入选 FINAL 导出、5000 源/10000 OUT 夹具、失败/迟到响应）与 HOST13 UI01—UI06（主题/缩放/键盘/减少动态/原生文件选择/图片与断网失败/冻结后移除追加）。
- **NP03 PARTIAL**：受控卷工具与 25 项故障注入（24 PASS + 1 SKIP）通过；真实 ENOSPC DISK01—05 **NOT_RUN**（非管理员、无独立受控卷）。
- **NP04/NP05 NOT_RUN**：无用户实际生产实例、工作流、模型和维护窗口；HOST14 与目标安装/回退/试用未执行。
- 因此：**隔离组合与真实宿主补验可声明；用户生产环境、真实 ENOSPC、目标安装不能声明通过。**

## 1. 交付结论（2026-11-12 基线，历史）

- 插件代码、安装器、示例工作流、中文文档和真实测试证据均已就位，可在隔离 ComfyUI 0.45.0 实例运行。
- Q01—Q35 在**隔离 CPU tiny 模型实例**上全部达到 PASS；F01—F12 L0 全部 PASS，其中 F07/F08 已在真实 ComfyUI 复现。
- **独立 GPU + 真实模型验收 PASS**：在 `D:\Projects\ComfyUI-gpu-acceptance` 使用 RTX 3070、torch 2.9.1+cu126、DreamShaper8_LCM 真实 checkpoint，完成 1×1、3×5、555（555/555，512×512，4 steps，约 19m34s），全部通过 SQLite/文件/哈希/PNG/清单核验。
- **提交后复验 PASS（2026-11-12）**：在 `744ff97` 产品代码上重跑 L0（85 项：84 PASS + 1 SKIP）、JS（7 PASS），并在同一独立 GPU 实例重新完成 1×1、3×5、555/555（555 用时 1170 s）；证据 `test-artifacts/gpu-acceptance-postcommit/`。
- **用户生产 ComfyUI 仍未安装/未接触**，其 production-specific 1×1、3×5、555 仍为 NOT_RUN；不得把独立 GPU 实例结果写成用户生产环境验收。

## 2. 交付物

| 交付物 | 位置/入口 |
| --- | --- |
| 插件源码 | `comfyui_gacha_master/`，入口 `__init__.py`，路由前缀 `/gacha_master/v1` |
| 安装/卸载器 | `tools/install_plugin.py --comfy-root <ROOT> [--mode link|copy] [--uninstall]` |
| 示例工作流 | `examples/workflow_img2img.json` |
| 中文使用文档 | `README.md`、`docs/` |
| 真实证据 | `test-artifacts/real-comfyui/`（CPU tiny L1/L2/Q/F）、`test-artifacts/gpu-acceptance/`（GPU 真实模型 1×1/3×5/555）、`test-artifacts/gpu-audit/`（环境审计） |
| 完整测试报告 | `docs/TEST-REPORT.md` |
| 环境事实 | `docs/ENVIRONMENT.md` |
| 已知限制 | `docs/KNOWN-ISSUES.md` |
| 生产验收步骤 | `docs/REAL-ACCEPTANCE-RUNBOOK.md` |

## 3. 隔离实例安装信息（已验证）

- 根目录：`D:\Projects\ComfyUI-acceptance`
- ComfyUI：0.45.0，源码 commit `4d64a8bc…`
- Python：`D:\Projects\ComfyUI-acceptance\venv\Scripts\python.exe`（3.12.10）
- PyTorch：2.9.1+cpu（`cuda.is_available()=False`）
- 模型：`ComfyUI/models/checkpoints/tiny-sdxs.ckpt`（347 MB，tiny UNet/CLIP + 随机 SD1 VAE，仅测试执行链，不代表画质）
- 插件：copy 安装到 `ComfyUI/custom_nodes/ComfyUI-Gacha-Master`
- 端口：`127.0.0.1:8199`，当前健康、无在途任务（当前 40 JOB，555 任务仍 COMPLETED 555/555）
- 数据根：`D:\Projects\ComfyUI-acceptance\user\gacha_master`
- 结果根：`D:\Projects\ComfyUI-acceptance\output\gacha_master\Jobs`
- 导出根：`D:\Projects\ComfyUI-acceptance\output\gacha_master\Exports`
- 备份：`D:\Projects\ComfyUI-acceptance\user\gacha_master\backups`

### 独立 GPU 实例（2026-11-12，已验证）

- 根目录：`D:\Projects\ComfyUI-gpu-acceptance`
- ComfyUI：0.45.0（源码参考副本）；Python 3.12.10；`torch 2.9.1+cu126`，`cuda.is_available()=True`
- GPU：NVIDIA GeForce RTX 3070 Laptop GPU，8192 MiB，驱动 546.30
- 模型：`DreamShaper8_LCM.safetensors`，2,133,804,992 B，SHA-256 `a4f3e1526c5dc4fcbe342f5c410d83ae202c7a415fcefcbb92e0f93fcd0a87c3`
- 工作流：`workflow_gpu_api.json` / `workflow_gpu_ui.json`；512×512，4 steps，cfg 1.5，lcm sampler
- 端口：`127.0.0.1:8288`
- 数据：`D:\Projects\ComfyUI-gpu-acceptance\user\gacha_master`；结果 `...\output\gacha_master\Jobs`
- 验收：`JOB-20261111-EB34AB` 1/1、`JOB-20261111-CF7356` 15/15、`JOB-20261111-D6DB8B` 555/555；证据 `test-artifacts/gpu-acceptance/`

生产安装命令：

```bash
<COMFY_PYTHON> tools/install_plugin.py --comfy-root <COMFY_ROOT> --mode copy
```

## 4. 真实验收摘要（隔离 CPU tiny 模型）

| 项目 | 结果 | 关键证据 |
| --- | --- | --- |
| 1×1 真实任务 | PASS | `L1_tiny.json`、`L1_outputs.complete.json` |
| 3×5 严格顺序 | PASS 15/15，AAAAA→BBBBB→CCCCC | `L2_3x5.json` |
| 555 真实规模 | PASS 555/555，555 唯一 prompt_id/种子、全哈希/PNG/清单 | `L2_555_completed.json`、`L2_555_verify.json` |
| 安全/立即暂停 | PASS；排队取消只移除插件 pending | Q03/Q04 证据 |
| 定向取消 | PASS 运行中+排队中，外部哨兵不受影响 | `Q04_*.json` |
| 强杀恢复 | PASS，UNKNOWN 不自动重提，人工 retry 同种子 | `Q05_kill_*.json` |
| 保存/DB 崩溃窗口 | PASS F07/F08 | `Q07_F07_*.json`、`Q07_F08_*.json` |
| HTTP 丢响应 | PASS，只提交 1 次并按原 prompt ID 找回 | `Q06_dropped_response.json` |
| 导出/跳过/冲突/续传 | PASS 四组合 + 显式跳过 + 409 + resume | `Q11_*.json`、`Q14_*.json`、`Q30_*.json` |
| 导出 pending cutoff | PASS，未完成 DRAW 不暗中并入 | `Q31_pending_cutoff.json` |
| 追加 tail/priority/嵌套 | PASS，块连续、新种子唯一 | `Q09_append5.json`、`Q29_*.json` |
| 1000 OUT 分页/重连 | PASS 250×4，初始 50 张+加载更多，服务重启恢复 | `Q27_*.json/png` |
| 磁盘/只读/卷失联 | PASS 提交前阻断，0 ATTEMPT/OUT | `Q19_*.json` |
| DB BUSY/迁移回滚/只读 | PASS | `Q25_*.json`、`Q35_db_readonly.json` |
| 多实例/并发幂等 | PASS | `Q22_*.json` |
| 多采样器种子 | PASS，DB 与 ComfyUI history 节点映射一致 | `Q21_multi_sampler_seeds.json` |
| MASK/额外 SaveImage | PASS，MASK 真实执行；额外保存节点仅警告不接管 | `Q20_mask_extra_save.json` |
| 同名源/失败重试隔离 | PASS，2 个 `same.png` 独立；失败后 ATTEMPT-002 同种子，旧 OUT 不变 | `Q14_same_name_sources.json`、`Q08_retry_after_save_failure.json` |
| 正常 Ctrl+C | PASS，主数据实例 exit 0、端口关闭、`Stopped server`，重启 32 JOB/555 保留 | `Q35_ctrlc_main.json`、`Q35_after_ctrlc_restart.json` |
| 安装/更新/代码回退 | PASS，卸载保留数据；更新与回退到 HEAD 后 32 JOB/555 保留 | `Q33_uninstall_reinstall.json`、`Q33_update_rollback.json` |

## 5. 真实 / 模拟 / 失败 / 未实现（分开报告）

| 类别 | 内容 |
| --- | --- |
| **真实执行** | ① 隔离 CPU tiny 模型：Q01—Q35 全部主场景；② 独立 GPU + DreamShaper8_LCM 真实模型：1×1、3×5、555（512×512、4 steps、LCM）；③ 独立 CPU 实例上的暂停/取消、强杀、F07/F08、HTTP 丢响应、导出、追加、1000 OUT、MASK、同名源、失败重试、安装/更新/回退、Ctrl+C。 |
| **L0/模拟** | FakeEngine 单元与集成测试、故障注入、JS/UI 测试、10000 DRAW 计划；用于验证状态机和文件/DB 不变量，**不是真实推理**。UI 改造的 20 项矩阵同样是 L0 浏览器测试台（stub 宿主 + FakeEngine），16 项 PASS、4 项 PARTIAL。 |
| **失败但已修复** | 早期 Ctrl+C `AttachConsole` 未触发 SIGINT：根因是自动化宿主继承“忽略 Ctrl+C”标志；启动器清除后真实通过。早期排队取消缺 history：已改为 cancel 确认 + absent 结算。均保留修复证据。UI 轮修复了 `confirmDialog` 永远返回 false、任务列表轮询推进分页 cursor、对照监听残留、详情全量重绘丢焦点/滚动等问题。 |
| **未实现/未验证** | 用户生产 ComfyUI 安装/升级；原生 OS 文件选择器上传；冻结后逻辑移除的浏览器 UI；真实 ENOSPC；Q34 逐字节“校验后立即替换”窗口；物理键盘 Ctrl+C（使用真实控制台事件）；**新 UI 在真实 ComfyUI 侧栏/前端 1.58.5 的复核**；浏览器拖入文件夹、真实双标签、图片加载失败 UI、200% 缩放、减少动态效果。 |

## 6. 独立 GPU 验收结果与用户生产边界

独立 GPU 实例已经实际执行并 PASS：

| 场景 | Job | 结果 | 关键核验 |
| --- | --- | --- | --- |
| 1×1 | `JOB-20261111-EB34AB` | PASS 1/1 | 1 DRAW/ATTEMPT、1 唯一 seed/prompt_id、OUT 512×512、哈希/PNG/清单 |
| 3×5 | `JOB-20261111-CF7356` | PASS 15/15 | 15 DRAW/ATTEMPT、15 唯一 seed/prompt_id、15 OUT 全哈希/PNG/清单 |
| 555 | `JOB-20261111-D6DB8B` | PASS 555/555 | 555 DRAW/ATTEMPT、555 唯一 seed/prompt_id、555 OUT 全哈希/PNG/清单、源图哈希不变、约 19m34s |

### 提交后复验（2026-11-12）

| 场景 | Job | 结果 | 耗时 |
| --- | --- | --- | --- |
| 1×1 | `JOB-20261111-EB2A1E` | PASS 1/1 | 4.1 s |
| 3×5 | `JOB-20261111-74D01F` | PASS 15/15 | 31.1 s |
| 555 | `JOB-20261111-CB4D67` | PASS 555/555 | 1170 s（19m30s） |

独立核验：555 DRAW/ATTEMPT/OUT 均为 555，555 唯一 `actual_prompt_id`、555 唯一种子；555 个 OUT 文件存在且大小/SHA-256/PNG 解码/清单全部通过；111 张源图哈希未变。证据 `test-artifacts/gpu-acceptance-postcommit/SUMMARY.json`。

这是**独立 GPU 验收实例**，不是用户生产环境。2026-11-12 的只读审计仍确认本机没有用户生产 ComfyUI 安装；旧机 `C:\COMFYUI_dapao`、旧 Python/PyTorch 和 F/G 盘模型不在这台机器。因此“用户生产 ComfyUI 的 1×1、3×5、555”仍为 **NOT_RUN**，不能把独立 GPU 结果冒充用户生产结果；提交后复验同样只代表该独立实例。

如需继续验证用户生产环境，需要提供生产根目录、其 Python 路径和真实模型/工作流；独立 GPU 实例可保留作为可复现的 GPU 真实验收证据。

## 7. 生产复跑工具（已就绪）

提供 `tools/production_acceptance.py`，拿到生产根目录/Python/模型后可直接执行：

```bash
.venv/Scripts/python.exe tools/production_acceptance.py   --base-url http://127.0.0.1:8188   --data-root <COMFY_USER>/gacha_master   --workflow-api <API工作流.json>   --workflow-ui <UI工作流.json>   --input-root <原图目录>   --load-node 1 --save-node 8   --comfy-root <COMFY_ROOT>   --scenarios 1x1,3x5,555
```

- `--dry-run` 只做 health/capabilities/preflight/checkpoint 存在性检查，不创建任务。
- 正式模式创建 1×1、3×5（3 源×5）、555（111 源×5），轮询完成后直接读 SQLite 和磁盘核验 DRAW/ATTEMPT/seed/prompt_id/OUT 哈希/PNG 解码/清单/源图哈希。
- 结果写入 `test-artifacts/production-acceptance-<时间>/SUMMARY.json`。
- 该工具已在隔离 CPU tiny 实例完成 1×1、3×5、555 smoke，并在独立 GPU 实例用 DreamShaper8_LCM 完成真实 1×1、3×5、555；GPU 证据 `test-artifacts/gpu-acceptance/*/SUMMARY.json`，CPU smoke 证据 `test-artifacts/production-tool-*/SUMMARY.json`。

## 8. 复现命令

```bash
# L0
.venv/Scripts/python.exe -m unittest tests.unit.test_core tests.unit.test_node tests.unit.test_scheduler tests.unit.test_exports tests.unit.test_reliability tests.unit.test_scale tests.integration.test_api tests.integration.test_install tests.integration.test_integration_register tests.integration.test_comfy_adapter tests.fault_injection.test_crashes -v
# JS（23 项：7 基线 + 16 UI 专项）
node --test tests/js/api.test.mjs tests/js/ui_refresh.test.mjs

# UI 改造浏览器 L0（stub 宿主 + FakeEngine；不是 ComfyUI）
.venv/Scripts/python.exe tests/ui_harness/server.py
# CPU 隔离实例
curl -s http://127.0.0.1:8199/gacha_master/v1/health
# 独立 GPU 实例
curl -s http://127.0.0.1:8288/gacha_master/v1/health
```

提交后复验证据与测试确定性修复随本次交付更新提交；用户生产环境验收仍为 NOT_RUN。

## 9. 正式使用前修复候选交付（2026-11-14）

代码：工作区候选（Git `8e388a5` + 本轮未提交改动），运行时 55 个文件与源码逐文件 SHA-256 一致。
证据：最终验收报告（本地证据：`test-artifacts/final-20261114/REPORT.md`）

- **修复完成**：结果分页可持续/可重试、201/500/5000 原图分页、跨页/双标签入选同步、异步分批创建与持久进度、暂停/放弃/继续屏障、DOM/缓存/图片预算、轻量 OUT 状态接口、schema v1→v2。
- **隔离真实组合复验 PASS**：ComfyUI 0.45.0 / 前端 1.58.5 / RTX 3070 / DreamShaper8_LCM；HOST01—HOST13、真实 1×1/3×5/555、四组合导出、强杀恢复、三启动周期、历史库迁移、安装更新/回退、重连与窄视口均已取证。
- **安装/更新**：`tools/install_plugin.py --mode copy --update` 先复制到暂存目录并逐文件校验，旧代码移动到 `<COMFY_ROOT>/user/gacha_master/install-backups/`（不在 custom_nodes 内，不会被当作第二个插件加载），失败自动恢复旧目录；`--verify` 输出源码/安装树哈希与差异。
- **数据库迁移/回退**：迁移前创建一致性备份；本轮用真实历史 v1 库副本验证 5 JOB/7 SRC/23 DRAW/21 OUT/4 选择/5 导出/8 备份全部保留、21 个输出哈希不变、integrity/外键通过。代码回退与数据库回退分别处理：新库被旧代码拒绝时，先保留升级后库/结果，再按升级前备份恢复；不自动删除新文件。
- **卸载**：保留数据、结果、导出、备份；重装后重新找到原实例。
- **仍 NOT_RUN**：用户生产 ComfyUI / 实际生产工作流（HOST14）、真实 ENOSPC（无管理员权限创建受控卷）、浏览器原生文件选择器上传。公开分发前仍需落实许可证。
