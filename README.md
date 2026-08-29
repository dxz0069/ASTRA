<div align="center">

# ASTRA · 星辰

### FGS 导航引擎 —— 面向 AI 攻防全链路的状态空间搜索与决策系统

ASTRA 不定义角色，不预设工作流。给定一个起点与一个目标，它在未知状态空间中搜索路径。
AI 漏洞挖掘与渗透测试只是这类问题的第一片已证明的星域。

</div>

## 是什么

安全攻防本质上是在近乎无限的状态空间中做**有向搜索**：

- **起点（Origin）**：已知（目标地址、目标系统、靶场题目）
- **目标（Goal）**：明确（拿到 flag、定位漏洞、完成利用）
- **路径（Path）**：未知

这一结构与漏洞研究、CTF、二进制分析并无不同。ASTRA 为这一类问题而生。

引擎基于**星图协议**（FGS 图：Fact–Goal–Step Graph）：所有状态外部化为一张
append-only 的图，图是唯一的共享记忆——进程可死可换，上下文不腐烂。五种原语足矣：

命名取自中国古代星官（北斗导航体系）：**玉衡权衡定向，摇光出击巡天；星官沿途记天枢、
偶见客星、辅星随行、逐宿而驻，终抵北辰。**

| 原语 | 古星名出处 | 含义 |
|------|-----------|------|
| **天枢（Fact）** | 北斗第一星，枢者根基 | 一条已确认的客观发现（当前世界状态；负结果同权） |
| **北辰（Goal）** | 北极星古称，众星共之 | 完成条件 = 搜索的终止条件；可挂动态**星宿（SubGoal，二十八宿为驿站）** |
| **斗柄（Step）** | 斗柄东指，天下皆春 | 从既有天枢出发、预期产出新天枢的因果行动；可关闭并留痕 |
| **客星（Finding）** | 史记载新现天体为客星 | 搜索过程的沿途产出（如漏洞）——不期而至的一等公民 |
| **辅星（Hint）** | 开阳旁辅星，助目辨星 | 任意时刻注入的人类判断，星官下次读取时吸收 |

## 双活动架构（Decide & Execute）

引擎只有两类活动，它们是**同一个运行器注入不同提示词与工具**的实例，而非固定角色的子智能体：

- **玉衡（Decide）**：北斗第五星，衡者权衡——串行、事件触发（任务开始或图变化）、
  每次从干净上下文起跑。只拥有图操作权限：判定北辰是否抵达（终止搜索）、开辟斗柄
  （必附"预期产出什么天枢"）、关闭失效斗柄（留痕防重开死路）、增删星宿。
- **摇光（Execute）**：斗柄之梢，光芒出击——并发多实例。拥有世界工具
  （read/bash/edit/write）与唯一的 `submit_fact` 入图闸口：确认责任在星官自证，
  跑过命令、亲眼看到输出才写回，可携一颗客星。

没有审查环、没有记忆压缩、没有预设技能——图本身是唯一的事实账本。

## 关键机制

### 焦点上下文（Focus Context）

传统智能体把整张图的历史无差别地塞进每次推理。ASTRA 对上下文做预算治理：

- 每次任务只内联与当前北辰最相关的天枢与斗柄（相关度 + 语义召回 + 时间近度 + 图距加成，
  凭据级关键天枢钉住保底），受 `context_budget` 硬上限约束
- 完整星图仅作文件引用提供（星官自行按需读取）
- **零膨胀**：内联量有硬上限，不随图规模线性增长

### 执行底座：PI

执行底座只有 [pi-coding-agent](https://github.com/badlogic/pi-mono)（Node）——选它不是因为强，
而是因为它**最原始、完全可控**。内置提示词极短且与安全任务零耦合：引擎是通用任务求解引擎，
任务知识只存在于任务描述层。

## 任务类型

| 任务 | 做什么 | 产出 |
|------|--------|------|
| **角宿（Bootstrap）** | 二十八宿第一宿——项目启动时的首次摇光：直接从起点开工 | 流式天枢 + 可能的归航 |
| **玉衡（Decide）** | 读星图：北辰抵达了吗？下一批斗柄怎么指？ | 归航 / 新斗柄+关斗柄+星宿 / 无操作 |
| **摇光（Execute）** | 认领一条斗柄，干完它 | 一条天枢（submit_fact，可携客星） |

## 快速开始

前置：macOS / Linux / Windows，Python ≥ 3.12，Node ≥ 18（pi CLI），Docker（可选，local 模式不需要）。

```bash
npm install -g @mariozechner/pi-coding-agent
cp dispatch.example.yaml dispatch.yaml   # 填写 PI_* 模型端点与密钥
uv run --project astra astra serve
uv run --project astra astra dispatch --config dispatch.yaml
```

### 双执行模式

- **Docker 模式**（默认）：`runtime.execution: docker`，调度器在每项目容器中 exec 执行器 CLI
- **local 模式**：`runtime.execution: local`，执行器 CLI 直接以宿主进程运行，无 Docker 依赖——托管平台与离线环境首选

### 靶场接入（astra-runner）

`container/astra_runner/` 提供评测平台编排器，负责题目容器的启动/关闭、逐题创建 ASTRA 项目、
从星图收集 flag 统一提交（幂等）并输出得分报告；附带赛制经济学层（价值排序开题 / defer 预算 /
hint 购买门控 / 多旗收割 / 四层自愈看门狗）：

```bash
# 本地演练（需先连接平台下发的 VPN，并配置凭证）
BENCHMARK_TOKEN=xxx BENCHMARK_BASE_URL=https://... python3 container/astra_runner/runner.py

# 托管模式：构建镜像后直接运行（ENTRYPOINT 已指向 runner）
docker build -f container/Dockerfile -t astra-runner .
docker save astra-runner:latest | gzip > agent.tar.gz   # 按平台规范上传
```

镜像内已内置 ASTRA 引擎、pi CLI 与 Kali 工具链；通过环境变量配置模型
（`PI_MODEL / PI_BASE_URL / PI_API_KEY / PI_PROVIDER_API`，智谱决策通道 `ZHIPU_PI_*`）。

### 测试

```bash
uv run --project astra --group dev pytest
```

## 安全声明

ASTRA 是通用问题求解引擎。尽管它支持渗透测试、CTF 求解、安全评估与漏洞研究等工作流，仅限在获得明确授权的环境中使用。未经许可的安全测试可能违法并造成损害，使用者须自行承担全部责任。

## License

GNU AGPLv3
