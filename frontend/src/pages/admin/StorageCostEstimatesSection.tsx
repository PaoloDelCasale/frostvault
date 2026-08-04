import { useCallback, useEffect, useId, useRef, useState } from "react";

import {
  estimateAdminStorageCost,
  fetchAdminVaultQuotas,
  fetchAdminVaults,
  type AdminVault,
  type StorageEstimateResponse,
} from "@/api";
import { FormField, FormSelect } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";
import { formatBytes } from "@/pages/archive/format";

const SCENARIOS = [
  { key: "standard", storageClass: "STANDARD" },
  { key: "glacier", storageClass: "GLACIER" },
  { key: "deep_archive", storageClass: "DEEP_ARCHIVE" },
] as const;

type ScenarioKey = (typeof SCENARIOS)[number]["key"];
type ScenarioEstimate = {
  key: ScenarioKey;
  estimate: StorageEstimateResponse;
};

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

function formatCost(value: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 6,
    }).format(value);
  } catch {
    return `${currency} ${value.toFixed(6)}`;
  }
}

export function StorageCostEstimatesSection() {
  const { t } = useI18n();
  const selectId = useId();
  const requestSequence = useRef(0);
  const translateRef = useRef(t);
  translateRef.current = t;
  const [vaults, setVaults] = useState<AdminVault[]>([]);
  const [selectedVaultId, setSelectedVaultId] = useState<number | null>(null);
  const [sizeBytes, setSizeBytes] = useState(0);
  const [estimates, setEstimates] = useState<ScenarioEstimate[]>([]);
  const [vaultsLoading, setVaultsLoading] = useState(true);
  const [estimatesLoading, setEstimatesLoading] = useState(false);
  const [error, setError] = useState("");

  const loadVaults = useCallback(async () => {
    setVaultsLoading(true);
    setError("");
    try {
      const response = await fetchAdminVaults();
      const items = response.items ?? [];
      setVaults(items);
      setSelectedVaultId((current) => {
        if (current !== null && items.some((vault) => vault.id === current)) {
          return current;
        }
        return items[0]?.id ?? null;
      });
    } catch (failure) {
      setError(
        errorMessage(failure, translateRef.current("admin.storage_estimates_error")),
      );
    } finally {
      setVaultsLoading(false);
    }
  }, []);

  const loadEstimates = useCallback(
    async (vaultId: number) => {
      const sequence = ++requestSequence.current;
      setEstimatesLoading(true);
      setEstimates([]);
      setError("");
      try {
        const quota = await fetchAdminVaultQuotas(vaultId);
        if (quota.usage?.storage_unknown) {
          throw new Error(
            translateRef.current("admin.storage_estimates_size_unknown"),
          );
        }
        const aggregateSize = Number(quota.usage?.storage_bytes ?? 0);
        const results = await Promise.all(
          SCENARIOS.map(async (scenario) => ({
            key: scenario.key,
            estimate: await estimateAdminStorageCost({
              size_bytes: aggregateSize,
              storage_class: scenario.storageClass,
            }),
          })),
        );
        if (sequence !== requestSequence.current) return;
        setSizeBytes(aggregateSize);
        setEstimates(results);
      } catch (failure) {
        if (sequence !== requestSequence.current) return;
        setError(
          errorMessage(failure, translateRef.current("admin.storage_estimates_error")),
        );
      } finally {
        if (sequence === requestSequence.current) setEstimatesLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void loadVaults();
  }, [loadVaults]);

  useEffect(() => {
    if (selectedVaultId !== null) void loadEstimates(selectedVaultId);
  }, [loadEstimates, selectedVaultId]);

  const selectedVault = vaults.find((vault) => vault.id === selectedVaultId);

  return (
    <section aria-labelledby="admin-storage-estimates-heading" className="grid gap-4">
      <div>
        <h2 id="admin-storage-estimates-heading" className="text-xl font-bold">
          {t("admin.storage_estimates_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">
          {t("admin.storage_estimates_subtitle")}
        </p>
      </div>

      {vaultsLoading ? (
        <p role="status" className="text-sm text-muted">
          {t("admin.storage_estimates_loading_vaults")}
        </p>
      ) : error && vaults.length === 0 ? (
        <div className="grid gap-3">
          <p role="alert" className="text-sm font-bold text-[var(--state-local-fg)]">
            {error}
          </p>
          <div>
            <Button type="button" variant="secondary" onClick={() => void loadVaults()}>
              {t("admin.storage_estimates_retry")}
            </Button>
          </div>
        </div>
      ) : vaults.length === 0 ? (
        <p className="text-sm text-muted">{t("admin.storage_estimates_empty")}</p>
      ) : (
        <>
          <Panel className="grid gap-4 p-5">
            <FormField label={t("admin.storage_estimates_vault")} htmlFor={selectId}>
              <FormSelect
                id={selectId}
                value={selectedVaultId ?? ""}
                onChange={(event) => setSelectedVaultId(Number(event.target.value))}
              >
                {vaults.map((vault) => (
                  <option key={vault.id} value={vault.id}>
                    {vault.name}
                  </option>
                ))}
              </FormSelect>
            </FormField>
            {selectedVault && !estimatesLoading && estimates.length > 0 ? (
              <div>
                <p className="text-sm font-bold text-muted">
                  {t("admin.storage_estimates_aggregate_size")}
                </p>
                <p className="text-2xl font-bold" data-testid="aggregate-vault-size">
                  {formatBytes(sizeBytes)}
                </p>
                <p className="mt-1 text-sm text-muted">
                  {t("admin.storage_estimates_aggregate_help", {
                    name: selectedVault.name,
                  })}
                </p>
                {sizeBytes === 0 ? (
                  <p role="status" className="mt-2 text-sm font-bold text-muted">
                    {t("admin.storage_estimates_zero_size")}
                  </p>
                ) : null}
              </div>
            ) : null}
          </Panel>

          {estimatesLoading ? (
            <p role="status" className="text-sm text-muted">
              {t("admin.storage_estimates_loading")}
            </p>
          ) : error ? (
            <div className="grid gap-3">
              <p role="alert" className="text-sm font-bold text-[var(--state-local-fg)]">
                {error}
              </p>
              <div>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={selectedVaultId === null}
                  onClick={() => {
                    if (selectedVaultId !== null) void loadEstimates(selectedVaultId);
                  }}
                >
                  {t("admin.storage_estimates_retry")}
                </Button>
              </div>
            </div>
          ) : (
            <ul className="grid gap-4 lg:grid-cols-3">
              {estimates.map(({ key, estimate }) => (
                <li key={key}>
                  <Panel className="h-full p-5">
                    <article aria-labelledby={`storage-scenario-${key}`}>
                      <h3 id={`storage-scenario-${key}`} className="text-lg font-bold">
                        {t(`admin.storage_estimates_scenario_${key}`)}
                      </h3>
                      <p className="mt-1 text-xs font-extrabold tracking-wide text-green uppercase">
                        {estimate.storage_class}
                      </p>
                      <p className="mt-4 text-sm font-bold text-muted">
                        {t("admin.storage_estimates_monthly_cost")}
                      </p>
                      <p className="text-2xl font-bold">
                        {formatCost(estimate.estimated_cost_eur, estimate.currency)}
                      </p>
                      <div className="mt-5 rounded-[10px] border-2 border-green bg-green-soft p-3">
                        <p className="text-xs font-extrabold tracking-wide text-green uppercase">
                          {t("admin.storage_estimates_price_book")}
                        </p>
                        <p className="mt-1 break-words text-base font-bold">
                          {estimate.price_book_name}
                        </p>
                        <dl className="mt-2 grid gap-2 text-sm">
                          <div>
                            <dt className="font-bold text-muted">
                              {t("admin.storage_estimates_price_book_id")}
                            </dt>
                            <dd>
                              {estimate.price_book_id === null
                                ? t("admin.storage_estimates_builtin_id")
                                : estimate.price_book_id}
                            </dd>
                          </div>
                          <div>
                            <dt className="font-bold text-muted">
                              {t("admin.storage_estimates_effective_at")}
                            </dt>
                            <dd className="break-all font-bold">
                              <time dateTime={estimate.pricing_effective_at}>
                                {estimate.pricing_effective_at}
                              </time>
                            </dd>
                          </div>
                        </dl>
                      </div>
                    </article>
                  </Panel>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
