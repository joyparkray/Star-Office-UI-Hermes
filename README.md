# Star Office UI · Hermes 版

🌐 **中文** | [English](#english)

> 把 Hermes 的工作状态实时投进一间像素风办公室——在 LLM 推理、执行命令、读写文件、甚至调 Codex/Claude 子代理时，办公室里的角色会自动走到对应区域并播放动画。

基于 [ringhyacinth/Star-Office-UI](https://github.com/ringhyacinth/Star-Office-UI)，保留原版全部功能，新增 Hermes 状态自动同步。**无需修改 Hermes 核心代码，无需 Agent 侧配置。**

---

## 与原版的区别

原版面向 OpenClaw，需要 Agent 手动调 `set_state.py` 切换状态。本版**零配置自动同步**——Hermes 工作时，办公室自动变化。

| 维度 | 原版 | 本版 |
|------|------|------|
| 状态驱动 | Agent 手动调 `set_state.py` | Hermes 生命周期事件自动推送 |
| 安装方式 | clone + 启动后端 | 同上 + 安装一个 Hermes 插件 |
| 支持平台 | OpenClaw | **Hermes**（Desktop / CLI / Gateway） |
| Codex/Claude | 不支持 | **独立 `syncing` 状态**，shlex 精确匹配 |

### 核心新增

- **一个插件**：`integrations/hermes/plugin/` —— 10 个生命周期钩子，Hermes Desktop 安装即用
- **一个适配器**：`star_office_hook.py`（635 行，纯 stdlib）—— 解析事件、维护并发状态机、推送到后端
- **Codex/Claude 识别**：当 Hermes 调用 `codex exec` 或 `claude -p` 时，自动切到 `syncing` 状态，播放独立旋转动画；子代理结束后自动恢复

完整改动清单见 [DEVLOG](./docs/DEVLOG.md)。

---

## 🎬 办公室里的 6 种状态

角色会根据 Hermes 正在做的事自动走到对应位置：

| 状态 | 你在看什么 | Hermes 在做什么 |
|------|-----------|----------------|
| 🛋 **待命** `idle` | 角色躺沙发休息，服务器熄灯 | 会话空闲、任务完成 |
| ✍️ **写作** `writing` | 角色在桌前工作，服务器运行 | LLM 推理、写文件、编辑 |
| 🔍 **调研** `researching` | 同「写作」 | 搜索、抓取网页、读文件 |
| ⚡ **执行** `executing` | 同「写作」 | 跑 shell 命令、编译、测试 |
| 🔄 **同步** `syncing` | 右下角旋转脉冲动画 | **调 Codex CLI / Claude Code**、委派子代理 |
| 🐛 **错误** `error` | Bug 精灵满屏爬 | 工具调用失败、子代理报错 |

> **syncing 是我们新增的状态**。原版没有区分「普通 shell 命令」和「调用 Codex/Claude 子代理」。我们通过 shlex 词法分析器识别命令行中处于可执行位置的 `codex` 或 `claude`，精确匹配（不会把 `echo codex` 误判），让办公室在委派重型任务时显示独立的同步动画。

---

## 🚀 安装（3 步）

> 需要 Python 3.10+ 和已安装的 Hermes。

### 1. 启动办公室后端

```bash
git clone https://github.com/joyparkray/Star-Office-UI-Hermes.git
cd Star-Office-UI-Hermes
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cp state.sample.json state.json
cd backend && python3 app.py
```

打开 `http://127.0.0.1:19000` 应该能看到办公室。

### 2. 安装 Hermes 插件

```bash
mkdir -p ~/.hermes/plugins
ln -s "$PWD/integrations/hermes/plugin/star-office-ui-status" ~/.hermes/plugins/star-office-ui-status
hermes plugins enable star-office-ui-status
```

**重启 Hermes Desktop** 使插件生效。

> CLI/Gateway 用户：把 `integrations/hermes/hooks.example.yaml` 合并到 Hermes hooks 配置，替换路径后重启。

### 3. 验证

```bash
curl http://127.0.0.1:19000/health          # → {"status":"ok"}
python3 scripts/star-office-hook-verify.py   # → 10/10 通过
```

在 Hermes 中发一条消息——办公室里的角色应该自动走到办公桌前。

---

## 🔧 环境变量

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `STAR_OFFICE_URL` | 后端地址 | `http://127.0.0.1:19000` |
| `STAR_OFFICE_API_TOKEN` | 公网部署必设，Hook 与后端共用 | 未设置 |
| `STAR_OFFICE_HOOK_TIMEOUT` | HTTP 超时秒数 | `0.75` |

---

## 🧠 设计思路

**自动而不侵入。** Hook 和插件在 Hermes 事件流中拦截 10 个生命周期节点（LLM 调用前后、工具调用前后、会话起止、子代理生灭），解析后推送到办公室后端。整个过程对 Hermes 透明——不修改核心代码，异常只写 stderr，绝不阻断工作流。

**零内容泄露。** 推送到后端的只有聚合状态（如 `executing`）和固定标签（如 "Terminal activity"）。工具参数、命令内容、文件路径、对话历史一律不传输。

**Codex/Claude 为什么是独立状态。** 调用 Codex 或 Claude Code 子代理时，Hermes 本身在"等待"而非"执行"。如果仍显示 `executing`，用户无法区分「跑了个 npm test」和「委派了一个 5 分钟的重构」。`syncing` 状态 + 独立旋转动画让这个区别一目了然。检测逻辑使用 Python `shlex` 词法分析器，只匹配**可执行位置**的 `codex`/`claude`，支持绝对路径、`sudo`、`env`、`bash -c` 和管道分隔符等真实场景。

---

## ⚠️ 注意事项

- **公网部署务必设置 `STAR_OFFICE_API_TOKEN`**，否则 `/set_state` 无鉴权
- 插件仅 Hermes Desktop 可用（`HERMES_DESKTOP=1`），CLI/Gateway 需配 shell hooks
- 连续快速对话时 idle 状态可能来不及显示，这不是 bug

详见 [`docs/hermes-troubleshooting.md`](./docs/hermes-troubleshooting.md)。

---

## 📁 项目结构

```text
Star-Office-UI-Hermes/
├── backend/app.py                          # Flask 后端
├── frontend/                               # Phaser 像素前端
├── integrations/hermes/
│   ├── star_office_hook.py                 # 核心适配器（Hook & Plugin 共用）
│   ├── hooks.example.yaml                  # CLI/Gateway 配置模板
│   └── plugin/star-office-ui-status/       # Desktop 插件
├── scripts/star-office-hook-verify.py      # 状态机验证脚本
├── docs/
│   ├── HERMES_INTEGRATION.md               # 英文集成指南
│   ├── DEVLOG.md                           # 开发记录
│   ├── hermes-animations.md               # 动画映射参考
│   └── hermes-troubleshooting.md           # 排错指南
├── tests/                                  # 单元测试（35 条）
└── README.md
```

---

## 📄 许可

代码 MIT（同上游）· 美术资产禁止商用（来源 [LimeZu](https://limezu.itch.io/)）

---

## English

A **Hermes-integrated fork** of [Star Office UI](https://github.com/ringhyacinth/Star-Office-UI) — a pixel-art office dashboard that reflects your AI agent's real-time activity. Zero Agent-side configuration.

**What's different:** Instead of manually calling `set_state.py`, a Hermes plugin and shell-hook adapter automatically capture 10 lifecycle events and map them to 6 animated states. A dedicated `syncing` state with shlex-based detection handles Codex/Claude CLI invocations separately from regular shell commands.

| State | Visual |
|-------|--------|
| `idle` | Lounging on sofa |
| `writing` / `researching` / `executing` | Working at desk |
| `syncing` | Rotating sprite — **Codex/Claude sub-agents** |
| `error` | Bug crawling across screen |

**Quick Start:** Clone → start backend → install Hermes plugin → restart Desktop. See Chinese guide above. English details in [`docs/HERMES_INTEGRATION.md`](./docs/HERMES_INTEGRATION.md).

**Key properties:** Fail-open · Zero content leakage · Thread/process-safe atomic writes · Concurrent session aware.
