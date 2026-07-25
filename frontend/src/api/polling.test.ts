import { describe, expect, it } from "vitest";

import { ACTIVE_JOB_POLL_MS, IDLE_POLL_MS, jobPollIntervalMs } from "./polling";

describe("jobPollIntervalMs", () => {
  it("switches from 10s idle to 1s when active jobs appear and back to 10s when they finish", () => {
    expect(jobPollIntervalMs(0)).toBe(IDLE_POLL_MS);
    expect(jobPollIntervalMs(0)).toBe(10_000);

    expect(jobPollIntervalMs(1)).toBe(ACTIVE_JOB_POLL_MS);
    expect(jobPollIntervalMs(3)).toBe(1_000);

    expect(jobPollIntervalMs(0)).toBe(IDLE_POLL_MS);
  });
});
