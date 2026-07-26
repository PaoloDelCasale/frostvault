import { apiRequest } from "@/api/client";

const PERMISSION_DENIED_KEY = "frostvault.push.permissionDenied";

export type PushConfigResponse = {
  configured: boolean;
  vapid_public_key: string | null;
};

export type PushSubscribePayload = {
  endpoint: string;
  keys: { p256dh: string; auth: string };
};

function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    output[i] = raw.charCodeAt(i);
  }
  return output;
}

export function wasPushPermissionDenied(
  storage: Storage = localStorage,
): boolean {
  return storage.getItem(PERMISSION_DENIED_KEY) === "1";
}

export function rememberPushPermissionDenied(
  storage: Storage = localStorage,
): void {
  storage.setItem(PERMISSION_DENIED_KEY, "1");
}

export function clearPushPermissionDenied(
  storage: Storage = localStorage,
): void {
  storage.removeItem(PERMISSION_DENIED_KEY);
}

/**
 * Ask for notification permission only when starting a long operation — never
 * on first load. Denied stays denied and is remembered so we do not re-prompt.
 */
export async function ensurePushSubscription(options?: {
  storage?: Storage;
  fetchConfig?: () => Promise<PushConfigResponse>;
  subscribe?: (payload: PushSubscribePayload) => Promise<unknown>;
  requestPermission?: () => Promise<NotificationPermission>;
  getRegistration?: () => Promise<ServiceWorkerRegistration | null>;
}): Promise<"subscribed" | "skipped" | "denied" | "unavailable"> {
  const storage = options?.storage ?? localStorage;
  if (typeof window === "undefined") return "unavailable";
  if (wasPushPermissionDenied(storage)) {
    return "denied";
  }

  const fetchConfig =
    options?.fetchConfig ??
    (() => apiRequest<PushConfigResponse>("/api/push/config"));
  let config: PushConfigResponse;
  try {
    config = await fetchConfig();
  } catch {
    return "unavailable";
  }
  if (!config.configured || !config.vapid_public_key) {
    return "skipped";
  }

  if (!("Notification" in window) || !("serviceWorker" in navigator)) {
    return "unavailable";
  }

  let permission = Notification.permission;
  if (permission === "default") {
    const request =
      options?.requestPermission ??
      (() => Notification.requestPermission());
    permission = await request();
  }
  if (permission === "denied") {
    rememberPushPermissionDenied(storage);
    return "denied";
  }
  if (permission !== "granted") {
    return "skipped";
  }
  clearPushPermissionDenied(storage);

  const getRegistration =
    options?.getRegistration ??
    (async () => {
      const existing = await navigator.serviceWorker.getRegistration();
      return existing ?? null;
    });
  const registration = await getRegistration();
  if (!registration?.pushManager) {
    return "unavailable";
  }

  const applicationServerKey = urlBase64ToUint8Array(config.vapid_public_key);
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: applicationServerKey as BufferSource,
  });
  const json = subscription.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
    return "unavailable";
  }
  const payload: PushSubscribePayload = {
    endpoint: json.endpoint,
    keys: { p256dh: json.keys.p256dh, auth: json.keys.auth },
  };
  const subscribe =
    options?.subscribe ??
    ((body) =>
      apiRequest("/api/push/subscriptions", {
        method: "POST",
        body: JSON.stringify(body),
      }));
  try {
    await subscribe(payload);
  } catch {
    return "unavailable";
  }
  return "subscribed";
}
