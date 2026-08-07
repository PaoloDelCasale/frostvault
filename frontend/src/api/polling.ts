/** Active-Job progress cadence. */
export const ACTIVE_JOB_POLL_MS = 1_000;

/**
 * Idle Job list cadence. Catalog file/stats discovery must not use this value
 * after issue #227 — those surfaces are event-driven when idle.
 */
export const IDLE_POLL_MS = 10_000;

/**
 * Return the TanStack Query refetch interval for Job progress screens.
 * Active Jobs poll quickly; idle Job lists keep a slow dedicated cadence that
 * is not used to discover external filesystem catalog changes.
 */
export function jobPollIntervalMs(activeJobCount: number): number {
  return activeJobCount > 0 ? ACTIVE_JOB_POLL_MS : IDLE_POLL_MS;
}

/**
 * Build a TanStack Query `refetchInterval` callback that follows active jobs.
 * `selectActiveCount` extracts the active-job count from the query data.
 */
export function jobAwareRefetchInterval<T>(
  selectActiveCount: (data: T | undefined) => number,
): (query: { state: { data: T | undefined } }) => number {
  return (query) => jobPollIntervalMs(selectActiveCount(query.state.data));
}

/**
 * Files/stats idle discovery is event-driven. Poll only while Jobs are active
 * so row state can track in-flight work; otherwise disable fixed polling.
 */
export function catalogPollIntervalMs(activeJobCount: number): number | false {
  return activeJobCount > 0 ? ACTIVE_JOB_POLL_MS : false;
}
