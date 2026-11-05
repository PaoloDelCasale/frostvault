import { useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchMe, fetchVaults, logout, useCatalogEvents, type MeResponse, type VaultListItem } from "@/api";
import { fetchOverview } from "@/api/vault";
import { useI18n } from "@/i18n";
import { useTheme } from "@/theme";
import { AppShell } from "@/layout/AppShell";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { LoginPage } from "@/pages/login/LoginPage";
import { GeneralPage } from "@/pages/general/GeneralPage";
import { VaultCreatePage } from "@/pages/vault-create";
import { ProblemsPage } from "@/pages/general/ProblemsPage";

export default function App() {
  const { t, locale, setLocale } = useI18n();
  const { setUserId } = useTheme();
  const client = useQueryClient();
  const [me, setMe] = useState<MeResponse | null>(null);
  const [vaults, setVaults] = useState<VaultListItem[]>([]);
  const [error, setError] = useState("");
  const [leaving, setLeaving] = useState(false);
  const pathname = window.location.pathname;
  const params = new URLSearchParams(window.location.search);
  const overview = useQuery({ queryKey: ["general", locale], queryFn: fetchOverview, enabled: Boolean(me) && !leaving, refetchInterval: 30_000 });
  const refresh = useCallback(() => { void client.invalidateQueries({ queryKey: ["general"] }); }, [client]);
  useEffect(() => {
    if (pathname === "/login") return;
    let live = true;
    void Promise.all([fetchMe(), fetchVaults()]).then(([owner, listed]) => {
      if (!live) return;
      setUserId(owner.id); setMe(owner); setVaults(listed.items);
    }).catch((cause: unknown) => { if (live) setError(String(cause)); });
    return () => { live = false; };
  }, [pathname, setUserId]);
  let remembered = 0;
  try { remembered = Number(localStorage.getItem(`frostvault.home.${me?.id}`)); } catch { /* Storage is optional. */ }
  const requested = params.get("vault_id");
  const chosen = requested ? vaults.find((vault) => vault.id === Number(requested))
    : vaults.find((vault) => vault.id === remembered) ?? vaults.find((vault) => vault.id === me?.vault?.id) ?? vaults[0];
  useEffect(() => {
    if (!me || !chosen) return;
    try { localStorage.setItem(`frostvault.home.${me.id}`, String(chosen.id)); } catch { /* Storage is optional. */ }
  }, [me, chosen]);
  useCatalogEvents({ vaultId: me?.vault?.id ?? null, queryClient: client, enabled: Boolean(me?.vault) && !leaving, onSessionChanged: () => window.location.reload() });
  if (pathname === "/login") return <LoginPage />;
  if (!me || leaving) return <p role={error ? "alert" : "status"} className="p-6">{error || t("ui.loading")}</p>;
  const go = (url: string) => window.location.assign(url);
  return <AppShell capabilities={{ vaultName: chosen?.name ?? "FrostVault", currentVaultId: chosen?.id, locale, locales: me.locales, vaults }} queryClient={client} t={t}
    filter={params.get("state") ?? ""} openProblems={overview.data?.open_problem_count}
    problemCounts={Object.fromEntries((overview.data?.vaults ?? []).map((vault) => [vault.id, vault.open_problem_count]))}
    spend={overview.data ? <><p>{t("essential.month_estimate", { month: overview.data.month, cost: overview.data.monthly_estimate.toFixed(2), currency: overview.data.currency })}</p><p>{overview.data.previous_month_delta === null ? t("essential.no_comparison") : t("essential.month_delta", { cost: overview.data.previous_month_delta.toFixed(2), currency: overview.data.currency })}</p><p>{t("essential.not_bill")}</p></> : null}
    handlers={{ onGeneral: () => go("/general"), onNewVault: () => go("/vaults/new"), onProblems: () => go("/problems"),
      onVaultChange: (id) => go(`/?vault_id=${id}`),
      onFilterChange: (state) => go(`/?vault_id=${chosen?.id ?? ""}&state=${state}`),
      onLocaleChange: (next) => { void setLocale(next); },
      onSignOut: () => { void (async () => { setLeaving(true); await client.cancelQueries(); client.clear(); setUserId(null); try { await logout(); go("/login"); } catch (cause) { setError(String(cause)); setLeaving(false); } })(); },
    }}>
    {pathname === "/vaults/new" ? <VaultCreatePage displayName={me.display_name} onNavigate={go} />
      : pathname === "/general" || (!chosen && !requested) ? <GeneralPage overview={overview.data} onRefresh={refresh} />
      : pathname === "/problems" ? <ProblemsPage />
      : chosen ? <FileBrowser key={`${chosen.id}:${locale}`} t={t} vaultId={chosen.id} vaultName={chosen.name} capabilities={{ delete_enabled: true, cloud_deletion_enabled: true }} problemCount={overview.data?.vaults.find((vault) => vault.id === chosen.id)?.open_problem_count ?? 0} onRefresh={refresh} />
      : <p role="alert">{t("essential.vault_unavailable")}</p>}
  </AppShell>;
}
