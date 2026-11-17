# 环境事实（2026-11-11 基线；2026-11-15 追加）

本文件只记录本轮实际观察到的事实；未执行的安装/启动/推理明确标注为未发生。

## 2026-11-15 下一阶段验收环境

- 隔离真实宿主：`D:\Projects\ComfyUI-gpu-acceptance\ComfyUI`，ComfyUI 0.45.0，Python 3.12.10 / torch 2.9.1+cu126，RTX 3070 Laptop 8 GiB / 驱动 546.30，端口 8296；`--base-directory` 指向本轮证据运行时，`--models-directory` 指向既有 GPU 模型目录。文中 8199/8288/8765 为 2026-11-11/12 历史端口，2026-11-15 未使用。
- 浏览器：Playwright 1.65.1 + Chromium 159.0.8154.6（`%LOCALAPPDATA%\ms-playwright\chromium-1255\chrome-win64\chrome.exe`），Node 24.22.1；Playwright npm 包安装在仓库外 `%TEMP%\gm-host-node`，通过 `NODE_PATH` 使用。
- 数据：1×101 为真实 GPU 作业；5000 源/10000 OUT 为 FakeEngine 夹具，明确标记 `data_source=fixture`；UI06 为真实 GPU 4×2 作业（暂停后保留未执行源）。
- 卷：C: 系统 510.8 GB、D: 项目 1000.2 GB；另有一个无盘符 1.03 GB NTFS 卷（未触碰）。当前账户非管理员；`diskpart`/`fsutil` 拒绝访问，`New-VHD` 不可用；无独立受控卷，真实 ENOSPC 未执行。
- 2026-11-15 正式投用开发复测（`test-artifacts/production-use-20261115/20261115T113000-pu01-pu02/environment/VOLUMES.json`）：C: `30A9DFC7`（系统，510.8 GB，空闲 393.1 GB）、D: `BC413648`（项目，1000.2 GB，空闲 688.4 GB），没有第三个卷。`admin=false`；`diskpart list disk` 以 `WinError 740`（需要提升）失败。受控卷候选为空 → PU03 真实写满仍 NOT_RUN。
- 详细值：`test-artifacts/next-20261115/20261115T081500-np02/environment/ENVIRONMENT.json`、`20261115T090500-np03/environment/VOLUMES.json`。

## 结论先行

