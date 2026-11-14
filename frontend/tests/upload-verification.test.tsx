import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { resetApiClientForTests } from "@/api";
import { VerificationEvidence } from "@/pages/archive/VerificationEvidence";
import { createVaultAccessFetch, defaultPolicy, jsonResponse, loadCatalog, renderVaultAccess } from "./vault-settings-harness";

afterEach(resetApiClientForTests);

describe("new-upload verification choice", () => {
  it("shows inherited deployment and effective modes and updates only the scoped override", async () => {
    const fetch = createVaultAccessFetch({
      "GET /api/vault/operation-policy": () => jsonResponse({ ...defaultPolicy, upload_verification: null, global_upload_verification: "s3_checksum", effective_upload_verification: "s3_checksum" }),
      "PUT /api/vault/operation-policy": (init) => {
        expect(JSON.parse(String(init?.body))).toEqual({ upload_verification: "read_back" });
        return jsonResponse({ ...defaultPolicy, upload_verification: "read_back", global_upload_verification: "s3_checksum", effective_upload_verification: "read_back" });
      },
    });
    renderVaultAccess({ vaultId: 7, fetchImpl: fetch });
    const select = await screen.findByRole("combobox", { name: "New upload verification" });
    expect(select).toHaveValue("inherit");
    expect(await screen.findByText("Effective for new uploads: S3 SHA-256 check on receipt")).toBeInTheDocument();
    await userEvent.setup().selectOptions(select, "read_back");
    expect(await screen.findByText("Effective for new uploads: Full SHA-256 read-back")).toBeInTheDocument();
    expect(fetch.mock.calls.some(([url, init]) => String(url) === "/api/vault/operation-policy?vault_id=7" && init?.method === "PUT")).toBe(true);
    expect(screen.getByText(/Only new uploads use this choice/)).toBeInTheDocument();
  });
  it("inherit sends null rather than a mutable global setting", async () => {
    const fetch = createVaultAccessFetch({
      "GET /api/vault/operation-policy": () => jsonResponse({ ...defaultPolicy, upload_verification: "read_back", global_upload_verification: "s3_checksum", effective_upload_verification: "read_back" }),
      "PUT /api/vault/operation-policy": (init) => {
        expect(JSON.parse(String(init?.body))).toEqual({ upload_verification: null });
        return jsonResponse({ ...defaultPolicy, upload_verification: null, global_upload_verification: "s3_checksum", effective_upload_verification: "s3_checksum" });
      },
    });
    renderVaultAccess({ fetchImpl: fetch });
    const select = await screen.findByRole("combobox", { name: "New upload verification" });
    await userEvent.setup().selectOptions(select, "inherit");
    expect(await screen.findByText("Effective for new uploads: S3 SHA-256 check on receipt")).toBeInTheDocument();
  });
  it.each(["en", "it"] as const)("shows actual fallback/method/egress evidence in %s, not today's policy", (locale) => {
    const catalog = loadCatalog(locale);
    const t = (key: string) => catalog[key] ?? key;
    render(<VerificationEvidence requested="s3_checksum" method="read_back" fallback="checksum_missing" egress={1234} t={t} />);
    expect(screen.getByRole("status")).toHaveTextContent(catalog["verification.checksum_missing"]!);
    expect(screen.getByText(new RegExp(catalog["verification.egress"]!))).toBeInTheDocument();
    expect(screen.getByText(`${catalog["verification.method"]}: ${catalog["verification.read_back"]}`)).toBeInTheDocument();
  });
});
