import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApiClient, resetApiClientForTests } from "@/api";
import { I18nProvider } from "@/i18n/I18nProvider";
import { VaultCreatePage } from "@/pages/vault-create";
import { jsonResponse as json } from "./support/http";
import { renderWithQueryClient } from "./support/queryClient";

const locales = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app/locales");
const en = JSON.parse(readFileSync(path.join(locales, "en.json"), "utf8")) as Record<string, string>;
const itCatalog = JSON.parse(readFileSync(path.join(locales, "it.json"), "utf8")) as Record<string, string>;
const bucket = (state: "ready" | "failed", error: string | null = null) => ({
  vault_id: 42, bucket: "frostvault-photos-a1b2c3d4", region: "eu-west-1", dedicated: true, state,
  stage: state === "failed" ? "reserved" : null, attempts: state === "failed" ? 1 : 0,
  last_attempt_at: null, error, retention_lock_days: null,
});

describe("plaintext Vault creation", () => {
  const fetchMock = vi.fn();
  const navigate = vi.fn();
  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    navigate.mockReset();
    configureApiClient({ fetch: fetchMock, navigate });
    document.documentElement.lang = "en";
  });
  afterEach(() => { cleanup(); resetApiClientForTests(); });

  function renderPage() {
    return renderWithQueryClient(
      <I18nProvider><VaultCreatePage displayName="Ada" onNavigate={navigate} /></I18nProvider>,
      { config: { defaultOptions: { queries: { retry: false } } } },
    );
  }
  function requests({ adopt = false, fail = false, messages = en, volumesFail = false, bucketFails = false, retryReady = false } = {}) {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return Promise.resolve(json({ locale: messages === en ? "en" : "it", locales: ["en", "it"], messages }));
      if (url === "/api/general/source-volumes") return Promise.resolve(volumesFail ? json({ detail: "boom" }, 500) : json({ items: adopt ? [{ alias: "photos", health: "ok", access: "rw" }] : [] }));
      if (url.startsWith("/api/source-volumes/photos/browse")) return Promise.resolve(json({ volume_alias: "photos", relative_path: "albums", items: [{ name: "2024", relative_path: "albums/2024", navigable: true, selectable: true, occupation: null }] }));
      if (url === "/api/vaults" && init?.method === "POST") return Promise.resolve(fail
        ? json({ message_key: "ui.vault_create.failed", detail: "Invalid name" }, 422)
        : json({ id: 42, uuid: "vault-uuid", name: "Photos", creation_mode: adopt ? "adopt" : "empty",
          lifecycle: bucketFails ? "provisioning" : "active",
          bucket: bucketFails ? bucket("failed", "S3 refused s3:CreateBucket (AccessDenied)") : bucket("ready") }, 201));
      if (url === "/api/vaults/42/bucket/retry" && init?.method === "POST") return Promise.resolve(json(retryReady
        ? bucket("ready") : { ...bucket("failed", "S3 refused s3:PutBucketVersioning (AccessDenied)"), stage: "created", attempts: 2 }));
      if (url === "/api/vaults/select") return Promise.resolve(json({ vault_id: 42 }));
      return Promise.reject(new Error(`Unexpected request ${url}`));
    });
  }
  function payload() {
    const call = fetchMock.mock.calls.find(([url, init]) => url === "/api/vaults" && init?.method === "POST");
    expect(call).toBeDefined();
    return JSON.parse(String(call![1].body)) as unknown;
  }

  it("creates and selects immediately, with no encryption choice or custody/export step", async () => {
    requests();
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: en["ui.vault_create.title"] });
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    await user.type(screen.getByRole("textbox", { name: en["ui.vault_create.name"] }), "Photos");
    await user.click(screen.getByRole("button", { name: en["ui.vault_create.submit"] }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/?vault_id=42"));
    expect(payload()).toEqual({ name: "Photos", creation_mode: "empty" });
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/recovery"))).toBe(false);
  });

  it("offers an optional Retention Lock, 180 days when enabled, only at creation (#736)", async () => {
    requests({ messages: itCatalog });
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: itCatalog["ui.vault_create.title"] });
    const fieldset = screen.getByTestId("vault-create-retention-lock");
    expect(fieldset).toHaveTextContent(itCatalog["ui.vault_create.retention_lock_help"]);
    expect(within(fieldset).queryByRole("combobox")).toBeNull();
    await user.type(screen.getByRole("textbox", { name: itCatalog["ui.vault_create.name"] }), "Photos");
    await user.click(within(fieldset).getByRole("checkbox", { name: itCatalog["ui.vault_create.retention_lock_enable"] }));
    const days = within(fieldset).getByRole("combobox", { name: itCatalog["ui.vault_create.retention_lock_days"] });
    expect(days).toHaveValue("180");
    expect(within(days).getAllByRole("option").map((option) => option.getAttribute("value"))).toEqual(["30", "90", "180", "365"]);
    await user.selectOptions(days, "90");
    expect(screen.getByTestId("vault-create-retention-lock-consequences")).toHaveTextContent("Governance, 90");
    await user.click(screen.getByRole("button", { name: itCatalog["ui.vault_create.submit"] }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/?vault_id=42"));
    expect(payload()).toEqual({ name: "Photos", creation_mode: "empty", retention_lock_days: 90 });
  });

  it("explains while the Vault's bucket is being created", async () => {
    requests();
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(en["ui.vault_create.bucket_hint"])).toBeInTheDocument();
    let release: (value: Response) => void = () => {};
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => String(input) === "/api/vaults" && init?.method === "POST"
      ? new Promise<Response>((resolve) => { release = resolve; }) : base(input, init));
    await user.type(screen.getByRole("textbox", { name: en["ui.vault_create.name"] }), "Photos");
    await user.click(screen.getByRole("button", { name: en["ui.vault_create.submit"] }));
    expect(await screen.findByTestId("vault-create-progress")).toHaveTextContent(en["ui.vault_create.creating_bucket"]);
    release(json({ id: 42, uuid: "vault-uuid", name: "Photos", creation_mode: "empty", lifecycle: "active", bucket: bucket("ready") }, 201));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/?vault_id=42"));
  });

  it("keeps a Vault whose bucket failed, shows why, and retries it (#735)", async () => {
    requests({ bucketFails: true, messages: itCatalog });
    const user = userEvent.setup();
    renderPage();
    await user.type(await screen.findByRole("textbox", { name: itCatalog["ui.vault_create.name"] }), "Photos");
    await user.click(screen.getByRole("button", { name: itCatalog["ui.vault_create.submit"] }));
    const panel = await screen.findByTestId("vault-create-bucket-failed");
    expect(panel).toHaveTextContent(itCatalog["ui.vault_create.bucket_failed_title"]);
    expect(panel).toHaveTextContent("frostvault-photos-a1b2c3d4: S3 refused s3:CreateBucket (AccessDenied)");
    expect(panel).toHaveTextContent(itCatalog["vault_bucket.stage_reserved"]);
    expect(navigate).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/vaults/select")).toBe(false);

    await user.click(screen.getByRole("button", { name: itCatalog["ui.vault_create.bucket_retry"] }));
    await waitFor(() => expect(screen.getByTestId("vault-create-bucket-failed")).toHaveTextContent("PutBucketVersioning"));
    expect(screen.getByTestId("vault-create-bucket-failed")).toHaveTextContent(itCatalog["vault_bucket.stage_created"]);

    requests({ bucketFails: true, retryReady: true, messages: itCatalog });
    await user.click(screen.getByRole("button", { name: itCatalog["ui.vault_create.bucket_retry"] }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/?vault_id=42"));
  });

  it("preserves localized validation errors and the form", async () => {
    requests({ fail: true, messages: itCatalog });
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: itCatalog["ui.vault_create.title"] });
    const name = screen.getByRole("textbox", { name: itCatalog["ui.vault_create.name"] });
    await user.type(name, "Photos");
    await user.click(screen.getByRole("button", { name: itCatalog["ui.vault_create.submit"] }));
    expect(await screen.findByRole("alert")).toHaveTextContent(itCatalog["ui.vault_create.failed"]);
    expect(name).toHaveValue("Photos");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("adopts a free Source Volume directory without a crypt contract", async () => {
    requests({ adopt: true });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("radio", { name: en["ui.vault_create.mode_adopt"] }));
    await screen.findByTestId("vault-create-adopt");
    await user.type(screen.getByRole("textbox", { name: en["ui.vault_create.name"] }), "Photos");
    const buttons = await screen.findAllByRole("button", { name: new RegExp(`^${en["ui.source_directory_browser_select"]} `) });
    await user.click(buttons[0]!);
    await user.click(screen.getByRole("button", { name: en["ui.vault_create.submit"] }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/?vault_id=42"));
    expect(payload()).toEqual({ name: "Photos", creation_mode: "adopt", volume_alias: "photos", relative_path: "albums/2024" });
  });

  it("uses the standard page header, without the sign-in card label", async () => {
    requests();
    renderPage();
    const heading = await screen.findByRole("heading", { level: 1, name: en["ui.vault_create.title"] });
    expect(heading).toHaveClass("text-2xl");
    expect(screen.queryByText("FROSTVAULT")).not.toBeInTheDocument();
  });

  it("explains inline why an existing directory cannot be used when volumes fail to load (#657)", async () => {
    requests({ volumesFail: true });
    const user = userEvent.setup();
    renderPage();
    const notice = await screen.findByTestId("vault-create-volumes-error");
    expect(notice).toHaveTextContent(en["ui.vault_create.volumes_failed"]);
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    requests({ adopt: true });
    await user.click(screen.getByRole("button", { name: en["ui.retry"] }));
    expect(await screen.findByRole("radio", { name: en["ui.vault_create.mode_adopt"] })).toBeInTheDocument();
    expect(screen.queryByTestId("vault-create-volumes-error")).not.toBeInTheDocument();
  });
});

describe("a Vault whose bucket is not active yet (#735)", () => {
  const fetchMock = vi.fn();
  const navigate = vi.fn();
  const calls: { url: string; method: string }[] = [];
  beforeEach(() => {
    resetApiClientForTests();
    fetchMock.mockReset();
    navigate.mockReset();
    calls.length = 0;
    configureApiClient({ fetch: fetchMock, navigate });
    document.documentElement.lang = "en";
  });
  afterEach(() => { cleanup(); resetApiClientForTests(); vi.useRealTimers(); });

  function respond(state: Record<string, unknown>, { cancel = "created", polled = state }: { cancel?: string; polled?: Record<string, unknown> } = {}) {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input); const method = init?.method ?? "GET";
      calls.push({ url, method });
      if (url.startsWith("/api/i18n/catalog")) return Promise.resolve(json({ locale: "en", locales: ["en", "it"], messages: en }));
      if (url === "/api/general/source-volumes") return Promise.resolve(json({ items: [] }));
      if (url === "/api/vaults" && method === "POST") return Promise.resolve(json({
        id: 42, uuid: "vault-uuid", name: "Photos", creation_mode: "empty", lifecycle: "provisioning", bucket: state }, 201));
      if (url === "/api/vaults/42/bucket") return Promise.resolve(json(polled));
      if (url === "/api/vaults/42/creation/cancel" && method === "POST") return Promise.resolve(json({
        vault_id: 42, vault_name: "Photos", bucket: "frostvault-photos-a1b2c3d4", region: "eu-west-1", bucket_left: cancel }));
      if (url === "/api/vaults/select") return Promise.resolve(json({ vault_id: 42 }));
      return Promise.reject(new Error(`Unexpected request ${url}`));
    });
  }
  async function create() {
    const user = userEvent.setup(vi.isFakeTimers() ? { advanceTimers: vi.advanceTimersByTime } : {});
    renderWithQueryClient(
      <I18nProvider><VaultCreatePage displayName="Ada" onNavigate={navigate} /></I18nProvider>,
      { config: { defaultOptions: { queries: { retry: false } } } },
    );
    await user.type(await screen.findByRole("textbox", { name: en["ui.vault_create.name"] }), "Photos");
    await user.click(screen.getByRole("button", { name: en["ui.vault_create.submit"] }));
    return user;
  }
  const creating = { ...bucket("ready"), state: "creating", stage: "created", attempts: 0, error: null, active_at: null, active_in_seconds: null };
  const settling = { ...creating, state: "settling", stage: "verified", active_at: "2026-11-17T12:15:00+00:00", active_in_seconds: 721 };

  it("shows progress, never an empty error, while the bucket is created", async () => {
    respond(creating);
    await create();
    const panel = await screen.findByTestId("vault-create-bucket-creating");
    expect(panel).toHaveAttribute("role", "status");
    expect(panel).toHaveTextContent("FrostVault is creating and verifying the bucket frostvault-photos-a1b2c3d4");
    expect(panel).toHaveTextContent(en["vault_bucket.stage_created"]);
    expect(screen.queryByTestId("vault-create-bucket-failed")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says in about how many minutes a verified Vault becomes active, then opens it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    respond(settling, { polled: { ...settling, state: "ready", stage: null, active_at: null, active_in_seconds: null } });
    await create();
    const panel = await screen.findByTestId("vault-create-bucket-settling");
    expect(panel).toHaveTextContent("the Vault becomes active in about 13 minutes");
    expect(panel).toHaveTextContent("AWS recommends waiting 15 minutes after enabling versioning");
    expect(screen.queryByRole("button", { name: en["ui.vault_create.bucket_retry"] })).toBeNull();
    await vi.advanceTimersByTimeAsync(30_000);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/?vault_id=42"));
    expect(calls.some((call) => call.url === "/api/vaults/42/bucket")).toBe(true);
  });

  it("cancels the creation and names the bucket left in AWS", async () => {
    respond({ ...creating, state: "failed", error: "S3 refused s3:PutBucketVersioning (AccessDenied)" });
    const user = await create();
    await screen.findByTestId("vault-create-bucket-failed");
    await user.click(screen.getByRole("button", { name: en["ui.vault_create.cancel_creation"] }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Cancel the creation of Photos?");
    expect(dialog).toHaveTextContent("frostvault-photos-a1b2c3d4");
    await user.click(within(dialog).getByRole("button", { name: en["ui.vault_create.cancel_creation"] }));
    const outcome = await screen.findByTestId("vault-create-cancelled");
    expect(outcome).toHaveTextContent("The bucket frostvault-photos-a1b2c3d4 (eu-west-1) stays in your AWS account");
    expect(calls.filter((call) => call.url === "/api/vaults/42/creation/cancel" && call.method === "POST")).toHaveLength(1);
    expect(screen.queryByTestId("vault-create-bucket-failed")).toBeNull();
  });
});
