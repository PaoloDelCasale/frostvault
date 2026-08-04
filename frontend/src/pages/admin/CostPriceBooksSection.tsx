import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";

import {
  activateAdminCostPriceBook,
  createAdminCostPriceBook,
  fetchActiveAdminCostPriceBook,
  fetchAdminCostPriceBooks,
  type CostPriceBook,
} from "@/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FormField, FormInput } from "@/components/FormField";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/useI18n";

const TEXTAREA_CLASS =
  "min-h-36 w-full resize-y rounded-[10px] border border-input bg-surface px-3.5 py-3 font-mono text-xs leading-relaxed text-ink";

type JsonEditorProps = {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  invalid: boolean;
  help: string;
};

function JsonEditor({
  id,
  label,
  value,
  onChange,
  invalid,
  help,
}: JsonEditorProps) {
  return (
    <FormField label={label} htmlFor={id} help={help}>
      <textarea
        id={id}
        className={TEXTAREA_CLASS}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        aria-invalid={invalid || undefined}
      />
    </FormField>
  );
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseObject(value: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(value);
    return isObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function parseNumberMap(value: string): Record<string, number> | null {
  const parsed = parseObject(value);
  if (!parsed) return null;
  const result: Record<string, number> = {};
  for (const [key, item] of Object.entries(parsed)) {
    if (typeof item !== "number" || !Number.isFinite(item) || item < 0) {
      return null;
    }
    result[key] = item;
  }
  return result;
}

function parseRestoreRates(
  value: string,
): Record<string, Record<string, number>> | null {
  const parsed = parseObject(value);
  if (!parsed) return null;
  const result: Record<string, Record<string, number>> = {};
  for (const [storageClass, tiers] of Object.entries(parsed)) {
    if (!isObject(tiers)) return null;
    const rates: Record<string, number> = {};
    for (const [tier, rate] of Object.entries(tiers)) {
      if (typeof rate !== "number" || !Number.isFinite(rate) || rate < 0) {
        return null;
      }
      rates[tier] = rate;
    }
    result[storageClass] = rates;
  }
  return result;
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "{}";
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}

function mergeBooks(items: CostPriceBook[], active: CostPriceBook): CostPriceBook[] {
  const persisted = items.map((book) => ({
    ...book,
    is_active: active.id !== null && book.id === active.id,
  }));
  if (active.id === null || !persisted.some((book) => book.id === active.id)) {
    return [active, ...persisted];
  }
  return persisted;
}

function PriceBookCard({
  book,
  onActivate,
}: {
  book: CostPriceBook;
  onActivate: (book: CostPriceBook) => void;
}) {
  const { t } = useI18n();
  const headingId = `price-book-${book.id ?? "builtin"}`;
  const active = book.is_active;

  return (
    <li>
      <article
        aria-labelledby={headingId}
        data-testid={active ? "active-price-book" : "price-book"}
        className={`rounded-panel border-2 p-5 ${
          active
            ? "border-green bg-green-soft"
            : "border-line bg-surface"
        }`}
      >
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 id={headingId} className="break-words text-lg font-bold">
              {book.name}
            </h3>
            <p className="mt-1 text-sm text-muted">
              {active
                ? t("admin.price_books_active")
                : t("admin.price_books_inactive")}
            </p>
          </div>
          {active ? (
            <span className="rounded-badge bg-green px-3 py-1.5 text-xs font-extrabold text-white">
              {t("admin.price_books_active_badge")}
            </span>
          ) : null}
        </header>

        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="font-bold text-muted">{t("admin.price_books_currency")}</dt>
            <dd>{book.currency}</dd>
          </div>
          <div>
            <dt className="font-bold text-muted">{t("admin.price_books_effective_at")}</dt>
            <dd className="break-all">{book.effective_at}</dd>
          </div>
          <div>
            <dt className="font-bold text-muted">{t("admin.price_books_last_changed")}</dt>
            <dd>
              {book.updated_at ? (
                <time dateTime={book.updated_at}>{book.updated_at}</time>
              ) : (
                t("admin.price_books_last_changed_unavailable")
              )}
            </dd>
          </div>
        </dl>

        <div className="mt-5 grid gap-4">
          <section aria-labelledby={`${headingId}-assumptions`}>
            <h4 id={`${headingId}-assumptions`} className="font-bold">
              {t("admin.price_books_assumptions")}
            </h4>
            <pre className="mt-2 max-h-64 overflow-auto rounded-[10px] bg-canvas p-3 text-xs leading-relaxed whitespace-pre-wrap break-words">
              {formatJson(book.assumptions)}
            </pre>
          </section>
          <section aria-labelledby={`${headingId}-storage-rates`}>
            <h4 id={`${headingId}-storage-rates`} className="font-bold">
              {t("admin.price_books_storage_rates")}
            </h4>
            <pre className="mt-2 max-h-64 overflow-auto rounded-[10px] bg-canvas p-3 text-xs leading-relaxed whitespace-pre-wrap break-words">
              {formatJson(book.storage_rates)}
            </pre>
          </section>
          <section aria-labelledby={`${headingId}-restore-rates`}>
            <h4 id={`${headingId}-restore-rates`} className="font-bold">
              {t("admin.price_books_restore_rates")}
            </h4>
            <pre className="mt-2 max-h-64 overflow-auto rounded-[10px] bg-canvas p-3 text-xs leading-relaxed whitespace-pre-wrap break-words">
              {formatJson(book.restore_rates)}
            </pre>
          </section>
        </div>

        {book.id !== null && !active ? (
          <div className="mt-5 flex justify-end">
            <Button
              type="button"
              variant="secondary"
              onClick={() => onActivate(book)}
              aria-label={t("admin.price_books_activate_label", {
                name: book.name,
              })}
            >
              {t("admin.price_books_activate")}
            </Button>
          </div>
        ) : null}
      </article>
    </li>
  );
}

export function CostPriceBooksSection() {
  const { t } = useI18n();
  const id = useId();
  const formPrefilled = useRef(false);
  const [books, setBooks] = useState<CostPriceBook[]>([]);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [activationTarget, setActivationTarget] = useState<CostPriceBook | null>(null);
  const [activationReason, setActivationReason] = useState("");

  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("");
  const [effectiveAt, setEffectiveAt] = useState("");
  const [assumptions, setAssumptions] = useState("{}");
  const [storageRates, setStorageRates] = useState("{}");
  const [restoreRates, setRestoreRates] = useState("{}");
  const [reason, setReason] = useState("");
  const [formError, setFormError] = useState("");

  const loadBooks = useCallback(async (prefill = false) => {
    setLoading(true);
    setPageError("");
    try {
      const [listed, active] = await Promise.all([
        fetchAdminCostPriceBooks(),
        fetchActiveAdminCostPriceBook(),
      ]);
      setBooks(mergeBooks(listed.items ?? [], active));
      if (prefill && !formPrefilled.current) {
        setName(active.name);
        setCurrency(active.currency);
        setEffectiveAt(active.effective_at);
        setAssumptions(formatJson(active.assumptions));
        setStorageRates(formatJson(active.storage_rates));
        setRestoreRates(formatJson(active.restore_rates));
        formPrefilled.current = true;
      }
    } catch (failure) {
      setPageError(errorMessage(failure));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadBooks(true);
  }, [loadBooks]);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError("");
    setNotice("");

    if (name.trim().length === 0) {
      setFormError(t("admin.price_books_name_required"));
      return;
    }
    if (reason.trim().length < 3) {
      setFormError(t("admin.price_books_reason_required"));
      return;
    }
    const parsedAssumptions = parseObject(assumptions);
    const parsedStorageRates = parseNumberMap(storageRates);
    const parsedRestoreRates = parseRestoreRates(restoreRates);
    if (!parsedAssumptions || !parsedStorageRates || !parsedRestoreRates) {
      setFormError(t("admin.price_books_json_invalid"));
      return;
    }

    setBusy(true);
    try {
      await createAdminCostPriceBook({
        name: name.trim(),
        currency: currency.trim(),
        effective_at: effectiveAt.trim(),
        assumptions: parsedAssumptions,
        storage_rates: parsedStorageRates,
        restore_rates: parsedRestoreRates,
        reason: reason.trim(),
      });
      setReason("");
      setNotice(t("admin.price_books_created"));
      await loadBooks();
    } catch (failure) {
      setPageError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  async function handleActivate() {
    if (!activationTarget || activationTarget.id === null) return;
    if (activationReason.trim().length < 3) return;
    setBusy(true);
    setPageError("");
    try {
      await activateAdminCostPriceBook(activationTarget.id, {
        reason: activationReason.trim(),
      });
      setActivationTarget(null);
      setActivationReason("");
      setNotice(t("admin.price_books_activated"));
      await loadBooks();
    } catch (failure) {
      setPageError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="admin-price-books-heading" className="grid gap-4">
      <div>
        <h2 id="admin-price-books-heading" className="text-xl font-bold">
          {t("admin.price_books_heading")}
        </h2>
        <p className="mt-1 text-sm text-muted">{t("admin.price_books_subtitle")}</p>
      </div>

      {pageError ? (
        <p role="alert" className="break-words text-sm font-bold text-[var(--state-local-fg)]">
          {pageError}
        </p>
      ) : null}
      {notice ? (
        <p role="status" className="text-sm font-bold text-green">
          {notice}
        </p>
      ) : null}

      <Panel className="p-5">
        <h3 className="text-lg font-bold">{t("admin.price_books_create_heading")}</h3>
        <p className="mt-1 text-sm text-muted">{t("admin.price_books_create_help")}</p>
        <form className="mt-4 grid gap-4" onSubmit={(event) => void handleCreate(event)}>
          <div className="grid gap-4 sm:grid-cols-2">
            <FormField label={t("admin.price_books_name")} htmlFor={`${id}-name`}>
              <FormInput
                id={`${id}-name`}
                required
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </FormField>
            <FormField label={t("admin.price_books_currency")} htmlFor={`${id}-currency`}>
              <FormInput
                id={`${id}-currency`}
                required
                minLength={3}
                maxLength={8}
                value={currency}
                onChange={(event) => setCurrency(event.target.value)}
              />
            </FormField>
            <FormField
              label={t("admin.price_books_effective_at")}
              htmlFor={`${id}-effective-at`}
              help={t("admin.price_books_effective_at_help")}
            >
              <FormInput
                id={`${id}-effective-at`}
                required
                minLength={10}
                maxLength={64}
                value={effectiveAt}
                onChange={(event) => setEffectiveAt(event.target.value)}
              />
            </FormField>
            <FormField
              label={t("admin.price_books_reason")}
              htmlFor={`${id}-reason`}
              help={t("admin.price_books_reason_help")}
            >
              <FormInput
                id={`${id}-reason`}
                required
                minLength={3}
                maxLength={500}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </FormField>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            <JsonEditor
              id={`${id}-assumptions`}
              label={t("admin.price_books_assumptions")}
              value={assumptions}
              onChange={setAssumptions}
              invalid={Boolean(formError)}
              help={t("admin.price_books_assumptions_help")}
            />
            <JsonEditor
              id={`${id}-storage-rates`}
              label={t("admin.price_books_storage_rates")}
              value={storageRates}
              onChange={setStorageRates}
              invalid={Boolean(formError)}
              help={t("admin.price_books_rates_help")}
            />
            <JsonEditor
              id={`${id}-restore-rates`}
              label={t("admin.price_books_restore_rates")}
              value={restoreRates}
              onChange={setRestoreRates}
              invalid={Boolean(formError)}
              help={t("admin.price_books_rates_help")}
            />
          </div>
          {formError ? (
            <p role="alert" className="text-sm font-bold text-[var(--state-local-fg)]">
              {formError}
            </p>
          ) : null}
          <div className="flex flex-wrap justify-end">
            <Button type="submit" variant="primary" disabled={busy || loading}>
              {t("admin.price_books_create")}
            </Button>
          </div>
        </form>
      </Panel>

      <section aria-labelledby="admin-price-books-list-heading">
        <h3 id="admin-price-books-list-heading" className="mb-3 text-lg font-bold">
          {t("admin.price_books_list_heading")}
        </h3>
        {loading ? (
          <p className="text-sm text-muted">{t("admin.price_books_loading")}</p>
        ) : books.length === 0 ? (
          <p className="text-sm text-muted">{t("admin.price_books_empty")}</p>
        ) : (
          <ul className="grid gap-4">
            {books.map((book) => (
              <PriceBookCard key={book.id ?? "builtin"} book={book} onActivate={setActivationTarget} />
            ))}
          </ul>
        )}
      </section>

      <ConfirmDialog
        open={activationTarget !== null}
        onOpenChange={(open) => {
          if (!open && !busy) {
            setActivationTarget(null);
            setActivationReason("");
          }
        }}
        title={t("admin.price_books_activation_title")}
        description={t("admin.price_books_activation_help")}
        confirmLabel={t("admin.price_books_activation_confirm")}
        cancelLabel={t("admin.cancel")}
        confirmDisabled={busy || activationReason.trim().length < 3}
        keepOpenOnConfirm
        onConfirm={() => void handleActivate()}
      >
        <FormField
          label={t("admin.price_books_activation_reason")}
          htmlFor={`${id}-activation-reason`}
          help={t("admin.price_books_reason_help")}
        >
          <FormInput
            id={`${id}-activation-reason`}
            required
            minLength={3}
            maxLength={500}
            value={activationReason}
            onChange={(event) => setActivationReason(event.target.value)}
          />
        </FormField>
      </ConfirmDialog>
    </section>
  );
}
