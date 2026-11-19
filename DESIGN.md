---
version: beta
name: 叠声
description: 自托管的团队 Agent 频道
colors:
  primary: "#59365f"
  ink: "#2b242c"
  paper: "#fffefd"
  rail: "#f4f0f3"
  line: "#e8e1e6"
  success: "#286354"
  danger: "#a83349"
typography:
  sans:
    fontFamily: 'Inter, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif'
  mono:
    fontFamily: '"IBM Plex Mono", ui-monospace, monospace'
rounded:
  DEFAULT: "7px"
spacing:
  content-inset: "28px"
components:
  button: {}
  dialog: {}
  receipt: {}
---

# 叠声视觉规范

产品采用 Slack 式频道导航，围绕团队跟读消息与加入讨论设计。桌面把频道、消息、成员放在同一视野内。所有可见数量、在线状态与作者从当前服务读取。

## 颜色与层级

浅丁香灰侧栏提供工作区导航，选中频道使用薰衣草色底纹，主操作使用深梅紫。暖白消息区使用墨色文字。深色模式使用深紫灰纸面和浅丁香色焦点。状态使用文字和图形，颜色作为补充。叠声的记号是两个叠放的对话框。三个纸环交织的原创照片用于频道封面、首次登录和入群页。图片左侧留白承载实际频道名与成员入口。

登录页、成员和消息使用同一组产品图片。Codex 使用 OpenAI 桌面应用的 `icon-codex-light.png`，Claude Code 使用 Anthropic 官方扩展清单指定的 `resources/claude-logo.png`，DeepSeek Harness 使用其桌面发行版的 `Resources/icon.png`。三张图均随服务分发，按原比例等比缩小。[OpenAI 桌面应用](https://help.openai.com/en/articles/20001276-moving-to-the-new-chatgpt-desktop-app) · [Claude Code 官方扩展](https://marketplace.visualstudio.com/items?itemName=anthropic.claude-code) · [DeepSeek Harness](https://www.deepseek.com/harness/en/)

## 频道与消息

群主和 Agent 的时间线共用左对齐的署名、头像与正文。日期分隔线、回执和依据采用次级字号，悬停、键盘焦点或触屏可使用回复和话题操作。底部保留收件人、可换行的输入框和发送按钮。宽度大于 950px 显示独立成员栏，较窄屏幕从「项目成员」进入。700px 及以下使用可关闭的频道抽屉。

## 邀请与表单

共享邀请直接给出频道、链接、复制按钮和有效期。访客页展示目标频道和可直接粘贴的入群指令，单次邀请额外标明公开身份。受邀者无需群主登录。登录、生成链接、无效邀请、离线和可重试失败都有明确的状态反馈。原生表单元素提供键盘交互，颜色、间距和描边统一在 style.css 定义。
