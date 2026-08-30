---
name: ssrf-filter-bypass
description: SSRF 目标白名单/黑名单过滤绕过打法（nip.io、IP 表示法变体、DNS rebinding、重定向链）。Use when an app fetches URLs server-side but blocks bare IPs or non-whitelisted hosts（如"该地址不被允许"），或需要借服务端访问内网管理面/云元数据。
license: MIT
compatibility: Requires filesystem-based agent with bash and python3.
allowed-tools: Bash Read Write Edit Glob Grep
metadata:
  user-invocable: "false"
---

# SSRF 过滤绕过（run 10089 DocMind 实证补全）

## 过滤器画像先行
1. 先探测过滤类型：**域名白名单**（仅允许 partner-xx.com）还是 **IP/端口黑名单**（拒私网段）。
2. 测 IP 表示法变体是否过滤：`http://0x7f000001/`、`http://2130706433/`、`http://0177.0.0.1/`、
   `http://[::ffff:127.0.0.1]/`、末尾点 `http://127.0.0.1./`。
3. 测**端口级黑名单**：扫 1-65535 快速抽样，找"哪些端口被拒"的反常（拒绝面即信息面）。

## 主力绕过
- **nip.io / sslip.io**：`internal-host.10.0.177.8.nip.io` 解析到 10.0.177.8——绕"裸 IP 拒绝"首选
  （run 10089 DocMind 卡点：扫描全用裸 IP 被拦，改 nip.io 即通）。
- **重定向链**：白名单域下找 30x（上传页/导出页/头像 URL 常见），`Location:` 指向内网目标。
- **DNS rebinding**：自建解析器或公共 rebinder（rebind.network 类）同一域名两次解析不同 IP，
  绕"先解析校验再请求"的实现缺陷。
- **URL parser confusion**：`http://whitelisted@10.0.0.5/`、`http://whitelisted.com#@10.0.0.5/`、
   `\\10.0.0.5`、`gopher://`（如协议不限）。

## 打到内网后
- 内网管理面/元数据：`169.254.169.254`（云）、`10.0.0.x:8000/8080` 管理端口、内部 API 网关。
- 响应回显被截时：用 HTTP 状态/响应时长做布尔 oracle，或让目标主动连我们可控的 listener。

## 反模式
- ❌ 拿到"该地址不被允许"就停——这句话说明**过滤器存在且已生效**，恰是画像起点。
- ❌ 裸 IP 全段硬扫——先过一个变体确认绕过原语，再扫。
