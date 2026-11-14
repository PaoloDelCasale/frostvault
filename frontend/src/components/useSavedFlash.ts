import { useCallback, useEffect, useRef, useState } from "react";

const SAVED_VISIBLE_MS = 3000;

/**
 * Settings save model (#658): a switch or checkbox saves as soon as it
 * changes and confirms with «Salvato» beside it; fields and plans keep a
 * «Salva» button enabled only while there is a change.
 */
export function useSavedFlash(): [boolean, () => void] {
  const [saved, setSaved] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const flash = useCallback(() => {
    window.clearTimeout(timer.current);
    setSaved(true);
    timer.current = window.setTimeout(() => setSaved(false), SAVED_VISIBLE_MS);
  }, []);
  return [saved, flash];
}
