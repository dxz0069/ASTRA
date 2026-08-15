import { MaskReveal } from "../../components/MaskReveal";
import type { ChapterStepProps } from "../../registry/types";
import "./Coldopen.css";

/* 章节视觉语言：扫描线 + 星点网格 + 双色霓虹（青=事实，品红=目标/flag） */

export default function ColdopenChapter({ step }: ChapterStepProps) {
  /* step 0 — 起点与目标：两个终端式输入卡 + 扫描线 */
  if (step === 0) {
    return (
      <div className="co-scene">
        <div className="co-scanline" />
        <div className="co-head label-mono">
          <span className="dot-accent" />&nbsp;ASTRA · autonomous offense engine
        </div>
        <div className="co-io">
          <div className="co-card card">
            <div className="co-card-label label-mono">ORIGIN · 起点</div>
            <div className="co-card-val">10.0.0.12:8080</div>
            <div className="co-card-sub">授权靶场 · Web 应用</div>
          </div>
          <div className="co-arrow">
            <svg viewBox="0 0 120 24" className="co-arrow-svg">
              <line x1="4" y1="12" x2="112" y2="12" stroke="var(--accent)" strokeWidth="1.5" className="co-draw" />
              <polygon points="104,6 116,12 104,18" fill="var(--accent)" />
            </svg>
            <div className="co-arrow-mid label-mono">?</div>
          </div>
          <div className="co-card card co-card-goal">
            <div className="co-card-label label-mono">GOAL · 目标</div>
            <div className="co-card-val co-flag">flag&#123;…&#125;</div>
            <div className="co-card-sub">路径未知</div>
          </div>
        </div>
        <h1 className="co-slogan">
          <MaskReveal show duration={800}>剩下的事，</MaskReveal>
          <MaskReveal show delay={350} duration={800}><span className="co-em">不用人管</span></MaskReveal>
        </h1>
      </div>
    );
  }

  /* step 1 — 自主拿下 flag（不提战绩数字） */
  if (step === 1) {
    return (
      <div className="co-scene">
        <div className="co-flags">
          {Array.from({ length: 30 }).map((_, i) => (
            <span key={i} className="co-flagdot" style={{ animationDelay: `${(i % 15) * 90}ms` }} />
          ))}
        </div>
        <div className="co-hero-wrap">
          <div className="hero-num co-hero-word">AUTONOMOUS</div>
          <h1 className="co-title">
            <MaskReveal show duration={800}>每一面 flag，都是它自主拿下的</MaskReveal>
          </h1>
          <div className="co-brand label-mono">
            <span className="co-brand-name">ASTRA</span>
            <span className="co-brand-cn">星辰 · AI 攻防全链路引擎</span>
          </div>
        </div>
      </div>
    );
  }

  /* step 2 — 星图三原语：星记 / 航向 / 指引 */
  if (step === 2) {
    return (
      <div className="co-scene">
        <h1 className="co-h2">不预设角色，不写死流程。<span className="co-em">只维护一张星图。</span></h1>
        <div className="co-prims">
          <div className="co-prim card">
            <svg viewBox="0 0 64 64" className="co-prim-icon">
              <polygon points="32,8 40,28 60,32 40,36 32,56 24,36 4,32 24,28" fill="var(--accent)" opacity="0.9" />
            </svg>
            <div className="co-prim-name">星记</div>
            <div className="co-prim-desc">已确认的发现<br />携带证据与置信度</div>
          </div>
          <svg viewBox="0 0 120 40" className="co-prim-link">
            <line x1="6" y1="20" x2="114" y2="20" stroke="var(--accent-glow)" strokeWidth="1.5" className="co-draw co-draw-slow" />
            <polygon points="106,14 118,20 106,26" fill="var(--accent-glow)" />
          </svg>
          <div className="co-prim card">
            <svg viewBox="0 0 64 64" className="co-prim-icon">
              <circle cx="32" cy="14" r="7" fill="none" stroke="var(--accent)" strokeWidth="2" />
              <circle cx="12" cy="48" r="5" fill="var(--accent)" opacity="0.5" />
              <circle cx="52" cy="48" r="5" fill="var(--accent)" opacity="0.5" />
              <line x1="32" y1="21" x2="14" y2="43" stroke="var(--accent)" strokeWidth="1.5" />
              <line x1="32" y1="21" x2="50" y2="43" stroke="var(--accent)" strokeWidth="1.5" />
            </svg>
            <div className="co-prim-name">航向</div>
            <div className="co-prim-desc">声明未执行的<br />探索方向</div>
          </div>
          <svg viewBox="0 0 120 40" className="co-prim-link">
            <line x1="6" y1="20" x2="114" y2="20" stroke="var(--accent-glow)" strokeWidth="1.5" className="co-draw co-draw-slow" />
            <polygon points="106,14 118,20 106,26" fill="var(--accent-glow)" />
          </svg>
          <div className="co-prim card">
            <svg viewBox="0 0 64 64" className="co-prim-icon">
              <path d="M32 8 L52 20 L52 44 L32 56 L12 44 L12 20 Z" fill="none" stroke="var(--accent)" strokeWidth="2" />
              <circle cx="32" cy="32" r="6" fill="var(--accent-glow)" />
            </svg>
            <div className="co-prim-name">指引</div>
            <div className="co-prim-desc">随时注入的判断<br />与风险提示</div>
          </div>
        </div>
      </div>
    );
  }

  /* step 3 — 全流程管线：下发 → 生长 → flag → 提交 → 回收 */
  return (
    <div className="co-scene">
      <h1 className="co-h2">题目下发到成绩回收，<span className="co-em">全程无人值守</span></h1>
      <div className="co-pipeline">
        {[
          { k: "01", t: "题目下发", s: "astra-runner" },
          { k: "02", t: "星图生长", s: "scout × fleet" },
          { k: "03", t: "flag 出现", s: "stellar · flag{…}" },
          { k: "04", t: "统一提交", s: "idempotent" },
          { k: "05", t: "成绩回收", s: "score report" },
        ].map((n, i) => (
          <div key={n.k} className="co-pipe card" style={{ animationDelay: `${i * 260}ms` }}>
            <div className="co-pipe-k label-mono">{n.k}</div>
            <div className="co-pipe-t">{n.t}</div>
            <div className="co-pipe-s label-mono">{n.s}</div>
            {i < 4 && <div className="co-pipe-line" style={{ animationDelay: `${i * 260 + 200}ms` }} />}
          </div>
        ))}
      </div>
      <div className="co-foot label-mono">
        <span className="dot-accent" />&nbsp;human-in-the-loop → <span className="co-em">0</span>
      </div>
    </div>
  );
}
