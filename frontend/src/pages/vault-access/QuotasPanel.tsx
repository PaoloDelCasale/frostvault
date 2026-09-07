import { useEffect, useRef, useState } from "react";

import {
  createLatestRequestScope,
  fetchVaultQuotas,
  updateAdminVaultQuotas,
  type VaultQuotaUpdatePayload,
  type VaultQuotasResponse,
} from "@/api";
import { FormField, FormInput } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

import {
  buildQuotaPayload,
  formatQuotaValue,
  quotaStateItems,
  type QuotaFormValues,
} from "./quota";

type QuotasPanelProps = {
  vaultId: number;
  isAdmin: boolean;
  onNotice: (message: string, error?: boolean) => void;
};

const emptyForm: QuotaFormValues = {
  storage_soft_limit_bytes: "",
  storage_hard_limit_bytes: "",
  concurrency_soft_limit: "",
  concurrency_hard_limit: "",
  restore_30d_soft_limit_bytes: "",
  restore_30d_hard_limit_bytes: "",
  reason: "",
};

function limitsToForm(data: VaultQuotasResponse): QuotaFormValues {
  const limits = data.limits ?? {};
  const asText = (value: number | null | undefined) =>
    value === null || value === undefined ? "" : String(value);
  return {
    storage_soft_limit_bytes: asText(limits.storage_soft_limit_bytes),
    storage_hard_limit_bytes: asText(limits.storage_hard_limit_bytes),
    concurrency_soft_limit: asText(limits.concurrency_soft_limit),
    concurrency_hard_limit: asText(limits.concurrency_hard_limit),
    restore_30d_soft_limit_bytes: asText(limits.restore_30d_soft_limit_bytes),
    restore_30d_hard_limit_bytes: asText(limits.restore_30d_hard_limit_bytes),
    reason: "",
  };
}

