import type { ChapterStepProps } from "../../registry/types";
import "./Dualstar.css";

/* 章节视觉语言：决策流水线逐步点亮；青=通过，品红=否决/风险 */

const STAGES = [
  { k: "预审", en: "PRE-CHECK", icon: "check" },
  { k: "质询", en: "CHALLENGE", icon: "chat" },
  { k: "裁决", en: "VERDICT", icon: "scale" },
] as const;

function StageRail({ active }: { active: number }) {
  return (
    <div className="ds-rail">
      {STAGES.map((s, i) => (
        <div key={s.en} className={`ds-rail-node ${i <= active ? "ds-rail-on" : ""}`} style={{ animationDelay: `${i * 120}ms` }}>
          <div className="ds-rail-k label-mono">0{i + 1}</div>
          <div className="ds-rail-t">{s.k}</div>
          <div className="ds-rail-e label-mono">{s.en}</div>
        </div>
      ))}
    </div>
  );
}

export default function DualstarChapter({ step }: ChapterStepProps) {
  /* step 0 — 问题：单体自问自答 → 漂移 */
  if (step === 0) {
    return (
      <div className="ds-scene ds-center">
        <div className="ds-kicker label-mono">MECHANISM 02 · 双星决策</div>
        <h1 className="ds-h1">
          单个模型自问自答，<br />最大的问题是<span className="ds-bad">方向漂移</span>
        </h1>
        <div className="ds-loop card">
          <div className="ds-loop-node">自己提问</div>
          <div className="ds-loop-arrow">→</div>
          <div className="ds-loop-node">自己回答</div>
          <div className="ds-loop-arrow ds-loop-back">↺</div>
          <div className="ds-loop-node ds-loop-drift">宣布完成<span className="ds-x">✕</span></div>
        </div>
        <p className="ds-p">高风险结论，不能只靠模型自信。</p>
      </div>
    );
  }

  /* step 1 — 机器预审 */
  if (step === 1) {
    const checks = [
      { t: "引用星记存在性", ok: true },
      { t: "结构与语义合法", ok: true },
      { t: "声明与星图证据匹配", ok: true },
      { t: "flag 格式完整精确", ok: true },
    ];
    return (
      <div className="ds-scene">
        <div className="ds-copy">
          <StageRail active={0} />
          <h1 className="ds-h1-sm">先过<span className="ds-good">机器预审</span></h1>
          <p className="ds-p">本地契约校验，毫秒级，零 token。</p>
        </div>
        <div className="ds-demo card">
          <div className="ds-checklist">
            {checks.map((c, i) => (
              <div key={c.t} className="ds-check" style={{ animationDelay: `${i * 220}ms` }}>
                <span className={`ds-check-box ${c.ok ? "ds-check-ok" : ""}`}>
                  <svg viewBox="0 0 16 16" className="ds-check-svg"><polyline points="3,9 7,13 13,4" fill="none" strokeWidth="2.5" stroke="currentColor" /></svg>
                </span>
                <span className="ds-check-t">{c.t}</span>
                <span className="ds-check-tag label-mono">pass</span>
              </div>
            ))}
          </div>
          <div className="ds-demo-foot label-mono">fail → conservative reject（保守不写回）</div>
        </div>
      </div>
    );
  }

  /* step 2 — 质询 */
  if (step === 2) {
    return (
      <div className="ds-scene">
        <div className="ds-copy">
          <StageRail active={1} />
          <h1 className="ds-h1-sm">独立<span className="ds-good">质询</span>视角挑毛病</h1>
          <p className="ds-p">拒绝橡皮图章——必须有证据，才允许质疑。</p>
        </div>
        <div className="ds-demo card ds-chat-wrap">
          <div className="ds-chat">
            <div className="ds-msg ds-msg-prop">
              <div className="ds-msg-who label-mono">PROPOSAL</div>
              <div className="ds-msg-body">「注入点已确认，建议宣布完成」</div>
            </div>
            <div className="ds-msg ds-msg-obj" style={{ animationDelay: "400ms" }}>
              <div className="ds-msg-who label-mono">CHALLENGE</div>
              <div className="ds-msg-body">
                盲注无回显——数据真实性未验证。<br />
                建议：补充带外证据后再提案。
                <span className="ds-conf label-mono">confidence · medium</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* step 3 — 裁决 */
  if (step === 3) {
    return (
      <div className="ds-scene">
        <div className="ds-copy">
          <StageRail active={2} />
          <h1 className="ds-h1-sm"><span className="ds-good">裁决</span>——通过，才写进星图</h1>
          <p className="ds-p">首轮通过率约 <span className="ds-good ds-70">70%</span>，修正后显著上升。</p>
        </div>
        <div className="ds-demo card ds-verdict-wrap">
          <div className="ds-verdict">
            <div className="ds-stamp ds-stamp-ok">通过 · 写入星图</div>
            <div className="ds-verdict-bars">
              {[70, 88, 96].map((v, i) => (
                <div key={i} className="ds-vbar-row">
                  <span className="ds-vbar-label label-mono">{["round 1", "round 2", "round 3"][i]}</span>
                  <div className="ds-vbar"><div className="ds-vbar-fill" style={{ width: `${v}%`, animationDelay: `${i * 200}ms` }} /></div>
                  <span className="ds-vbar-num label-mono">{v}%</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  /* step 4 — 否决回路：否决原因 → 指引 */
  return (
    <div className="ds-scene ds-center">
      <h1 className="ds-h1-center">否决原因，<span className="ds-bad">变成风险指引</span></h1>
      <div className="ds-loopback card">
        <div className="ds-lb-flow">
          <div className="ds-lb-node ds-lb-reject">
            <div className="ds-lb-t">提案被否决</div>
            <div className="ds-lb-s label-mono">verdict · rejected</div>
          </div>
          <svg viewBox="0 0 140 40" className="ds-lb-arrow">
            <line x1="8" y1="20" x2="126" y2="20" stroke="var(--accent-glow)" strokeWidth="2" className="ds-draw" />
            <polygon points="120,13 134,20 120,27" fill="var(--accent-glow)" />
          </svg>
          <div className="ds-lb-node ds-lb-guide">
            <div className="ds-lb-t">沉淀为指引</div>
            <div className="ds-lb-s label-mono">guidance · risk hint</div>
          </div>
          <svg viewBox="0 0 140 40" className="ds-lb-arrow">
            <line x1="8" y1="20" x2="126" y2="20" stroke="var(--accent)" strokeWidth="2" className="ds-draw" />
            <polygon points="120,13 134,20 120,27" fill="var(--accent)" />
          </svg>
          <div className="ds-lb-node ds-lb-block">
            <div className="ds-lb-t">同方向禁止重复提案</div>
            <div className="ds-lb-s label-mono">no repeat · navigate rule</div>
          </div>
        </div>
      </div>
      <p className="ds-p">同一个坑，不会踩第二次。</p>
    </div>
  );
}
