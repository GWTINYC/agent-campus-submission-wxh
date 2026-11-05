# 素材审核与发布排期 Agent（第一题）

帮助内容团队审核合成素材、处理时段冲突并保存可确认的排期草稿。**仅保存草稿，没有发布工具或真实发布接入。**

Python 标准库实现，无 Agent 框架或第三方 Python 依赖。DeepSeek 真实模型通过结构化 JSON 选择动作；工具结果回传模型后再决定下一步。另有明确标注的离线 Mock。

## 评审入口

- [完整题干与统一考核规则](EXAM_QUESTIONS.md)
- [需求与验收标准](docs/01-requirements.md) · [设计与决策循环](docs/02-solution.md)
- [评估与实际运行记录](docs/03-evaluation.md)
- [AI 使用与贡献说明](docs/04-ai-usage.md) · [复盘](docs/05-retrospective.md)
- [按时间记录的做题过程](docs/06-process-log.md)
- 输入在 `examples/inputs/`；实际输出和逐步 Trace 在 `examples/mock/`、`examples/deepseek/`。

## 环境与离线运行

Python 3.11+（本地验证为 3.12.4），Git。离线入口无需密钥、配置或 pip 安装。

```sh
git clone git@github.com:GWTINYC/agent-campus-submission-wxh.git
cd agent-campus-submission-wxh
git checkout q1-material-scheduling
python -m src.cli --provider mock --input examples/inputs/normal.json --db runtime/demo.sqlite --output runtime/normal.json
python -m scripts.verify
python -m scripts.evaluate --provider mock
```

相同命令再次运行，会返回 `save_status: existing`，数据库仍只有一份草稿。`scripts.evaluate` 为每个基础案例创建独立数据库，并在同一数据库上执行重复提交和占用变更；可重复运行，不依赖上次手工运行残留的数据。

Mock 的 `evidence` 是 `flow_only`，只证明流程、存储和约束，不证明真实模型决策能力。

## 真实模型运行

推荐已验证的 DeepSeek 接口。`.env.example` 列出变量；本项目**不会自动读取 `.env` 或个人软件配置**，请在当前终端设置环境变量。

PowerShell：

```powershell
$env:DEEPSEEK_API_KEY = '<你的密钥>'
$env:DEEPSEEK_MODEL = 'deepseek-flash'
python -m src.cli --provider deepseek --input examples/inputs/conflict.json --db runtime/live.sqlite --output runtime/live-conflict.json
python -m scripts.evaluate --provider deepseek
```

macOS / Linux：

```sh
export DEEPSEEK_API_KEY='<你的密钥>'
export DEEPSEEK_MODEL='deepseek-flash'
python -m scripts.evaluate --provider deepseek
```

默认 DeepSeek 地址为 `https://api.deepseek.com`。也提供 `--provider openai`，使用 `OPENAI_API_KEY`、`OPENAI_MODEL` 和可选 `OPENAI_BASE_URL`，要求服务支持 Chat Completions 的 JSON Schema 输出；这一适配器未做真实服务验证。

模型请求出错时明确返回 `incomplete / model_error`，不会自动切换 Mock。每次调用默认超时 90 秒，可用 `--timeout 30` 调整。

## 输入和输出

每次处理一个素材，可重复调用以处理多个素材；共用同一 `--db` 时，已确认草稿之间检查冲突。输入文件包含 `request`，可选 `world_patch` 仅用于合成环境演示和故障注入。

```json
{
  "request": {
    "request_id": "demo-001",
    "material_id": "M01",
    "requested_slot": "2026-11-06T18:00:00+08:00",
    "hard_requirement": true
  }
}
```

时间必须带时区，内部统一为北京时间。可增加 `confirmed_slot` 表达用户已明确确认的替代时间；它来自可信 CLI 输入，模型动作不能设置它。确认属于新请求修订，使用新的 `request_id`。

输出中 `result` 包含审核状态、风险原因、建议时段、待确认项、草稿保存状态和失败原因；`trace` 包含每步动作、参数、工具返回、后续动作及简短依据。`model_response` 仅保存请求 ID、模型名和用量，不含内部思维过程或密钥。

- `completed`：草稿处理完成；`effective_slot` 是草稿内确认时段。
- `needs_confirmation`：有待确认事项；硬性时段冲突时，保存建议草稿，`effective_slot` 为 null。
- `needs_information` / `blocked`：信息不足或风险阻断，不新增排期。
- `failed`：工具或保存失败；`incomplete`：无可用时段、模型错误或达到步数上限。
- `save_status`：`saved` / `existing` / `updated` / `failed` / `not_attempted`，独立于任务状态。
- `published` 始终为 false。

默认最多 12 个模型决策步（含 finish），可设 `--max-steps 1..30`。达到上限明确返回未完成；若此前确已保存，仍如实保留保存状态。CLI 对完成和待确认返回退出码 0，其余返回 2；评估脚本依据预期状态判断，因此“预期保存失败”可以是通过的验收案例。

## 保存与确认语义

相同素材、原时段、硬性要求和确认时段组成需求指纹，独立于传输用的请求 ID。重复执行只保留一个草稿；相同请求 ID 对应不同需求会被拒绝。占用变化后重新审核、查询，并在同一草稿上更新建议；数据库事务与唯一索引避免重复占位。待确认建议不占位，保存和确认时都需要检查当前时段。

合成排期按“频道 + 固定一小时起始时段”判冲突，没有跨频道资源共享、任意时长区间或真实素材内容审核。更完整的边界见评估文档。
