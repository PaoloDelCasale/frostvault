import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiQueryProvider,
  configureApiClient,
  createAppQueryClient,
  resetApiClientForTests,
  type MetadataBackupRun,
} from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { MetadataBackupsSection } from "@/pages/admin/MetadataBackupsSection";

const localesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../app/locales",
);
const messages = JSON.parse(
  readFileSync(path.join(localesDir, "en.json"), "utf8"),
) as Record<string, string>;

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const runs = [
  {
    id: 4,
    created_at: "2026-07-04T00:00:00+00:00",
    finished_at: null,
    reason: "manual",
    backend: "sqlite",
    status: "running",
    digest_sha256: null,
    database_sha256: null,
    s3_key: null,
    size_bytes: null,
    error_message: null,
    verified_at: null,
  },
  {
    id: 3,
    created_at: "2026-07-03T00:00:00+00:00",
    finished_at: "2026-07-03T00:00:02+00:00",
    reason: "manual",
    backend: "sqlite",
    status: "succeeded",
    digest_sha256: "a".repeat(64),
    database_sha256: "b".repeat(64),
    s3_key: "system/backups/metadata-3.bak.enc",
    size_bytes: 12,
    error_message: null,
    verified_at: "2026-07-03T00:01:00+00:00",
  },
  {
    id: 2,
    created_at: "2026-07-02T00:00:00+00:00",
    finished_at: "2026-07-02T00:00:02+00:00",
    reason: "manual",
    backend: "sqlite",
    status: "succeeded",
    digest_sha256: "c".repeat(64),
    database_sha256: "d".repeat(64),
    s3_key: null,
    size_bytes: 11,
    error_message: null,
    verified_at: null,
  },
  {
    id: 1,
    created_at: "2026-07-01T00:00:00+00:00",
    finished_at: "2026-07-01T00:00:01+00:00",
    reason: "manual",
    backend: "sqlite",
    status: "failed",
    digest_sha256: null,
    database_sha256: null,
    s3_key: null,
    size_bytes: null,
    error_message: "Configured off-host storage is unavailable",
    verified_at: null,
  },
];

function backupResponse() {
  return {
    status: {
      last_status: "running",
      last_run: runs[0],
      succeeded_count: 2,
      failed_count: 1,
    },
    runs,
  };
}

function installWebCryptoForTests(): void {
  if (!globalThis.crypto?.subtle) {
    vi.stubGlobal("crypto", webcrypto);
  }
}

function stubObjectUrls() {
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;
  const createObjectURL = vi.fn(() => "blob:metadata-backup");
  const revokeObjectURL = vi.fn();
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: createObjectURL,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: revokeObjectURL,
  });
  return {
    createObjectURL,
    restore() {
      if (originalCreate) {
        Object.defineProperty(URL, "createObjectURL", {
          configurable: true,
          value: originalCreate,
        });
      } else {
        Reflect.deleteProperty(URL, "createObjectURL");
      }
      if (originalRevoke) {
        Object.defineProperty(URL, "revokeObjectURL", {
          configurable: true,
          value: originalRevoke,
        });
      } else {
        Reflect.deleteProperty(URL, "revokeObjectURL");
      }
    },
  };
}

const INTEGRITY_RUN: MetadataBackupRun = {
  id: 10,
  created_at: "2026-07-10T00:00:00+00:00",
  finished_at: "2026-07-10T00:00:01+00:00",
  reason: "manual",
  backend: "sqlite",
  status: "succeeded",
  digest_sha256: "01119f43c3f2170b4eb39ef1494a06214d1a9679807666f103e18ceae596fb8c",
  database_sha256: "b".repeat(64),
  s3_key: "system/backups/metadata-10.bak.enc",
  size_bytes: 21,
  error_message: null,
  verified_at: null,
};

function integrityResponse(run: MetadataBackupRun = INTEGRITY_RUN) {
  return {
    status: {
      last_status: "succeeded",
      last_run: run,
      succeeded_count: 1,
      failed_count: 0,
    },
    runs: [run],
  };
}

function renderSection() {
  render(
    <ApiQueryProvider client={createAppQueryClient()}>
      <I18nProvider>
        <MetadataBackupsSection />
      </I18nProvider>
    </ApiQueryProvider>,
  );
}

function configureIntegrityScenario(
  body: string,
  checksumHeader?: string,
  run: MetadataBackupRun = INTEGRITY_RUN,
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/i18n/catalog")) {
      return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
    }
    if (url === "/api/admin/metadata-backups") {
      return jsonResponse(integrityResponse(run));
    }
    if (url === "/api/admin/metadata-backups/download/10") {
      const headers: Record<string, string> = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": "attachment; filename=metadata-10.bak.enc",
      };
      if (checksumHeader !== undefined) {
        headers["X-Checksum-SHA256"] = checksumHeader;
      }
      return new Response(body, { headers });
    }
    throw new Error(`Unexpected request: ${url}`);
  });
  configureApiClient({ fetch: fetchMock });
  return fetchMock;
}

afterEach(() => {
  resetApiClientForTests();
  vi.unstubAllGlobals();
});

