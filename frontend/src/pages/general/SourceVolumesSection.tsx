import { useEffect, useState } from "react";

import {
  fetchGeneralSourceVolumes,
  type SourceVolumeInventoryItem,
} from "@/api";
import { Panel } from "@/components/Panel";
import { useI18n } from "@/i18n/useI18n";

export function SourceVolumesSection() {
  const { t } = useI18n();
  const [items, setItems] = useState<SourceVolumeInventoryItem[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    void fetchGeneralSourceVolumes()
      .then((volumes) => {
        if (!cancelled) setItems(volumes.items);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!items) {
    return error ? (
      <p role="alert">{error}</p>
    ) : (
      <p className="text-sm text-muted">{t("general.sources_loading")}</p>
    );
  }

  return (
    <section aria-labelledby="general-sources-heading" className="grid gap-4">
      <div>
        <h2 id="general-sources-heading" className="text-xl font-bold">
          {t("general.sources_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">{t("general.sources_subtitle")}</p>
      </div>
      {items.length === 0 ? (
        <Panel className="p-5">
          <p className="text-sm text-muted">{t("general.sources_empty")}</p>
          <p className="mt-2 text-sm">{t("general.sources_nested_mount_help")}</p>
        </Panel>
      ) : (
        <ul className="grid gap-3" role="list">
          {items.map((item) => (
            <li key={item.alias}>
              <Panel className="p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-lg font-bold">/sources/{item.alias}</h3>
                    <p className="mt-1 break-all text-sm text-muted">{item.path}</p>
                  </div>
                  <span className="rounded-[10px] bg-canvas px-3 py-1 text-xs font-bold uppercase">
                    {t(`general.sources_health_${item.health}`)}
                  </span>
                </div>
                <dl className="mt-4 grid gap-2 text-sm sm:grid-cols-2">
                  <div>
                    <dt className="font-bold text-muted">
                      {t("general.sources_access")}
                    </dt>
                    <dd>{item.access}</dd>
                  </div>
                  <div>
                    <dt className="font-bold text-muted">
                      {t("general.sources_vault_count")}
                    </dt>
                    <dd>{item.vault_count}</dd>
                  </div>
                </dl>
                {item.diagnostic || item.health !== "ok" ? (
                  <p className="mt-3 text-sm" role="status">
                    {item.diagnostic ||
                      t(`general.sources_diagnostic_${item.health}`)}
                  </p>
                ) : null}
              </Panel>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
