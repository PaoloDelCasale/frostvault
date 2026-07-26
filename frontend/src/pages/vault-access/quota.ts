import type { QuotaEvaluation, VaultQuotaUpdatePayload } from "@/api";

export type QuotaFormValues = {
  storage_soft_limit_bytes: string;
  storage_hard_limit_bytes: string;
  concurrency_soft_limit: string;
  concurrency_hard_limit: string;
  restore_30d_soft_limit_bytes: string;
  restore_30d_hard_limit_bytes: string;
  reason: string;
};

type Translate = (key: string, params?: Record<string, unknown>) => string;

function readQuotaValue(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value < 0 || !Number.isInteger(value)) {
    throw new Error("Invalid quota value");
  }
  return value;
}

export function buildQuotaPayload(
  form: QuotaFormValues,
  t: Translate,
): { ok: true; payload: VaultQuotaUpdatePayload } | { ok: false; error: string } {
  try {
    const payload: VaultQuotaUpdatePayload = {
      storage_soft_limit_bytes: readQuotaValue(form.storage_soft_limit_bytes),
      storage_hard_limit_bytes: readQuotaValue(form.storage_hard_limit_bytes),
      concurrency_soft_limit: readQuotaValue(form.concurrency_soft_limit),
      concurrency_hard_limit: readQuotaValue(form.concurrency_hard_limit),
      restore_30d_soft_limit_bytes: readQuotaValue(
        form.restore_30d_soft_limit_bytes,
      ),
      restore_30d_hard_limit_bytes: readQuotaValue(
        form.restore_30d_hard_limit_bytes,
      ),
      reason: form.reason.trim(),
    };

    const pairs: Array<[number | null, number | null, string]> = [
      [
        payload.storage_soft_limit_bytes,
        payload.storage_hard_limit_bytes,
        "access.quotas_soft_exceeds_hard_storage",
      ],
      [
        payload.concurrency_soft_limit,
        payload.concurrency_hard_limit,
        "access.quotas_soft_exceeds_hard_concurrency",
      ],
      [
        payload.restore_30d_soft_limit_bytes,
        payload.restore_30d_hard_limit_bytes,
        "access.quotas_soft_exceeds_hard_restore",
      ],
    ];
    for (const [soft, hard, key] of pairs) {
      if (soft !== null && hard !== null && soft > hard) {
        return { ok: false, error: t(key) };
      }
    }
    if (!payload.reason) {
      return { ok: false, error: t("access.quotas_reason_required") };
    }
    return { ok: true, payload };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

export function formatQuotaValue(
  value: number | null | undefined,
  unit: string,
  t: Translate,
  unknown = false,
): string {
  if (unknown) return t("access.quotas_unknown");
  if (value === null || value === undefined) return t("access.quotas_unlimited");
  return `${value} ${unit}`;
}

export type QuotaStateItem = {
  kind: "ok" | "warning" | "block" | "unavailable";
  text: string;
};

export function quotaStateItems(
  evaluation: QuotaEvaluation | undefined,
  t: Translate,
): QuotaStateItem[] {
  if (
    !evaluation ||
    evaluation.state === "unevaluated" ||
    typeof evaluation.allowed !== "boolean" ||
    !Array.isArray(evaluation.decisions)
  ) {
    const text =
      evaluation?.state === "unevaluated"
        ? t("access.quotas_unevaluated")
        : t("access.quotas_unavailable");
    return [{ kind: "unavailable", text }];
  }
  if (!evaluation.decisions.length) {
    if (evaluation.allowed) {
      return [{ kind: "ok", text: t("access.quotas_ok") }];
    }
    return [{ kind: "unavailable", text: t("access.quotas_unavailable") }];
  }
  return evaluation.decisions.map((decision) => {
    const code = decision.code || "quota decision";
    if (decision.severity === "block") {
      return { kind: "block" as const, text: t("access.quotas_block", { code }) };
    }
    return {
      kind: "warning" as const,
      text: t("access.quotas_warning", { code }),
    };
  });
}
