import { useEffect, useState } from "react";

import {
  activateOidcConfiguration,
  disableOidcConfiguration,
  fetchOidcConfiguration,
  rotateOidcSecret,
  saveOidcDraft,
  validateOidcDraft,
  type OidcConfigurationResponse,
} from "@/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField, FormInput } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

export function OidcSection() {
  const { t } = useI18n();
  const [configuration, setConfiguration] = useState<OidcConfigurationResponse | null>(null);
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [scopes, setScopes] = useState("openid profile");
  const [ttl, setTtl] = useState("300");
  const [rotationSecret, setRotationSecret] = useState("");
  const [disableOpen, setDisableOpen] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function applyResponse(response: OidcConfigurationResponse) {
    setConfiguration(response);
    const editable = response.draft ?? response.active;
    setIssuer(editable.issuer ?? "");
    setClientId(editable.client_id ?? "");
    setScopes((editable.scopes ?? ["openid", "profile"]).join(" "));
    setTtl(String(editable.login_transaction_ttl_seconds ?? 300));
    setClientSecret("");
    setRotationSecret("");
  }

  useEffect(() => {
    let cancelled = false;
    void fetchOidcConfiguration()
      .then((response) => {
        if (!cancelled) applyResponse(response);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function run(action: () => Promise<OidcConfigurationResponse>) {
    setBusy(true);
    setError("");
    try {
      applyResponse(await action());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!configuration) {
    return error ? <p role="alert">{error}</p> : <p className="text-sm text-muted">{t("admin.oidc_loading")}</p>;
  }

  const draftValid = configuration.draft?.validation_status === "valid";
  const active = configuration.active;

  return (
    <section aria-labelledby="admin-oidc-heading" className="grid gap-4">
      <div>
        <h2 id="admin-oidc-heading" className="text-xl font-bold">{t("admin.oidc_heading")}</h2>
        <p className="mt-1 text-sm text-muted">{t("admin.oidc_issuer_warning")}</p>
      </div>
      {error ? <p role="alert" className="text-sm text-red-700">{error}</p> : null}
      <Panel className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-lg font-bold">{t("admin.oidc_status")}</h3>
            <p className="mt-1 text-sm font-bold text-muted">
              {active.enabled ? t("admin.oidc_active") : t("admin.oidc_inactive")}
            </p>
          </div>
          <span className="rounded-full bg-canvas px-3 py-1 text-xs font-bold capitalize">
            {t(`admin.oidc_configuration_status_${configuration.configuration_status}`)}
          </span>
        </div>
        <dl className="mt-4 grid gap-2 text-sm sm:grid-cols-2">
          <div><dt className="font-bold text-muted">{t("admin.oidc_callback")}</dt><dd className="break-all">{active.callback_url}</dd></div>
          <div><dt className="font-bold text-muted">{t("admin.oidc_secret_status")}</dt><dd>{active.client_secret_configured ? t("admin.configured") : t("admin.not_configured")}</dd></div>
        </dl>
      </Panel>

      <Panel className="p-5">
        <h3 className="text-lg font-bold">{t("admin.oidc_draft")}</h3>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <FormField label={t("admin.oidc_issuer")} htmlFor="oidc-issuer">
            <FormInput id="oidc-issuer" type="url" required value={issuer} onChange={(event) => setIssuer(event.target.value)} />
          </FormField>
          <FormField label={t("admin.oidc_client_id")} htmlFor="oidc-client-id">
            <FormInput id="oidc-client-id" required value={clientId} onChange={(event) => setClientId(event.target.value)} />
          </FormField>
          <FormField label={t("admin.oidc_client_secret")} htmlFor="oidc-client-secret" help={t("admin.oidc_secret_write_only")}>
            <FormInput id="oidc-client-secret" type="password" autoComplete="new-password" required value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} />
          </FormField>
          <FormField label={t("admin.oidc_scopes")} htmlFor="oidc-scopes">
            <FormInput id="oidc-scopes" required value={scopes} onChange={(event) => setScopes(event.target.value)} />
          </FormField>
          <FormField label={t("admin.oidc_ttl")} htmlFor="oidc-ttl">
            <FormInput id="oidc-ttl" type="number" min={60} value={ttl} onChange={(event) => setTtl(event.target.value)} />
          </FormField>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button type="button" variant="primary" disabled={busy || !issuer || !clientId || !clientSecret} onClick={() => void run(() => saveOidcDraft({ issuer, client_id: clientId, client_secret: clientSecret, scopes: scopes.split(/\s+/).filter(Boolean), login_transaction_ttl_seconds: Number(ttl) }))}>
            {t("admin.oidc_save_draft")}
          </Button>
          <Button type="button" variant="secondary" disabled={busy || !configuration.draft} onClick={() => void run(validateOidcDraft)}>
            {t("admin.oidc_validate")}
          </Button>
          <Button type="button" variant="primary" disabled={busy || !draftValid} onClick={() => void run(activateOidcConfiguration)}>
            {t("admin.oidc_activate")}
          </Button>
        </div>
        {configuration.last_validation ? (
          <p className="mt-3 text-sm capitalize" role="status">
            {t("admin.oidc_validation_result", {
              status: t(`admin.oidc_validation_status_${configuration.last_validation.status}`),
            })}
            {configuration.last_validation.error ? `: ${configuration.last_validation.error}` : ""}
          </p>
        ) : null}
      </Panel>

      <Panel className="p-5">
        <h3 className="text-lg font-bold">{t("admin.oidc_secret_rotation")}</h3>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <FormField label={t("admin.oidc_replacement_secret")} htmlFor="oidc-rotation-secret" className="min-w-60 flex-1">
            <FormInput id="oidc-rotation-secret" type="password" autoComplete="new-password" value={rotationSecret} onChange={(event) => setRotationSecret(event.target.value)} />
          </FormField>
          <Button type="button" variant="secondary" disabled={busy || !rotationSecret || !active.enabled} onClick={() => void run(() => rotateOidcSecret(rotationSecret))}>{t("admin.oidc_rotate")}</Button>
          <Button type="button" variant="danger" disabled={busy || !active.enabled} onClick={() => setDisableOpen(true)}>{t("admin.oidc_disable")}</Button>
        </div>
      </Panel>

      <ConfirmDialog
        open={disableOpen}
        onOpenChange={setDisableOpen}
        title={t("admin.oidc_disable_title")}
        description={t("admin.oidc_disable_help")}
        confirmLabel={t("admin.oidc_disable_confirm")}
        cancelLabel={t("admin.cancel")}
        onConfirm={() => {
          setDisableOpen(false);
          void run(disableOidcConfiguration);
        }}
      />
    </section>
  );
}
