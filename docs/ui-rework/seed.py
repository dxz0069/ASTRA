# -*- coding: utf-8 -*-
import json, urllib.request

B = "http://127.0.0.1:8321"

def api(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

# --- 星域 1：进行中的渗透任务，图谱丰满 ---
p1 = api("POST", "/projects", {
    "title": "nebula-ctf-web-01",
    "origin": "http://10.0.0.15:8080/login",
    "goal": "获取后台管理员权限并读取 /flag 中的 flag{...} 完整字符串",
    "bootstrap_enabled": False,
    "hints": [
        {"content": "目标站点在 WAF 之后，优先尝试 multipart 解析差异绕过", "creator": "human"},
        {"content": "[审查否决] 直接对 /admin 路径暴力枚举被否决：噪声过大且已触发临时封禁", "creator": "reviewer"},
        {"content": "[失败学习] sqlmap 默认参数注入登录接口失败，WAF 拦截了 union/select 关键字", "creator": "learner"},
    ],
})
pid = p1["project"]["id"] if "project" in p1 else p1["id"]
print("p1:", pid)

i1 = api("POST", f"/projects/{pid}/intents", {"from": ["origin"], "description": "侦察登录接口与 WAF 指纹，确定可绕过的请求构造方式", "creator": "human", "worker": None})
i1id = i1["intent"]["id"] if "intent" in i1 else "i001"
api("POST", f"/projects/{pid}/intents/{i1id}/conclude", {"worker": "scout-01", "description": "确认 WAF 为某云厂商默认规则集，multipart/form-data 边界混淆可绕过其参数解析，响应耗时基线 220ms"})

i2 = api("POST", f"/projects/{pid}/intents", {"from": ["f001"], "description": "利用 multipart 边界混淆构造登录绕过 payload", "creator": "human", "worker": None})
i2id = i2["intent"]["id"] if "intent" in i2 else "i002"
api("POST", f"/projects/{pid}/intents/{i2id}/conclude", {"worker": "scout-01", "description": "admin'-- 变体在混淆边界下成功绕过认证，获得 session cookie（低置信，待复验）"})

api("POST", f"/projects/{pid}/intents", {"from": ["f002"], "description": "携带 session 访问 /admin 面板，枚举可用功能点", "creator": "human", "worker": None})
api("POST", f"/projects/{pid}/hints", {"content": "session 有效期约 10 分钟，每次操作前注意续期", "creator": "human"})

# --- 星域 2：已完成 ---
p2 = api("POST", "/projects", {
    "title": "orion-api-leak-03",
    "origin": "https://api.internal.example.com/v1",
    "goal": "证明未授权可读取用户手机号清单",
    "bootstrap_enabled": False,
    "hints": [{"content": "重点关注 JWT alg=none 与越权 IDOR", "creator": "human"}],
})
pid2 = p2["project"]["id"] if "project" in p2 else p2["id"]
print("p2:", pid2)
j1 = api("POST", f"/projects/{pid2}/intents", {"from": ["origin"], "description": "测试 JWT 验签逻辑", "creator": "human", "worker": None})
j1id = j1["intent"]["id"] if "intent" in j1 else "i001"
api("POST", f"/projects/{pid2}/intents/{j1id}/conclude", {"worker": "api-01", "description": "alg=none 被接受，可伪造任意 uid 的 token（高置信，已双重复验）"})
api("POST", f"/projects/{pid2}/complete", {"from": ["f001"], "description": "伪造 uid=1 的 token 成功读取全量用户手机号导出接口，目标达成", "worker": "human"})

# --- 星域 3：暂停状态，留空图谱 ---
p3 = api("POST", "/projects", {
    "title": "vega-iot-firmware-07",
    "origin": "uart 调试口 / 固件 bin",
    "goal": "提取固件中的硬编码凭据",
    "bootstrap_enabled": False,
})
pid3 = p3["project"]["id"] if "project" in p3 else p3["id"]
api("PUT", f"/projects/{pid3}/status", {"status": "stopped"})
print("p3:", pid3)
print("seed done")
