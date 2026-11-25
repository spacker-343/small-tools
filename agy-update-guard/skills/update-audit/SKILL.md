---
name: update-audit
description: >-
  评估 Antigravity 最新版本更新、模型可用性及本机 builtin 内置技能与默认配置快照差异，
  识别工作区权限、终端沙箱与兼容性影响。用户执行 /update-audit、询问是否建议升级，
  或通过 Update Guard 交互卡片发起审查时激活本技能。
---

# Update Audit（Antigravity 版本更新与权限兼容性评估）

当用户触发 `/update-audit`、询问近期 Antigravity 更新的兼容性影响，或通过 Update Guard 状态卡片按钮自动发送预采集材料发起评估时，按以下流程执行：

## Step 1：判断是否已附带预采集材料

- 若当前消息中**已经包含** `Audit-Run-ID:` 与完整的采集材料（来自 Update Guard 交互卡片「🚀 新建对话审查本次更新」或「💬 在当前对话审查」按钮），**直接基于消息内提供的材料完成结构化分析**，无需重复执行外部命令或再次拉取网页。
- 若为用户手动输入 `/update-audit`（未附带采集数据），按 Step 2 获取最新状态。

## Step 2：展示状态卡片或运行只读预检采集

1. 若当前环境已加载 `update-guard` MCP 工具，优先调用 `show_update_card`（参数 `{}`，默认在线刷新）向用户内联展示带操作按钮的 Update Guard 状态卡片，并直接读取其返回内容。
2. 也可在终端定位本技能文件向上两级的插件根目录 `<plugin_root>`，运行只读预检命令获取 JSON 数据：

```bash
python "<plugin_root>/sidecars/update-sentinel/sentinel.py" --dry-run --audit-latest --json
```

> [!NOTE]
> - 所有采集与预检均**零额度只读执行**，不会在后台自动创建新对话，也不触发系统弹窗。

## Step 3：按工程化兼容性框架输出中文评估报告

基于真实采集证据逐项输出：

1. **版本更新与修复摘要**：概括近期新增特性、交互调整与问题修复。
2. **工作区权限、终端沙箱与配置兼容性评估**：
   - `permission_sandbox`：`Auto` 审批策略、跨工作区文件读写范围、终端沙箱与命令执行限制变化；
   - `model_safety_or_deprecation`：模型安全策略、模型可用性及下架/迁移公告（`Will be removed on` / `leaving on`）；
   - `browser_network`：内置浏览器域名允许列表、CSP 与代理配置变化；
   - `builtin_prompt_change`：本机 `~/.gemini/antigravity/builtin` 内置技能与默认配置快照的新增、删除或修改情况；
   - `telemetry_upload`：诊断或遥测上传策略变化。
   结合以上维度说明对日常自动化脚本与多目录开发工作流的兼容性影响。
3. **生效范围区分**：
   - **【客户端本地版本变更（随客户端安装包生效）】**：桌面端 UI、终端沙箱、权限审批预设、自动更新器及内置技能改动；若检测到 `%LOCALAPPDATA%\antigravity-updater` 已缓存待安装 `.exe`，提醒用户暂缓升级时不要点击“重启更新”或直接重启客户端。
   - **【云端模型及服务端配置变更（与本地客户端版本无关）】**：`models.md` 中模型上下架计划、默认推理策略与服务端运行配置调整。
4. **升级建议结论**：
   明确输出 `HOLD`（建议暂缓升级，待确认工作流兼容性）、`CAUTION`（建议关注变更项后升级）或 `SAFE`（常规功能与修复升级），并在回复末尾附上以下可解析结果块便于归档：

```text
[AUDIT_RESULT]
VERDICT: HOLD | CAUTION | SAFE
HEADLINE: <不超过36字的中文一句话结论>
RISK_DIMENSIONS: <逗号分隔维度名或 NONE>
[/AUDIT_RESULT]
```
