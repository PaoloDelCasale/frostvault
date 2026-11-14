import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, ReauthenticationRedirectError, apiRequest, configureApiClient, resetApiClientForTests } from "@/api";
import { translate } from "@/i18n";
import { errorMessage } from "@/lib/errorMessage";
import { jsonResponse, loadCatalog } from "./vault-settings-harness";

const it_ = loadCatalog("it");
const en = loadCatalog("en");
const t = (key: string, params?: Record<string, string | number>) => translate(it_, key, params);

afterEach(resetApiClientForTests);

describe("errorMessage (#651)", () => {
  it("translates an ApiError with message_key and its params, without technical prefix", () => {
    const error = new ApiError("raw", { status: 409, messageKey: "ui.error_http", messageParams: { status: 409 } });
    expect(String(error)).toMatch(/^ApiError:/);
    expect(errorMessage(error, t)).toBe("La richiesta non è riuscita (HTTP 409).");
  });

  it("keeps the server message of an ApiError without message_key", () => {
    expect(errorMessage(new ApiError("Il bucket ha rifiutato", { status: 409 }), t)).toBe("Il bucket ha rifiutato");
  });

  it("falls back to the catalog by HTTP status for an ApiError without key or message", () => {
    expect(errorMessage(new ApiError("", { status: 502 }), t)).toBe("Errore del server (HTTP 502). Riprova più tardi.");
    expect(errorMessage(new ApiError("", { status: 404 }), t)).toBe("La richiesta non è riuscita (HTTP 404).");
  });

  it("falls back to the message when the key is missing from the catalog", () => {
    expect(errorMessage(new ApiError("Testo del server", { status: 400, messageKey: "server.unknown_key" }), t)).toBe("Testo del server");
  });

  it("reports a fetch TypeError as the server being unreachable", () => {
    expect(errorMessage(new TypeError("Failed to fetch"), t)).toBe(it_["ui.error_network"]);
    expect(it_["ui.error_network"]).toMatch(/^Impossibile contattare il server/);
  });

  it("localizes reauthentication redirects and unknown values", () => {
    expect(errorMessage(new ReauthenticationRedirectError(), t)).toBe(it_["ui.error_reauth_redirect"]);
    expect(errorMessage(undefined, t)).toBe(it_["ui.error_unknown"]);
    expect(errorMessage(new Error("Messaggio"), t)).toBe("Messaggio");
  });

  it.each([
    [500, {}, "Errore del server (HTTP 500). Riprova più tardi."],
    [400, {}, "La richiesta non è riuscita (HTTP 400)."],
    [422, { detail: [{ msg: "field required" }] }, "Alcuni valori non sono validi."],
  ])("the API client builds HTTP %s fallbacks from the catalog", async (status, body, expected) => {
    configureApiClient({
      fetch: vi.fn(async () => jsonResponse(body, status)),
      translate: (key, params) => translate(it_, key, params),
    });
    const failure = await apiRequest("/api/x").catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).message).toBe(expected);
    expect(errorMessage(failure, t)).toBe(expected);
  });

  it("defines every fallback key in both catalogs", () => {
    for (const key of ["ui.error_network", "ui.error_http", "ui.error_server", "ui.error_validation", "ui.error_invalid_response", "ui.error_auth_required", "ui.error_reauth_redirect", "ui.error_unknown"]) {
      expect(it_[key], key).toBeTruthy();
      expect(en[key], key).toBeTruthy();
    }
  });
});
