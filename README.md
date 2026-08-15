<div align="center">

# ASTRA · 星辰

### 星图导航引擎 —— 面向 AI 攻防全链路的状态空间搜索与决策系统

ASTRA 不定义角色，不预设工作流。给定一个起点与一个目标，它在未知状态空间中搜索路径。
AI 漏洞挖掘与渗透测试只是这类问题的第一片已证明的星域。

</div>

## 是什么

安全攻防本质上是在近乎无限的状态空间中做**有向搜索**：

- **起点（Origin）**：已知（目标地址、目标系统、靶场题目）
- **目标（Goal）**：明确（拿到 flag、定位漏洞、完成利用）
- **路径（Path）**：未知

这一结构与漏洞研究、CTF、二进制分析并无不同。ASTRA 为这一类问题而生。

引擎基于**星图（Star Chart）**架构，用显式的星记-航向图支撑协作探索，三种原语足矣：

| 原语 | 含义 |
|------|------|
| **星记（Stellar）** | 一条已确认的客观发现，写入星图 |
| **航向（Bearing）** | 一个声明但尚未执行的探索方向 |
| **指引（Guidance）** | 任意时刻注入的人类判断，星探下次读取时吸收 |

星图从 `origin` 向 `goal` 生长：每颗新星记都是一块踏脚石，每个航向都是一步迈向未知。

星探（Scout）运行 OODA 循环——观察整张星图、定位当前态势、决定下一批航向、行动探索——并把发现写回为新的星记。
星探没有固定角色，任务由星图当前状态在运行时生成，而非来自预定义岗位说明。

## 关键机制

### 星尘记忆（Stardust Memory）—— 上下文管理

传统智能体把整张图的历史无差别地塞进每次推理。ASTRA 对上下文做三层治理：

- **焦点子图（Focus）**：每次任务只内联与当前航向、目标最相关的星记与航向（相关度 + 时间近度 + 引用链），并受 `context_budget` 硬上限约束；完整星图仅作文件引用保留
- **摘要记忆（Epitome）**：超出预算的旧星记由轻量模型压缩为摘要星记，prompt 只呈现摘要
- **零膨胀**：内联量有硬上限，不再随图线性增长

### 双星决策（Dual-star Decision）—— 批判性独立审查

单一智能体的自问自答容易方向漂移。ASTRA 对关键决策引入对抗式双星机制：

- **质询（Challenge）**：独立星探对定航提案（宣布完成、新航向）与低置信巡猎产出做批判性审查，输出反驳意见与置信度评估
- **裁决（Verdict）**：综合提案与质询做最终决策，内置目标对齐评估，通过才写入星图

质询与裁决使用轻量模型，只对关键节点触发，成本可控。

## 任务类型

| 任务 | 做什么 | 产出 |
|------|--------|------|
| **首探（Probe）** | 项目启动时直接尝试解题 | 星记 + 可能的归航 |
| **定航（Navigate）** | 读整张星图：目标达成了吗？下一步探索什么？ | 归航 / 新航向 / 无操作 |
| **巡猎（Patrol）** | 认领一个航向，执行探索，报告发现 | 一条星记 |
| **质询（Challenge）** | 对关键提案做批判审查 | 反驳 + 置信度 |
| **裁决（Verdict）** | 综合提案与质询，判定是否写回星图 | 终裁 |

## 快速开始

前置：macOS / Linux / Windows，Python ≥ 3.12，Docker（可选，local 模式不需要）。

```bash
cp dispatch.example.yaml dispatch.yaml   # 填写模型端点与密钥
uv run --project astra astra serve
uv run --project astra astra dispatch --config dispatch.yaml
```

### 双执行模式

- **Docker 模式**（默认）：`runtime.execution: docker`，领航在每项目容器中 exec 星探 CLI
- **local 模式**：`runtime.execution: local`，星探 CLI 直接以宿主进程运行，无 Docker 依赖——托管平台与离线环境首选

### 靶场接入（astra-runner）

`container/astra_runner/` 提供评测平台编排器，负责题目容器的启动/关闭、逐题创建 ASTRA 项目、从星图收集 flag 统一提交（幂等）并输出得分报告：

```bash
# 本地演练（需先连接平台下发的 VPN，并配置凭证）
BENCHMARK_TOKEN=xxx BENCHMARK_BASE_URL=https://... python3 container/astra_runner/runner.py

# 托管模式：构建镜像后直接运行（ENTRYPOINT 已指向 runner）
docker build -f container/Dockerfile -t astra-runner .
docker save astra-runner:latest | gzip > agent.tar.gz   # 按平台规范上传
```

镜像内已内置 ASTRA 引擎、模型 CLI 与 Kali 工具链；通过环境变量配置模型（`ANTHROPIC_MODEL/ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN` 等）。

### 测试

```bash
uv run --project astra --group dev pytest
```

## 安全声明

ASTRA 是通用问题求解引擎。尽管它支持渗透测试、CTF 求解、安全评估与漏洞研究等工作流，仅限在获得明确授权的环境中使用。未经许可的安全测试可能违法并造成损害，使用者须自行承担全部责任。

## License

GNU AGPLv3