- **授权前，本机没有安装 ComfyUI**：在 `C:\`、`D:\` 搜索未发现 `ComfyUI` 目录、`custom_nodes`、`main.py` 形式的 ComfyUI 安装；`C:\COMFYUI_dapao`（旧机记录中的路径）不存在。
- 用户明确授权后，已在独立目录 `D:\Projects\ComfyUI-acceptance` 创建**隔离测试实例**（ComfyUI 0.45.0、CPU torch 2.9.1、极小测试模型、端口 8199）；它不是生产环境，详见本文末尾。
- 项目另克隆了官方源码作为接口参考（不改动、不运行）：
  - ComfyUI：`D:\Projects\ComfyUI-reference`，commit `4d64a8bc072b81c9b47321789b4d83fb7348ae2a`，`comfyui_version.py` = `0.45.0`。
  - ComfyUI_frontend：`D:\Projects\ComfyUI-frontend-reference`，commit `e8791746d266a3e7da22c64eab88225341dc0fb0`（仅 sparse checkout `src`）。
- 真实集成已在隔离实例执行：L1 1×1、L2 3×5、**555/555**、安全暂停、定向取消（运行中+排队中）、强杀恢复、F07/F08 保存窗口、HTTP 丢响应恢复、四组合导出/续传、备份恢复、目标冲突、浏览器关闭、追加 5 次、安装/更新/代码回退、正常 Ctrl+C（主数据实例重启后 32 JOB/555 保留）均 **PASS**；独立 GPU + DreamShaper8_LCM 也完成 1×1/3×5/555。用户生产 ComfyUI 未运行。见 TEST-REPORT。

## 机器

| 项目 | 实际值 |
| --- | --- |
| 操作系统 | Windows 11 家庭版 10.0.26300（Build 26300） |
| CPU | 12th Gen Intel Core i9-12900H，14 核 / 20 逻辑处理器 |
| 内存 | 34,142,150,656 字节（约 31.8 GiB） |
| GPU | NVIDIA GeForce RTX 3070 Laptop GPU，8192 MiB |
| NVIDIA 驱动 | 546.30 |
| 盘符 | `C:` 系统；`D:` KIOXIA EXCERIA G2 1TB；无其他已挂载固定卷 |

## Python 与依赖

| 解释器/环境 | 事实 |
| --- | --- |
| 系统 Python | `C:\Users\XING\AppData\Local\Python\pythoncore-3.12-64\python.exe`，3.12.10 |
| 系统 Python 包 | 初始只有 pip 26.3；**没有** torch、Pillow、aiohttp、numpy |
| 项目测试 venv | `D:\Projects\ComfyUI Gacha Master\.venv`，Python 3.12.10；Pillow 13.0.0；aiohttp 3.14.5；numpy 2.5.4（仅节点 L0 测试用，非插件新增运行依赖） |
| ComfyUI Python | `D:\Projects\ComfyUI-acceptance\venv\Scripts\python.exe`，Python 3.12.10（隔离测试实例；不是生产 ComfyUI） |
| PyTorch / CUDA | torch 2.9.1+cpu、torchvision 0.24.1+cpu（仅隔离测试实例；CPU 构建，未使用 CUDA）；`nvidia-smi` 仅证明宿主驱动存在 |

测试 venv 只用于本插件的自动化测试；**没有**用它冒充 ComfyUI Python，也没有向系统 Python 安装运行依赖。

## ComfyUI 源码核对（只读）

| 事实 | 源码位置/值 |
| --- | --- |
| 版本 | 0.45.0（commit `4d64a8b`） |
| 客户端 prompt_id | `server.py` `/prompt` 接受客户端 UUID，缺省时服务端生成；非法 UUID 返回 400 |
| 定向取消 | `POST /api/jobs/{job_id}/cancel` 使用 `PromptQueue.interrupt_if_running`，在队列锁内检查运行 ID；源码语义为原子 |
| 自动取消竞态说明 | 源码注释说明全局中断标志在每个 prompt 开始时重置，避免取消落到后继任务 |
| 队列/历史 | `GET /queue`、`GET /history/{prompt_id}`、`GET /api/jobs/{job_id}` |
| 终态 | history `status.status_str` = `success`/`error`，`completed` 布尔，`messages` 中含 `execution_success`/`execution_error`/`execution_interrupted` |
| SaveImage 契约 | `RETURN_TYPES=("IMAGE",)`，`save_images` 返回 `{"ui": {"images": [...]}, "result": (images,)}` |
| LoadImage 契约 | `RETURN_TYPES=("IMAGE","MASK")`；`IS_CHANGED` 对文件内容做 SHA-256 |
| 自定义路由 | `PromptServer.instance.routes`（`aiohttp.web.RouteTableDef`），在 `prompt_server.add_routes()` 前注册 |
| 自定义节点加载 | `nodes.load_custom_node` 用 `spec_from_file_location` 执行 `__init__.py`，读取 `WEB_DIRECTORY`、`NODE_CLASS_MAPPINGS` |
| 前端侧栏 | `SidebarTabExtension` 支持 `type: "custom"` + `render(container)`；`app.extensionManager.registerSidebarTab` |
| 图导出 | `app.graphToPrompt()` 返回 `{workflow, output}`（UI 工作流 + API prompt） |
| 前端版本要求 | ComfyUI 0.45.0 `requirements.txt` 要求 `comfyui-frontend-package==1.58.5` |

**限制**：上表是源码事实。隔离实例已真实运行 1×1/3×5/555，并完成定向取消的外部哨兵隔离测试；该实例凭据下能力为 `verified_atomic`。仓库默认值仍为 `unverified`/false，生产或其他实例必须在目标环境重新验证后才能开启。

## 目录与项目状态

| 项目 | 事实 |
| --- | --- |
| 工作区 | `D:\Projects\ComfyUI Gacha Master` |
| 初始内容 | 仅 `DEVELOPMENT.md`（96,436 字节）；无 `AGENTS.md`；无 Git 仓库 |
| 本轮 Git | 已 `git init` 并提交；基线 `744ff97`，提交后复验更新随交付提交 |
| 测试证据 | `test-artifacts/`（full test log、JS log、UI 截图、UI harness 信息） |
| 参考克隆 | `D:\Projects\ComfyUI-reference`、`D:\Projects\ComfyUI-frontend-reference`（均为独立参考目录，不是生产安装） |

## 启动/端口

- 隔离验收实例在 `127.0.0.1:8199` 启动（仅本机回环），当前实例仍在运行；555 已完成，当前无在途 ATTEMPT、全局槽空，不自动执行队列。强杀测试后已用同一命令重启并完成恢复。
- 独立 GPU 实例在 `127.0.0.1:8288` 启动；提交后复验完成后无在途 ATTEMPT、全局槽空，当前保持运行（空闲）。
- 未发现或使用生产 ComfyUI 的 8188/8189/8878/8898 端口。
- 测试用 UI 测试台临时监听 `127.0.0.1:8765`，仅用于 L0 浏览器验证，已停止。

## 未执行事项

- 未安装到用户生产 ComfyUI（本机原本没有生产安装）；只安装了隔离测试实例。
- 未升级或修改用户环境的 torch/CUDA/ComfyUI/其他插件；torch 仅装在隔离实例 venv。
- CPU 实例未下载大型画质模型：仅使用 8.8 MB 的 tiny 测试权重并生成 347 MB 测试 checkpoint；独立 GPU 实例按授权下载了 2.13 GB 的 DreamShaper8_LCM，均位于各自隔离目录。
- 未修改用户原图、模型、工作流或既有插件。
- 真实 L1 1×1、L2 3×5、555、暂停/取消、强杀恢复、F07/F08 保存窗口、HTTP 丢响应恢复、导出/续传、备份/旧库对账、目标冲突、浏览器关闭继续、追加、安装/更新/回退、正常 Ctrl+C 已执行；独立 GPU + DreamShaper8_LCM 已执行 1×1/3×5/555 并完成提交后复验；用户自己的生产模型/工作流仍未执行。F01—F12 的其余进程崩溃测试仍针对本插件测试进程，不是 ComfyUI 进程。

## 真实验收测试实例（2026-11-11，用户授权后创建）

用户明确授权在独立测试目录安装 ComfyUI（含 torch）与兼容小模型后，创建了**隔离测试实例**；它不是用户生产环境，也不是旧机记录中的 `C:\COMFYUI_dapao`。

| 项目 | 实际值 |
| --- | --- |
| 测试根目录 | `D:\Projects\ComfyUI-acceptance` |
| ComfyUI 源码 | `D:\Projects\ComfyUI-acceptance\ComfyUI`，复制自官方源码 commit `4d64a8bc…`，版本 0.45.0；已移除外层 `.git` |
| ComfyUI Python | `D:\Projects\ComfyUI-acceptance\venv\Scripts\python.exe`，Python 3.12.10 |
| PyTorch | 2.9.1+cpu（CPU 构建；未使用 CUDA） |
| 额外测试包 | pywinpty 3.1.2（仅用于 PTY 探测；最终 Ctrl+C 使用 Win32 真实控制台事件，插件运行不依赖它） |
| torchvision | 0.24.1+cpu |
| 前端包 | comfyui-frontend-package 1.58.5 |
| 启动命令 | `main.py --cpu --port 8199 --listen 127.0.0.1 --disable-auto-launch --input-directory D:\Projects\ComfyUI-acceptance\input --output-directory ...\output --user-directory ...\user --temp-directory ...\temp` |
| 插件安装 | `D:\Projects\ComfyUI-acceptance\ComfyUI\custom_nodes\ComfyUI-Gacha-Master`（copy 模式），加载日志 `Gacha Master routes registered` |
| 插件数据根 | `D:\Projects\ComfyUI-acceptance\user\gacha_master` |
| 结果根 | `D:\Projects\ComfyUI-acceptance\output\gacha_master\Jobs` |
| 输入根 | `D:\Projects\ComfyUI-acceptance\input` |
| 定向取消设置 | 隔离实例 `targeted_cancel_verified=true`，能力显示 `verified_atomic`；仓库默认值仍为 `false`，生产安装需按运行手册重新验证后开启 |
| 导出根 | `D:\Projects\ComfyUI-acceptance\output\gacha_master\Exports`（已登记） |
| 模型 | `D:\Projects\ComfyUI-acceptance\ComfyUI\models\checkpoints\tiny-sdxs.ckpt`（347 MB） |
| 模型来源 | `hf-internal-testing/tiny-stable-diffusion-torch` 的 tiny UNet/CLIP；为测试执行器补充**随机初始化的标准 SD1 VAE**并转换为 ComfyUI checkpoint。它是**真实可执行的测试用极小模型**，不是预训练画质模型；仅用于验证 ComfyUI 真实执行/保存链路，不代表生成质量。 |

测试实例只在 `127.0.0.1:8199` 监听；测试数据、临时文件、模型和插件数据均在该隔离根目录内，不触碰用户现有 ComfyUI（本机原本也没有）。CPU 实例真实执行使用 CPU；独立 GPU 实例见下一节，仍不是用户生产环境验收。

测试结束后，该目录保留为证据/可复现实例；不自动删除。用户可整体删除 `D:\Projects\ComfyUI-acceptance` 以清理，不影响生产 ComfyUI 或用户原图。

## GPU / 真实模型候选审计（2026-11-12，只读）

- `nvidia-smi`：NVIDIA GeForce RTX 3070 Laptop GPU，8192 MiB，驱动 546.30，驱动报告 CUDA 12.3；当前显存占用约 1.3 GiB，无 ComfyUI 进程。
- 全盘（C:/D:）搜索 `*.safetensors/*.ckpt/*.pt/*.pth/*.gguf/*.onnx`：未发现生产 ComfyUI、真实扩散模型或 CUDA 版 PyTorch；仅有的模型类文件是隔离 tiny checkpoint、Vim 教程 `.pt` 和 PixPin OCR `.onnx`。
- Torch 包审计：仅 `D:\Projects\ComfyUI-acceptance\venv\Lib\site-packages\torch`，版本 `2.9.1+cpu`，无 `torch_cuda.dll`。
- 若用户明确授权，候选为：CUDA torch 2.5.1+cu121/torchvision 0.20.1（约 2.5GB，新独立 venv）+ DreamShaper8_LCM 2.0GiB 或 SD1.5 4.0GiB；**尚未下载或安装**。
- 原始输出与脚本：`test-artifacts/gpu-audit/`（`SUMMARY.json`、`REPORT.md`、`nvidia-smi.txt`、`commands.txt`、`audit.py`）。

## 独立 GPU 真实模型验收实例（2026-11-12）

| 项目 | 实际值 |
| --- | --- |
| 根目录 | `D:\Projects\ComfyUI-gpu-acceptance`（独立实例，不是用户生产） |
| ComfyUI | 0.45.0，复制自 `D:\Projects\ComfyUI-reference`（已恢复被初始复制过滤掉的 `comfy/ldm/models` 与 `comfy_api/input`） |
| Python | `D:\Projects\ComfyUI-gpu-acceptance\venv\Scripts\python.exe`，3.12.10 |
| PyTorch / CUDA | 2.9.1+cu126 / CUDA 12.6；`cuda.is_available()=True` |
| GPU | NVIDIA GeForce RTX 3070 Laptop GPU，8192 MiB，驱动 546.30 |
| 模型 | `DreamShaper8_LCM.safetensors`，2,133,804,992 B，SHA-256 `a4f3e1526c5dc4fcbe342f5c410d83ae202c7a415fcefcbb92e0f93fcd0a87c3` |
| 工作流 | `workflow_gpu_api.json` / `workflow_gpu_ui.json`；512×512，4 steps，cfg 1.5，lcm sampler |
| 端口 | `127.0.0.1:8288` |
| 数据/结果 | `user\gacha_master`、`output\gacha_master\Jobs` |
| 1×1 | `JOB-20261111-EB34AB`，1/1 PASS |
| 3×5 | `JOB-20261111-CF7356`，15/15 PASS |
| 555 | `JOB-20261111-D6DB8B`，555/555 PASS，约 19m34s |
| 提交后复验 1×1 | `JOB-20261111-EB2A1E`，1/1 PASS，4.1 s |
| 提交后复验 3×5 | `JOB-20261111-74D01F`，15/15 PASS，31.1 s |
| 提交后复验 555 | `JOB-20261111-CB4D67`，555/555 PASS，1170 s |
| 证据 | `test-artifacts/gpu-acceptance/`、`test-artifacts/gpu-acceptance-postcommit/` |

torch 2.9.1+cu121 与 2.6.0+cu124 均因 `comfy_kitchen 0.2.41` 的 `list[int]` schema 不兼容而无法启动 ComfyUI；改为 2.9.1+cu126 后通过。该实例与 CPU 实例、用户生产环境完全隔离。

## 正式使用前修复复验实例（2026-11-14/15）

| 项目 | 实际值 |
| --- | --- |
| 根目录 | `D:\Projects\ComfyUI Gacha Master\test-artifacts\real-host-20261114\runtime` |
| ComfyUI / 前端 | 0.45.0 / 1.58.5 |
| Python / PyTorch / CUDA | 3.12.10 / 2.9.1+cu126 / CUDA 12.6 |
| GPU | RTX 3070 Laptop 8 GiB，驱动 546.30 |
| 模型 | DreamShaper8_LCM.safetensors（2,133,804,992 B，SHA-256 见上一节） |
| 工作流 | 标准 LoadImage→KSampler(seed)→VAEDecode→SaveImage；512×512，4 steps（强杀用 100 steps） |
| 端口 | 127.0.0.1:8296 |
| 安装 | `runtime/custom_nodes/ComfyUI-Gacha-Master`，55 个运行时文件与最终源码逐文件哈希一致 |
| 1×1/3×5/555 | `test-artifacts/real-acceptance-20261114/run2/SUMMARY.json`，全部 PASS（555=555/555） |
| 最终包 1×1 | `test-artifacts/real-acceptance-20261114/run5/SUMMARY.json`，PASS；安装树 55/55 一致（2026-11-14 最终报告口径；run3 为更早的中间运行，不再作为最终包引用） |
| 启动周期 | `startup-cycles.json`，3/3 PASS，正常 SIGINT 关停 exit 0 |
| 强杀恢复 | `crash-recovery.json`，UNKNOWN→人工 retry→同种子 ATTEMPT-2，仅 1 个接受 OUT |
| 已知启动异常 | 2026-11-14 首次启动的 `torchvision::nms` 缺失未在本轮 3 次冻结启动中复发；保留为未定位环境异常，不宣称永久解决 |
| 浏览器 | ZCode IAB + Playwright Chromium 1.65.1 headless |
| ENOSPC | NOT_RUN（2026-11-15 复核：非管理员、无独立受控卷、diskpart/fsutil 拒绝访问；见 `next-20261115/20261115T090500-np03/`） |