export function QuotasPanel({ vaultId, isAdmin, onNotice }: QuotasPanelProps) {
  const { t, ready } = useI18n();
  const [loadState, setLoadState] = useState("");
  const [data, setData] = useState<VaultQuotasResponse | null>(null);
  const [form, setForm] = useState<QuotaFormValues>(emptyForm);
  const [saving, setSaving] = useState(false);
  const scope = useRef(createLatestRequestScope()).current;
  const loadedRef = useRef(false);

  useEffect(() => {
    if (!ready) return;
    const handle = scope.begin();
    loadedRef.current = false;
    setLoadState(t("access.quotas_loading"));
    void (async () => {
      try {
        const result = await handle.settle(fetchVaultQuotas());
        if (result === undefined) return;
        setData(result);
        setForm(limitsToForm(result));
        loadedRef.current = true;
        setLoadState(t("access.quotas_loaded"));
      } catch (error) {
        if (handle.isCurrent()) {
          const message = error instanceof Error ? error.message : String(error);
          setLoadState(message);
          onNotice(message, true);
        }
      }
    })();
  }, [onNotice, ready, scope, t, vaultId]);

  function setField(name: keyof QuotaFormValues, value: string) {
    setForm((prev) => ({ ...prev, [name]: value }));
  }

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    if (!loadedRef.current || !scope.hasSettledCurrent()) {
      onNotice(t("access.quotas_wait_loading"), true);
      return;
    }
    const built = buildQuotaPayload(form, t);
    if (!built.ok) {
      onNotice(built.error, true);
      return;
    }
    const payload: VaultQuotaUpdatePayload = built.payload;
    setSaving(true);
    const handle = scope.begin();
    try {
      const result = await handle.settle(
        updateAdminVaultQuotas(vaultId, payload),
      );
      if (result === undefined) return;
      setData(result);
      setForm(limitsToForm(result));
      loadedRef.current = true;
      setLoadState(t("access.quotas_loaded"));
      onNotice(t("access.quotas_updated"));
    } catch (error) {
      if (handle.isCurrent()) {
        onNotice(error instanceof Error ? error.message : String(error), true);
      }
    } finally {
      if (handle.isCurrent()) setSaving(false);
    }
  }

  const stateItems = data ? quotaStateItems(data.evaluation, t) : [];
  const usage = data?.usage ?? {};
  const limits = data?.limits ?? {};
  const metrics = [
    {
      title: t("access.quotas_storage"),
      help: t("access.quotas_storage_help"),
      used: formatQuotaValue(
        usage.storage_bytes,
        t("access.quotas_bytes"),
        t,
        usage.storage_unknown,
      ),
      softName: "storage_soft_limit_bytes" as const,
      hardName: "storage_hard_limit_bytes" as const,
      softValue: formatQuotaValue(
        limits.storage_soft_limit_bytes,
        t("access.quotas_bytes"),
        t,
      ),
      hardValue: formatQuotaValue(
        limits.storage_hard_limit_bytes,
        t("access.quotas_bytes"),
        t,
      ),
    },
    {
      title: t("access.quotas_concurrency"),
      help: t("access.quotas_concurrency_help"),
      used: formatQuotaValue(usage.concurrency, t("access.quotas_jobs"), t),
      softName: "concurrency_soft_limit" as const,
      hardName: "concurrency_hard_limit" as const,
      softValue: formatQuotaValue(
        limits.concurrency_soft_limit,
        t("access.quotas_jobs"),
        t,
      ),
      hardValue: formatQuotaValue(
        limits.concurrency_hard_limit,
        t("access.quotas_jobs"),
        t,
      ),
    },
    {
      title: t("access.quotas_restore"),
      help: t("access.quotas_restore_help"),
      used: formatQuotaValue(
        usage.restore_30d_bytes,
        t("access.quotas_bytes"),
        t,
        usage.restore_request_unknown,
      ),
      softName: "restore_30d_soft_limit_bytes" as const,
      hardName: "restore_30d_hard_limit_bytes" as const,
      softValue: formatQuotaValue(
        limits.restore_30d_soft_limit_bytes,
        t("access.quotas_bytes"),
        t,
      ),
      hardValue: formatQuotaValue(
        limits.restore_30d_hard_limit_bytes,
        t("access.quotas_bytes"),
        t,
      ),
    },
  ];

  const body = (
    <>
      <p className="mt-3 text-sm text-muted">{t("access.quotas_soft_help")}</p>
      <p className="text-sm text-muted">{t("access.quotas_hard_help")}</p>
      {isAdmin ? (
        <p className="mt-2 text-sm text-muted">{t("access.quotas_form_help")}</p>
      ) : null}

      <div className="mt-4 grid gap-3">
        {metrics.map((metric) => (
          <div
            key={metric.softName}
            className="rounded-[14px] border border-line bg-canvas p-3"
          >
            <h3 className="text-sm font-bold text-ink">{metric.title}</h3>
            <p className="mt-1 text-sm text-muted">{metric.help}</p>
            <div className="mt-3 grid gap-3 text-sm">
              <div className="flex flex-wrap justify-between gap-2">
                <p className="font-bold text-muted">{t("access.quotas_usage")}</p>
                <p className="font-bold text-ink">{metric.used}</p>
              </div>
              {isAdmin ? (
                <>
                  <FormField
                    label={t("access.quotas_soft_limit")}
                    htmlFor={`quota-${metric.softName}`}
                  >
                    <FormInput
                      id={`quota-${metric.softName}`}
                      name={metric.softName}
                      inputMode="numeric"
                      aria-label={`${metric.title} (${t("access.quotas_soft")})`}
                      placeholder={t("access.quotas_unlimited")}
                      value={form[metric.softName]}
                      onChange={(event) => setField(metric.softName, event.target.value)}
                    />
                  </FormField>
                  <FormField
                    label={t("access.quotas_hard_limit")}
                    htmlFor={`quota-${metric.hardName}`}
                  >
                    <FormInput
                      id={`quota-${metric.hardName}`}
                      name={metric.hardName}
                      inputMode="numeric"
                      aria-label={`${metric.title} (${t("access.quotas_hard")})`}
                      placeholder={t("access.quotas_unlimited")}
                      value={form[metric.hardName]}
                      onChange={(event) => setField(metric.hardName, event.target.value)}
                    />
                  </FormField>
                </>
              ) : (
                <>
                  <div className="flex flex-wrap justify-between gap-2">
                    <p className="font-bold text-muted">
                      {t("access.quotas_soft_limit")}
                    </p>
                    <p>{metric.softValue}</p>
                  </div>
                  <div className="flex flex-wrap justify-between gap-2">
                    <p className="font-bold text-muted">
                      {t("access.quotas_hard_limit")}
                    </p>
                    <p>{metric.hardValue}</p>
                  </div>
                </>
              )}
            </div>
          </div>
        ))}

        <div aria-live="polite" data-testid="quota-state">
          <h3 className="text-sm font-bold text-ink">{t("access.quotas_state")}</h3>
          <ul className="mt-2 grid gap-1">
            {stateItems.map((item) => (
              <li
                key={`${item.kind}-${item.text}`}
                data-quota-state={item.kind}
                className={
                  item.kind === "block"
                    ? "rounded-[10px] bg-red-soft px-3 py-2 text-sm font-bold text-ink"
                    : item.kind === "warning"
                      ? "rounded-[10px] bg-amber-soft px-3 py-2 text-sm font-bold text-ink"
                      : item.kind === "ok"
                        ? "rounded-[10px] bg-green-soft px-3 py-2 text-sm font-bold text-ink"
                        : "rounded-[10px] bg-canvas px-3 py-2 text-sm font-bold text-muted"
                }
              >
                {item.text}
              </li>
            ))}
          </ul>
        </div>
      </div>

      {isAdmin ? (
        <div className="mt-5">
          <Button
            type="submit"
            className="min-h-11 w-full sm:w-auto"
            disabled={saving}
          >
            {t("access.quotas_save")}
          </Button>
        </div>
      ) : null}
    </>
  );

  return (
    <section data-panel="quotas">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.quotas_title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("access.quotas_help")}</p>
        <p className="mt-2 text-sm text-muted" role="status">
          {loadState}
        </p>
        {isAdmin ? (
          <form className="mt-1" onSubmit={(event) => void onSave(event)}>
            {body}
          </form>
        ) : (
          body
        )}
      </Panel>
    </section>
  );
}
