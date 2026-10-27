import type { ComponentType } from "react";
import {
  Briefcase,
  Building2,
  Camera,
  Code2,
  Contrast,
  Cpu,
  Gamepad2,
  Gift,
  Globe2,
  GraduationCap,
  Hammer,
  HeartHandshake,
  Landmark,
  Laptop,
  Lightbulb,
  Megaphone,
  Palette,
  PenTool,
  Rocket,
  Shapes,
  Sparkles,
  Trophy,
  Type,
  Users,
  Waves,
} from "lucide-react";

/**
 * A fitting picture for a line of the brand profile ("Parents", "Free for
 * everyone", "Real campus photography"), found from the words in it. The
 * first rule that matches wins; anything unknown gets the sparkle.
 */
const RULES: Array<[RegExp, ComponentType<any>]> = [
  [/\btype\b|typeface|font|lettering|headline|typograph/i, Type],
  [/pixel|voxel|isometric|3d|illustrat|shape|geometr|icon/i, Shapes],
  [/\bparent|famil|mother|father|guardian/i, HeartHandshake],
  [/compan|partner|employer|business|corporate|industr|sponsor|recruit/i, Building2],
  [/career|profession|working adult|job|employ|salary|switch/i, Briefcase],
  [/government|ministry|\bnation|\buae\b|emirat|abu dhabi|public sector/i, Landmark],
  [/international|global|world|expat|divers|culture/i, Globe2],
  [/piscine|pool|dive|swim/i, Waves],
  [/free|tuition|no fee|cost|scholar/i, Gift],
  [/student|school|learn|youth|young|graduate|teen|leaver|educat/i, GraduationCap],
  [/project|build|hands-on|doing|practical|make/i, Hammer],
  [/communit|peer|team|together|friend|social|network/i, Users],
  [/\bai\b|artificial|robot|machine|cyber|data|tech/i, Cpu],
  [/code|coding|program|developer|software/i, Code2],
  [/game|gaming|play|fun/i, Gamepad2],
  [/innovat|future|launch|grow|ambit/i, Rocket],
  [/success|achiev|win|award|alumni|story|stories/i, Trophy],
  [/idea|creativ|curio|think/i, Lightbulb],
  [/photo|camera|shot|portrait|campus|scene/i, Camera],
  [/contrast|black|white|bold/i, Contrast],
  [/colou?r|palette|neon|bright/i, Palette],
  [/draw|design|art|brush/i, PenTool],
  [/laptop|screen|computer|lab|desk/i, Laptop],
  [/event|announce|news|launch|open day|apply|admission/i, Megaphone],
];

/** The label is asked first, then the description. */
export function topicIcon(...texts: Array<string | null | undefined>): ComponentType<any> {
  for (const t of texts) {
    if (!t) continue;
    for (const [re, Icon] of RULES) if (re.test(t)) return Icon;
  }
  return Sparkles;
}
