/** Polling cadence preserved from the Jinja archive UI (`app/static/app.js`). */
export const ACTIVE_JOB_POLL_MS = 1_000;
export const IDLE_POLL_MS = 10_000;

/** Return the TanStack Query refetch interval for job-aware screens. */
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
