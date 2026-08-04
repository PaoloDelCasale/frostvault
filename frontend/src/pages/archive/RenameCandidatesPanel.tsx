import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  apiQueryKeys,
  confirmFileRename,
  confirmFolderRename,
  renameCandidatesQueryOptions,
  type RenameCandidate,
} from "@/api";
import { Button } from "@/components/ui/button";

import { formatBytes } from "./format";

type Translate = (key: string, params?: Record<string, string | number>) => string;

type FolderCandidate = {
  oldPrefix: string;
  newPrefix: string;
  evidenceCount: number;
};

function parentPath(path: string): string {
  const separator = path.lastIndexOf("/");
  return separator < 0 ? "" : path.slice(0, separator);
}

function baseName(path: string): string {
  const separator = path.lastIndexOf("/");
  return separator < 0 ? path : path.slice(separator + 1);
}

/**
 * A folder suggestion is UI-derived evidence, not persisted state. Requiring at
 * least two same-name descendant pairs avoids presenting a folder action from a
 * single coincidental digest match. The server remains authoritative on use.
 */
function deriveFolderCandidates(
  candidates: RenameCandidate[],
): FolderCandidate[] {
  const groups = new Map<string, FolderCandidate>();
  for (const candidate of candidates) {
    const oldPrefix = parentPath(candidate.missing_path);
    const newPrefix = parentPath(candidate.new_path);
    if (
      !oldPrefix ||
      !newPrefix ||
      oldPrefix === newPrefix ||
      baseName(candidate.missing_path) !== baseName(candidate.new_path)
    ) {
      continue;
    }
    const key = `${oldPrefix}\u0000${newPrefix}`;
    const existing = groups.get(key);
    groups.set(key, {
      oldPrefix,
      newPrefix,
      evidenceCount: (existing?.evidenceCount ?? 0) + 1,
    });
  }
  return [...groups.values()].filter((group) => group.evidenceCount >= 2);
}

function isRenameCandidate(value: unknown): value is RenameCandidate {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<RenameCandidate>;
  return (
    typeof candidate.missing_vault_file_id === "string" &&
    typeof candidate.missing_path === "string" &&
    typeof candidate.new_vault_file_id === "string" &&
    typeof candidate.new_path === "string" &&
    typeof candidate.digest === "string"
  );
}

function candidateKey(candidate: RenameCandidate): string {
  return [
    candidate.missing_vault_file_id,
    candidate.missing_path,
    candidate.new_vault_file_id,
    candidate.new_path,
  ].join("\u0000");
}

export type RenameCandidatesPanelProps = {
  vaultId: number;
  canOperate: boolean;
  t: Translate;
};

