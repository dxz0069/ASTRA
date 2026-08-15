import { MaskReveal } from "../../components/MaskReveal";
import type { ChapterStepProps } from "../../registry/types";
import "./Closing.css";

export default function ClosingChapter({ step }: ChapterStepProps) {
  /* step 0 — 三机制三元组 */
  if (step === 0) {
    const pillars = [
      { t: "星尘记忆", s: "context", d: "管上下文" },
      { t: "双星决策", s: "challenge · verdict", d: "管方向" },
      { t: "混合舰队", s: "DS × GLM", d: "管算力" },
    ];
    return (
      <div className="cl-scene cl-center">
        <div className="cl-pillars">
          {pillars.map((p, i) => (
            <div key={p.t} className="cl-pillar card" style={{ animationDelay: `${i * 240}ms` }}>
              <div className="cl-beam" />
              <div className="cl-pillar-t">{p.t}</div>
              <div className="cl-pillar-s label-mono">{p.s}</div>
              <div className="cl-pillar-d">{p.d}</div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  /* step 1 — 金句 */
  if (step === 1) {
    return (
      <div className="cl-scene cl-center">
        <h1 className="cl-quote">
          <MaskReveal show duration={800}>全链路无人值守</MaskReveal>
        </h1>
        <div className="cl-zero">
          <span className="cl-zero-label label-mono">human verification time</span>
          <span className="hero-num cl-zero-n">→ 0</span>
        </div>
      </div>
    );
  }

  /* step 2 — 谢幕 */
  return (
    <div className="cl-scene cl-center">
      <div className="cl-mark">ASTRA</div>
      <h1 className="cl-final">
        <MaskReveal show duration={900}>经验驱动</MaskReveal>
        <span className="cl-arrow">→</span>
        <MaskReveal show delay={400} duration={900}><span className="cl-good">机制保障</span></MaskReveal>
      </h1>
      <div className="cl-team label-mono">
        <span>ASTRA · 星辰</span>
        <span>ST4R0110</span>
        <span>国防科技大学</span>
        <span>ST4R × mushuzhe</span>
      </div>
      <div className="cl-thanks">星辰大海 · 谢谢观看</div>
    </div>
  );
}
