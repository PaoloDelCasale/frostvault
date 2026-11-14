import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchMe, fetchVaults, logout, useCatalogEvents, type MeResponse, type VaultListItem } from "@/api";
import { fetchOverview } from "@/api/vault";
import { useI18n } from "@/i18n";
import { useTheme } from "@/theme";
import { VaultHeader } from "@/layout/VaultHeader";
import { AppShell } from "@/layout/AppShell";
import type { ShellSection } from "@/layout/types";
import { FileBrowser } from "@/pages/archive/FileBrowser";
import { LoginPage } from "@/pages/login/LoginPage";
import { GeneralPage } from "@/pages/general/GeneralPage";
import { OverviewPage } from "@/pages/overview/OverviewPage";
import { VaultCreatePage } from "@/pages/vault-create";
import { ProblemsPage } from "@/pages/general/ProblemsPage";
import { VaultSettingsPanel } from "@/pages/vault-settings/VaultSettingsPanel";
import { UploadActivity } from "@/components/UploadActivity";
import { interceptLinkClick, legacyRedirect, navigate, parseRoute, useLocation } from "@/router";
import { errorMessage } from "@/lib/errorMessage";

const rememberedKey = (ownerId: number) => `frostvault.home.${ownerId}`;

export default function App() {
  const { t, locale, setLocale } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const { setUserId } = useTheme();
  const client = useQueryClient();
  const [me, setMe] = useState<MeResponse | null>(null);
  const [vaults, setVaults] = useState<VaultListItem[]>([]);
  const [error, setError] = useState("");
  const [leaving, setLeaving] = useState(false);
  const { pathname, search } = useLocation();
  const legacy = legacyRedirect(pathname, search);
  const route = parseRoute(legacy ? new URL(legacy, window.location.origin).pathname : pathname);
  useEffect(() => { if (legacy) navigate(legacy, { replace: true }); }, [legacy]);
  useEffect(() => {
    document.addEventListener("click", interceptLinkClick);
    return () => document.removeEventListener("click", interceptLinkClick);
  }, []);
  const signedIn = route.name !== "login";
  const overview = useQuery({ queryKey: ["general", locale], queryFn: fetchOverview, enabled: Boolean(me) && !leaving, refetchInterval: 30_000 });
  const refresh = useCallback(() => { void client.invalidateQueries({ queryKey: ["general"] }); }, [client]);
  const loadVaults = useCallback(async () => { setVaults((await fetchVaults()).items); }, []);
  useEffect(() => {
    if (!signedIn) return;
    let live = true;
    void Promise.all([fetchMe(), fetchVaults()]).then(([owner, listed]) => {
      if (!live) return;
      setUserId(owner.id); setMe(owner); setVaults(listed.items);
    }).catch((cause: unknown) => { if (live) setError(errorMessage(cause, translateRef.current)); });
    return () => { live = false; };
  }, [signedIn, setUserId]);
  let remembered = 0;
  try { remembered = Number(localStorage.getItem(rememberedKey(me?.id ?? 0))); } catch { /* Storage is optional. */ }
  const requested = route.name === "files" || route.name === "settings" ? route.vaultId : undefined;
  // The routed Vault, else the last one used: the sidebar's File/Settings target.
  const current = requested !== undefined ? vaults.find((vault) => vault.id === requested)
    : vaults.find((vault) => vault.id === remembered) ?? vaults.find((vault) => vault.id === me?.vault?.id) ?? vaults[0];
  useEffect(() => {
    if (!me || !current || requested === undefined) return;
    try { localStorage.setItem(rememberedKey(me.id), String(current.id)); } catch { /* Storage is optional. */ }
  }, [me, current, requested]);
  const refreshDocument = useCallback(() => window.location.reload(), []);
  useCatalogEvents({ vaultId: current?.id ?? null, explicitVault: true, queryClient: client, enabled: Boolean(current) && !leaving, onSessionChanged: refreshDocument });
  if (!signedIn) return <LoginPage />;
  if (!me || leaving) return <p role={error ? "alert" : "status"} className="p-6">{error || t("ui.loading")}</p>;
  const section: ShellSection | undefined = route.name === "files" || route.name === "settings" || route.name === "problems" || route.name === "system" || route.name === "overview" ? route.name : undefined;
  const problemCounts = Object.fromEntries((overview.data?.vaults ?? []).map((vault) => [vault.id, vault.open_problem_count]));
  const unavailable = <p role="alert" className="rounded-card border border-line bg-surface p-4">{t("essential.vault_unavailable")}</p>;
  const preflight = overview.data?.cloud_preflight?.status === "blocked"
    ? <section role="status" className="mb-4 rounded-card border border-[var(--health-warn-border)] bg-amber-soft p-4"><h2 className="font-bold">{t("uploads.preflight_blocked")}</h2><p>{overview.data.cloud_preflight.remediation}</p><p className="text-sm">{t("uploads.preflight_retry")}</p></section>
    : null;
  let page;
  if (route.name === "overview") page = <OverviewPage vaults={vaults} overview={overview.data} />;
  else if (route.name === "new-vault") page = <VaultCreatePage displayName={me.display_name} onNavigate={(url) => { void loadVaults().then(() => navigate(url)); }} embedded />;
  else if (route.name === "system") page = <GeneralPage overview={overview.data} />;
  else if (route.name === "problems") page = <ProblemsPage />;
  else if (route.name === "files") page = current ? <FileBrowser key={`${current.id}:${locale}`} t={t} vaultId={current.id} vaultName={current.name} capabilities={{ delete_enabled: true, cloud_deletion_enabled: true }} problemCount={problemCounts[current.id] ?? 0} onRefresh={refresh} /> : unavailable;
  else if (route.name === "settings") page = current ? <div className="grid min-w-0 gap-4"><VaultHeader vaultId={current.id} vaultName={current.name} active="settings" t={t} />
    <VaultSettingsPanel key={current.id} vaultId={current.id} vaultName={current.name} onStopped={() => { void loadVaults().then(() => navigate("/")); }} /></div> : unavailable;
  else page = <p role="alert" className="rounded-card border border-line bg-surface p-4">{t("nav.not_found")}</p>;
  return <AppShell capabilities={{ currentVaultId: current?.id, locale, locales: me.locales, vaults }} queryClient={client} t={t}
    section={section} openProblems={overview.data?.open_problem_count} problemCounts={problemCounts}
    activity={<UploadActivity t={t} vaultId={current?.id} />} banner={preflight}
    handlers={{
      onNavigate: navigate,
      onLocaleChange: (next) => { void setLocale(next); },
      onSignOut: () => { void (async () => { setLeaving(true); await client.cancelQueries(); client.clear(); setUserId(null); try { await logout(); window.location.assign("/login"); } catch (cause) { setError(errorMessage(cause, t)); setLeaving(false); } })(); },
    }}>
    {page}
  </AppShell>;
}