export function RenameCandidatesPanel({
  vaultId,
  canOperate,
  t,
}: RenameCandidatesPanelProps) {
  const queryClient = useQueryClient();
  const candidatesQuery = useQuery(renameCandidatesQueryOptions(vaultId));
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ error: boolean; text: string } | null>(null);

  const candidates = useMemo(
    () => (candidatesQuery.data?.items ?? [])
      .filter(isRenameCandidate)
      .filter((candidate) => !dismissed.has(candidateKey(candidate))),
    [candidatesQuery.data?.items, dismissed],
  );
  const folders = useMemo(() => deriveFolderCandidates(candidates), [candidates]);

  async function refreshRelatedState(): Promise<void> {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: apiQueryKeys.renameCandidates(vaultId),
        refetchType: "active",
      }),
      queryClient.invalidateQueries({ queryKey: ["files"], refetchType: "active" }),
      queryClient.invalidateQueries({ queryKey: ["file-history"], refetchType: "active" }),
      queryClient.invalidateQueries({ queryKey: apiQueryKeys.stats, refetchType: "active" }),
      queryClient.invalidateQueries({ queryKey: apiQueryKeys.jobs, refetchType: "active" }),
    ]);
  }

  async function runConfirmation(
    key: string,
    confirm: () => Promise<unknown>,
  ): Promise<void> {
    setPendingKey(key);
    setNotice(null);
    try {
      await confirm();
      setDismissed(new Set());
      setNotice({ error: false, text: t("ui.rename_confirmed") });
    } catch {
      // Identity mutation commits before cloud Job queueing. Always refetch: an
      // error response can still follow a committed rename on partial failure.
      setNotice({ error: true, text: t("ui.rename_confirmation_uncertain") });
    } finally {
      await refreshRelatedState();
      setPendingKey(null);
    }
  }

  function dismiss(candidate: RenameCandidate): void {
    setDismissed((current) => {
      const next = new Set(current);
      next.add(candidateKey(candidate));
      return next;
    });
    setNotice({ error: false, text: t("ui.rename_dismissed_for_now") });
  }

  async function refreshCandidates(): Promise<void> {
    setDismissed(new Set());
    setNotice(null);
    await queryClient.invalidateQueries({
      queryKey: apiQueryKeys.renameCandidates(vaultId),
      refetchType: "active",
    });
  }

  if (candidatesQuery.isLoading) return null;
  if (!candidatesQuery.isError && candidates.length === 0 && dismissed.size === 0) {
    return null;
  }

  return (
    <section
      aria-labelledby="rename-candidates-heading"
      className="mb-4 rounded-xl border border-line bg-canvas p-3 sm:p-4"
      data-testid="rename-candidates"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 id="rename-candidates-heading" className="font-bold text-ink">
            {t("ui.rename_candidates_title")}
          </h2>
          <p className="mt-1 text-sm text-muted">
            {t("ui.rename_candidates_intro")}
          </p>
        </div>
        <Button
          type="button"
          variant="secondary"
          disabled={pendingKey !== null || candidatesQuery.isFetching}
          onClick={() => void refreshCandidates()}
        >
          {t("ui.refresh_rename_candidates")}
        </Button>
      </div>

      {candidatesQuery.isError ? (
        <p role="status" className="mt-3 text-sm text-destructive">
          {t("ui.rename_candidates_error")}
        </p>
      ) : null}

      {notice ? (
        <p
          role={notice.error ? "alert" : "status"}
          aria-live="polite"
          className={`mt-3 text-sm ${notice.error ? "text-destructive" : "text-ink"}`}
        >
          {notice.text}
        </p>
      ) : null}

      {folders.length > 0 ? (
        <div className="mt-4">
          <h3 className="text-sm font-bold text-ink">{t("ui.folder_rename_candidates")}</h3>
          <ul className="mt-2 grid gap-3" aria-label={t("ui.folder_rename_candidates")}>
            {folders.map((folder) => {
              const key = `folder:${folder.oldPrefix}:${folder.newPrefix}`;
              return (
                <li key={key} className="min-w-0 rounded-lg border border-line bg-surface p-3">
                  <p className="text-sm text-ink">
                    {t("ui.folder_rename_evidence", { count: folder.evidenceCount })}
                  </p>
                  <dl className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
                    <div className="min-w-0">
                      <dt className="font-bold text-muted">{t("ui.previous_path")}</dt>
                      <dd className="break-all font-mono text-ink">{folder.oldPrefix}</dd>
                    </div>
                    <div className="min-w-0">
                      <dt className="font-bold text-muted">{t("ui.new_path")}</dt>
                      <dd className="break-all font-mono text-ink">{folder.newPrefix}</dd>
                    </div>
                  </dl>
                  <p className="mt-2 text-sm text-muted">{t("ui.folder_rename_scope")}</p>
                  <Button
                    type="button"
                    className="mt-3 w-full sm:w-auto"
                    disabled={!canOperate || pendingKey !== null}
                    aria-busy={pendingKey === key}
                    onClick={() => void runConfirmation(
                      key,
                      () => confirmFolderRename({
                        old_prefix: folder.oldPrefix,
                        new_prefix: folder.newPrefix,
                      }),
                    )}
                  >
                    {pendingKey === key
                      ? t("ui.confirming_rename")
                      : t("ui.confirm_folder_rename")}
                  </Button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {candidates.length > 0 ? (
        <ul className="mt-4 grid gap-3" aria-label={t("ui.file_rename_candidates")}>
          {candidates.map((candidate) => {
            const key = candidateKey(candidate);
            return (
              <li key={key} className="min-w-0 rounded-lg border border-line bg-surface p-3">
                <p className="font-bold text-ink">
                  {t("ui.rename_candidate_statement")}
                </p>
                <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                  <div className="min-w-0">
                    <dt className="font-bold text-muted">{t("ui.previous_path")}</dt>
                    <dd className="break-all font-mono text-ink">{candidate.missing_path}</dd>
                  </div>
                  <div className="min-w-0">
                    <dt className="font-bold text-muted">{t("ui.new_path")}</dt>
                    <dd className="break-all font-mono text-ink">{candidate.new_path}</dd>
                  </div>
                  <div className="min-w-0">
                    <dt className="font-bold text-muted">{t("ui.fingerprint_evidence")}</dt>
                    <dd className="break-all font-mono text-ink">{candidate.digest}</dd>
                  </div>
                  <div className="min-w-0">
                    <dt className="font-bold text-muted">{t("ui.size_evidence")}</dt>
                    <dd className="text-ink">
                      {candidate.size == null
                        ? t("ui.size_evidence_unavailable")
                        : formatBytes(candidate.size)}
                    </dd>
                  </div>
                </dl>
                <p className="mt-3 text-sm text-muted">
                  {t("ui.rename_confirm_consequence")}
                </p>
                <p className="mt-1 text-sm text-muted">
                  {t("ui.rename_reject_consequence")}
                </p>
                {!canOperate ? (
                  <p className="mt-2 text-sm font-bold text-muted">
                    {t("ui.rename_read_only")}
                  </p>
                ) : null}
                <div className="mt-3 grid gap-2 sm:flex sm:flex-wrap">
                  <Button
                    type="button"
                    className="w-full sm:w-auto"
                    disabled={!canOperate || pendingKey !== null}
                    aria-busy={pendingKey === key}
                    onClick={() => void runConfirmation(
                      key,
                      () => confirmFileRename({
                        vault_file_id: candidate.missing_vault_file_id,
                        new_path: candidate.new_path,
                      }),
                    )}
                  >
                    {pendingKey === key
                      ? t("ui.confirming_rename")
                      : t("ui.confirm_file_rename")}
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    className="w-full sm:w-auto"
                    disabled={pendingKey !== null}
                    onClick={() => dismiss(candidate)}
                  >
                    {t("ui.dismiss_rename_candidate")}
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}

      {dismissed.size > 0 ? (
        <p className="mt-3 text-sm text-muted">
          {t("ui.rename_dismissal_not_persisted")}
        </p>
      ) : null}
    </section>
  );
}
