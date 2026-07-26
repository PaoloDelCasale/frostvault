export type LatestRequestHandle = {
  readonly token: number;
  isCurrent: () => boolean;
  /**
   * Await a promise and return its value only when this handle is still the
   * newest generation. Stale successes resolve to `undefined`; stale errors
   * are swallowed the same way so they cannot replace newer UI state.
   */
  settle: <T>(promise: Promise<T>) => Promise<T | undefined>;
};

export type LatestRequestScope = {
  begin: () => LatestRequestHandle;
  /** True when the current generation completed via settle (success or thrown current error). */
  hasSettledCurrent: () => boolean;
  readonly current: number;
};

/**
 * Generation token helper ported from the admin UI race guards in
 * the legacy admin race-condition cases.
 */
export function createLatestRequestScope(): LatestRequestScope {
  let generation = 0;
  let settledGeneration = 0;

  return {
    get current() {
      return generation;
    },
    hasSettledCurrent() {
      return settledGeneration === generation && generation > 0;
    },
    begin() {
      const token = ++generation;
      const isCurrent = () => token === generation;
      return {
        token,
        isCurrent,
        async settle<T>(promise: Promise<T>): Promise<T | undefined> {
          try {
            const value = await promise;
            if (!isCurrent()) return undefined;
            settledGeneration = token;
            return value;
          } catch (error) {
            if (!isCurrent()) return undefined;
            settledGeneration = token;
            throw error;
          }
        },
      };
    },
  };
}
