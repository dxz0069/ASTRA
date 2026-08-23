---
name: spring-jolokia-rce
description: Spring Boot / Jolokia / Actuator RCE 完整链（MBean → Tomcat context/WAR 部署 → shell 触发）。Use when /jolokia 或 /actuator 暴露且目标为 Spring 内嵌 Tomcat。run 10089 ezSpring 差最后一步：裸建 context 的 JSP 404——正确收尾是 WAR 部署或完整 servlet 映射。
license: MIT
compatibility: Requires filesystem-based agent with bash, curl, python3.
allowed-tools: Bash Read Write Edit Glob Grep
metadata:
  user-invocable: "false"
---

# Spring/Jolokia RCE 完整链

## 侦察
`/jolokia/list` 找 `Catalina:type=MBeanFactory`、`Memory`、`WebModule` 等敏感 MBean；
actuator 面注意 `/env`、`/heapdump`（凭据/内网地址直出）。

## 三条利用链（按可靠性排序）
1. **WAR 部署链（首选）**：本地 `jar cvf shell.war` 打一个含 JSP 的 war →
   通过 `jolokia` 读 `MBeanFactory` 不足以传文件时，用 `/actuator/env + /restart` 或
   `tomcat MBean deploy`：`Catalina:host=localhost,type=Host` 的 `deployWAR` 需要落盘路径——
   先用 `PUT /` 挂载或日志写文件（`valve` 写 access log 到 webapps）。
2. **createStandardContext + docBase 指向可写目录**（run 10089 走的路，**注意坑**）：
   - `docBase` 指向 `/tmp/jspdir` 建 context 后 JSP 404 的原因：**内嵌 Tomcat 的 context
     需要 servlet mapping**——裸建 context 不加载 JspServlet。
   - 修正：context 的 `path` 必须以 war/jsp 可达的 `docBase` 配套，且 webapps 下的 ROOT
     才有默认 servlet 映射。**优先把 shell 写进已有可写 static 路径**再触发。
3. **Thread/Executor MBean + script**：`Threading` 无直接 exec，跳过。

## 触发后
`curl http://host/shell.jsp?c=id`；JSP 马用最短无依赖版（`Runtime.exec` 单行）。

## 反模式
- ❌ context 建成后 JSP 404 还在反复重建 context——404 的根因是 servlet 映射缺失，
  换 WAR 部署链或写 static 目录（run 10089 在此烧掉整个 45min）。
- ❌ 忘了 `/jolokia/list` 先确认 MBean 权限（部分部署只读）。
