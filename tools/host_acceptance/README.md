# 隔离真实宿主验收工具

这些脚本只管理已经存在的隔离 ComfyUI 实例，不安装 ComfyUI、不修改模型、不触碰用户生产环境。所有结果写入调用方指定的证据目录，并在每条证据里记录 `data_source`、`environment` 和 `host`。

## 组成

| 文件 | 职责 |
| --- | --- |
| `server.py` | 启动/停止/强杀隔离 ComfyUI；记录 PID、日志、启动参数、健康探测 |
| `comfy_runner.py` | 把 stop 文件转换成 Ctrl+C 的包装入口；不增加产品行为 |
| `seed_fixture.py` | 用产品 Service/Scheduler + FakeEngine 生成 5000 原图/10000 OUT 的合法夹具，供真实宿主加载 |
| `host06_*.cjs` | 真实宿主多页、后页入选、导出、迟到响应场景（Playwright） |
| `host13_*.cjs` | 交互矩阵：主题、缩放、键盘、文件选择、网络、冻结（Playwright） |
| `verify_host_*.py` | 独立 SQL/文件/导出核验，不调用产品同一验证函数 |

## 浏览器依赖

Playwright 不放进仓库。将其安装到仓库外并在运行时设置 `NODE_PATH`：

```bash
mkdir -p "$TEMP/gm-host-node" && (cd "$TEMP/gm-host-node" && npm install playwright@1.65.1)
export NODE_PATH="$TEMP/gm-host-node/node_modules"
node tools/host_acceptance/host06_multipage.cjs --base-url http://127.0.0.1:8296 ...
```

浏览器二进制可复用 `%LOCALAPPDATA%/ms-playwright`。证据必须记录 Node、Playwright、浏览器版本和实际命令。

## 启动示例

```bash
.venv/Scripts/python.exe tools/host_acceptance/server.py start \
  --comfy-root D:/Projects/ComfyUI-gpu-acceptance/ComfyUI \
  --python D:/Projects/ComfyUI-gpu-acceptance/venv/Scripts/python.exe \
  --base-dir test-artifacts/next-20261115/<run>/runtime \
  --models-dir D:/Projects/ComfyUI-gpu-acceptance/ComfyUI/models \
  --port 8296 --evidence-dir test-artifacts/next-20261115/<run>/host
```

`--base-directory` 会把 models 默认指向隔离运行时下的 `models/`；必须用 `--models-dir`
显式指向已有模型目录，否则 ComfyUI 的 checkpoint 列表为空，`/prompt` 会以
`prompt_outputs_failed_validation` 拒绝。

启动前必须：

1. 用 `tools/install_plugin.py --mode copy --update` 把当前候选安装到 `<base-dir>/custom_nodes/ComfyUI-Gacha-Master`；
2. 运行 `--verify`，确认运行时文件 55/55 与候选一致且没有多余运行时文件；
3. 用 `tools/production_acceptance.py --dry-run` 证明工作流/模型/路径可用。

## 大清单夹具

```bash
.venv/Scripts/python.exe tools/host_acceptance/seed_fixture.py \
  --runtime-root test-artifacts/next-20261115/<run>/runtime-large \
  --sources 5000 --draws-per-source 2 \
  --report test-artifacts/next-20261115/<run>/host/H06-C-fixture.json
```

生成的数据使用 FakeEngine，报告固定标记 `data_source=fixture`。真实宿主只负责加载并操作这些数据；不得把该报告写成真实 GPU 采样。

## 结果边界

- 真实宿主的 `1x101` OUT 来自隔离 GPU 真实推理时，标记 `real_gpu`。
- 5000/10000 列表来自夹具时，标记 `fixture`。
- 浏览器截图存在不等于操作验收；每个用例必须同时保存操作步骤、断言和请求/控制台日志。