describe("MetadataBackupsSection", () => {
  it("shows full, local-only, and failed outcomes with verification and eligible downloads", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) {
        return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
      }
      if (url === "/api/admin/metadata-backups") {
        return jsonResponse(backupResponse());
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    configureApiClient({ fetch: fetchMock });

    renderSection();

    expect(
      await screen.findByRole("heading", { name: "Metadata backups", level: 2 }),
    ).toBeInTheDocument();
    expect(screen.getByText("Full success · off-host copy")).toBeInTheDocument();
    expect(
      screen.getByText("Local-only success · no off-host copy"),
    ).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("In progress")).toBeInTheDocument();
    expect(screen.getByText("Verified")).toBeInTheDocument();
    expect(screen.getAllByText("Not verified")).toHaveLength(3);
    expect(screen.getByText("Configured off-host storage is unavailable")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Download artifact" })).toHaveLength(2);
    expect(screen.queryByText("/data/backups/metadata-3.bak.enc")).not.toBeInTheDocument();
  });

  it("keeps a synchronous run visibly pending, refreshes, and surfaces POST errors", async () => {
    const user = userEvent.setup();
    const run = deferred<Response>();
    let shouldFail = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.startsWith("/api/i18n/catalog")) {
        return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
      }
      if (url === "/api/admin/metadata-backups" && method === "GET") {
        return jsonResponse(backupResponse());
      }
      if (url === "/api/admin/metadata-backups/run" && method === "POST") {
        if (shouldFail) return jsonResponse({ detail: "S3 upload failed" }, 500);
        return run.promise;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    configureApiClient({ fetch: fetchMock });

    renderSection();
    await screen.findByRole("heading", { name: "Metadata backups", level: 2 });

    const runButton = screen.getByRole("button", { name: "Run metadata backup" });
    await user.click(runButton);
    expect(
      screen.getByRole("button", { name: "Running metadata backup…" }),
    ).toBeDisabled();
    expect(
      screen.getByText("Creating and storing the encrypted backup…"),
    ).toBeInTheDocument();

    run.resolve(jsonResponse({ ok: true }));
    await waitFor(() => {
      expect(
        screen.getByText("Metadata backup completed; the run list has been refreshed."),
      ).toBeInTheDocument();
    });

    shouldFail = true;
    await user.click(screen.getByRole("button", { name: "Run metadata backup" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("S3 upload failed");
    expect(screen.getByRole("button", { name: "Run metadata backup" })).toBeEnabled();
  });

  it("downloads only after the recorded, response, and blob SHA-256 values agree", async () => {
    installWebCryptoForTests();
    const objectUrls = stubObjectUrls();
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    try {
      configureIntegrityScenario(
        "valid metadata backup",
        INTEGRITY_RUN.digest_sha256.toUpperCase(),
      );
      renderSection();
      await screen.findByRole("heading", { name: "Metadata backups", level: 2 });
      await user.click(screen.getByRole("button", { name: "Download artifact" }));

      await waitFor(() => expect(objectUrls.createObjectURL).toHaveBeenCalledOnce());
      expect(anchorClick).toHaveBeenCalledOnce();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      anchorClick.mockRestore();
      objectUrls.restore();
    }
  });

  it.each([
    ["missing", undefined],
    ["malformed", "not-a-sha256"],
  ] as const)(
    "fails closed when the response checksum header is %s",
    async (_label, checksumHeader) => {
      installWebCryptoForTests();
      const objectUrls = stubObjectUrls();
      const user = userEvent.setup();
      try {
        configureIntegrityScenario("valid metadata backup", checksumHeader);
        renderSection();
        await screen.findByRole("heading", { name: "Metadata backups", level: 2 });
        await user.click(screen.getByRole("button", { name: "Download artifact" }));

        expect(await screen.findByRole("alert")).toHaveTextContent(
          "Download stopped because the artifact integrity could not be verified.",
        );
        expect(objectUrls.createObjectURL).not.toHaveBeenCalled();
      } finally {
        objectUrls.restore();
      }
    },
  );

  it.each([
    ["missing", null],
    ["malformed", "not-a-sha256"],
  ] as const)(
    "fails closed when the recorded run checksum is %s",
    async (_label, digest) => {
      installWebCryptoForTests();
      const objectUrls = stubObjectUrls();
      const user = userEvent.setup();
      try {
        const run = { ...INTEGRITY_RUN, digest_sha256: digest };
        configureIntegrityScenario(
          "valid metadata backup",
          INTEGRITY_RUN.digest_sha256,
          run,
        );
        renderSection();
        await screen.findByRole("heading", { name: "Metadata backups", level: 2 });
        await user.click(screen.getByRole("button", { name: "Download artifact" }));

        expect(await screen.findByRole("alert")).toHaveTextContent(
          "Download stopped because the artifact integrity could not be verified.",
        );
        expect(objectUrls.createObjectURL).not.toHaveBeenCalled();
      } finally {
        objectUrls.restore();
      }
    },
  );

  it("fails closed when the downloaded blob is corrupted", async () => {
    installWebCryptoForTests();
    const objectUrls = stubObjectUrls();
    const user = userEvent.setup();
    try {
      configureIntegrityScenario(
        "corrupted metadata backup",
        INTEGRITY_RUN.digest_sha256,
      );
      renderSection();
      await screen.findByRole("heading", { name: "Metadata backups", level: 2 });
      await user.click(screen.getByRole("button", { name: "Download artifact" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "The downloaded artifact checksum does not match the recorded checksum.",
      );
      expect(objectUrls.createObjectURL).not.toHaveBeenCalled();
    } finally {
      objectUrls.restore();
    }
  });
});
