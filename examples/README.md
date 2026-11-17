# 示例工作流

## 文件

- `workflow_img2img.json`：ComfyUI 编辑器工作流（UI 格式）。
- `workflow_img2img_api.json`：同一工作流的 API 执行格式；插件的“捕获当前画布工作流”会同时得到两者。
- `example_source.png`：本项目生成的 256×256 合成图，仅用于演示；不是用户私有素材。

## 使用

1. 把 `example_source.png` 放入 ComfyUI 的 `input/` 目录（或替换为你自己的图）。
2. 打开 ComfyUI，把 `workflow_img2img.json` 拖入画布。
3. 把 `CheckpointLoaderSimple` 的 `sd15.safetensors` 改成你本机已有的兼容模型（示例不附带模型）。
4. 检查节点/连线；点击 ComfyUI 原生 Queue 先跑通一次（可选）。
5. 打开“抽卡大师”侧栏 → 新建任务 → 捕获当前画布工作流。
6. 绑定：
   - LoadImage：节点 `#1`
   - 最终 SaveImage：节点 `#8`
7. 运行预检；把 `example_source.png` 或你的图加入任务；每张次数设为 1 先验证。
8. 创建并启动；完成后检查 `SRC-0001/DRAW-000001/ATTEMPT-001-*/OUT-0001.png` 与 `outputs.complete.json`。

## 说明

- 示例是标准图生图链：`LoadImage → VAEEncode → KSampler → VAEDecode → SaveImage`，只有一个受控种子 `KSampler.seed`。
- 插件会把执行副本中的 `#8 SaveImage` 原位替换为 `GachaSaveImage`，保留节点 ID 与 `images` 连线；编辑器文件不变。
- 如果你的模型/VAE/CLIP 不是标准 SD1.5 图生图，请按实际工作流调整；插件只要求一个 LoadImage、最终 SaveImage 和一个直接数值种子。
- 示例模型名是占位符；本仓库不下载、不附带任何模型。
