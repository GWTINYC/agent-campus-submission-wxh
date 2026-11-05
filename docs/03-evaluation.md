# 03 评估、实际结果与限制

## 运行方式与证据分层

```sh
python -m scripts.verify
python -m scripts.evaluate --provider mock
python -m scripts.evaluate --provider deepseek
```

真实模型命令需先配置 DEEPSEEK_API_KEY；默认 deepseek-flash。所有业务资料和故障为合成，只有模型请求是真实 API 调用。实际输出与逐步 Trace 同存一个 JSON：包含不可变请求、审核和保存结果、每步工具参数和返回、后续动作、简短依据、模型响应 ID 与 token 用量。

- [自动化测试汇总](../examples/test-results.json) 与 [原始测试输出](../examples/test-results.txt)：业务不变量、恶意动作、并发、步数上限和 HTTP 适配器契约；不计为真实模型效果。
- [离线评估汇总](../examples/mock/evaluation.json)：12 组基础输入，加 2 次重复、1 次换占用后提交，共 15 次运行。Mock 只验证流程。
- [真实模型评估汇总](../examples/deepseek/evaluation.json)：同组输入与实际 API 输出；以该文件记录的运行时间、通过数为准。

## 必须验收项与实际输出

2026-11-05 最终本地验证：27 项自动化测试全部通过；离线 15/15、DeepSeek 15/15 符合预期。最终真实评估从北京时间 12:41:49 开始，共 74 次模型请求、59 次业务工具调用，API 报告合计 58,235 tokens（包括各次请求的重复上下文，不是去重后的文本长度）。这是合成案例回归结果，不是生产成功率。

| 场景 | 输入 | 真实输出与 Trace | 核对项 |
| --- | --- | --- | --- |
| 正常排期 | [normal](../examples/inputs/normal.json) | [normal](../examples/deepseek/normal.json) | approved、原 18:00、saved、数据库 1 份 |
| 信息缺失 | [missing](../examples/inputs/missing.json) | [missing](../examples/deepseek/missing.json) | 补查失败后返回版权字段缺口，不保存 |
| 补查成功 | [supplement](../examples/inputs/supplement.json) | [supplement](../examples/deepseek/supplement.json) | 归档补齐后审核，通过后排期 |
| 规则风险 | [risk](../examples/inputs/risk.json) | [risk](../examples/deepseek/risk.json) | blocked，无新增草稿 |
| 人工复核 | [manual_review](../examples/inputs/manual_review.json) | [manual_review](../examples/deepseek/manual_review.json) | needs_confirmation，风险原因可见 |
| 时段冲突 | [conflict](../examples/inputs/conflict.json) | [conflict](../examples/deepseek/conflict.json) | 原 18:00 不变；建议 19:00；effective_slot=null |
| 用户明确确认 | [confirmed](../examples/inputs/confirmed.json) | [confirmed](../examples/deepseek/confirmed.json) | 用户输入 confirmed_slot=19:00 后才有确认时段 |
| 重复提交 | normal / conflict，同一数据库 | [normal_repeat](../examples/deepseek/normal_repeat.json)、[conflict_repeat](../examples/deepseek/conflict_repeat.json) | existing，ID 不变，database_draft_count=1 |
| 更换占用后重复提交 | conflict 原请求，工具侧占用从 18:00 改为 19:00 | [conflict_moved](../examples/deepseek/conflict_moved.json) | 改回原 18:00，updated，仍同 ID、1 份草稿 |
| 保存失败 | [save_failure](../examples/inputs/save_failure.json) | [save_failure](../examples/deepseek/save_failure.json) | failed，未生成 draft_id，数据库计数 0 |
| 查询暂时失败 | [query_retry](../examples/inputs/query_retry.json) | [query_retry](../examples/deepseek/query_retry.json) | 模型观察错误后再查询，保留已完成审核 |
| 保存时竞争 | [race](../examples/inputs/race.json) | [race](../examples/deepseek/race.json) | 保存冲突后重新查询，再保存待确认替代方案 |
| 无时段 | [no_slots](../examples/inputs/no_slots.json) | [no_slots](../examples/deepseek/no_slots.json) | incomplete，无新增草稿 |
| 独立占用变体 | [changed_occupancy](../examples/inputs/changed_occupancy.json) | [changed_occupancy](../examples/deepseek/changed_occupancy.json) | 19:00 占用不影响原 18:00 可用 |

同名离线结果位于 examples/mock/；其 evidence=flow_only。真实结果为 evidence=real_model，不允许模型失败后悄悄替换为 Mock。保存成功、任务完成与发布状态分别核对，所有 published=false。

## 动态决策证据

1. normal：模型观察审核通过、18:00 可用后选择保存原时段，收到成功返回后 finish。
2. conflict：模型观察 18:00 已占用后选择 19:00，工具保存为待确认建议；没有擅自修改硬性要求。
3. race：第一次保存收到 slot_conflict，模型重新 query_slots，观察新的可用集合后再次选择保存。
4. save_failure：收到非暂时 save_unavailable 后，模型停止，结果保留失败。

上述动作取自实际 Trace，未预设真实模型路径。步骤顺序可以因模型决策而不同；评价业务约束和可观察结果，而非与 Mock 序列逐字一致。

## 发现的失败与修正

修正前完整真实评估为 **14/15**。信息缺失案例中，模型读取主资料、补查归档失败后直接 finish，没有先调用 check_rules；程序把该路径误归类为 failed。

保留 [原始失败 Trace](../examples/regressions/missing-before-fix.json) 和 [修正前评估汇总](../examples/regressions/evaluation-before-missing-fix.json)。修正后从实际已读到的缺失字段判断 needs_information，保留 review_status=not_checked，避免虚报审核完成。增加 test_model_can_finish_after_failed_supplement_before_rule_check 后重新运行验证。

此外，原位更新重复请求与成功结果保留来自实现审查发现的边界，分别有业务测试。并发测试使用两个独立 SQLite 连接竞争同一频道时段，断言仅一个事务成功。离线断言覆盖模型直接越过审核、注入确认字段和达到步数上限的情形。

## 已知限制

- 当前真实评估是小规模合成案例的一次最终回归，不能把通过比例解释为生产准确率。没有做大规模统计、多模型对比、真实视频审核或长对话评估。
- 审核基于结构化元数据和合成风险标签，不具备图像、视频、语音或法律判断能力。
- 固定一小时时段按频道精确匹配，不支持任意时长区间和跨频道资源；待确认建议不占位，后续确认仍需重新检查。
- 同一需求的草稿会原位更新，不保留草稿修改历史；确认使用新请求修订，旧待确认草稿未自动归档。
- 资料和规则没有版本失效机制；已有草稿不会因资料更新自动撤销，需要重新审核。
- CLI 是可信本地操作入口，confirmed_slot 不具备身份认证与审计签名；没有部署成多人服务。
- 模型 HTTP 错误或无效 JSON 会明确停止，尚未实现限流退避或跨进程任务续跑。
- 可选 OpenAI 兼容入口未做真实服务验证；已验证的真实模型为 DeepSeek。

最终运行计数与时间以 examples/ 下的实际汇总为准；开发过程和用时见 [06-process-log.md](06-process-log.md)。
