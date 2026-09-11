export function registerSW(_options?: {
  immediate?: boolean;
  onNeedReload?: () => void;
  onRegisteredSW?: (
    swUrl: string,
    registration: ServiceWorkerRegistration | undefined,
  ) => void;
}): (reloadPage?: boolean) => Promise<void> {
  void _options;
  return async () => undefined;
}
