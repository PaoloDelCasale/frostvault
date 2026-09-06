import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

import { applySession } from "../helpers/auth";

const ARTIFACT_BODY = "valid metadata backup";
const ARTIFACT_DIGEST = createHash("sha256")
  .update(ARTIFACT_BODY)
  .digest("hex");

const INTEGRITY_RUN = {
  id: 10,
  created_at: "2026-07-10T00:00:00+00:00",
  finished_at: "2026-07-10T00:00:01+00:00",
  reason: "manual",
  backend: "sqlite",
  status: "succeeded",
  digest_sha256: ARTIFACT_DIGEST,
  database_sha256: "b".repeat(64),
  s3_key: "system/backups/metadata-10.bak.enc",
  size_bytes: ARTIFACT_BODY.length,
  error_message: null,
  verified_at: null,
};

function backupsPayload() {
  return {
    status: {
      last_status: "succeeded",
      last_run: INTEGRITY_RUN,
      succeeded_count: 1,
      failed_count: 0,
    },
    runs: [INTEGRITY_RUN],
  };
}

async function mockMetadataBackupApis(
  page: Page,
  downloadBody: string,
): Promise<void> {
  await page.route("**/api/admin/metadata-backups/download/10", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/octet-stream",
      headers: {
        "Content-Disposition": "attachment; filename=metadata-10.bak.enc",
        "X-Checksum-SHA256": ARTIFACT_DIGEST,
      },
      body: downloadBody,
    });
  });
  await page.route("**/api/admin/metadata-backups", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(backupsPayload()),
    });
  });
}

test.describe("metadata backup download checksum", () => {
  test("Chromium downloads the artifact only after SHA-256 verification", async ({
    page,
    context,
  }) => {
    await applySession(context, "admin");
    await mockMetadataBackupApis(page, ARTIFACT_BODY);

    await page.goto("/admin/metadata-backups");
    await expect(
      page.getByRole("heading", {
        name: /metadata backups|backup dei metadati/i,
        level: 2,
      }),
    ).toBeVisible();

    const downloadPromise = page.waitForEvent("download");
    await page
      .getByRole("button", { name: /download artifact|scarica artefatto/i })
      .click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("metadata-10.bak.enc");

    const saved = await download.path();
    expect(saved).toBeTruthy();
    const bytes = readFileSync(saved as string);
    expect(bytes.toString("utf8")).toBe(ARTIFACT_BODY);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(
      ARTIFACT_DIGEST,
    );
  });

  test("Chromium stops a corrupted artifact instead of saving it", async ({
    page,
    context,
  }) => {
    await applySession(context, "admin");
    await mockMetadataBackupApis(page, "corrupted metadata backup");

    await page.goto("/admin/metadata-backups");
    await expect(
      page.getByRole("heading", {
        name: /metadata backups|backup dei metadati/i,
        level: 2,
      }),
    ).toBeVisible();

    await page
      .getByRole("button", { name: /download artifact|scarica artefatto/i })
      .click();
    await expect(page.getByRole("alert")).toContainText(
      /checksum does not match|checksum dell.artefatto/i,
    );
  });
});
