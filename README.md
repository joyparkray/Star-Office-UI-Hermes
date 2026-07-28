# Star Office UI — Hermes 集成分支

🌐 Language: **中文** | [English](#english)

> 将 [Hermes](https://hermes-agent.nousresearch.com) 的工作状态实时映射到像素风办公室看板。无需修改 Hermes 核心代码，无需 Agent 侧配置。

基于 [ringhyacinth/Star-Office-UI](https://github.com/ringhyacinth/Star-Office-UI)（上游 v2.4.0），保留原版全部功能，新增完整的 Hermes 状态自动同步层。

---

## 与原版的区别

### 总体思路

原版面向 [OpenClaw](https://github.com/openclaw/openclaw)，需要在 Agent 的 SOUL.md 中配置规则并手动调用 `set_state.py` 驱动状态变化。本分支让 **Hermes 用户无需任何 Agent 侧配置**——办公室看板自动反映 Hermes 实时在做什么。

**实现方式**：通过 Hermes 生命周期事件钩子（shell hooks）和桌面插件（Python plugin），在 10 个关键节点拦截事件，映射为 6 种标准状态，推送到 Star Office 后端驱动像素动画。

### 改动清单

#### 新增文件（9 个）

| 文件 | 行数 | 说明 |
|------|------|------|
| `integrations/hermes/star_office_hook.py` | 635 | **核心适配器**。纯 stdlib，接收 Hermes 生命周期事件 JSON，维护跨 session 并发状态机，分类工具活动类型（llm/terminal/files/web/delegation），检测 Codex/Claude 子代理命令，HTTP 推送到后端。fail-open：异常仅输出 stderr 并 exit 0。 |
| `integrations/hermes/plugin/star-office-ui-status/__init__.py` | 106 | **Hermes Desktop 插件**。Desktop 的 `tui_gateway` 不注册 shell hooks，由本插件注册 10 个生命周期观察者，回调中剥离敏感内容后委托 bridge。`HERMES_DESKTOP=1` 门控。 |
| `integrations/hermes/plugin/star-office-ui-status/plugin.yaml` | 3 | 插件元数据 |
| `integrations/hermes/hooks.example.yaml` | 33 | CLI/Gateway 用户的 shell hooks 配置模板 |
| `docs/HERMES_INTEGRATION.md` | 70 | 英文集成指南（安装、鉴权、排错、回滚） |
| `docs/hermes-animations.md` | 75 | 6 种状态→动画映射参考（附 curl 测试命令） |
| `docs/hermes-troubleshooting.md` | 346 | 系统排查指南（分层诊断：Star Office → Hook → Plugin → Hermes） |
| `scripts/star-office-hook-verify.py` | 114 | 状态机验证脚本：10 项自动化检查（孤儿清理、并发工具、sync 检测、启动覆盖） |
| `tests/test_hermes_hook.py` | 567 | Hook 单元测试（状态映射、并发、去重、生命周期、命令解析，35 条） |

#### 新增测试文件（3 个）

| 文件 | 行数 |
|------|------|
| `tests/test_hermes_plugin.py` | 149 |
| `tests/test_state_api.py` | 229 |
| `tests/test_set_state_helper.py` | 47 |

#### 修改文件（5 个）

| 文件 | 改动概要 |
|------|---------|
| `backend/app.py` | **活动遥测**：`/set_state` 接收结构化 `activity` 字段（activityKind/activityLabel/activeSubagents/recentEvents），经严格校验后由 `/status` 返回。**线程安全**：`STATE_LOCK` 保护原子读写。**启动重置**：`reset_working_state_on_startup()` 清除异常退出后的残留工作状态。**TTL 扩展**：`syncing` 加入 `WORKING_STATES` 以支持超时恢复。**安全解析**：`parse_backend_host()` 防 SSRF。 |
| `frontend/game.js` | 同步动画帧数从硬编码 52 帧改为动态读取 spritesheet，新增 `syncAnimPlayable` 守卫 |
| `frontend/index.html` | 微调 |
| `set_state.py` | 保留 desktop-pet 兼容的 `receiving`/`replying`，后端 API 仅接受 6 种标准状态 |
| `README.md` 等 | 增加 Hermes 集成说明 |

### 架构图

```
Hermes 生命周期事件
  ├── CLI / Gateway → shell hooks → star_office_hook.py (子进程, HTTP)
  └── Desktop        → Python 插件 → star_office_hook.py (in-process)
                                        │
                          ┌─────────────┘
                          ▼
              star_office_hook.py
              ├── 解析事件 + 工具分类
              ├── 跨 session 并发状态机 (fcntl 锁)
              ├── Codex/Claude 识别 (shlex lexer)
              └── POST /set_state ──► backend/app.py
                                          │
                              ┌───────────┘
                              ▼
                        state.json (原子写入)
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
        GET /status (2s 轮询)           GET /status (外部查询)
              │
              ▼
        frontend/game.js (Phaser)
        └── 6 种动画 + 气泡 + 多人协作
```

### 关键设计决策

| 决策 | 说明 |
|------|------|
| **Fail-open** | Hook/插件异常仅输出诊断信息，绝不阻断 Hermes 工作流 |
| **零内容泄露** | 仅传输聚合状态和固定标签，不传输工具参数、命令、路径、对话内容 |
| **双通道覆盖** | CLI/Gateway 走 shell hooks，Desktop 走 Python 插件 |
| **线程 + 进程安全** | `STATE_LOCK`(RLock) + `fcntl.flock`(LOCK_EX) + `mkstemp`/`os.replace`/`fsync` 原子写入 |
| **并发感知** | 多 session 按优先级确定性排序：error > syncing > executing > researching > writing > idle |
| **Codex/Claude 识别** | `shlex` 词法分析器仅在可执行位置匹配，排除 `echo codex` 等误报 |

### 已修复的 Bug（v2.4.0+）

1. **孤儿子代理污染** — `display_state()` 遍历全部 session，子代理工具调用产生独立条目但清理事件以父 session ID 触发，永久残留。修复：`subagent_stop` 清理 child session。
2. **`syncing` 无法 TTL 恢复** — `WORKING_STATES` 不含 `syncing`，卡住后永远无法超时回退。修复：加入 `syncing`。
3. **插件 `args`/`tool_input` 字段不匹配** — Hermes 传 `kwargs["args"]` 但 bridge 期望 `payload.tool_input`。修复：插件映射 `key = "tool_input" if field == "args" else field`。
4. **旧 schema 残留污染** — 无 `schema_version` 的旧条目（如测试留下的 `s1` fallback queue）永久污染 `display_state()`。修复：schema v2 迁移，旧条目自动清除。
5. **`on_session_start` 推送 idle** — 每次新 session（含 cron）将后端状态重置为 idle。修复（2026-07-26）：`reserve()` 守卫，`on_session_start` + idle 组合跳过推送。

---

## 🎬 动画状态

| 状态 | 英文标识 | 位置 | 动画 | Hermes 触发场景 |
|------|----------|------|------|----------------|
| 待命 | `idle` | 🛋 沙发 | 角色躺沙发（48帧），服务器熄灯 | 会话空闲、任务完成 |
| 写作 | `writing` | 💻 办公桌 | 桌前工作（192帧），服务器运行 | LLM 调用、写文件、编辑 |
| 调研 | `researching` | 💻 办公桌 | 同 writing | 搜索、抓取、read_file |
| 执行 | `executing` | 💻 办公桌 | 同 writing | shell 命令、编译、测试 |
| 同步 | `syncing` | 右下角 | 旋转/脉冲精灵（v3 spritesheet），服务器运行 | Codex/Claude 子代理、委派 |
| 错误 | `error` | 🐛 错误区 | Bug 精灵左右爬行（96帧），服务器运行 | 工具调用失败 |

**附加动画**：服务器机柜 40 帧灯光 | 咖啡机 96 帧循环 | 植物 16 种造型（点击切换）| 海报 32 种 | 小猫独立气泡（18s 间隔）

---

## 🚀 安装与使用

### 前提

- **Python 3.10+**（`X \| Y` union type 语法）
- **Hermes**（v0.18.2 验证通过）

### 第一步：启动 Star Office 后端

```bash
git clone https://github.com/joyparkray/Star-Office-UI-Hermes.git
cd Star-Office-UI-Hermes

python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

cp state.sample.json state.json
cd backend && python3 app.py
# 打开 http://127.0.0.1:19000
```

### 第二步：配置 Hermes 集成

#### Desktop 用户（推荐）

```bash
# 安装插件（符号链接）
mkdir -p ~/.hermes/plugins
ln -s "$PWD/integrations/hermes/plugin/star-office-ui-status" ~/.hermes/plugins/star-office-ui-status
hermes plugins enable star-office-ui-status
# 重启 Hermes Desktop
```

验证：`hermes plugins list | grep star` → 应显示 enabled。

#### CLI/Gateway 用户

将 `integrations/hermes/hooks.example.yaml` 内容合并到 Hermes hooks 配置，替换绝对路径后重启。

### 第三步：验证

```bash
curl -s http://127.0.0.1:19000/health     # → {"status":"ok"}
curl -s http://127.0.0.1:19000/status      # → 查看状态与活动遥测
python3 scripts/star-office-hook-verify.py  # → 状态机 10 项检查
```

### 环境变量

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `STAR_OFFICE_URL` | 后端地址 | `http://127.0.0.1:19000` |
| `STAR_OFFICE_API_TOKEN` | Bearer Token（公网部署必设） | 未设置 |
| `STAR_OFFICE_HOOK_TIMEOUT` | HTTP 超时（0.05–10s） | `0.75` |
| `STAR_OFFICE_HOOK_STATE_FILE` | Hook 协调文件 | 系统临时目录 |
| `STAR_BACKEND_HOST` | 后端绑定地址 | `0.0.0.0` |

---

## ⚠️ 注意事项

**安全性**：公网部署必须设置 `STAR_OFFICE_API_TOKEN`，不要将 Token 写入命令行或提交到仓库。

**已知限制**：
1. **连续对话中 idle 不可见** — `post_llm_call` 的 idle 推送被下一条 `pre_llm_call` 的 writing 立即覆盖，前端 2s 轮询来不及捕捉
2. **Desktop 插件专属** — 插件仅在 `HERMES_DESKTOP=1` 时生效，CLI/Gateway 需单独配置 shell hooks
3. **不推送敏感内容** — 只传输聚合状态和固定标签

**排错**：见 [`docs/hermes-troubleshooting.md`](./docs/hermes-troubleshooting.md)（分层诊断指南）。

---

## 📁 项目结构（Hermes 集成相关）

```text
Star-Office-UI-Hermes/
├── backend/app.py                              # Flask 后端（活动遥测 API）
├── frontend/
│   ├── index.html / game.js / layout.js        # Phaser 前端
├── integrations/hermes/
│   ├── star_office_hook.py                     # 核心适配器
│   ├── hooks.example.yaml                      # Shell hooks 模板
│   └── plugin/star-office-ui-status/           # Desktop 插件
├── scripts/
│   └── star-office-hook-verify.py              # 状态机验证脚本
├── docs/
│   ├── HERMES_INTEGRATION.md                   # 英文集成指南
│   ├── hermes-animations.md                    # 动画映射参考
│   └── hermes-troubleshooting.md               # 分层排查指南
├── tests/
│   ├── test_hermes_hook.py / test_hermes_plugin.py
│   ├── test_state_api.py / test_set_state_helper.py
├── set_state.py / state.sample.json
└── README.md
```

---

## 📄 许可

- 代码：MIT（同上游）
- 美术资产：禁止商用（来源 LimeZu）

---

## English

**Hermes-integrated fork** of [Star Office UI](https://github.com/ringhyacinth/Star-Office-UI). Automatically captures Hermes lifecycle events (LLM calls, tool execution, sub-agent delegation, errors) and maps them to 6 animated pixel-art office states — no Agent-side configuration required.

| State | Visual |
|-------|--------|
| `idle` | Character lounging on sofa |
| `writing` / `researching` / `executing` | Working at desk |
| `syncing` | Rotating/pulsing sprite (Codex/Claude sub-agents) |
| `error` | Bug sprite crawling across screen |

**Quick Start**: See Chinese guide above. English details in [`docs/HERMES_INTEGRATION.md`](./docs/HERMES_INTEGRATION.md).

**Key properties**: Fail-open · Zero content leakage · Dual delivery (shell hooks + Python plugin) · Thread/process-safe · Concurrent session aware · Codex/Claude detection via shlex lexer.
