import type { ChapterDef } from "./types";
import CoverChapter from "../chapters/00-cover/Cover";
import { narrations as coverNarrations } from "../chapters/00-cover/narrations";
import ColdopenChapter from "../chapters/01-coldopen/Coldopen";
import { narrations as coldopenNarrations } from "../chapters/01-coldopen/narrations";
import StardustChapter from "../chapters/02-stardust/Stardust";
import { narrations as stardustNarrations } from "../chapters/02-stardust/narrations";
import DualstarChapter from "../chapters/03-dualstar/Dualstar";
import { narrations as dualstarNarrations } from "../chapters/03-dualstar/narrations";
import FleetChapter from "../chapters/04-fleet/Fleet";
import { narrations as fleetNarrations } from "../chapters/04-fleet/narrations";
import ClosingChapter from "../chapters/05-closing/Closing";
import { narrations as closingNarrations } from "../chapters/05-closing/narrations";

export const CHAPTERS: ChapterDef[] = [
  {
    id: "cover",
    title: "封面",
    narrations: coverNarrations,
    Component: CoverChapter,
  },
  {
    id: "coldopen",
    title: "开场 · ASTRA 是什么",
    narrations: coldopenNarrations,
    Component: ColdopenChapter,
  },
  {
    id: "stardust",
    title: "机制一 · 星尘记忆",
    narrations: stardustNarrations,
    Component: StardustChapter,
  },
  {
    id: "dualstar",
    title: "机制二 · 双星决策",
    narrations: dualstarNarrations,
    Component: DualstarChapter,
  },
  {
    id: "fleet",
    title: "混合舰队与实测",
    narrations: fleetNarrations,
    Component: FleetChapter,
  },
  {
    id: "closing",
    title: "收尾",
    narrations: closingNarrations,
    Component: ClosingChapter,
  },
];
