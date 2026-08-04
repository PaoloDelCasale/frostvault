import { useId, useState, type FormEvent } from "react";

import {
  saveAdminSmtpEndpoint,
  saveAdminWebhookEndpoint,
} from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

type BusyChannel = "webhook" | "smtp" | null;

function errorMessage(
  reason: unknown,
  secret: string,
  fallback: string,
): string {
  // Do not expose server error text for an SMTP request at all: a proxy or API
  // must not be able to reflect the password into the admin page.
  if (secret) return fallback;
  return reason instanceof Error && reason.message
    ? reason.message
    : String(reason);
}

export function NotificationsSection() {
  const { t } = useI18n();
  const id = useId();
  const [webhookUrl, setWebhookUrl] = useState("");
  const [webhookEnabled, setWebhookEnabled] = useState(true);
  const [webhookReason, setWebhookReason] = useState("");
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState("587");
  const [smtpUsername, setSmtpUsername] = useState("");
  const [smtpPassword, setSmtpPassword] = useState("");
  const [smtpFromAddress, setSmtpFromAddress] = useState("");
  const [smtpUseTls, setSmtpUseTls] = useState(true);
  const [smtpEnabled, setSmtpEnabled] = useState(true);
  const [smtpReason, setSmtpReason] = useState("");
  const [busy, setBusy] = useState<BusyChannel>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function handleWebhookSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!webhookUrl.trim()) {
      setError(t("admin.notifications_webhook_url_required"));
      return;
    }
    if (webhookReason.trim().length < 3) {
      setError(t("admin.notifications_reason_required"));
      return;
    }

    setBusy("webhook");
    try {
      await saveAdminWebhookEndpoint({
        url: webhookUrl.trim(),
        enabled: webhookEnabled,
        reason: webhookReason.trim(),
      });
      setWebhookReason("");
      setNotice(t("admin.notifications_webhook_saved"));
    } catch (reason) {
      setError(
        errorMessage(
          reason,
          "",
          t("admin.notifications_save_failed"),
        ),
      );
    } finally {
      setBusy(null);
    }
  }

  async function handleSmtpSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!smtpHost.trim() || !smtpFromAddress.trim()) {
      setError(t("admin.notifications_smtp_required"));
      return;
    }
    if (smtpReason.trim().length < 3) {
      setError(t("admin.notifications_reason_required"));
      return;
    }
    const port = Number(smtpPort);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setError(t("admin.notifications_smtp_port_invalid"));
      return;
    }

    setBusy("smtp");
    try {
      await saveAdminSmtpEndpoint({
        host: smtpHost.trim(),
        port,
        username: smtpUsername.trim(),
        password: smtpPassword,
        from_address: smtpFromAddress.trim(),
        use_tls: smtpUseTls,
        enabled: smtpEnabled,
        reason: smtpReason.trim(),
      });
      // The API returns only endpoint metadata. The password is intentionally
      // cleared and is never copied into response or success state.
      setSmtpPassword("");
      setSmtpReason("");
      setNotice(t("admin.notifications_smtp_saved"));
    } catch (reason) {
      setError(
        errorMessage(
          reason,
          smtpPassword,
          t("admin.notifications_save_failed"),
        ),
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      aria-labelledby="admin-notifications-heading"
      className="grid gap-4"
      data-testid="admin-notifications"
    >
      <div>
        <h2 id="admin-notifications-heading" className="text-xl font-bold">
          {t("admin.notifications_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">
          {t("admin.notifications_subtitle")}
        </p>
        <p className="mt-2 text-sm text-muted">
          {t("admin.notifications_no_test_send")}
        </p>
      </div>

      {error ? (
        <p role="alert" className="break-words text-sm font-bold text-[var(--state-local-fg)]">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p role="status" className="text-sm font-bold text-green">
          {notice}
        </p>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel className="p-5">
          <h3 className="text-lg font-bold">{t("admin.notifications_webhook_heading")}</h3>
          <p className="mt-1 text-sm text-muted">
            {t("admin.notifications_webhook_help")}
          </p>
          <form
            className="mt-4 grid gap-3"
            onSubmit={(event) => void handleWebhookSubmit(event)}
          >
            <FormField
              label={t("admin.notifications_webhook_url")}
              htmlFor={`${id}-webhook-url`}
            >
              <FormInput
                id={`${id}-webhook-url`}
                type="url"
                required
                value={webhookUrl}
                onChange={(event) => setWebhookUrl(event.target.value)}
              />
            </FormField>
            <label className="flex min-h-11 items-center gap-2 text-sm font-bold text-muted">
              <input
                id={`${id}-webhook-enabled`}
                type="checkbox"
                checked={webhookEnabled}
                onChange={(event) => setWebhookEnabled(event.target.checked)}
              />
              {t("admin.notifications_enabled")}
            </label>
            <FormField
              label={t("admin.notifications_reason")}
              htmlFor={`${id}-webhook-reason`}
              help={t("admin.notifications_reason_help")}
            >
              <FormInput
                id={`${id}-webhook-reason`}
                required
                minLength={3}
                maxLength={500}
                value={webhookReason}
                onChange={(event) => setWebhookReason(event.target.value)}
              />
            </FormField>
            <div className="flex justify-end">
              <Button
                type="submit"
                variant="primary"
                disabled={busy !== null}
              >
                {busy === "webhook"
                  ? t("admin.notifications_saving")
                  : t("admin.notifications_save_webhook")}
              </Button>
            </div>
          </form>
        </Panel>

        <Panel className="p-5">
          <h3 className="text-lg font-bold">{t("admin.notifications_smtp_heading")}</h3>
          <p className="mt-1 text-sm text-muted">
            {t("admin.notifications_smtp_help")}
          </p>
          <form
            className="mt-4 grid gap-3"
            onSubmit={(event) => void handleSmtpSubmit(event)}
          >
            <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_8rem]">
              <FormField
                label={t("admin.notifications_smtp_host")}
                htmlFor={`${id}-smtp-host`}
              >
                <FormInput
                  id={`${id}-smtp-host`}
                  required
                  value={smtpHost}
                  onChange={(event) => setSmtpHost(event.target.value)}
                />
              </FormField>
              <FormField
                label={t("admin.notifications_smtp_port")}
                htmlFor={`${id}-smtp-port`}
              >
                <FormInput
                  id={`${id}-smtp-port`}
                  type="number"
                  required
                  min={1}
                  max={65535}
                  value={smtpPort}
                  onChange={(event) => setSmtpPort(event.target.value)}
                />
              </FormField>
            </div>
            <FormField
              label={t("admin.notifications_smtp_username")}
              htmlFor={`${id}-smtp-username`}
            >
              <FormInput
                id={`${id}-smtp-username`}
                autoComplete="username"
                value={smtpUsername}
                onChange={(event) => setSmtpUsername(event.target.value)}
              />
            </FormField>
            <FormField
              label={t("admin.notifications_smtp_password")}
              htmlFor={`${id}-smtp-password`}
              help={t("admin.notifications_smtp_password_write_only")}
            >
              <FormInput
                id={`${id}-smtp-password`}
                type="password"
                autoComplete="new-password"
                value={smtpPassword}
                onChange={(event) => setSmtpPassword(event.target.value)}
              />
            </FormField>
            <FormField
              label={t("admin.notifications_smtp_from_address")}
              htmlFor={`${id}-smtp-from-address`}
            >
              <FormInput
                id={`${id}-smtp-from-address`}
                type="email"
                required
                value={smtpFromAddress}
                onChange={(event) => setSmtpFromAddress(event.target.value)}
              />
            </FormField>
            <div className="grid gap-2">
              <label className="flex min-h-11 items-center gap-2 text-sm font-bold text-muted">
                <input
                  id={`${id}-smtp-tls`}
                  type="checkbox"
                  checked={smtpUseTls}
                  onChange={(event) => setSmtpUseTls(event.target.checked)}
                />
                {t("admin.notifications_smtp_use_tls")}
              </label>
              <label className="flex min-h-11 items-center gap-2 text-sm font-bold text-muted">
                <input
                  id={`${id}-smtp-enabled`}
                  type="checkbox"
                  checked={smtpEnabled}
                  onChange={(event) => setSmtpEnabled(event.target.checked)}
                />
                {t("admin.notifications_enabled")}
              </label>
            </div>
            <FormField
              label={t("admin.notifications_reason")}
              htmlFor={`${id}-smtp-reason`}
              help={t("admin.notifications_reason_help")}
            >
              <FormInput
                id={`${id}-smtp-reason`}
                required
                minLength={3}
                maxLength={500}
                value={smtpReason}
                onChange={(event) => setSmtpReason(event.target.value)}
              />
            </FormField>
            <div className="flex justify-end">
              <Button type="submit" variant="primary" disabled={busy !== null}>
                {busy === "smtp"
                  ? t("admin.notifications_saving")
                  : t("admin.notifications_save_smtp")}
              </Button>
            </div>
          </form>
        </Panel>
      </div>
    </section>
  );
}
