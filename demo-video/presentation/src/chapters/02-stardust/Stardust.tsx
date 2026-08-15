import type { ChapterStepProps } from "../../registry/types";
import "./Stardust.css";

/* 章节视觉语言：左侧叙事 + 右侧演示图。曲线生长 / 聚光灯 / 压缩 / 环形占比 */

export default function StardustChapter({ step }: ChapterStepProps) {
  /* step 0 — 问题：上下文线性膨胀 */
  if (step === 0) {
    return (
      <div className="sd-scene">
        <div className="sd-copy">
          <div className="sd-kicker label-mono">MECHANISM 01 · 星尘记忆</div>
          <h1 className="sd-h1">
            传统 Agent 的通病：<br />
            <span className="sd-bad">任务越长，上下文越爆</span>
          </h1>
          <p className="sd-p">全部历史塞进每次推理——token 随图线性膨胀。</p>
        </div>
        <div className="sd-demo card">
          <svg viewBox="0 0 560 360" className="sd-chart">
            <line x1="40" y1="320" x2="540" y2="320" stroke="var(--rule)" strokeWidth="1" />
            <line x1="40" y1="320" x2="40" y2="20" stroke="var(--rule)" strokeWidth="1" />
            <path
              d="M40 318 C 160 314, 240 300, 320 250 C 400 200, 460 110, 530 30"
              fill="none"
              stroke="var(--accent-glow)"
              strokeWidth="4"
              className="sd-curve"
            />
            <text x="60" y="60" className="sd-chart-label" fill="var(--text-mute)">context tokens →</text>
            <text x="380" y="80" className="sd-chart-warn" fill="var(--accent-glow)">溢出</text>
          </svg>
        </div>
      </div>
    );
  }

  /* step 1 — 焦点子图：聚光灯只照亮相关节点 */
  if (step === 1) {
    const nodes = [
      { x: 90, y: 80, hot: false }, { x: 230, y: 50, hot: false }, { x: 380, y: 90, hot: true },
      { x: 500, y: 60, hot: false }, { x: 150, y: 200, hot: true }, { x: 300, y: 180, hot: true },
      { x: 450, y: 210, hot: false }, { x: 100, y: 300, hot: false }, { x: 260, y: 300, hot: false },
      { x: 420, y: 310, hot: true },
    ];
    const links: Array<[number, number]> = [[0, 1], [1, 2], [2, 3], [1, 4], [2, 5], [5, 6], [4, 5], [4, 7], [5, 8], [5, 9], [8, 9], [7, 8]];
    return (
      <div className="sd-scene">
        <div className="sd-copy">
          <div className="sd-kicker label-mono">SOLUTION · 焦点子图</div>
          <h1 className="sd-h1">
            每次推理，只内联<br /><span className="sd-good">与当前目标最相关的部分</span>
          </h1>
          <p className="sd-p">相关度 × 时间近度打分，其余留文件引用，按需读取。</p>
        </div>
        <div className="sd-demo card sd-spot-wrap">
          <div className="sd-spotlight" />
          <svg viewBox="0 0 560 360" className="sd-graph">
            {links.map(([a, b], i) => (
              <line
                key={i}
                x1={nodes[a].x} y1={nodes[a].y} x2={nodes[b].x} y2={nodes[b].y}
                stroke={nodes[a].hot && nodes[b].hot ? "var(--accent)" : "var(--rule)"}
                strokeWidth={nodes[a].hot && nodes[b].hot ? 2 : 1}
                className="sd-link"
                style={{ animationDelay: `${i * 40}ms` }}
              />
            ))}
            {nodes.map((n, i) => (
              <circle
                key={i}
                cx={n.x} cy={n.y} r={n.hot ? 13 : 8}
                fill={n.hot ? "var(--accent)" : "var(--surface-3)"}
                stroke={n.hot ? "var(--accent)" : "var(--text-faint)"}
                strokeWidth="1.5"
                className={n.hot ? "sd-node-hot" : "sd-node"}
                style={{ animationDelay: `${i * 60}ms` }}
              />
            ))}
          </svg>
          <div className="sd-legend label-mono">
            <span className="sd-lg"><i className="sd-lg-dot sd-lg-on" /> inline · 焦点子图</span>
            <span className="sd-lg"><i className="sd-lg-dot" /> file ref · 按需读取</span>
          </div>
        </div>
      </div>
    );
  }

  /* step 2 — 摘要记忆：多节点坍缩成一个摘要节点 */
  if (step === 2) {
    return (
      <div className="sd-scene sd-center">
        <h1 className="sd-h1-center">
          超预算的旧发现，<span className="sd-good">自动压缩成摘要</span>
        </h1>
        <div className="sd-collapse">
          <div className="sd-old">
            {["端口 22 开放", "robots.txt 泄露", "CMS 指纹 v2.3", "登录接口 302", "目录 /admin", "cookie 无 HttpOnly"].map((t, i) => (
              <div key={t} className="sd-oldnode card" style={{ animationDelay: `${i * 70}ms` }}>{t}</div>
            ))}
          </div>
          <svg viewBox="0 0 100 40" className="sd-collapse-arrow">
            <line x1="6" y1="20" x2="88" y2="20" stroke="var(--accent)" strokeWidth="2" className="sd-curve" />
            <polygon points="82,13 96,20 82,27" fill="var(--accent)" />
          </svg>
          <div className="sd-sumnode card">
            <div className="sd-sum-tag label-mono">EPITOME · 摘要星记</div>
            <div className="sd-sum-txt">侦察阶段 6 项确认发现<br />边界面收敛，无未验证断言</div>
          </div>
        </div>
      </div>
    );
  }

  /* step 3 — 硬上限预算 */
  if (step === 3) {
    return (
      <div className="sd-scene sd-center">
        <h1 className="sd-h1-center">内联量有<span className="sd-good">硬上限</span>，星图长到多大都不膨胀</h1>
        <div className="sd-budgets">
          {[
            { n: "60", t: "星记", s: "max_inline_facts" },
            { n: "12", t: "航向", s: "max_inline_intents" },
            { n: "8", t: "指引", s: "max_inline_hints" },
          ].map((b, i) => (
            <div key={b.s} className="sd-budget card" style={{ animationDelay: `${i * 180}ms` }}>
              <div className="hero-num sd-bn">{b.n}</div>
              <div className="sd-bt">{b.t}</div>
              <div className="sd-bs label-mono">{b.s}</div>
              <div className="sd-cap label-mono">hard cap</div>
            </div>
          ))}
        </div>
        <div className="sd-note label-mono">end-to-end tested · 零膨胀断言</div>
      </div>
    );
  }

  /* step 4 — 干净的推理窗口：全量投喂 vs 焦点子图（示意对比） */
  if (step === 4) {
    return (
      <div className="sd-scene">
        <div className="sd-copy">
          <div className="sd-kicker label-mono">CLEAN WINDOW</div>
          <h1 className="sd-h1-sm">推理窗口<br /><span className="sd-good">始终干净</span></h1>
          <p className="sd-p">上下文可控 · 缓存友好<br />注意力集中在当前决策链</p>
        </div>
        <div className="sd-demo card sd-bar-wrap">
          <div className="sd-barrow">
            <span className="sd-bar-label label-mono">全量投喂</span>
            <div className="sd-bar"><div className="sd-bar-fill sd-bar-bad" /></div>
            <span className="sd-bar-tag label-mono">线性膨胀</span>
          </div>
          <div className="sd-barrow">
            <span className="sd-bar-label label-mono">焦点子图</span>
            <div className="sd-bar"><div className="sd-bar-fill sd-bar-good" /></div>
            <span className="sd-bar-tag label-mono">硬上限</span>
          </div>
          <div className="sd-bar-note label-mono">same task · budgeted inline vs unbounded history</div>
        </div>
      </div>
    );
  }

  /* step 5 — 收束：成本 + 注意力 */
  return (
    <div className="sd-scene sd-center">
      <div className="sd-final-pair">
        <div className="sd-final card" style={{ animationDelay: "0ms" }}>
          <div className="sd-final-t">成本</div>
          <div className="sd-final-v sd-good">压住了</div>
          <div className="sd-final-s label-mono">budgeted inline context</div>
        </div>
        <div className="sd-final-plus">+</div>
        <div className="sd-final card" style={{ animationDelay: "220ms" }}>
          <div className="sd-final-t">注意力</div>
          <div className="sd-final-v sd-good">在当前决策链</div>
          <div className="sd-final-s label-mono">focus subgraph only</div>
        </div>
      </div>
      <h1 className="sd-h1-center sd-mt">星图再大，推理窗口始终干净</h1>
    </div>
  );
}
