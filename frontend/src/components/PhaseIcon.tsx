import {
  Atom,
  BookOpen,
  Compass,
  Download,
  Eye,
  FileSearch2,
  FileText,
  Film,
  Globe,
  ImageDown,
  Layers,
  Palette,
  Scissors,
  Sparkles,
  Wand2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

const ICONS: Record<string, LucideIcon> = {
  discovery: Compass,
  fetch: Globe,
  extract: FileText,
  chunk: Scissors,
  summarize: Sparkles,
  embed_text: Atom,
  "download:image": ImageDown,
  image: Eye,
  "download:video": Download,
  video: Film,
  synthesize: Wand2,
  doc_process: FileSearch2,
  doc_summarize_embed: BookOpen,
  pdf_pages: Layers,
  brand_extract: Palette,
};

interface Props {
  phase: string;
  className?: string;
  strokeWidth?: number;
}

export function PhaseIcon({ phase, className, strokeWidth = 1.8 }: Props) {
  const Icon = ICONS[phase] ?? FileText;
  return <Icon className={className ?? "w-4 h-4"} strokeWidth={strokeWidth} />;
}
