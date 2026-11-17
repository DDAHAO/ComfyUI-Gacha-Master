# 许可证与第三方说明

## 本插件

本仓库公开提供源码，目前尚未选择和添加开源许可证。具体使用授权请联系仓库所有者；本次公开发布不替代正式许可证。后续确定许可证时，将同步更新根目录 `LICENSE`、本文件和 `pyproject.toml`。第三方组件按其各自许可证使用。

## 第三方组件（未捆绑）

| 组件 | 用途 | 许可证/来源 |
| --- | --- | --- |
| ComfyUI | 宿主运行时、节点/路由/前端扩展 API | GPL-3.0（官方仓库 `LICENSE`） |
| ComfyUI_frontend | 侧栏 tab/扩展宿主 | 见官方仓库许可证 |
| Pillow | 图片解码、PNG 编码、缩略图 | MIT-CMU（HPND 风格） |
| aiohttp | ComfyUI 自带 HTTP 服务依赖，本插件路由使用 | Apache-2.0 |
| SQLite | Python 标准库 `sqlite3` | Public Domain |
| PrimeIcons（侧栏 icon class） | 前端图标字体 | MIT |

本仓库不包含 ComfyUI 源码、模型、用户图片或上述第三方二进制。插件通过 ComfyUI 公共 Python/HTTP/JS 接口集成，不修改 ComfyUI 核心。

## 交付物中的原创素材

- `examples/example_source.png` 由本项目用 Pillow 生成（纯色渐变+几何图形），不包含任何用户照片或私有素材。
- 测试图片均为运行时生成的合成图。

## UI 候选组件

UI-DEVELOPMENT.md 研究的 jjtsc.cn 候选组件（步骤条、投放区、图片对比、骨架、通知、工具提示、聚光卡片）
仅作为交互与视觉组织参考。本轮 UI 改造为独立原生实现，未复制候选源码、演示照片、字体、品牌脚本或
`tokens.css`；仓库不依赖这些候选包，也不向其站点或 CDN 运行时请求资源。若未来实际复用第三方源码，
必须按 UI-DEVELOPMENT.md 第 10.3 节补充来源、取得日期/版本、许可证文本、改动摘要与随包文件清单。
