import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiQueryProvider, configureApiClient, createAppQueryClient, resetApiClientForTests, type ApiFetch } from "@/api";
import { Badge } from "@/components/Badge";
import { ProblemCount } from "@/components/ProblemCount";
import { I18nProvider } from "@/i18n/I18nProvider";
import { ShellNavItems } from "@/layout/ShellNavItems";
import { OverviewPage } from "@/pages/overview/OverviewPage";
import { createVaultAccessFetch, defaultLifecycle, defaultPolicy, jsonResponse, loadCatalog, renderVaultAccess } from "./vault-settings-harness";

afterEach(() => { cleanup(); resetApiClientForTests(); });

// #658: one save model, one problem-count style, tones that follow the state.
describe("settings save model", () => {
  it("saves the auto-upload checkbox at once and confirms «Saved» beside it, without a toast", async () => {
    const user = userEvent.setup();
    const updates: unknown[] = [];
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "PUT /api/vault/operation-policy": (init) => {
        const body = JSON.parse(String(init?.body)) as { auto_upload: boolean };
        updates.push(body);
        return jsonResponse({ ...defaultPolicy, ...body });
      },
    }) });
    const toggle = await screen.findByRole("checkbox", { name: "Enable automatic upload for this Vault" });
    await waitFor(() => expect(toggle).toBeEnabled());
    const protection = screen.getByRole("region", { name: "Protection" });
    expect(within(protection).getByTestId("saved-indicator")).toBeEmptyDOMElement();
    await user.click(toggle);
    await waitFor(() => expect(updates).toEqual([{ auto_upload: false }]));
    expect(await within(protection).findByTestId("saved-indicator")).toHaveTextContent("Saved");
    expect(within(protection).getByTestId("auto-upload-off-hint")).toBeInTheDocument();
    expect(screen.queryByText(/Automatic upload turned/)).toBeNull();
  });

  it("enables «Save plan» only after the plan changes", async () => {
    const user = userEvent.setup();
    renderVaultAccess({ fetchImpl: createVaultAccessFetch({
      "GET /api/vault/lifecycle": () => jsonResponse({ ...defaultLifecycle, profile: defaultLifecycle.guided_profiles.economical }),
    }) });
    await user.click(await screen.findByText("Show plan and moves", { exact: true }));
    const save = await screen.findByRole("button", { name: "Save plan" });
    expect(save).toBeDisabled();
    await user.click(screen.getByRole("combobox", { name: "Storage plan" }));
    await user.click(screen.getByRole("option", { name: /^Custom/ }));
    expect(save).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Add transition" }));
    expect(save).toBeEnabled();
  });

  it("names the immediate «Saved» confirmation and the verification save button in both languages", () => {
    for (const locale of ["en", "it"] as const) {
      const messages = loadCatalog(locale);
      expect(messages["settings.saved"]).toBeTruthy();
      expect(messages["settings.verification_save"]).toBeTruthy();
      expect(messages["settings.auto_on"]).toBeUndefined();
    }
  });
});

describe("badge and count tones", () => {
  it("shows «Pending» in amber, apart from the red of a Problem", () => {
    render(<><Badge state="pending" /><Badge state="problem" /></>);
    expect(screen.getByText("Pending")).toHaveClass("bg-[var(--state-pending-bg)]");
    expect(screen.getByText("Pending").className).not.toMatch(/state-(local|problem)/);
    expect(screen.getByText("Problem")).toHaveClass("bg-[var(--state-problem-bg)]");
  });

  it("uses one problem-count style in the navigation", () => {
    render(<ShellNavItems openProblems={3} />);
    const count = screen.getByTestId("nav-problem-count");
    expect(count).toHaveAttribute("data-tone", "problem");
    expect(count).toHaveClass("bg-[var(--state-problem-bg)]");
    cleanup();
    render(<ProblemCount count={0} testId="zero" />);
    expect(screen.getByTestId("zero")).toHaveAttribute("data-tone", "none");
  });
});

describe("Overview «Protected files» tile", () => {
  function setup(stats: Record<string, object>) {
    const messages = loadCatalog("en");
    configureApiClient({ fetch: vi.fn<ApiFetch>(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/i18n/catalog")) return jsonResponse({ locale: "en", locales: ["en", "it"], messages });
      if (url.startsWith("/api/stats")) return jsonResponse(stats[new URL(url, "http://x").searchParams.get("vault_id") ?? ""]);
      if (url.startsWith("/api/general/vaults")) return jsonResponse({ items: [] });
      return jsonResponse({}, 404);
    }) });
    const vaults = Object.keys(stats).map((id) => ({ id: Number(id), uuid: `v${id}`, name: `Vault ${id}` }));
    render(<ApiQueryProvider client={createAppQueryClient()}><I18nProvider><OverviewPage vaults={vaults} /></I18nProvider></ApiQueryProvider>);
  }

  it.each([
    ["good", { protected: 4 }],
    ["warn", { protected: 1, pending: 3 }],
    ["warn", { pending: 3 }],
  ] as const)("is %s for %j", async (tone, states) => {
    setup({ 1: { states, protection_state: "pending", storage: { local_bytes: 10, cloud_bytes: 2048 } } });
    const tile = await screen.findByTestId("tile-protected");
    await waitFor(() => expect(tile).toHaveAttribute("data-tone", tone));
  });

  it("shows the cloud column on mobile cards", async () => {
    setup({ 1: { states: { protected: 1 }, protection_state: "protected", storage: { local_bytes: 10, cloud_bytes: 2048 } } });
    const card = within(await screen.findByTestId("overview-cards"));
    expect(await card.findByText("In cloud")).toBeInTheDocument();
  });
});
