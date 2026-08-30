---
name: android-static-reversing
description: Android APK 静态逆向路线（jadx/apktool）。Use when the target is an Android app/APK (deeplink, hardcoded secrets, flag assembly logic, WebView logic). 本容器无 KVM/无外网，模拟器与 Frida/objection 动态路线在本环境不可行——直接走静态反编译读码，禁止在环境可行性核验上浪费时间。
license: MIT
compatibility: Requires filesystem-based agent with bash. jadx/apktool preinstalled in image.
allowed-tools: Bash Read Write Edit Glob Grep
metadata:
  user-invocable: "false"
---

# Android 静态逆向（无模拟器环境首选路线）

## 路线约束（环境实证：run 10089 两题因走模拟器路线整窗空转）
- 本环境无 /dev/kvm、无 emulator/AVD、无外网下载镜像、adb devices 为空 →
  **动态模拟器/插桩路线一律不可行，发现"环境缺模拟器"类结论直接记为终局 fact，不再复核、不再尝试。**
- 唯一正确路线：APK → jadx 反编译 → 读 Java 源码 → 定位逻辑/secret → 本地复现算法。

## 标准流程
1. 拿到 APK 后先 `unzip -l` 看结构、`strings` 快筛（快而不全，只作初筛）。
2. `jadx -d out/ app.apk --no-res`（大 APK 加 `--no-res` 提速；卡住则 `jadx --show-bad-code` 或按 dex 拆）。
3. 资源/manifest 层用 `apktool d app.apk`（AndroidManifest、deeplink scheme、字符串资源）。
4. 搜关键词：`flag`、`secret`、`assembleFlag`、`token`、`deeplink/scheme`、`getSecret`、
   `BuildConfig`、Base64/hex 常量、`SharedPreferences` key。
5. flag 组装逻辑类题目：把相关类抽出来用 python/java 本地复现算法（不要在反编译代码里硬读）。

## 典型题型映射
- 无界面 APK + "检查你的环境"类：secret 多在 resources/BuildConfig/native so（so 用 strings+IDA/objdump）。
- deeplink 保险库：AndroidManifest 的 scheme/host → WebView JS bridge → 客户端校验函数。
- 双段 secret 组装（HR value + prefix/suffix）：通常是两处资源/两段 prompt 解密，拼装函数在 util 类。

## 反模式
- ❌ 讨论/核验"能否装模拟器、能否下 Frida"——本环境答案恒为否，写一条 fact 后立即转静态。
- ❌ 手写 DEX 解析器（run 10089 FileBox 曾花 6min 造轮子）——jadx 一行命令的事。
