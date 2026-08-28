# 审计第六轮：多 agent 交叉审计（2026-08-28）

> 方法：四路并行 agent（上游对比/榜首取证/astra核心审计/容器与文档审计）+ 人工复核
> （关键 P0/P1 逐条亲自验证后才修）。本轮发现 2 个存量 P0、5 个 P1、若干 P2/P3。

## 已修复（本批 commit）

| 级别 | 问题 | 修复 |
|---|---|---|
| P0 | 引擎 shutdown 定义两次，后置简版覆盖 P0 加固版（killpg/端口等待/含密钥 yaml 清理从未生效） | 删除简版，保留加固版 |
| P0 | runner:1755 包路径 import（astra_runner.astra_runner_engine）在容器内必 ImportError 被 except 吞 → watchdog 自愈的 shutdown 从未执行（D1 修复形同虚设） | 改平铺 import（与全文件一致） |
| P1 | defer 停项目/复活项目 PUT 缺 auth 头 → 开启 ASTRA_AUTH_TOKEN 时 defer 全链 401、僵尸项目回归 | 补 headers=_auth_headers()；修 reactivate 返回注解 |
| P1 | 多旗收割边界：flag_count=0（未知）+全错旗 → 落多旗 defer 分支反复回队烧窗口；注释宣称的"保守关题"分支不可达 | 未知旗数一律保守关题 |
| P1 | server 幂等重 claim 不回传租约令牌 → 调用方持 None token，heartbeat/complete 全 403 至租约过期 | 幂等路径回传已存 token |
| P1 | scheduler _validate_server_settings "钳制"只 LOG 不动作（空头支票）——租约<=interval 必过期、同任务双跑 | 新增 client.update_settings，真 PATCH 到 interval*2；修不动才告警 |
| P1 | model_watchdog 用 os.environ["TEMP"]（Linux 容器无 TEMP → 落 ./）→ 双保险扫描恒空 | 改 tempfile.gettempdir() |
| P2 | 容器 Dockerfile（全量版）COPY ./container/dsh 目录已删 → 不可构建；claude-code 版本漂移 2.1.98 | 删死行，版本对齐 2.1.250，README 指向 slim |
| P2 | codex 健康检查硬编码 /dev/null（Windows local 模式失败） | os.devnull + import os |
| P3 | loop.py 用未导入 Any；models.py 重复 Literal 导入 | 一行修 |
| 定位 | 机制全解/runner docstring 把平台写成单一赛事 | 改通用平台口径（benchmark-agnostic，适配件可替换） |

## 遗留 backlog（未修，按优先级）

1. **P2 explore conclude 回退路径不传 kind**（explore.py:552-563）——负结果在回退路径落
   regular，V8 负结果保活失效；且负结果分流零测试覆盖。
2. **P2 reason 写航向的租约复查在写之后**（reason.py:619-626）——双写窗口与注释相反。
3. **P2 pi 附加 env 无 schema 校验**（PI_THINKING_LEVEL_MAP 等 json/int 值在 build 时才炸，
   任务反复 crash+冷却）。
4. **P2 Dockerfile.slim 层序**：COPY ./astra 在 npm 层之前，改引擎源码使 200MB npm 层缓存
   失效（打包慢的下限约束）。
5. **P3 批**：heartbeat 死 max 分支；consolidate noop 告警噪音；cli memory stats 顶层数组
   栈崩；embeddings 死逻辑；P3-5 complete 漏清 reason_token；测试盲区（负结果/钳制/
   幂等claim/心跳宽限——本批已补幂等 claim 修复但未加测试）。
6. **文档漂移**：docs/项目总览.md（停在 08-25，dsh 舰队描述）、docs/技术方案.md（4-worker
   混合舰队/defer 2 次/双端点探活等与现实现不符）、docs/论文框架.md:52 行动层 dsh 措辞。
7. **dispatch.astra.yaml 明文密钥**：本地惯例未入库（git log -S 证实零泄漏），但含密钥的
   /tmp dispatch yaml 退出不删（已随 P0 shutdown 修复恢复清理路径）。

## 测试盲区清单（下轮补测优先序）

幂等 claim 返 token / 负结果 kind 分流 / 钳制 PATCH 行为 / 心跳宽限 / pi 附加 env 校验 /
引擎 shutdown 加固版（本批两 P0 恰好都长在测试盲区里）。
