// Rebrand studio — load and decode an image BEFORE it goes on the stage.
//
// The stage swaps one picture for another in the same <img>. If the new one
// is not decoded yet, the browser keeps painting the old one (or a half-drawn
// new one) for a moment, and a blend of two frames of the speaker is exactly
// the "two faces" look the owner reported. So nothing is shown until
// decode() has resolved: then the swap is instant and hard.
//
// Never rejects: a picture that fails to load is the <img>'s problem to show,
// not a reason to keep the stage waiting. Each URL is decoded once.

const decoded = new Set<string>();
const inflight = new Map<string, Promise<void>>();

export function isDecoded(url: string | null | undefined): boolean {
  return !!url && decoded.has(url);
}

export function decodeImage(url: string, timeoutMs = 30000): Promise<void> {
  if (decoded.has(url)) return Promise.resolve();
  const known = inflight.get(url);
  if (known) return known;
  const p = new Promise<void>((resolve) => {
    const img = new Image();
    let settled = false;
    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      inflight.delete(url);
      if (ok) decoded.add(url);
      resolve();
    };
    const timer = window.setTimeout(() => finish(false), timeoutMs);
    img.src = url;
    img.decode().then(
      () => finish(true),
      () => finish(false),
    );
  });
  inflight.set(url, p);
  return p;
}

export function decodeAll(urls: (string | null | undefined)[], timeoutMs?: number): Promise<void> {
  const unique = [...new Set(urls.filter((u): u is string => !!u))];
  return Promise.all(unique.map((u) => decodeImage(u, timeoutMs))).then(() => undefined);
}
