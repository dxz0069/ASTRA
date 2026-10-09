# ASTRA 高价值 Finding 核验

Execute 将已观察到的事实写回 `description`，将值得独立复核的线索放在 `finding` 中：

```json
{
  "accepted": true,
  "data": {
    "description": "本步骤的客观观察及证据文件位置",
    "finding": {
      "description": "具体目标、待复核观察、证据位置及预期任务收益",
      "high_value": true
    }
  }
}
```

## 执行流程

1. 服务端在同一事务中保存来源 Fact、Finding 和开放的 Strike Step。Finding 此时为 `pending`。
2. 调度器优先尝试 Strike。多条线索按派发次数、创建时间轮转，防止一条持续失败的任务占住队列。
3. Strike 使用独立 Pi 会话和核验提示词，通过现有 minimal 工具集做可复现检查。
4. Strike 输出明确的 `confirmed`、`refuted` 或 `blocked`，以及非空核验摘要。Fact 和 Finding 状态在同一事务中更新。

无效输出、超时或写回失败会释放 Step，保留 `pending` 并进入重试冷却。`blocked` 表示本次核验已记录阻碍，不等于确认线索。

## 配置

`tasks.strike.timeout` 默认 120 秒。可配置 `task_types: [strike]` 的 Pi Worker，完整示例见 `dispatch.example.yaml`。

存在 Strike Worker 时，核验任务等待该 Worker 可用；其他任务仍可推进。未配置 Strike Worker 时，由 Execute Worker 使用独立的 Strike 调用路径完成核验。

## 兼容与记录

- 旧字符串 Finding 和未标记高价值的对象 Finding 仍可写入，不自动升级。
- 重复 Fact 携带 Finding 时复用已有 Fact，保留这条发现和来源关系。
- 同项目中描述经大小写与空白标准化后相同的待核验高价值线索，只创建一条核验任务。
- 前端与 YAML/时间线导出显示状态、来源、核验步骤、核验 Fact 和摘要；旧数据库启动时补齐字段。
- Decide 不关闭 Strike Step，待核验 Finding 不能作为已确认的完成依据。

这条链路已经过 Mock、API 和调度测试；真实模型的核验质量和比赛收益需要在比赛靶场中对比验证。
