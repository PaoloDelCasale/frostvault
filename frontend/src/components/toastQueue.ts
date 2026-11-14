/** The app's single toast queue (#656); `ToastRegion` renders it. */
export type ToastVariant = "success" | "error";
export type ToastItem = { id: number; message: string; variant: ToastVariant };

/** Success toasts close by themselves after this delay; errors wait for the user. */
export const TOAST_SUCCESS_MS = 5000;

let queue: ToastItem[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function publishToasts(next: ToastItem[]) {
  queue = next;
  listeners.forEach((listener) => listener());
}

/**
 * Enqueue an action outcome in the app's single toast queue. One toast is
 * visible at a time; an identical message already queued is not repeated.
 * Loading errors of a panel belong inline in that panel, not here.
 */
export function notify(message: string, variant: ToastVariant = "success"): void {
  if (!message || queue.some((item) => item.message === message && item.variant === variant)) return;
  publishToasts([...queue, { id: nextId++, message, variant }]);
}

export function dismissToast(id: number) {
  publishToasts(queue.filter((item) => item.id !== id));
}

export function subscribeToasts(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function currentToasts(): ToastItem[] {
  return queue;
}

/** Drops every queued toast (an unmounted shell, or a test cleanup). */
export function clearToasts(): void {
  publishToasts([]);
}
