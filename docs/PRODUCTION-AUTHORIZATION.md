# 生产 GPU 验收授权与执行范围

状态：**独立 GPU 实例已执行并 PASS；用户生产环境仍未执行**
日期：2026-11-12（2026-11-15 追加说明）

本文件把“安装 CUDA torch + 下载真实模型”的范围、体积、路径和回滚方式固定下来。2026-11-12 已在独立目录 `D:\Projects\ComfyUI-gpu-acceptance` 完成 CUDA torch 2.9.1+cu126 + DreamShaper8_LCM 安装/下载，并完成 1×1、3×5、555 验收；用户生产 ComfyUI 仍未触碰。

2026-11-15 追加：本轮（PU01—PU02）只新增/修改开发与验收工具、测试和文档，**未启动任何 ComfyUI 实例、未执行 GPU 推理、未触碰生产环境**。PU03 真实写满需要单独的可丢弃受控卷授权；PU05 目标安装/回退与 PU06 日常周期需要维护窗口与明确的生产操作范围，均尚未提供。见 PU01—PU02 完成报告（本地证据：`test-artifacts/production-use-20261115/20261115T113000-pu01-pu02/FINAL-REPORT.md`）。

## 1. 授权选项

### A. 使用你已有的生产环境

请提供：

- 生产 ComfyUI 根目录；
- 该实例使用的 Python 路径；
- GPU 可用的真实模型文件；
- 实际工作流（API + UI JSON）。

不会修改生产环境；只通过 API 创建任务，证据写入本仓库 `test-artifacts/`。

### B. 独立 GPU 验收环境（已执行）

实际使用 torch 2.9.1+cu126（2.5.1+cu121 与 2.6.0+cu124 因 `comfy_kitchen` schema 不兼容被替换）；模型仍为 DreamShaper8_LCM。范围仅限：

- 新目录 `D:\Projects\ComfyUI-gpu-acceptance`；
- 新建独立 venv；
- 安装 CUDA torch 2.5.1+cu121、torchvision 0.20.1 及 ComfyUI 0.45.0 所需依赖；
- 从 HuggingFace 下载 `DreamShaper8_LCM.safetensors`；
- 在该目录启动独立 ComfyUI（端口 8288）；
- 运行 1×1、3×5、555 验收。

明确**不包括**：

- 不修改 `D:\Projects\ComfyUI-acceptance` 现有 CPU 实例；
- 不修改任何生产 ComfyUI、生产 Python、系统 Python、模型目录或其他插件；
- 不升级现有 PyTorch/CUDA；
- 不删除用户原图、结果、工作流或模型。

## 2. 实际下载与磁盘

| 项目 | 大小 | 校验 |
| --- | --- | --- |
| torch 2.9.1+cu126 / torchvision 0.24.1+cu126 | 约 2.6GB | pip 包校验（最终可用组合） |
| `DreamShaper8_LCM.safetensors` | 2,133,804,992 B | SHA-256 `a4f3e1526c5dc4fcbe342f5c410d83ae202c7a415fcefcbb92e0f93fcd0a87c3`（已核验） |
| ComfyUI 0.45.0 依赖 | 约数百 MB | pip 包校验 |

当前 D 盘可用约 736GB，满足空间要求。

## 3. 授权后执行步骤

1. 创建 `D:\Projects\ComfyUI-gpu-acceptance`；
2. 用 Python 3.12 新建 venv，安装 CUDA torch/torchvision；
3. 复制 ComfyUI 0.45.0 源码（已在本机 `D:\Projects\ComfyUI-reference`）并安装 requirements；
4. copy 安装本插件到该实例 `custom_nodes`；
5. 下载 DreamShaper8_LCM 到 `models/checkpoints`，校验 SHA-256；
6. 生成基于该 checkpoint 的 API/UI 工作流；
7. 启动独立 ComfyUI `127.0.0.1:8288`；
8. 运行：
   ```bash
   .venv/Scripts/python.exe tools/production_acceptance.py \
     --base-url http://127.0.0.1:8288 \
     --data-root D:\Projects\ComfyUI-gpu-acceptance\user\gacha_master \
     --workflow-api ... --workflow-ui ... \
     --input-root ... --comfy-root D:\Projects\ComfyUI-gpu-acceptance\ComfyUI \
     --load-node 1 --save-node 8 --scenarios 1x1,3x5,555
   ```
9. 保存 `SUMMARY.json`、GPU 日志、`nvidia-smi` 采样；
10. 更新 `docs/TEST-REPORT.md`、`DELIVERY.md`，明确标注“独立 GPU 实例 + DreamShaper8_LCM”，不冒充用户生产环境。

## 4. 回滚

删除整个 `D:\Projects\ComfyUI-gpu-acceptance` 即可撤销；不会影响现有隔离 CPU 实例、生产环境或用户数据。

## 5. 当前状态

- 独立 GPU 验收 PASS：`test-artifacts/gpu-acceptance/ENVIRONMENT.json`、`1x1/SUMMARY.json`、`3x5/SUMMARY.json`、`555/SUMMARY.json`；
- 2026-11-15 追加：隔离宿主完成真实 1×101 与 HOST06/13；候选/运行时 `821edc19…`；见 `test-artifacts/next-20261115/FINAL-REPORT.md`；
- 用户生产 ComfyUI：未找到/未触碰，仍为 NOT_RUN；NP04/NP05 也未执行；
- 机器审计：`test-artifacts/gpu-audit/SUMMARY.json`、`test-artifacts/real-comfyui/PRODUCTION-BLOCKER.json`。
