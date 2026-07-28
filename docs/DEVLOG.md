# Star Office UI Hermes — 开发记录

## 2026-07-26: v2.4.1 — on_session_start idle 推送修复 + 文档发布

**改动**：
- `integrations/hermes/star_office_hook.py`：`run()` → `reserve()` 新增守卫，`on_session_start` + idle 组合跳过推送（2行）
- `tests/test_hermes_hook.py`：新增 `test_session_start_does_not_push_idle_state`（4行）
- `README.md`：完全重写——差异清单、架构图、动画表、安装指南、设计决策、已修复 Bug、English
- 新增 `docs/hermes-animations.md`：6 种状态→Phaser 动画映射参考
- 新增 `docs/hermes-troubleshooting.md`：分层诊断指南（Star Office → Hook → Plugin → Hermes）
- 新增 `scripts/star-office-hook-verify.py`：状态机 10 项验证脚本

**根因**：`on_session_start` 无条件推送 idle 到后端，每次新 session（含 cron）将 state.json 重置为 idle。

**修复**：在 `reserve()` 闭包 `last_push` dedup 检查之后增加守卫——`hook_event_name == "on_session_start"` 且 `state == "idle"` 时返回 None 跳过推送。需重启 Hermes Desktop 使插件加载新代码。

**测试**：全部 35 条通过（含新增）。

---

## 2026-07-26: v2.4.0 — orphan session cleanup + sync animation detection

**已提交（commit a48d2a7）**：
- Schema v2 迁移：旧条目自动清除，防止测试残留永久污染 display_state()
- Orphan child session 清理：subagent_stop 删除 sessions[child_id]
- 启动状态重置：reset_working_state_on_startup()
- WORKING_STATES 扩展：加入 syncing 支持 TTL 恢复
- 字段映射修复：plugin _payload() 中 args → tool_input

---

## 2026-07-26: feat — safe Hermes activity telemetry

**已提交（commit 57eb3eb）**：
- backend/app.py：/set_state 接收 activity 结构，/status 返回 activityKind/Label/recentEvents
- backend/app.py：STATE_LOCK 线程安全、原子写入
- 后端验证：validate_activity() schema 校验

---

## 2026-07-26: chore — mark Hermes integration v1.1.0

**已提交（commit 07abcf9）**：
- 版本标记与文档更新

---

## 2026-07-26: fix — support Hermes Desktop status plugin

**已提交（commit cb18728）**：
- 新增 integrations/hermes/plugin/star-office-ui-status/
- plugin.yaml、__init__.py（10 个生命周期观察者）

---

## 2026-07-26: feat — integrate Hermes status hooks

**已提交（commit 461a4c3）**：
- 新增 integrations/hermes/star_office_hook.py（核心适配器，595行）
- 新增 integrations/hermes/hooks.example.yaml（CLI/Gateway 配置）
- 新增 docs/HERMES_INTEGRATION.md（英文集成指南）
- 新增 tests/test_hermes_hook.py、test_hermes_plugin.py、test_state_api.py
- 新增 set_state.py 测试辅助
- 修改 backend/app.py：/set_state 端点扩展、auto-idle TTL、安全解析
- 修改 frontend/game.js：sync 动画帧数动态读取
