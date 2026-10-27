import { useEffect, useState } from "react";

/**
 * Like useState, but the value is mirrored to localStorage so it survives a
 * full page reload (F5) / browser restart. Drop-in replacement for useState
 * for JSON-serialisable DRAFT / form state (typed prompt, selected style, …).
 *
 * Do NOT use it for transient run flags like a "generating" spinner — persisting
 * those would restore a frozen spinner after reload with nothing driving it.
 * Keep those on plain useState; the actual result is rehydrated from the backend.
 *
 * All storage access is best-effort: private-mode / quota / serialization
 * errors are swallowed so persistence can never break the component.
 */
export function usePersistentState<T>(
  key: string,
  initial: T,
): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw != null ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* private mode / quota / cyclic — persistence is best-effort */
    }
  }, [key, value]);

  return [value, setValue];
}
