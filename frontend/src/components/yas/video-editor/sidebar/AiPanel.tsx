/**
 * Video Editor · AI assist tab (Phase 3)
 *
 * One feature = one backend job (POST /video-editor/{id}/ai/*). The store
 * (runAi) flushes local edits, pauses autosave, posts the job and polls
 * /ai/status; on done it refetches the doc and applies it as ONE undo step —
 * so every AI edit is undoable. While a job runs, this panel shows the live
 * status/progress instead of the buttons (the timeline stays interactive).
 */
import { useEffect, useRef, useState } from "react";

import { api } from "../../../../lib/api";
import { aiStatusLabel, useVideoEditorStore, type ChatMsg } from "../../../../lib/store";
import type { VideoEditAgentStep, VideoEditVoiceClone } from "../../../../lib/types";
import {
  attachmentForApi,
  attachmentRefKey,
  makeAttachmentUid,
  parseAttachmentDrop,
  type ChatAttachment,
} from "./attachmentDnd";

/** Collapsible group so the (many) AI tools stay organized + accessible without
 * a wall of buttons. `defaultOpen` opens it on first render. */
function Section({
  title,
  defaultOpen = false,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-2.5 py-1.5 bg-bg-muted/60 hover:bg-bg-muted transition-colors"
      >
        <span className="eyebrow">{title}</span>
        <span className="text-3xs text-muted">{open ? "▾" : "▸"}</span>
      </button>
      {open && <div className="p-2.5 space-y-3">{children}</div>}
    </div>
  );
}

/** One message bubble in the assistant chat thread. */
function ChatBubble({ msg }: { msg: ChatMsg }) {
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <span className="max-w-[85%] rounded-lg rounded-br-sm bg-accent/15 text-fg px-2 py-1 text-[10.5px] break-words">
          {msg.text}
        </span>
      </div>
    );
  }
  const tone =
    msg.kind === "error" ? "text-danger" : msg.kind === "done" ? "text-fg" : "text-fg/85";
  return (
    <div className="flex justify-start">
      <span
        className={`max-w-[85%] rounded-lg rounded-bl-sm bg-bg-muted px-2 py-1 text-[10.5px] break-words ${tone}`}
      >
        {msg.kind === "done" ? "✓ " : ""}
        {msg.text}
      </span>
    </div>
  );
}

/** The agent's tool names in plain words (job.agent_steps[].tool). Anything
 * unmapped falls back to the raw name with the underscores knocked out. */
const AGENT_TOOL_LABEL: Record<string, string> = {
  inspect_timeline: "Checked the timeline",
  read_transcript: "Read the transcript",
  look: "Watched the video",
  preview: "Previewed the edit",
  suggest_overlay_slots: "Looked for gaps to fill",
  search_assets: "Searched your assets",
  get_popup_examples: "Looked at some examples",
  read_edit_log: "Reviewed earlier edits",
  generate_image: "Made an image",
  run_pass: "Ran an edit pass",
  edit: "Edited the timeline",
  finish: "Wrapped up",
};

const agentToolLabel = (tool: string) =>
  AGENT_TOOL_LABEL[tool] || tool.replace(/_/g, " ");

/** Live "what the AI is doing" list — one row per tool call, oldest first.
 * Rows are ~8 deep and scrollable; while `live`, it sticks to the newest row. */
