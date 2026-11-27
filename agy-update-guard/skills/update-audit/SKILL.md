---
name: update-audit
description: >-
  评估 Antigravity 最新版本更新对本机 antigravity-fork、antigravity-chat-editor 补丁的兼容性，
  检测新版本外审模型（PolicyGuardian / PolicyProctor / LLMClassifier / Skill 扫描过滤）是否增强，
  以及 builtin 内置技能快照差异与模型上下架影响。用户执行 /update-audit、询问是否建议升级，
  或通过 Update Guard 交互卡片发起审查时激活本技能。
---

# Update Audit（Fork/Edit 补丁兼容性与外审强度评估）

当用户触发 `/update-audit`、询问近期 Antigravity 更新是否安全可升，或通过 Update Guard 状态卡片按钮自动发送预采集材料发起评估时，按以下流程执行：

## Step 1：判断是否已附带预采集材料

- 若当前消息中**已经包含** `Audit-Run-ID:` 及完整的预检证据（包括待安装安装包解包实测与二进制审查符号对比），优先直接基于证据输出深度评估；若部分本地插件细节或二进制符号需进一步核实，可按需使用只读工具核查（严禁执行写入、安装或覆盖操作）。
- 若为用户手动输入 `/update-audit`（未附带采集数据），按 Step 2 获取最新状态。

## Step 2：展示状态卡片或运行只读预检采集

1. 若当前环境已加载 `update-guard` MCP 工具，优先调用 `show_update_card`（参数 `{}`，默认在线刷新并触发本地待安装安装包解包预检）内联展示交互卡片，并读取其返回内容。
2. 也可在终端定位本技能文件向上两级的插件根目录 `<plugin_root>`，运行只读预检命令获取 JSON 数据：

```bash
python "<plugin_root>/sidecars/update-sentinel/sentinel.py" --dry-run --audit-latest --json
```

> [!NOTE]
> - 所有采集与解包预检均在临时目录**零额度只读执行**（结果按文件指纹缓存），绝不修改当前安装目录，也不会在后台自动创建对话。

## Step 3：按 5 大核心维度输出中文评估报告

严格围绕以下核心目的给出有实际决策价值的分析：

1. **本地核心补丁兼容性评估（`antigravity-fork` & `antigravity-chat-editor`）**：
   - **`antigravity-fork`**：检查待安装版本的 `language_server.exe` 内嵌前端 ZIP、`conversationForkingEnabled` / `conversationForkingHistoricalStep` 开关及 Go 后端 `CascadeManager.allowForkWithSubagents`（`enable-fork-with-subagents`）试构建是否通过；结合 Changelog 判断会话/子代理派生逻辑是否有变动，说明升级后执行 `fork_toolbar.py --apply` 能否无缝恢复。
   - **`antigravity-chat-editor`**：检查待安装版本的 Electron ASAR Fuse 完整性策略、`dist/main.js` & `dist/preload.js` 桥接点、`dist/languageServer.js` 本地服务能力、`SetCloudCodeURL` 本地转发接口、Protobuf 描述符、聊天气泡/侧栏 DOM 锚点以及压缩摘要（compaction summary / checkpoint）重建链路是否兼容，说明升级后能否通过启动脚本或 `install.py` 恢复。
2. **外审模型、安全护栏与本地 Skill 扫描强度评估**：
   - 结合 `language_server.exe` 二进制对比（`PolicyGuardian`、`PolicyProctor`、`LLMClassifier`、`classifyTrajectory` 轨迹外审、`AuditPostExecStep` 工具后置审计、`SkillScanMode` / `filterBlockedSkills` 自定义技能扫描与封禁、`McpDriftRecord`）、`builtin` 内置技能提示词差异以及 `models.md` 变动，**明确回答“新版本外审模型与安全审查机制是否变强”**，并指出对本地自定义技能（如逆向分析、自动化脚本）和日常开发操作的具体风险。
3. **工作区权限、终端沙箱与模型生命周期评估**：
   - 概括本次版本功能修复、`Auto` 跨仓库/项目外读写策略、敏感配置文件保护、终端沙箱、浏览器策略及模型上下架时间表。
4. **生效范围区分**：
   - **【随客户端本地安装包生效】**：`app.asar` 与 `language_server.exe` 中的新版审查引擎代码、本地补丁被官方文件覆盖、桌面端 UI 与权限预设变更；若 `%LOCALAPPDATA%\antigravity-updater` 已存在待安装 `.exe` 且评级为 `HOLD`，明确提醒**切勿点击“重启更新”或直接重启客户端**。
   - **【随云端服务端开关及模型配置生效】**：服务端实验 flag（如是否激活 `proctor_skill_scan_mode` 或新分类器）、云端外审模型权重及 `models.md` 旧模型下架计划。
5. **安全评级与升级建议结论**：
   - 任一核心补丁（`fork` / `edit`）不兼容，或检测到外审/Skill 扫描过滤机制实质性增强时，应评为 **`HOLD`**；存在需留意的行为边界调整但核心补丁与外审无恶化时评为 **`CAUTION`**；纯常规修复且补丁与外审均无风险时评为 **`SAFE`**。
   - 回复末尾必须保留以下结构块以便归档：

```text
[AUDIT_RESULT]
VERDICT: HOLD | CAUTION | SAFE
HEADLINE: <不超过36字的中文一句话结论>
RISK_DIMENSIONS: <逗号分隔维度名或 NONE>
[/AUDIT_RESULT]
```
