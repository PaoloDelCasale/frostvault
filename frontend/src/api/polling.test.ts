import { describe, expect, it } from "vitest";

import {
  ACTIVE_JOB_POLL_MS,
  IDLE_POLL_MS,
  catalogPollIntervalMs,
  jobPollIntervalMs,
} from "./polling";

describe("jobPollIntervalMs", () => {
  it("keeps the dedicated Job progress cadence while active and idle", () => {
    expect(jobPollIntervalMs(0)).toBe(IDLE_POLL_MS);
    expect(jobPollIntervalMs(1)).toBe(ACTIVE_JOB_POLL_MS);
    expect(jobPollIntervalMs(3)).toBe(1_000);
    expect(jobPollIntervalMs(0)).toBe(10_000);
  });
});

describe("catalogPollIntervalMs", () => {
  it("disables idle files/stats polling and keeps the active-Job cadence", () => {
    expect(catalogPollIntervalMs(0)).toBe(false);
    expect(catalogPollIntervalMs(0)).not.toBe(IDLE_POLL_MS);
    expect(catalogPollIntervalMs(2)).toBe(ACTIVE_JOB_POLL_MS);
  });
});
