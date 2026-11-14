import type { ComponentType, ReactNode } from "react";
import { CircleAlert, FolderOpen, LayoutDashboard, Server, Settings } from "lucide-react";

import { cn } from "@/lib/utils";
import { vaultFilesUrl, vaultSettingsUrl } from "@/router";
import { shellLabel, type ShellTranslator } from "./labels";
import type { ShellSection } from "./types";

function NavLink({ href, icon: Icon, label, active, onNavigate, badge, nested, ariaLabel }: {
  ariaLabel?: string; href: string; icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>; label: string; active: boolean;
  onNavigate?: () => void; badge?: ReactNode; nested?: boolean;
}) {
  return <a href={href} aria-label={ariaLabel} aria-current={active ? "page" : undefined} onClick={() => onNavigate?.()}
    className={cn("flex min-h-11 min-w-0 items-center gap-3 rounded-lg px-3 text-sm font-semibold text-ink no-underline hover:bg-green-soft",
      nested && "pl-5", active && "bg-green-soft text-[var(--state-both-fg)]")}>
    <Icon className={cn("size-4 shrink-0", active ? "text-green" : "text-muted")} aria-hidden={true} />
    <span className="min-w-0 flex-1 truncate">{label}</span>{badge}
  </a>;
}

/**
 * Fixed destinations only: the Vault list lives in the header switcher and
 * the Overview, file filters live in the browser.
 */
export function ShellNavItems({ section, currentVault, openProblems, t, onNavigate }: {
  section?: ShellSection; currentVault?: { id: number; name: string }; openProblems?: number;
  t?: ShellTranslator; onNavigate?: () => void;
}) {
  const problems = openProblems === undefined ? null
    : <span className={cn("shrink-0 rounded-badge px-2 text-xs font-bold tabular-nums", openProblems > 0 ? "bg-destructive text-destructive-foreground" : "bg-[var(--state-unsupported-bg)] text-muted")}
      data-testid="nav-problem-count">{openProblems}</span>;
  return <div className="grid min-w-0 gap-1">
    <NavLink href="/" icon={LayoutDashboard} label={shellLabel(t, "nav.overview", "Overview")} active={section === "overview"} onNavigate={onNavigate} />
    {currentVault && <div className="mt-3 grid gap-1" role="group" aria-label={currentVault.name}>
      <p className="truncate px-3 pb-1 text-xs font-semibold text-muted" title={currentVault.name}>{currentVault.name}</p>
      <NavLink href={vaultFilesUrl(currentVault.id)} icon={FolderOpen} label={shellLabel(t, "nav.files", "Files")} active={section === "files"} onNavigate={onNavigate} />
      <NavLink href={vaultSettingsUrl(currentVault.id)} icon={Settings} label={shellLabel(t, "nav.settings", "Settings")} active={section === "settings"} onNavigate={onNavigate} />
    </div>}
    <div className="mt-3 grid gap-1 border-t border-line pt-3">
      <NavLink href="/problems" icon={CircleAlert} label={shellLabel(t, "nav.problems", "Problems")} active={section === "problems"} onNavigate={onNavigate} badge={problems}
        ariaLabel={openProblems === undefined ? undefined : shellLabel(t, "nav.problems_open", "Problems: {count} open", { count: openProblems })} />
      <NavLink href="/system" icon={Server} label={shellLabel(t, "nav.system", "System")} active={section === "system"} onNavigate={onNavigate} />
    </div>
  </div>;
}
