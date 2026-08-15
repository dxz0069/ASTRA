import { MaskReveal } from "../../components/MaskReveal";
import type { ChapterStepProps } from "../../registry/types";
import "./Cover.css";

/**
 * 封面：ASTRA 星座徽记 + 战队名 S10WD0WN。
 * 徽记语言：导航星（青，主星）+ 倾斜轨道环（品红）+ 三颗卫星星点 —— 星图引擎的具象化。
 */

function StarMark() {
  return (
    <svg viewBox="0 0 320 320" className="cv-mark" aria-label="ASTRA logo">
      {/* 径向导航线 */}
      <g className="cv-rays" stroke="var(--accent)" strokeWidth="1">
        <line x1="160" y1="160" x2="160" y2="18" opacity="0.35" />
        <line x1="160" y1="160" x2="302" y2="160" opacity="0.18" />
        <line x1="160" y1="160" x2="160" y2="302" opacity="0.18" />
        <line x1="160" y1="160" x2="18" y2="160" opacity="0.35" />
      </g>

      {/* 倾斜轨道环（品红） */}
      <g className="cv-orbit">
        <ellipse
          cx="160" cy="160" rx="128" ry="52"
          fill="none" stroke="var(--accent-glow)" strokeWidth="2"
          transform="rotate(-24 160 160)"
          className="cv-orbit-path"
        />
        {/* 卫星星点（随环旋转） */}
        <circle cx="160" cy="34" r="6" fill="var(--accent-glow)" className="cv-sat cv-sat-1" />
        <circle cx="283" cy="212" r="4.5" fill="var(--accent-glow)" className="cv-sat cv-sat-2" />
        <circle cx="52" cy="220" r="4.5" fill="var(--accent-glow)" className="cv-sat cv-sat-3" />
      </g>

      {/* 内环（青，细） */}
      <circle cx="160" cy="160" r="86" fill="none" stroke="var(--accent)" strokeWidth="1" opacity="0.4" className="cv-inner-ring" />

      {/* 主导航星：四芒星 */}
      <g className="cv-star">
        <polygon
          points="160,74 172,148 246,160 172,172 160,246 148,172 74,160 148,148"
          fill="var(--accent)"
        />
        <polygon
          points="160,108 166,154 212,160 166,166 160,212 154,166 108,160 154,154"
          fill="var(--text)"
          opacity="0.92"
        />
      </g>

      {/* 外辉光 */}
      <circle cx="160" cy="160" r="60" fill="url(#cv-glow)" className="cv-halo" />
      <defs>
        <radialGradient id="cv-glow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.35" />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
        </radialGradient>
      </defs>
    </svg>
  );
}

export default function CoverChapter(_props: ChapterStepProps) {
  return (
    <div className="cv-scene">
      <StarMark />
      <div className="cv-title">
        <MaskReveal show duration={900}>
          <span className="cv-word">ASTRA</span>
        </MaskReveal>
      </div>
      <div className="cv-sub">星辰 · AI 攻防全链路引擎</div>
      <div className="cv-team">
        <span className="cv-team-bracket">[</span>
        <span className="cv-team-name">S10WD0WN</span>
        <span className="cv-team-bracket">]</span>
        <span className="cv-cursor" />
      </div>
      <div className="cv-org label-mono">National University of Defense Technology</div>
    </div>
  );
}
