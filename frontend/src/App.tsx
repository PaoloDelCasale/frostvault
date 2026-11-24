import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { readOnMount, apiQueryKeys, fetchMe, fetchOverview, fetchVaults, logout, useCatalogEvents, type MeResponse, type VaultListItem } from "@/api";
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

type Session = { owner: MeResponse; vaults: VaultListItem[] };
const NO_VAULTS: VaultListItem[] = [];
const rememberedKey = (ownerId: number) => `frostvault.home.${ownerId}`;

export default function App() {
  const { t, locale, setLocale } = useI18n();
  const translateRef = useRef(t);
  translateRef.current = t;
  const { setUserId } = useTheme();
  const client = useQueryClient();
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
  // Read once per sign-in: nothing but `loadVaults` refreshes it, not even a
  // blanket invalidation of the cache.
  const session = useQuery({
    queryKey: apiQueryKeys.session,
    queryFn: async (): Promise<Session> => {
      const [owner, listed] = await Promise.all([fetchMe(), fetchVaults()]);
      return { owner, vaults: listed.items };
    },
    enabled: signedIn && !leaving,
    ...readOnMount,
    staleTime: "static",
    gcTime: Infinity,
  });
  const me = session.data?.owner ?? null;
  const vaults = session.data?.vaults ?? NO_VAULTS;
  const overview = useQuery({ queryKey: apiQueryKeys.overview(locale), queryFn: fetchOverview, enabled: Boolean(me) && !leaving, refetchInterval: 30_000 });
  const refresh = useCallback(() => { void client.invalidateQueries({ queryKey: apiQueryKeys.general }); }, [client]);
  const loadVaults = useCallback(async () => {
    const listed = (await fetchVaults()).items;
    client.setQueryData<Session>(apiQueryKeys.session, (session) => session && { ...session, vaults: listed });
  }, [client]);
  useEffect(() => { if (me) setUserId(me.id); }, [me, setUserId]);
  useEffect(() => { if (session.error) setError(errorMessage(session.error, translateRef.current)); }, [session.error]);
  let remembered = 0;
  try { remembered = Number(localStorage.getItem(rememberedKey(me?.id ?? 0))); } catch { /* Storage is optional. */ }
  const requested = route.name === "files" || route.name === "settings" ? route.vaultId : undefined;
  // The routed Vault, else the one remembered in this browser, else the first listed:
  // the sidebar's File/Settings target. The Session holds no Vault (#805).
  const current = requested !== undefined ? vaults.find((vault) => vault.id === requested)
    : vaults.find((vault) => vault.id === remembered) ?? vaults[0];
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
  else if (route.name === "problems") page = <ProblemsPage vaults={vaults} />;
  else if (route.name === "files") page = current ? <FileBrowser key={`${current.id}:${locale}`} t={t} vaultId={current.id} vaultName={current.name} capabilities={{ delete_enabled: true, cloud_deletion_enabled: true }} problemCount={problemCounts[current.id] ?? 0} onRefresh={refresh} /> : unavailable;
  else if (route.name === "settings") page = current ? <div className="grid min-w-0 gap-4"><VaultHeader vaultId={current.id} vaultName={current.name} active="settings" t={t} />
    <VaultSettingsPanel key={current.id} vaultId={current.id} vaultName={current.name} onStopped={() => { void loadVaults().then(() => navigate("/")); }} /></div> : unavailable;
  else page = <p role="alert" className="rounded-card border border-line bg-surface p-4">{t("nav.not_found")}</p>;
  return <AppShell capabilities={{ currentVaultId: current?.id, allVaults: requested === undefined, locale, locales: me.locales, vaults }} queryClient={client} t={t}
    section={section} openProblems={overview.data?.open_problem_count} problemCounts={problemCounts}
    activity={<UploadActivity t={t} vaultId={current?.id} />} banner={preflight}
    handlers={{
      onNavigate: navigate,
      onLocaleChange: (next) => { void setLocale(next); },
      onSignOut: () => { void (async () => {
        // Sign-out empties the cache; if it fails the Owner is still signed in, so their session is put back, not read again.
        const kept = client.getQueryData<Session>(apiQueryKeys.session);
        setLeaving(true); await client.cancelQueries(); client.clear(); setUserId(null);
        try { await logout(); window.location.assign("/login"); }
        catch (cause) { client.setQueryData(apiQueryKeys.session, kept); setError(errorMessage(cause, t)); setLeaving(false); }
      })(); },
    }}>
    {page}
  </AppShell>;
}
