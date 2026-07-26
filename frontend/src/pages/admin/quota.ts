import type {
  QuotaDecision,
  QuotaEvaluation,
  QuotaLimits,
  VaultQuotaUpdatePayload,
} from "@/api";

export const QUOTA_INPUT_NAMES = [
  "storage_soft_limit_bytes",
  "storage_hard_limit_bytes",
  "concurrency_soft_limit",
  "concurrency_hard_limit",
  "restore_30d_soft_limit_bytes",
  "restore_30d_hard_limit_bytes",
] as const;

export type QuotaInputName = (typeof QUOTA_INPUT_NAMES)[number];

export type QuotaFormValues = Record<QuotaInputName, string> & {
  reason: string;
};

export function emptyQuotaFormValues(): QuotaFormValues {
  return {
    storage_soft_limit_bytes: "",
    storage_hard_limit_bytes: "",
    concurrency_soft_limit: "",
    concurrency_hard_limit: "",
    restore_30d_soft_limit_bytes: "",
    restore_30d_hard_limit_bytes: "",
    reason: "",
  };
}

export function limitsToFormValues(
  limits: Partial<QuotaLimits> | undefined,
): QuotaFormValues {
  const values = emptyQuotaFormValues();
  for (const name of QUOTA_INPUT_NAMES) {
    const value = limits?.[name];
    values[name] =
      value === null || value === undefined ? "" : String(value);
  }
  return values;
}

export function readQuotaValue(raw: string, name: string): number | null {
  const trimmed = String(raw ?? "").trim();
  if (!trimmed) return null;
  if (!/^\d+$/.test(trimmed)) {
    throw new Error(`${name} must be a nonnegative integer.`);
  }
  const value = Number(trimmed);
  if (!Number.isSafeInteger(value)) {
    throw new Error(`${name} is too large.`);
  }
  return value;
}

export type QuotaBuildResult =
  | { ok: true; payload: VaultQuotaUpdatePayload }
  | { ok: false; error: string };

/**
 * Client-side quota validation for admin quota forms.
 * Blank fields become null (unlimited). Soft cannot exceed hard.
 */
export function buildQuotaUpdatePayload(
  form: QuotaFormValues,
): QuotaBuildResult {
  try {
    const payload: VaultQuotaUpdatePayload = {
      storage_soft_limit_bytes: null,
      storage_hard_limit_bytes: null,
      concurrency_soft_limit: null,
      concurrency_hard_limit: null,
      restore_30d_soft_limit_bytes: null,
      restore_30d_hard_limit_bytes: null,
      reason: "",
    };
    for (const name of QUOTA_INPUT_NAMES) {
      payload[name] = readQuotaValue(form[name], name);
    }
    const pairs: Array<[QuotaInputName, QuotaInputName, string]> = [
      ["storage_soft_limit_bytes", "storage_hard_limit_bytes", "storage"],
      ["concurrency_soft_limit", "concurrency_hard_limit", "concurrency"],
      [
        "restore_30d_soft_limit_bytes",
        "restore_30d_hard_limit_bytes",
        "restore 30-day",
      ],
    ];
    for (const [softName, hardName, label] of pairs) {
      const soft = payload[softName];
      const hard = payload[hardName];
      if (soft !== null && hard !== null && soft > hard) {
        throw new Error(
          `Soft ${label} limit cannot exceed the hard limit.`,
        );
      }
    }
    payload.reason = String(form.reason || "").trim();
    if (!payload.reason) {
      throw new Error("Enter a reason for this quota change.");
    }
    return { ok: true, payload };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

export type QuotaStatusKind = "ok" | "warning" | "block" | "unavailable";

export type QuotaStatusItem = {
  kind: QuotaStatusKind;
  label: string;
};

export function quotaStatusItems(
  evaluation: QuotaEvaluation | undefined,
): QuotaStatusItem[] {
  if (
    !evaluation ||
    evaluation.state === "unevaluated" ||
    typeof evaluation.allowed !== "boolean" ||
    !Array.isArray(evaluation.decisions)
  ) {
    return [
      {
        kind: "unavailable",
        label:
          evaluation?.state === "unevaluated"
            ? "Quota state not evaluated."
            : "Quota state unavailable.",
      },
    ];
  }
  if (!evaluation.decisions.length) {
    return evaluation.allowed
      ? [
          {
            kind: "ok",
            label: "No active warnings or blocks reported.",
          },
        ]
      : [{ kind: "unavailable", label: "Quota state unavailable." }];
  }
  return evaluation.decisions.map((decision: QuotaDecision) => {
    const severity = decision.severity === "block" ? "Block" : "Warning";
    const kind: QuotaStatusKind =
      decision.severity === "block" ? "block" : "warning";
    return {
      kind,
      label: `${severity}: ${decision.code || "quota decision"}`,
    };
  });
}

export function formatQuotaValue(
  value: number | null | undefined,
  unit: string,
): string {
  return value === null || value === undefined
    ? "Unlimited"
    : `${value} ${unit}`;
}
