export function registerSW(_options?: {
  immediate?: boolean;
  onRegisteredSW?: (
    swUrl: string,
    registration: ServiceWorkerRegistration | undefined,
  ) => void;
}): (reloadPage?: boolean) => Promise<void> {
  void _options;
  return async () => undefined;
}