function AgentSteps({ steps, live }: { steps: VideoEditAgentStep[]; live?: boolean }) {
  const endRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (live) endRef.current?.scrollIntoView({ block: "end" });
  }, [steps.length, live]);
  if (steps.length === 0) return null;
  return (
    <div className="rounded-md border border-border bg-bg-muted/50 p-1.5 max-h-[136px] overflow-y-auto space-y-0.5">
      {steps.map((s, i) => (
        <div
          key={`${s.step}-${i}`}
          className="flex items-start gap-1 text-[9.5px] leading-snug"
          title={s.result}
        >
          <span className={s.ok ? "shrink-0 text-accent" : "shrink-0 text-danger"}>
            {s.ok ? "✓" : "✕"}
          </span>
          <span className="shrink-0 text-fg/80">{agentToolLabel(s.tool)}</span>
          {s.result ? (
            <span className="text-muted truncate">— {s.result}</span>
          ) : null}
          {s.images ? (
            <span className="shrink-0 text-muted/70">🖼{s.images}</span>
          ) : null}
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}

/** Which reasoning brain is live. Unobtrusive by design — the reason is the
 * tooltip; a failed probe turns it into a warning (the reason is then also
 * spelled out under the header). */
/** Per-kind chip glyph for attachments dragged in from the left sidebar. */
const ATT_ICON: Record<string, string> = {
  sticker: "✦",
  font: "Aa",
  audio: "♪",
  media: "🎬",
  shape: "◻",
  text: "T",
};

/** Why the voice controls need a clip — shown inline + as the disabled tooltip. */
const SELECT_CLIP_MSG =
  "Pick a video clip on the timeline first, and I will redo its sound in your voice.";

/** api.ts http() throws `Error("422 …human message…")` — strip the code. */
const stripStatus = (e: unknown) =>
  (e instanceof Error ? e.message : String(e)).replace(/^\d{3}\s+/, "");

/**
 * Upload-or-record audio control. Recording uses MediaRecorder (webm, mp4
 * fallback on Safari); the resulting File is handed to onAudio. When the mic
 * is unsupported/blocked we show a hint and the upload path still works.
 */
function AudioCapture({
  disabled,
  disabledReason,
  busy,
  busyLabel,
  onAudio,
  onError,
}: {
  disabled?: boolean;
  /** Why the controls are disabled — shown as the buttons' tooltip. */
  disabledReason?: string;
  busy?: boolean;
  busyLabel?: string;
  onAudio: (file: File) => void;
  /** Surface recording failures in the panel's main error banner. */
  onError?: (msg: string) => void;
}) {
  const [recording, setRecording] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [secs, setSecs] = useState(0);
  const [hint, setHint] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);
  // Sync re-entrancy guard (state updates are async — a double-click would
  // otherwise race two getUserMedia calls and leak a mic stream).
  const requestingRef = useRef(false);

  // Recording failures must be VISIBLE — main banner when wired, inline hint otherwise.
  const report = (msg: string) => {
    if (onError) onError(msg);
    else setHint(msg);
  };

  const stopTimer = () => {
    if (timerRef.current != null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  };

  // Unmount mid-recording: stop the recorder + release the mic, drop the take.
  useEffect(
    () => () => {
      stopTimer();
      const rec = recRef.current;
      if (rec && rec.state !== "inactive") {
        rec.onstop = null;
        rec.stop();
        rec.stream.getTracks().forEach((t) => t.stop());
      }
    },
    [],
  );

  const startRecording = async () => {
    // Re-entrancy guard: ignore clicks while already requesting or recording.
    if (requestingRef.current || (recRef.current && recRef.current.state === "recording"))
      return;
    setHint(null);
    if (!navigator.mediaDevices?.getUserMedia) {
      report("Recording needs https or localhost.");
      return;
    }
    if (typeof MediaRecorder === "undefined") {
      report("Recording isn't supported in this browser — upload a file instead.");
      return;
    }
    // Release any stray previous recorder/stream so we never leak a second mic.
    const prev = recRef.current;
    if (prev) {
      if (prev.state !== "inactive") {
        prev.onstop = null;
        prev.stop();
      }
      prev.stream.getTracks().forEach((t) => t.stop());
      recRef.current = null;
    }
    stopTimer();
    requestingRef.current = true;
    setRequesting(true);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      requestingRef.current = false;
      setRequesting(false);
      const name = err instanceof Error ? err.name : "";
      report(
        name === "NotAllowedError" || name === "SecurityError"
          ? "Allow microphone access to record (check the browser's mic permission)."
          : name === "NotFoundError"
            ? "No microphone found."
            : `Couldn't start recording${name ? ` (${name})` : ""} — upload a file instead.`,
      );
      return;
    }
    requestingRef.current = false;
    setRequesting(false);
    const mime = MediaRecorder.isTypeSupported("audio/webm")
      ? "audio/webm"
      : MediaRecorder.isTypeSupported("audio/mp4")
        ? "audio/mp4"
        : "";
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    chunksRef.current = [];
    rec.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    rec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      stopTimer();
      setRecording(false);
      const type = rec.mimeType || mime || "audio/webm";
      const ext = type.includes("mp4") ? "mp4" : "webm";
      onAudio(new File(chunksRef.current, `my-voice.${ext}`, { type }));
    };
    recRef.current = rec;
    rec.start();
    setRecording(true);
    setSecs(0);
    timerRef.current = window.setInterval(() => setSecs((v) => v + 1), 1000);
  };

  const stopRecording = () => {
    const rec = recRef.current;
    if (rec && rec.state !== "inactive") rec.stop();
  };

  const mmss = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;

  return (
    <div className="space-y-1">
      <div className="grid grid-cols-2 gap-1">
        <button
          onClick={() => fileRef.current?.click()}
          disabled={disabled || busy || recording || requesting}
          title={disabled && disabledReason ? disabledReason : undefined}
          className="btn btn-ghost text-[10.5px] py-1 border border-border disabled:opacity-50"
        >
          Use a file
        </button>
        <button
          onClick={() => (recording ? stopRecording() : void startRecording())}
          disabled={disabled || busy}
          title={
            disabled && disabledReason
              ? disabledReason
              : recording
                ? "Stop recording"
                : undefined
          }
          className={[
            "btn text-[10.5px] py-1 border disabled:opacity-50",
            recording
              ? "border-danger/50 text-danger bg-danger/10"
              : "btn-ghost border-border",
          ].join(" ")}
        >
          {recording
            ? `■ Recording ${mmss}`
            : requesting
              ? "Asking to use your mic…"
              : "● Record"}
        </button>
      </div>
      <input
        ref={fileRef}
        type="file"
        accept="audio/*"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = ""; // allow re-picking the same file
          if (f) onAudio(f);
        }}
      />
      {busy && (
        <p className="flex items-center gap-1.5 text-3xs text-muted">
          <span className="w-2.5 h-2.5 shrink-0 rounded-full border-2 border-accent border-t-transparent animate-spin" />
          {busyLabel || "Uploading…"}
        </p>
      )}
      {hint && <p className="text-[9.5px] text-danger">{hint}</p>}
    </div>
  );
}

