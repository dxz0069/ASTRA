// Explicitly loaded by the vulnerability profile; Pi discovers no other extensions.
// This is a tool boundary, not a general network sandbox for arbitrary processes.
import dns from "node:dns/promises";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import { createHash } from "node:crypto";

const MAX_BYTES = 65536;
let requestCount = 0;

function scope() {
  const raw = process.env.ASTRA_VULN_SCOPE_JSON;
  if (!raw) throw new Error("Missing ASTRA_VULN_SCOPE_JSON; request denied");
  const value = JSON.parse(raw);
  if (value.project_code !== "PROJ2026_AISRC01" || !Array.isArray(value.targets)) {
    throw new Error("Invalid challenge scope; request denied");
  }
  const now = Date.now();
  const start = Date.parse(value.not_before);
  const end = Date.parse(value.not_after);
  if (!Number.isFinite(start) || !Number.isFinite(end) || now < start || now > end) {
    throw new Error("Outside authorized time window; request denied");
  }
  return value;
}

function publicIPv4(address) {
  if (net.isIP(address) !== 4) return false;
  const [a, b, c] = address.split(".").map(Number);
  if (a === 0 || a === 10 || a === 127 || a >= 224) return false;
  if (a === 100 && b >= 64 && b <= 127) return false;
  if (a === 169 && b === 254) return false;
  if (a === 172 && b >= 16 && b <= 31) return false;
  if (a === 192 && (b === 0 || b === 168)) return false;
  if (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) return false;
  if (a === 203 && b === 0 && c === 113) return false;
  return true;
}

function allowedUrl(value, method, manifest) {
  let url;
  try { url = new URL(value); } catch { throw new Error("Invalid URL; request denied"); }
  if (url.username || url.password || url.hash) throw new Error("URL credentials/fragments are denied");
  if (url.pathname.includes("%") || url.pathname.includes("\\")) {
    throw new Error("Encoded or backslash path is denied");
  }
  const origin = url.origin;
  const target = manifest.targets.find((entry) => entry.origin === origin);
  if (!target || !target.methods.includes(method) ||
      !target.path_prefixes.some((prefix) =>
        prefix === "/" || url.pathname === prefix || url.pathname.startsWith(prefix.replace(/\/$/, "") + "/"))) {
    throw new Error("URL, method, or path is outside authorized scope");
  }
  const localTest = process.env.ASTRA_VULN_LOCAL_TEST === "1";
  const loopback = url.hostname === "127.0.0.1";
  if (url.protocol !== "https:" && !(localTest && loopback && url.protocol === "http:")) {
    throw new Error("Only HTTPS is allowed outside loopback local tests");
  }
  return url;
}

async function resolvePinned(url) {
  if (net.isIP(url.hostname)) {
    if (url.hostname === "127.0.0.1" && process.env.ASTRA_VULN_LOCAL_TEST === "1") return url.hostname;
    if (!publicIPv4(url.hostname)) throw new Error("Private/reserved IP is denied");
    return url.hostname;
  }
  const answers = await dns.lookup(url.hostname, { all: true, family: 4 });
  if (!answers.length || answers.some((item) => !publicIPv4(item.address))) {
    throw new Error("DNS resolved outside public IPv4; request denied");
  }
  return answers[0].address;
}

async function oneRequest(url, method, address, signal) {
  return await new Promise((resolve, reject) => {
    const transport = url.protocol === "https:" ? https : http;
    const request = transport.request(url, {
      method,
      agent: false,
      timeout: 10000,
      lookup: (_host, _opts, cb) => cb(null, address, 4),
      headers: { "user-agent": "ASTRA-vuln-scope/1", accept: "*/*" },
    }, (response) => {
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > MAX_BYTES) {
          request.destroy(new Error("Response exceeds 64 KiB limit"));
          return;
        }
        chunks.push(chunk);
      });
      response.on("end", () => resolve({
        status: response.statusCode,
        location: response.headers.location,
        contentType: response.headers["content-type"] || "",
        headers: { "content-type": response.headers["content-type"] || "" },
        body: Buffer.concat(chunks),
      }));
    });
    request.on("error", reject);
    request.on("timeout", () => request.destroy(new Error("Request timed out")));
    if (signal) signal.addEventListener("abort", () => request.destroy(new Error("Request cancelled")), { once: true });
    request.end();
  });
}

async function scopedRequest(value, method, signal) {
  const startedAt = new Date().toISOString();
  let current = value;
  for (let redirects = 0; redirects <= 3; redirects++) {
    // A redirect or DNS lookup may outlive the authorization window.
    const manifest = scope();
    const url = allowedUrl(current, method, manifest);
    const address = await resolvePinned(url);
    scope();
    if (++requestCount > 20) throw new Error("Per-run request budget exhausted");
    const result = await oneRequest(url, method, address, signal);
    if (result.status >= 300 && result.status < 400 && result.location) {
      if (redirects === 3) throw new Error("Too many redirects");
      current = new URL(result.location, url).href;
      continue;
    }
    return {
      url: url.href,
      method,
      status: result.status,
      content_type: result.contentType,
      sha256: createHash("sha256").update(result.body).digest("hex"),
      body: result.body.toString("utf8"),
      _astra_evidence: {
        requested_url: value,
        scope_sha256: createHash("sha256").update(process.env.ASTRA_VULN_SCOPE_JSON, "utf8").digest("hex"),
        url: url.href,
        method,
        status: result.status,
        headers: result.headers,
        body_base64: result.body.toString("base64"),
        started_at: startedAt,
        finished_at: new Date().toISOString(),
        pinned_address: address,
      },
    };
  }
  throw new Error("Redirect limit exceeded");
}

export default function (pi) {
  pi.registerTool({
    name: "scoped_request",
    label: "Scoped HTTP request",
    description: "Send one GET/HEAD request to an explicitly authorized target and return bounded evidence. Scope, time, DNS, and every redirect are checked.",
    parameters: {
      type: "object",
      properties: {
        url: { type: "string", description: "Absolute target URL" },
        method: { type: "string", enum: ["GET", "HEAD"] },
      },
      required: ["url", "method"],
      additionalProperties: false,
    },
    async execute(_id, params, signal) {
      const result = await scopedRequest(params.url, params.method, signal);
      const { _astra_evidence, ...visible } = result;
      return {
        content: [{ type: "text", text: JSON.stringify(visible) }],
        details: { _astra_evidence },
        _astra_evidence: true,
      };
    },
  });
}
