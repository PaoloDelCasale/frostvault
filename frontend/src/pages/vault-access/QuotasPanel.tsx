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

  return (
    <section data-panel="quotas">
      <Panel className="p-4 sm:p-5">
        <h2 className="text-lg font-bold">{t("access.quotas_title")}</h2>
        <p className="mt-1 text-sm text-muted">{t("access.quotas_help")}</p>
        <p className="mt-2 text-sm text-muted" role="status">
          {loadState}
        </p>

        <div className="mt-4 grid gap-4">
          <div>
            <h3 className="text-sm font-bold text-ink">{t("access.quotas_limits")}</h3>
            <dl className="mt-2 grid gap-2 text-sm">
              <div>
                <dt className="font-bold text-muted">{t("access.quotas_storage")}</dt>
                <dd>
                  {formatQuotaValue(limits.storage_soft_limit_bytes, t("access.quotas_bytes"), t)}{" "}
                  {t("access.quotas_soft")} ·{" "}
                  {formatQuotaValue(limits.storage_hard_limit_bytes, t("access.quotas_bytes"), t)}{" "}
                  {t("access.quotas_hard")}
                </dd>
              </div>
              <div>
                <dt className="font-bold text-muted">{t("access.quotas_concurrency")}</dt>
                <dd>
                  {formatQuotaValue(limits.concurrency_soft_limit, t("access.quotas_jobs"), t)}{" "}
                  {t("access.quotas_soft")} ·{" "}
                  {formatQuotaValue(limits.concurrency_hard_limit, t("access.quotas_jobs"), t)}{" "}
                  {t("access.quotas_hard")}
                </dd>
              </div>
              <div>
                <dt className="font-bold text-muted">{t("access.quotas_restore")}</dt>
                <dd>
                  {formatQuotaValue(
                    limits.restore_30d_soft_limit_bytes,
                    t("access.quotas_bytes"),
                    t,
                  )}{" "}
                  {t("access.quotas_soft")} ·{" "}
                  {formatQuotaValue(
                    limits.restore_30d_hard_limit_bytes,
                    t("access.quotas_bytes"),
                    t,
                  )}{" "}
                  {t("access.quotas_hard")}
                </dd>
              </div>
            </dl>
          </div>

          <div>
            <h3 className="text-sm font-bold text-ink">{t("access.quotas_usage")}</h3>
            <dl className="mt-2 grid gap-2 text-sm">
              <div>
                <dt className="font-bold text-muted">{t("access.quotas_storage")}</dt>
                <dd>
                  {formatQuotaValue(
                    usage.storage_bytes,
                    t("access.quotas_bytes"),
                    t,
                    usage.storage_unknown,
                  )}
                </dd>
              </div>
              <div>
                <dt className="font-bold text-muted">{t("access.quotas_concurrency")}</dt>
                <dd>
                  {formatQuotaValue(usage.concurrency, t("access.quotas_jobs"), t)}
                </dd>
              </div>
              <div>
                <dt className="font-bold text-muted">{t("access.quotas_restore")}</dt>
                <dd>
                  {formatQuotaValue(
                    usage.restore_30d_bytes,
                    t("access.quotas_bytes"),
                    t,
                    usage.restore_request_unknown,
                  )}
                </dd>
              </div>
            </dl>
          </div>

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
          <form className="mt-5 grid gap-3" onSubmit={(event) => void onSave(event)}>
            <h3 className="text-sm font-bold text-ink">{t("access.quotas_title")}</h3>
            {(
              [
                ["storage_soft_limit_bytes", "access.quotas_storage", "soft"],
                ["storage_hard_limit_bytes", "access.quotas_storage", "hard"],
                ["concurrency_soft_limit", "access.quotas_concurrency", "soft"],
                ["concurrency_hard_limit", "access.quotas_concurrency", "hard"],
                ["restore_30d_soft_limit_bytes", "access.quotas_restore", "soft"],
                ["restore_30d_hard_limit_bytes", "access.quotas_restore", "hard"],
              ] as const
            ).map(([name, labelKey, softHard]) => (
              <FormField
                key={name}
                label={`${t(labelKey)} (${t(softHard === "soft" ? "access.quotas_soft" : "access.quotas_hard")})`}
                htmlFor={`quota-${name}`}
              >
                <FormInput
                  id={`quota-${name}`}
                  name={name}
                  inputMode="numeric"
                  value={form[name]}
                  onChange={(event) => setField(name, event.target.value)}
                />
              </FormField>
            ))}
            <FormField label={t("access.quotas_reason")} htmlFor="quota-reason">
              <FormInput
                id="quota-reason"
                name="reason"
                value={form.reason}
                onChange={(event) => setField("reason", event.target.value)}
              />
            </FormField>
            <Button
              type="submit"
              className="min-h-11 w-full sm:w-auto"
              disabled={saving}
            >
              {t("access.quotas_save")}
            </Button>
          </form>
        ) : null}
      </Panel>
    </section>
  );
}