export function AiPanel() {
  const aiBusy = useVideoEditorStore((s) => s.aiBusy);
  const aiJob = useVideoEditorStore((s) => s.aiJob);
  const aiError = useVideoEditorStore((s) => s.aiError);
  const runAi = useVideoEditorStore((s) => s.runAi);
  const syncAiStatus = useVideoEditorStore((s) => s.syncAiStatus);
  const agentProviders = useVideoEditorStore((s) => s.agentProviders);
  const syncAgentProviders = useVideoEditorStore((s) => s.syncAgentProviders);
  const dismissAiError = useVideoEditorStore((s) => s.dismissAiError);
  const selection = useVideoEditorStore((s) => s.selection);
  const select = useVideoEditorStore((s) => s.select);
  const clips = useVideoEditorStore((s) => s.clips);
  const aiChat = useVideoEditorStore((s) => s.aiChat);
  const runChat = useVideoEditorStore((s) => s.runChat);
  const undo = useVideoEditorStore((s) => s.undo);
  const canUndo = useVideoEditorStore((s) => s.canUndo);
  const multiSelection = useVideoEditorStore((s) => s.multiSelection);
  const selectClipsByKind = useVideoEditorStore((s) => s.selectClipsByKind);
  const clearMultiSelection = useVideoEditorStore((s) => s.clearMultiSelection);
  const selectMode = useVideoEditorStore((s) => s.selectMode);
  const setSelectMode = useVideoEditorStore((s) => s.setSelectMode);

  const [chatInput, setChatInput] = useState("");
  // Items dragged from the left sidebar onto the chat — sent with the next
  // message so the AI applies the EXACT item (concrete id), not a guess.
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [dragOver, setDragOver] = useState(false);
  // "My voice" clone — undefined = still loading (or lookup failed), null = none saved.
  const [myVoice, setMyVoice] = useState<VideoEditVoiceClone | null | undefined>(undefined);
  const [voiceLookupFailed, setVoiceLookupFailed] = useState(false);
  const [voiceLookupAttempt, setVoiceLookupAttempt] = useState(0);
  const [replacingVoice, setReplacingVoice] = useState(false);
  const [cloneBusy, setCloneBusy] = useState(false);
  const [asIsBusy, setAsIsBusy] = useState(false);
  const [useAsIs, setUseAsIs] = useState(false);
  const [voiceError, setVoiceError] = useState<string | null>(null);
  // The store keeps aiJob after `done` so we can show the summary; track which
  // job ids the user has dismissed (local — no store churn).
  const [dismissedJobId, setDismissedJobId] = useState<string | null>(null);
  // After an agent run, its step list collapses behind "Show what I did".
  const [showAgentSteps, setShowAgentSteps] = useState(false);

  // Learn analysis_ready + adopt a job already running (page reload mid-job).
  useEffect(() => {
    void syncAiStatus();
  }, [syncAiStatus]);

  // Which reasoning brain is live (one GET per session — the store no-ops after).
  useEffect(() => {
    void syncAgentProviders();
  }, [syncAgentProviders]);

  // Look up the saved "my voice" clone on mount (and again on Retry). An
  // ERROR is not "no clone" — keep myVoice undefined and flag the failure so
  // the UI can offer a retry instead of telling the user to record first.
  useEffect(() => {
    let cancelled = false;
    setVoiceLookupFailed(false);
    api.yasProducer.videoEditor.ai
      .getVoiceClone()
      .then((c) => {
        if (!cancelled) setMyVoice(c);
      })
      .catch(() => {
        if (!cancelled) setVoiceLookupFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [voiceLookupAttempt]);

  // voiceError is panel-local — don't let a stale voice banner sit over an
  // unrelated AI job (Edit/Compose/Music/…).
  useEffect(() => {
    if (aiBusy) setVoiceError(null);
  }, [aiBusy]);

  // Convenience: if the timeline has exactly one video clip, select it so the
  // voice tools are ready to go. One-shot; never overrides a user selection.
  const autoSelectedRef = useRef(false);
  useEffect(() => {
    if (autoSelectedRef.current || clips.length === 0) return;
    autoSelectedRef.current = true;
    if (selection) return;
    const videoClips = clips.filter((c) => (!c.kind || c.kind === "video") && c.source);
    if (videoClips.length === 1) select({ kind: "clip", clipId: videoClips[0].id });
  }, [clips, selection, select]);

  // A fresh job gets a fresh (collapsed) step list.
  useEffect(() => {
    setShowAgentSteps(false);
  }, [aiJob?.id]);

  // Keep the chat thread scrolled to the latest message.
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ block: "end" });
  }, [aiChat.length, aiBusy, aiJob?.agent_steps?.length]);

  // Auto-grow the chat textarea up to its max-height (resets when sent/cleared).
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(128, ta.scrollHeight)}px`;
  }, [chatInput]);

  const sendChat = () => {
    if (aiBusy) return;
    // A bare drop+send (no typed words) defaults to "use the attached item(s)".
    const typed = chatInput.trim();
    const text = typed || (attachments.length ? "use the attached item(s)" : "");
    if (!text) return;
    setChatInput("");
    const atts = attachments.length ? attachments.map(attachmentForApi) : null;
    setAttachments([]);
    // Scope commands to the multi-selection, else the single selected clip.
    const ids = multiSelection.length > 0 ? multiSelection : selClipId ? [selClipId] : [];
    // Drop out of "Select items" mode so the timeline is draggable again; KEEP
    // the marks visible (they stay until Clear or a fresh selection).
    setSelectMode(false);
    void runChat(text, ids, atts);
  };

  // --- drag-and-drop: sidebar items → chat attachments ---
  const onChatDragOver = (e: React.DragEvent) => {
    if (e.dataTransfer.types.includes("application/x-ve-chat-attachment")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      if (!dragOver) setDragOver(true);
    }
  };
  const onChatDrop = (e: React.DragEvent) => {
    const parsed = parseAttachmentDrop(e.dataTransfer);
    setDragOver(false);
    if (!parsed) return;
    e.preventDefault();
    const att: ChatAttachment = { ...parsed, uid: makeAttachmentUid() };
    setAttachments((prev) =>
      prev.some((p) => attachmentRefKey(p) === attachmentRefKey(att)) ? prev : [...prev, att],
    );
  };
  const removeAttachment = (uid: string) =>
    setAttachments((prev) => prev.filter((a) => a.uid !== uid));

  const selClipId = selection?.kind === "clip" ? selection.clipId : null;
  const selClip = selClipId ? clips.find((c) => c.id === selClipId) : null;
  const selectedClips = clips.filter((c) => multiSelection.includes(c.id));
  const selBreakdown = (() => {
    const counts: Record<string, number> = {};
    for (const c of selectedClips) {
      const k = c.kind || "video";
      counts[k] = (counts[k] || 0) + 1;
    }
    return Object.entries(counts)
      .map(([k, n]) => `${n} ${k}`)
      .join(", ");
  })();
  const videoClipSelected =
    !!selClip && (!selClip.kind || selClip.kind === "video") && !!selClip.source;

  const doneJob =
    !aiBusy && aiJob && aiJob.status === "done" && aiJob.id !== dismissedJobId
      ? aiJob
      : null;

  // Agent runs narrate themselves — live while working, collapsed after.
  const agentSteps = aiJob?.agent_steps ?? [];

  // --- "My voice" handlers --------------------------------------------------

  // Clone the provided sample (upload or recording) into "my voice".
  const onCloneAudio = async (file: File) => {
    setCloneBusy(true);
    setVoiceError(null);
    try {
      const clone = await api.yasProducer.videoEditor.ai.voiceClone(file, "My voice");
      setVoiceError(null);
      setMyVoice(clone);
      setReplacingVoice(false);
    } catch (e) {
      // 422 = no ElevenLabs key / cloning needs a paid plan — show as-is.
      setVoiceError(stripStatus(e));
    } finally {
      setCloneBusy(false);
    }
  };

  const onRemoveClone = async () => {
    setCloneBusy(true);
    setVoiceError(null);
    try {
      await api.yasProducer.videoEditor.ai.deleteVoiceClone();
      setMyVoice(null);
      setReplacingVoice(false);
    } catch (e) {
      setVoiceError(stripStatus(e));
    } finally {
      setCloneBusy(false);
    }
  };

  // Re-voice the selected clip with the saved clone.
  const onChangeVoice = () => {
    setVoiceError(null);
    if (!selClipId) return;
    if (myVoice === undefined) {
      // Still loading, or the lookup failed (button is disabled then anyway).
      if (voiceLookupFailed)
        setVoiceError("Couldn't check your voice — is the backend up?");
      return;
    }
    if (!myVoice) {
      setVoiceError(
        "Record or upload your voice first — use the My-voice card above.",
      );
      return;
    }
    void runAi("voice", { clip_id: selClipId, useCloned: true });
  };

  // "Use as-is": upload the audio to the assets library, then hand the upload
  // id to the voice job — it replaces the clip audio directly (no cloning).
  const onAsIsAudio = async (file: File) => {
    if (!selClipId) return;
    setAsIsBusy(true);
    setVoiceError(null);
    try {
      const up = await api.yasProducer.videoEditor.uploadAudio(file);
      await runAi("voice", { clip_id: selClipId, useAsIs: true, sourceUploadId: up.id });
    } catch (e) {
      setVoiceError(stripStatus(e));
    } finally {
      setAsIsBusy(false);
    }
  };

  return (
    <div className="h-full flex flex-col">
      <div className="shrink-0 p-2.5 border-b border-border flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="eyebrow">AI assistant</span>
          {agentProviders && !agentProviders.ok && (
            <span
              title={
                `${agentProviders.model || "—"}${
                  agentProviders.reason ? ` — ${agentProviders.reason}` : ""
                }`
              }
              className={[
                "inline-flex items-center gap-1 shrink-0 rounded-full border px-1.5 py-0.5 text-[9px] leading-none",
                agentProviders.ok
                  ? "border-border text-muted"
                  : "border-warning/40 bg-warning/10 text-warning",
              ].join(" ")}
            >
              ⚠<span className="truncate max-w-[92px]">AI is offline</span>
            </span>
          )}
        </div>
        {canUndo && (
          <button
            onClick={() => undo()}
            title="Undo the last change (Ctrl+Z)"
            className="text-3xs text-muted hover:text-fg transition-colors"
          >
            ↶ Undo
          </button>
        )}
      </div>

      <div className="flex-1 min-h-0 p-2.5 flex flex-col gap-3">
        {/* ===== Selection scope — what the chatbot acts on ===== */}
        <div
          className={[
            "shrink-0 rounded-lg border bg-bg p-2 space-y-1.5 transition-colors",
            selectMode ? "border-accent ring-1 ring-accent/40" : "border-border",
          ].join(" ")}
        >
          <div className="flex items-center justify-between gap-1">
            <button
              onClick={() => setSelectMode(!selectMode)}
              title="Turn this on, then click the clips you want me to work on"
              className={[
                "flex items-center gap-1 rounded-md px-2 py-1 text-2xs font-medium border transition-colors",
                selectMode
                  ? "border-accent bg-accent/15 text-accent"
                  : "border-border text-muted hover:bg-bg-muted hover:text-fg",
              ].join(" ")}
            >
              ✦{" "}
              {selectMode
                ? selectedClips.length > 0
                  ? `Picking… (${selectedClips.length})`
                  : "Picking…"
                : "Pick clips"}
            </button>
            <div className="flex items-center gap-1">
              {multiSelection.length > 0 && (
                <button
                  onClick={clearMultiSelection}
                  className="text-3xs text-muted hover:text-danger transition-colors"
                >
                  Clear
                </button>
              )}
            </div>
          </div>
          <div className="flex flex-wrap gap-1">
            {(
              [
                ["all", "All"],
                ["video", "Video"],
                ["audio", "Audio"],
                ["text", "Text"],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                onClick={() => selectClipsByKind(k)}
                className="rounded-full border border-border px-2 py-0.5 text-3xs text-muted hover:bg-bg-muted hover:text-fg transition-colors"
              >
                {label}
              </button>
            ))}
          </div>
          {(selectedClips.length > 0 || selectMode) && (
            <p className="text-3xs text-muted leading-relaxed">
              {selectedClips.length > 0 ? (
                <>
                  <span className="text-accent font-medium">{selectedClips.length} picked</span>
                  {selBreakdown ? ` (${selBreakdown})` : ""}
                  {selectMode
                    ? ". Click more to add or remove them."
                    : ". Whatever you ask will apply to these."}
                </>
              ) : (
                <span className="text-accent">
                  Now click the clips you want.
                </span>
              )}
            </p>
          )}
        </div>

        {/* ===== Chat — the easy way: say what you want in plain words ===== */}
        <div
          onDragOver={onChatDragOver}
          onDragLeave={() => setDragOver(false)}
          onDrop={onChatDrop}
          className={[
            "relative flex-1 min-h-0 flex flex-col rounded-lg border bg-bg p-2 gap-2 transition-colors",
            dragOver ? "border-accent ring-2 ring-accent/40 bg-accent/5" : "border-border",
          ].join(" ")}
        >
          {dragOver && (
            <div className="absolute inset-0 z-10 rounded-lg bg-accent/10 border-2 border-dashed border-accent flex items-center justify-center pointer-events-none">
              <span className="text-[12px] font-medium text-accent">＋ Drop it here</span>
            </div>
          )}
          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5 pr-0.5">
            {aiChat.map((m) => (
              <ChatBubble key={m.id} msg={m} />
            ))}
            {aiBusy && (
              <div className="flex items-center gap-1.5 text-[10.5px] text-muted">
                <span className="w-2.5 h-2.5 shrink-0 rounded-full border-2 border-accent border-t-transparent animate-spin" />
                {aiStatusLabel(aiJob)}
                {aiJob?.progress ? (
                  <span className="text-muted/70 truncate">· {aiJob.progress}</span>
                ) : null}
              </div>
            )}
            {/* What the AI is doing, step by step — live while it works… */}
            {aiBusy && agentSteps.length > 0 && (
              <AgentSteps steps={agentSteps} live />
            )}
            {/* …then tucked away once it's finished. */}
            {!aiBusy && agentSteps.length > 0 && (
              <div className="space-y-1">
                <button
                  onClick={() => setShowAgentSteps((v) => !v)}
                  className="text-[9.5px] text-muted hover:text-fg transition-colors"
                >
                  {showAgentSteps
                    ? "▾ Hide what I did"
                    : `▸ Show what I did (${agentSteps.length} step${
                        agentSteps.length === 1 ? "" : "s"
                      })`}
                </button>
                {showAgentSteps && <AgentSteps steps={agentSteps} />}
              </div>
            )}
            <div ref={chatEndRef} />
          </div>

          {/* Attached items (dragged from the left sidebar) — sent with the message. */}
          {attachments.length > 0 && (
            <div className="shrink-0 flex flex-wrap gap-1">
              {attachments.map((a) => (
                <span
                  key={a.uid}
                  className="inline-flex items-center gap-1 rounded-full border border-accent/40 bg-accent/10 pl-1 pr-1.5 py-0.5 text-3xs text-fg/90 max-w-[150px]"
                >
                  {a.kind === "sticker" && a.thumbUrl ? (
                    <img src={a.thumbUrl} alt="" className="w-3.5 h-3.5 rounded-sm object-contain" />
                  ) : (
                    <span className="text-accent">{ATT_ICON[a.kind]}</span>
                  )}
                  <span className="truncate">{a.name}</span>
                  <button
                    onClick={() => removeAttachment(a.uid)}
                    title="Remove"
                    className="shrink-0 text-muted hover:text-danger leading-none"
                  >
                    ✕
                  </button>
                </span>
              ))}
            </div>
          )}

          <div className="shrink-0 flex items-end gap-1">
            <textarea
              ref={taRef}
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              onKeyDown={(e) => {
                // Enter sends; Shift+Enter inserts a newline.
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  sendChat();
                }
              }}
              rows={1}
              placeholder={
                attachments.length
                  ? "Say what to do with this…"
                  : "Tell me what to do…"
              }
              className="input flex-1 text-2xs px-2 py-1.5 rounded-md resize-none max-h-32 leading-snug"
            />
            <button
              onClick={sendChat}
              disabled={(!chatInput.trim() && attachments.length === 0) || aiBusy}
              className="btn btn-primary text-2xs px-3 py-1.5 disabled:opacity-50"
            >
              Send
            </button>
          </div>
        </div>

        {/* error — dismissible (store aiError + local voice-clone errors) */}
        {(aiError || voiceError) && (
          <div className="shrink-0 rounded-lg border border-danger/40 bg-danger/10 p-2 flex items-start gap-1.5">
            <p className="flex-1 text-[10.5px] text-danger break-words">
              {aiError || voiceError}
            </p>
            <button
              onClick={() => {
                dismissAiError();
                setVoiceError(null);
              }}
              className="shrink-0 text-2xs text-danger/70 hover:text-danger leading-none"
              title="Dismiss"
            >
              ✕
            </button>
          </div>
        )}

        {/* finished-job summary */}
        {doneJob && (
          <div className="shrink-0 rounded-lg border border-accent/40 bg-accent/10 p-2 flex items-start gap-1.5">
            <p className="flex-1 text-[10.5px] text-fg/85">
              Made {doneJob.ops_applied} change{doneJob.ops_applied === 1 ? "" : "s"}
              {doneJob.ops_failed.length > 0 && (
                <span className="text-muted"> ({doneJob.ops_failed.length} skipped)</span>
              )}
              {doneJob.feature !== "analyze" && (
                <span className="block text-[9.5px] text-muted pt-0.5">Press Ctrl+Z to undo.</span>
              )}
            </p>
            <button
              onClick={() => setDismissedJobId(doneJob.id)}
              className="shrink-0 text-2xs text-muted hover:text-fg leading-none"
              title="Dismiss"
            >
              ✕
            </button>
          </div>
        )}

        {/* ===== Voice setup — the one tool the chat can't do (mic capture).
            Everything else (captions, music, intro/outro, stickers, speed,
            ducking, silences…) happens through the chat above. ===== */}
        <div className={aiBusy ? "shrink-0 opacity-50 pointer-events-none" : "shrink-0"}>
          <Section title="🎤 Your voice">
            {/* Voice — "my voice" clone + per-clip change */}
            <section className="space-y-1.5">
              <p className="text-3xs text-muted">
                Record your voice here, then ask the chat to
                “use my voice on this clip”.
              </p>

              {/* My-voice card */}
              <div className="rounded-lg border border-border bg-bg p-2 space-y-1.5">
                {myVoice === undefined ? (
                  voiceLookupFailed ? (
                    <>
                      <p className="text-3xs text-danger">
                        Couldn't check your voice. Try again in a moment.
                      </p>
                      <button
                        onClick={() => setVoiceLookupAttempt((n) => n + 1)}
                        className="btn btn-ghost w-full text-[10.5px] py-1 border border-border"
                      >
                        Retry
                      </button>
                    </>
                  ) : (
                    <p className="text-3xs text-muted">Checking for your voice…</p>
                  )
                ) : myVoice && !replacingVoice ? (
                  <>
                    <p className="text-[10.5px] text-fg">
                      <span className="font-medium">{myVoice.name}</span>
                      <span className="block text-[9.5px] text-muted">
                        Ready to use.
                      </span>
                    </p>
                    <div className="grid grid-cols-2 gap-1">
                      <button
                        onClick={() => setReplacingVoice(true)}
                        disabled={cloneBusy}
                        className="btn btn-ghost text-[10.5px] py-1 border border-border disabled:opacity-50"
                      >
                        Record again
                      </button>
                      <button
                        onClick={() => void onRemoveClone()}
                        disabled={cloneBusy}
                        className="btn btn-ghost text-[10.5px] py-1 border border-border text-danger disabled:opacity-50"
                      >
                        Remove
                      </button>
                    </div>
                    {cloneBusy && (
                      <p className="flex items-center gap-1.5 text-3xs text-muted">
                        <span className="w-2.5 h-2.5 shrink-0 rounded-full border-2 border-accent border-t-transparent animate-spin" />
                        Removing…
                      </p>
                    )}
                  </>
                ) : (
                  <>
                    <p className="text-3xs text-muted">
                      {myVoice
                        ? "Record yourself again to replace it."
                        : "Record about 30 seconds of yourself talking, somewhere quiet."}
                    </p>
                    <AudioCapture
                      busy={cloneBusy}
                      busyLabel="Learning your voice…"
                      onAudio={(f) => void onCloneAudio(f)}
                      onError={setVoiceError}
                    />
                    {myVoice && (
                      <button
                        onClick={() => setReplacingVoice(false)}
                        disabled={cloneBusy}
                        className="btn btn-ghost w-full text-3xs py-0.5 disabled:opacity-50"
                      >
                        Cancel
                      </button>
                    )}
                  </>
                )}
              </div>

              {/* Change the selected clip's voice */}
              <label className="flex items-start gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={useAsIs}
                  onChange={(e) => setUseAsIs(e.target.checked)}
                  className="mt-0.5 shrink-0"
                />
                <span className="text-3xs text-muted">
                  Use my recording exactly as it is
                </span>
              </label>
              {useAsIs ? (
                <>
                  <AudioCapture
                    disabled={!videoClipSelected}
                    disabledReason={SELECT_CLIP_MSG}
                    busy={asIsBusy}
                    busyLabel="Sending your recording…"
                    onAudio={(f) => void onAsIsAudio(f)}
                    onError={setVoiceError}
                  />
                  <p className="text-[9.5px] text-muted">
                    {videoClipSelected
                      ? "Your recording replaces this clip's sound, so it needs to be about the same length."
                      : SELECT_CLIP_MSG}
                  </p>
                </>
              ) : (
                <>
                  <button
                    onClick={onChangeVoice}
                    disabled={!videoClipSelected || myVoice === undefined}
                    title={
                      !videoClipSelected
                        ? SELECT_CLIP_MSG
                        : myVoice === undefined
                          ? voiceLookupFailed
                            ? "Couldn't check your voice. Try again in a moment."
                            : "Still checking…"
                          : "Say this clip again, in your voice"
                    }
                    className="btn btn-primary w-full text-2xs py-1 disabled:opacity-50"
                  >
                    {myVoice === undefined && !voiceLookupFailed
                      ? "Checking…"
                      : "Use my voice"}
                  </button>
                  <p className="text-[9.5px] text-muted">
                    {videoClipSelected ? "Redoes the clip you picked, in your voice." : SELECT_CLIP_MSG}
                  </p>
                </>
              )}
            </section>

          </Section>
        </div>
      </div>
    </div>
  );
}
