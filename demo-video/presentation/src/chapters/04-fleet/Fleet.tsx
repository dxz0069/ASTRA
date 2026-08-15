import type { ChapterStepProps } from "../../registry/types";
import "./Fleet.css";

/* 章节视觉语言：青=DeepSeek（快攻），品红=GLM（深挖）；实测数据全部真实 */


export default function FleetChapter({ step }: ChapterStepProps) {
  /* step 0 — 舰队编成 */
  if (step === 0) {
    return (
      <div className="fr-scene fr-center">
        <div className="fr-kicker label-mono">FLEET · 混合模型舰队</div>
        <h1 className="fr-h1"><span className="fr-ds">DeepSeek</span> 负责探索，快、稳、便宜</h1>
        <div className="fr-fleet">
          <div className="fr-squad card">
            <div className="fr-squad-name">deepseek-main</div>
            <div className="fr-pods">
              {["dsh", "dsh", "dsh"].map((_, i) => <span key={i} className="fr-pod fr-pod-ds" style={{ animationDelay: `${i * 140}ms` }} />)}
            </div>
            <div className="fr-squad-meta label-mono">explore ×3 · p0</div>
          </div>
          <div className="fr-squad card">
            <div className="fr-squad-name">deepseek-fallback</div>
            <div className="fr-pods">
              {["pi", "pi"].map((_, i) => <span key={i} className="fr-pod fr-pod-ds fr-pod-dim" style={{ animationDelay: `${i * 140}ms` }} />)}
            </div>
            <div className="fr-squad-meta label-mono">fallback ×2 · p3</div>
          </div>
        </div>
        <div className="fr-traits label-mono">
          <span>fast · 吞吐高</span><span>stable · 工具调用稳</span><span>cheap · 成本低</span>
        </div>
      </div>
    );
  }

  /* step 1 — GLM 决策位 */
  if (step === 1) {
    return (
      <div className="fr-scene">
        <div className="fr-copy">
          <div className="fr-kicker label-mono">DECISION LAYER</div>
          <h1 className="fr-h1-sm"><span className="fr-glm">GLM-5.3</span> 最高推理档<br />负责决策</h1>
          <p className="fr-p">低频高价值——定航、质询、裁决、整理。<br />reasoning_effort = <span className="fr-glm">max</span></p>
        </div>
        <div className="fr-demo card fr-brain-wrap">
          <div className="fr-brain">
            <div className="fr-brain-core">max</div>
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="fr-brain-ring" style={{ animationDelay: `${i * 300}ms` }} />
            ))}
          </div>
          <div className="fr-brain-meta label-mono">glm-reason ×2 · 1M ctx · deep thinking</div>
        </div>
      </div>
    );
  }

  /* step 2 — 同题多路并进 */
  if (step === 2) {
    return (
      <div className="fr-scene fr-center">
        <h1 className="fr-h1-center">同一道题，多个航向，<span className="fr-ds">快攻</span> + <span className="fr-glm">深挖</span></h1>
        <div className="fr-races">
          <div className="fr-race card">
            <div className="fr-race-q label-mono">challenge · a-07</div>
            <div className="fr-lane">
              <div className="fr-lane-label fr-ds">DS</div>
              <div className="fr-track"><div className="fr-runner fr-runner-ds" /></div>
            </div>
            <div className="fr-lane">
              <div className="fr-lane-label fr-glm">GLM</div>
              <div className="fr-track"><div className="fr-runner fr-runner-glm" /></div>
            </div>
            <div className="fr-race-goal label-mono">flag</div>
          </div>
          <div className="fr-race card">
            <div className="fr-race-q label-mono">challenge · f2-05</div>
            <div className="fr-lane">
              <div className="fr-lane-label fr-ds">DS</div>
              <div className="fr-track"><div className="fr-runner fr-runner-ds fr-runner-slow" /></div>
            </div>
            <div className="fr-lane">
              <div className="fr-lane-label fr-glm">GLM</div>
              <div className="fr-track"><div className="fr-runner fr-runner-glm fr-runner-fast" /></div>
            </div>
            <div className="fr-race-goal label-mono">flag</div>
          </div>
        </div>
        <p className="fr-p">不同模型解不同的题——多路并进，互为冗余。</p>
      </div>
    );
  }

  /* step 3 — 赛场设定：74 flag（平台事实） */
  if (step === 3) {
    return (
      <div className="fr-scene fr-center">
        <div className="fr-kicker label-mono">ARENA · TSecBench v1 旗舰集</div>
        <div className="hero-num fr-74">74</div>
        <h1 className="fr-h1-center">面 flag · 63 道题 · 满分 23000</h1>
        <div className="fr-dims label-mono">
          <span>WEB 20%</span><span>EXPLOIT 20%</span><span>KILLCHAIN 20%</span><span>BINARY 15%</span><span>CLOUD 15%</span><span>EVASION 10%</span>
        </div>
      </div>
    );
  }

  /* step 4 — 复盘机制（工程保障，不提战绩） */
  if (step === 4) {
    const mechs = [
      { t: "开题排序", s: "easy → medium → hard", d: "16 道排队饿死题修复" },
      { t: "分级提示", s: "15 / 30 min hint", d: "卡题自动取平台提示" },
      { t: "断点续跑", s: "progress file", d: "崩溃重启不重跑" },
    ];
    return (
      <div className="fr-scene fr-center">
        <h1 className="fr-h1-center">每一轮的失分，都复盘成<span className="fr-good">机制</span></h1>
        <div className="fr-mechs">
          {mechs.map((m, i) => (
            <div key={m.t} className="fr-mech card" style={{ animationDelay: `${i * 220}ms` }}>
              <div className="fr-mech-t">{m.t}</div>
              <div className="fr-mech-s label-mono">{m.s}</div>
              <div className="fr-mech-d">{m.d}</div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  /* step 5 — 测试回归 */
  return (
    <div className="fr-scene fr-center">
      <div className="fr-shield card">
        <svg viewBox="0 0 64 64" className="fr-shield-icon">
          <path d="M32 4 L56 14 L56 34 C56 48 46 56 32 60 C18 56 8 48 8 34 L8 14 Z" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
          <polyline points="20,32 29,41 45,22" fill="none" stroke="var(--accent)" strokeWidth="3" />
        </svg>
        <div className="hero-num fr-tests">160</div>
        <div className="fr-shield-t">项测试全过</div>
        <div className="fr-shield-s label-mono">each fix ships with regression tests</div>
      </div>
      <h1 className="fr-h1-center">四轮迭代，全部有<span className="fr-good">测试回归</span>兜底</h1>
    </div>
  );
}
