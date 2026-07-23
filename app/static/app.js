const i18nCatalog = (() => {
  const node = typeof document !== "undefined"
    ? document.getElementById("i18n-catalog")
    : null;
  if (!node) return {};
  try {
    return JSON.parse(node.textContent || "{}");
  } catch (_) {
    return {};
  }
})();

function t(key, params = {}) {
  let message = i18nCatalog[key];
  if (message === undefined || message === null) {
    message = key;
  }
  return String(message).replace(/\{(\w+)\}/g, (_, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name])
      : `{${name}}`
  ));
}

function stateLabel(state) {
  const key = `state.${state}`;
  const label = t(key);
  return label === key ? state : label;
}

function storageClassLabel(value) {
  const key = `storage.${value}`;
  const label = t(key);
  return label === key ? value : label;
}

function actionLabel(action) {
  const key = `action.${action}`;
  const label = t(key);
  return label === key ? action : label;
}

function operationStatusLabel(operation) {
  if (operation.status === "completed") {
    if (operation.action === "upload") return t("operation.upload_verified");
    if (operation.action === "rename") return t("operation.rename_completed");
    return t("operation.completed");
  }
  const statusKey = `operation.${operation.status}`;
  const statusLabel = t(statusKey);
  if (statusLabel !== statusKey) {
    return statusLabel;
  }
  return actionLabel(operation.action) || t("operation.generic");
}

function formatFileHistory(history) {
  const paths = (history.path_history || []).map((entry) => entry.path);
  const versions = history.versions || [];
  const keys = versions.map((version) => version.object_key).filter(Boolean);
  const pathLabel = paths.length
    ? paths.join(" → ")
    : (history.path || "");
  const keyLabel = keys.length
    ? keys.slice().reverse().join(" → ")
    : "No cloud versions";
  return {
    pathLabel,
    keyLabel,
    summary: `${pathLabel} · ${versions.length} version${versions.length === 1 ? "" : "s"} across keys: ${keyLabel}`,
  };
}

let currentPage = 1;
let total = 0;
let currentDirectory = new URLSearchParams(window.location.search).get("directory") || "";
let jobGroups = [];
let searchTimer;
let refreshTimer;
let refreshing = false;
let lastShownError = "";
let noticeTimer;
let noticeHideTimer;
const pageSize = 100;
const seenFailuresStorageKey = "archive_seen_failed_operations";
const archiveMain = document.querySelector("main");
const canOperate = archiveMain.dataset.canOperate === "true";
const localDeleteEnabled = archiveMain.dataset.deleteEnabled === "true";
const cloudDeletionEnabled = archiveMain.dataset.cloudDeletionEnabled === "true";
const isVaultOwner = archiveMain.dataset.isVaultOwner === "true"
  || archiveMain.dataset.role === "owner";
const vaultName = archiveMain.dataset.vaultName || "";
const csrfCookieName = "frostvault_csrf";
const mutatingMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);
let authMethod = null;

function readCookie(name) {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const cookie = part.trim();
    if (cookie.startsWith(prefix)) {
      return decodeURIComponent(cookie.slice(prefix.length));
    }
  }
  return "";
}

function formatBytes(value) {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = Number(value), i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[character]));
}

function renderStorageClass(file) {
  if (!file.cloud_exists && file.type !== "directory") return "—";
  if (file.type === "directory" && !file.storage_class) {
    return file.storage_class_count > 1
      ? `<span class="storage-summary">${file.storage_class_count} cloud classes</span>`
      : "—";
  }
  const storageClass = file.storage_class || "STANDARD";
  const label = storageClassLabel(storageClass) || storageClass.replaceAll("_", " ");
  const kind = storageClass === "DEEP_ARCHIVE"
    ? "deep-archive"
    : storageClass.includes("GLACIER") ? "glacier" : "standard";
  const icon = storageClass === "DEEP_ARCHIVE" ? '<span aria-hidden="true">❄</span>' : "";
  return `<span class="storage-badge ${kind}" title="S3 class: ${escapeHtml(storageClass)}">${icon}${escapeHtml(label)}</span>`;
}

function renderBreadcrumbs() {
  const parts = currentDirectory ? currentDirectory.split("/") : [];
  const crumbs = [{name: t("ui.breadcrumb_archive"), path: ""}];
  let path = "";
  parts.forEach(name => {
    path = path ? `${path}/${name}` : name;
    crumbs.push({name, path});
  });
  document.querySelector("#breadcrumbs").innerHTML = crumbs.map((crumb, index) => {
    const current = index === crumbs.length - 1;
    return `${index ? '<span class="breadcrumb-separator">/</span>' : ''}
      <button type="button" data-directory="${escapeHtml(crumb.path)}" ${current ? 'aria-current="page" disabled' : ''}>${escapeHtml(crumb.name)}</button>`;
  }).join("");
  document.querySelector("#up-directory").disabled = !currentDirectory;
}

function openDirectory(path, addHistory = true) {
  currentDirectory = path;
  currentPage = 1;
  document.querySelector("#search").value = "";
  if (addHistory) {
    const url = new URL(window.location.href);
    if (path) url.searchParams.set("directory", path);
    else url.searchParams.delete("directory");
    window.history.pushState({directory: path}, "", url);
  }
  renderBreadcrumbs();
  refresh();
}

function notify(message, error = false) {
  const node = document.querySelector("#notice");
  window.clearTimeout(noticeTimer);
  window.clearTimeout(noticeHideTimer);
  node.querySelector(".notice-message").textContent = message;
  node.classList.toggle("error", error);
  node.setAttribute("role", error ? "alert" : "status");
  node.classList.remove("hidden", "is-visible", "is-leaving");
  void node.offsetWidth;
  node.classList.add("is-visible");
  noticeTimer = window.setTimeout(dismissNotice, 4200);
}

function dismissNotice() {
  const node = document.querySelector("#notice");
  window.clearTimeout(noticeTimer);
  node.classList.remove("is-visible");
  node.classList.add("is-leaving");
  noticeHideTimer = window.setTimeout(() => {
    node.classList.add("hidden");
    node.classList.remove("is-leaving");
  }, 280);
}

document.querySelector("#notice .notice-close").addEventListener("click", dismissNotice);

function seenFailedOperations() {
  try {
    return new Set(JSON.parse(window.localStorage.getItem(seenFailuresStorageKey) || "[]"));
  } catch (_) {
    return new Set();
  }
}

function rememberFailedOperation(id) {
  const seen = seenFailedOperations();
  seen.add(String(id));
  try {
    window.localStorage.setItem(
      seenFailuresStorageKey,
      JSON.stringify(Array.from(seen).slice(-200))
    );
  } catch (_) {
    // lastShownError still provides in-memory deduplication.
  }
}

async function stepUpReauthentication() {
  if (authMethod === "oidc") {
    const returnTo = window.location.pathname + window.location.search;
    window.location.href = `/auth/oidc/reauth?return_to=${encodeURIComponent(returnTo)}`;
    return false;
  }
  const password = window.prompt(
    "Confirm your password to continue with this sensitive action."
  );
  if (!password) {
    return false;
  }
  const response = await fetch("/api/reauth", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readCookie(csrfCookieName),
    },
    body: JSON.stringify({password}),
  });
  if (!response.ok) {
    notify(t("ui.reauth_failed"), true);
    return false;
  }
  return true;
}

async function request(url, options = {}, allowReauthRetry = true) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (mutatingMethods.has(method)) {
    headers["X-CSRF-Token"] = readCookie(csrfCookieName);
  }
  const response = await fetch(url, {...options, headers});
  const body = await response.text();
  let data = {};
  if (body) {
    try {
      data = JSON.parse(body);
    } catch (_) {
      if (response.ok) {
        throw new Error(`Invalid response from the server (HTTP ${response.status})`);
      }
    }
  }
  if (response.status === 403 && data.error === "reauth_required" && allowReauthRetry) {
    const reauthenticated = await stepUpReauthentication();
    if (reauthenticated) {
      return request(url, options, false);
    }
    throw new Error("Reauthentication required for this action.");
  }
  if (!response.ok) {
    const fallback = response.status >= 500
      ? `Internal server error (HTTP ${response.status})`
      : `Operation failed (HTTP ${response.status})`;
    throw new Error(data.detail || fallback);
  }
  return data;
}

async function loadStats() {
  const data = await request("/api/stats");
  const states = data.states || {};
  const storage = data.storage || {};
  const cards = [
    [t("state.both"), states.both || 0, "count"],
    [t("state.local_only"), states.local_only || 0, "count"],
    [t("state.cloud_only"), states.cloud_only || 0, "count"],
    [t("ui.server_space"), storage.local_bytes || 0, "bytes"],
    [t("ui.cloud_space"), storage.cloud_bytes || 0, "bytes"],
    [t("ui.active_operations"), data.active_jobs || 0, "count"],
  ];
  document.querySelector("#summary").innerHTML = cards.map(([label, value, kind]) =>
    `<div class="card"><span>${escapeHtml(label)}</span><strong>${kind === "bytes" ? formatBytes(value) : value.toLocaleString("en-US")}</strong></div>`
  ).join("");
  renderFilesystemHealth(data.filesystem);
  const runtimeError = data.runtime?.last_error;
  if (runtimeError && runtimeError !== lastShownError) {
    lastShownError = runtimeError;
    notify(runtimeError, true);
  }
}

function renderFilesystemHealth(filesystem) {
  const host = document.querySelector("#filesystem-health");
  if (!host) return;
  if (!filesystem) {
    host.hidden = true;
    host.innerHTML = "";
    return;
  }
  host.hidden = false;
  const identity = `uid=${filesystem.uid} gid=${filesystem.gid}`;
  if (filesystem.ok) {
    host.className = "filesystem-health ok";
    host.innerHTML = `<strong>Vault filesystem healthy</strong><span>${escapeHtml(identity)}</span>`;
    return;
  }
  const findings = filesystem.findings || [];
  const preview = findings.slice(0, 5).map(item =>
    `<li><code>${escapeHtml(item.path || "")}</code> — ${escapeHtml(item.message || item.code)}</li>`
  ).join("");
  const more = findings.length > 5
    ? `<li>${findings.length - 5} more reported path(s)</li>`
    : "";
  host.className = "filesystem-health warn";
  host.innerHTML = `
    <strong>Vault filesystem needs attention</strong>
    <span>${escapeHtml(identity)}. Symbolic links and permission errors are reported; ownership and modes are never changed automatically.</span>
    <ul>${preview}${more}</ul>
  `;
}

function fileStateLabel(file) {
  if (file.local_file_type === "symlink") {
    return { className: "unsupported", label: t("ui.symlink_rejected") };
  }
  if (file.local_file_type && file.local_file_type !== "regular" && file.local_exists) {
    return { className: "unsupported", label: t("ui.unsupported_local_entry") };
  }
  return {
    className: file.state,
    label: stateLabel(file.state),
  };
}

async function loadJobs() {
  const data = await request("/api/jobs");
  jobGroups = data.groups || [];
  const seen = seenFailedOperations();
  const failed = jobGroups.find(group =>
    group.status === "failed" && !seen.has(String(group.id))
  );
  if (failed) {
    rememberFailedOperation(failed.id);
    const message = `${failed.path}: ${failed.message || "operation failed"}`;
    if (message !== lastShownError) {
      lastShownError = message;
      notify(message, true);
    }
  }
  return jobGroups.some(group => !["completed", "failed", "cancelled"].includes(group.status));
}

function activeOperations(path) {
  return jobGroups.filter(group =>
    group.path === path && !["completed", "failed", "cancelled"].includes(group.status)
  );
}

function renderProgress(operation) {
  const percent = Math.max(0, Math.min(100, Number(operation.percent) || 0));
  const bytes = operation.total_bytes
    ? `${formatBytes(operation.transferred_bytes)} of ${formatBytes(operation.total_bytes)}`
    : `${operation.completed_count} of ${operation.item_count} files`;
  const status = operationStatusLabel(operation);
  const estimateBits = [];
  if (operation.estimated_cost_eur != null) {
    estimateBits.push(`~€${Number(operation.estimated_cost_eur).toFixed(2)}`);
  }
  if (operation.estimated_hours != null) {
    estimateBits.push(`~${operation.estimated_hours}h`);
  }
  if (operation.restore_tier) {
    estimateBits.push(operation.restore_tier);
  }
  const estimateLine = estimateBits.length
    ? `<small>${escapeHtml(estimateBits.join(" · "))}</small>`
    : "";
  const approveButton = operation.status === "pending_approval" && isVaultOwner
    ? `<button class="approve-operation" type="button" data-action="approve-recover" data-group-id="${escapeHtml(operation.id)}">Approve restore</button>`
    : "";
  const cancelButton = canOperate
    ? `<button class="cancel-operation" type="button" data-action="cancel-operation" data-operation-action="${escapeHtml(operation.action)}" data-group-id="${escapeHtml(operation.id)}">${escapeHtml(t("ui.stop"))}</button>`
    : "";
  return `<div class="operation-progress" role="progressbar" aria-label="${escapeHtml(status)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}">
    <div class="progress-heading"><strong>${escapeHtml(status)}</strong><span>${percent}%</span></div>
    <div class="progress-track"><span style="width:${percent}%"></span></div>
    <div class="progress-footer"><small>${bytes}</small>${estimateLine}${approveButton}${cancelButton}</div>
  </div>`;
}

function actionButton(action, path, isDirectory, count = 1) {
  const labels = {
    upload: "Upload",
    recover: "Recover",
    "free-space": "Free local space",
    "cloud-archive": "Hide in cloud",
    "cloud-purge": "Purge permanently",
  };
  const title = isDirectory && count > 1 ? `${labels[action]} ${count} file` : labels[action];
  const danger = action === "free-space" || action === "cloud-purge" ? "danger" : "";
  return `<button class="${danger}" data-action="${action}" data-path="${escapeHtml(path)}" data-is-directory="${isDirectory}">${escapeHtml(title)}</button>`;
}

function renderActions(file) {
  const operations = activeOperations(file.path);
  if (operations.length) return `<div class="progress-stack">${operations.map(renderProgress).join("")}</div>`;
  const buttons = [];
  if (canOperate) {
    if (file.type === "directory") {
      const available = file.available_actions || {};
      if (available.upload) buttons.push(actionButton("upload", file.path, true, available.upload));
      if (available.recover) buttons.push(actionButton("recover", file.path, true, available.recover));
      if (available["free-space"] && localDeleteEnabled) {
        buttons.push(actionButton("free-space", file.path, true, available["free-space"]));
      }
    } else {
      if (file.upload_eligible) buttons.push(actionButton("upload", file.path, false));
      if (file.recover_eligible) buttons.push(actionButton("recover", file.path, false));
      if (file.cleanup_eligible && localDeleteEnabled) {
        buttons.push(actionButton("free-space", file.path, false));
      }
    }
  }
  if (cloudDeletionEnabled && isVaultOwner && file.type !== "directory") {
    if (file.cloud_exists || file.state === "cloud_only" || file.state === "both") {
      buttons.push(actionButton("cloud-archive", file.path, false));
      buttons.push(actionButton("cloud-purge", file.path, false));
    }
  }
  if (!buttons.length) return "";
  return `<div class="row-actions compact">${buttons.join("")}</div>`;
}

function renderDirectoryState(file) {
  const counts = file.state_counts || {};
  const details = Object.entries(counts)
    .filter(([, count]) => count)
    .map(([state, count]) => `${count} ${stateLabel(state)}`)
    .join(" · ");
  return `<span class="badge ${escapeHtml(file.state)}">${escapeHtml(stateLabel(file.state))}</span><small class="state-detail">${escapeHtml(details)}</small>`;
}

async function loadFiles() {
  const q = encodeURIComponent(document.querySelector("#search").value);
  const state = encodeURIComponent(document.querySelector("#state-filter").value);
  const directory = encodeURIComponent(currentDirectory);
  const data = await request(`/api/files?q=${q}&state=${state}&directory=${directory}&page=${currentPage}&page_size=${pageSize}`);
  total = data.total;
  const body = document.querySelector("#file-list");
  if (!data.items.length) {
    body.innerHTML = `<tr><td colspan="5" class="empty">${escapeHtml(t("ui.empty_list"))}</td></tr>`;
  } else {
    body.innerHTML = data.items.map(file => {
      if (file.type === "directory") {
        const countLabel = file.item_count === 1 ? "1 file" : `${file.item_count.toLocaleString("en-US")} files`;
        return `<tr class="directory-row">
          <td class="path"><button type="button" class="folder-link" data-directory="${escapeHtml(file.path)}"><span class="folder-icon" aria-hidden="true">📁</span><span><strong>${escapeHtml(file.name)}</strong><small>${countLabel} in this folder</small></span></button></td>
          <td><strong>${formatBytes(file.total_size)}</strong><small class="size-detail">File total</small></td>
          <td class="state-cell">${renderDirectoryState(file)}</td>
          <td>${renderStorageClass(file)}</td>
          <td class="actions-cell">${renderActions(file)}</td>
        </tr>`;
      }
      const isDeepArchive = file.cloud_exists && file.storage_class === "DEEP_ARCHIVE";
      const state = fileStateLabel(file);
      return `<tr class="${isDeepArchive ? "deep-archive-row" : ""}" data-file-path="${escapeHtml(file.path)}">
        <td class="path"><button type="button" class="file-history-link" data-path="${escapeHtml(file.path)}"><span class="file-icon" aria-hidden="true">📄</span><span><strong>${escapeHtml(file.name)}</strong><small class="size-detail history-summary" hidden></small></span></button></td>
        <td>${formatBytes(file.local_size ?? file.cloud_size)}</td>
        <td class="state-cell"><span class="badge ${state.className}">${escapeHtml(state.label)}</span></td>
        <td>${renderStorageClass(file)}</td>
        <td class="actions-cell">${renderActions(file)}</td>
      </tr>`;
    }).join("");
    body.querySelectorAll(".file-history-link").forEach((button) => {
      button.addEventListener("click", async () => {
        const path = button.getAttribute("data-path");
        const summary = button.querySelector(".history-summary");
        if (!path || !summary) return;
        try {
          const history = await request(`/api/file-history?path=${encodeURIComponent(path)}`);
          const formatted = formatFileHistory(history);
          summary.hidden = false;
          summary.textContent = formatted.summary;
        } catch (error) {
          summary.hidden = false;
          summary.textContent = error.message || "Unable to load history";
        }
      });
    });
  }
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const unit = data.mode === "search" ? t("ui.files_found_unit") : t("ui.items_unit");
  document.querySelector("#page-label").textContent = t("ui.page_label", {
    page: currentPage,
    pages,
    total: total.toLocaleString("en-US"),
    unit,
  });
  document.querySelector("#previous").disabled = currentPage <= 1;
  document.querySelector("#next").disabled = currentPage >= pages;
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  window.clearTimeout(refreshTimer);
  let hasActiveJobs = false;
  try {
    hasActiveJobs = await loadJobs();
    await Promise.all([loadStats(), loadFiles()]);
  } catch (error) {
    notify(error.message, true);
  } finally {
    refreshing = false;
    refreshTimer = window.setTimeout(refresh, hasActiveJobs ? 1000 : 10000);
  }
}

async function loadVaults() {
  const data = await request("/api/vaults");
  const selector = document.querySelector("#vault-selector");
  selector.innerHTML = data.items.map(vault =>
    `<option value="${vault.id}">${escapeHtml(vault.name)}</option>`
  ).join("");
  const selectedName = document.querySelector("h1").textContent.trim();
  const selected = data.items.find(vault => vault.name === selectedName);
  if (selected) selector.value = selected.id;
  selector.closest(".account-bar").classList.toggle("single-vault", data.items.length <= 1);
}

const scanButton = document.querySelector("#scan-button");
if (scanButton) scanButton.addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const data = await request("/api/scan", {method: "POST"});
    notify(data.message);
    window.setTimeout(refresh, 1200);
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
});

document.querySelector("#file-list").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (button) {
    const action = button.dataset.action;
    if (action === "cancel-operation") {
      button.disabled = true;
      button.textContent = t("ui.stopping");
      try {
        const data = await request("/api/jobs/cancel", {
          method: "POST",
          body: JSON.stringify({
            group_id: button.dataset.groupId,
            action: button.dataset.operationAction,
          }),
        });
        notify(data.message);
        await refresh();
      } catch (error) {
        notify(error.message, true);
        button.disabled = false;
        button.textContent = t("ui.stop");
      }
      return;
    }
    if (action === "approve-recover") {
      button.disabled = true;
      button.textContent = "Approving…";
      try {
        const data = await request("/api/recover/approve", {
          method: "POST",
          body: JSON.stringify({group_id: button.dataset.groupId}),
        });
        notify(data.message);
        await refresh();
      } catch (error) {
        notify(error.message, true);
        button.disabled = false;
        button.textContent = "Approve restore";
      }
      return;
    }
    const path = button.dataset.path;
    const isDirectory = button.dataset.isDirectory === "true";
    button.disabled = true;
    button.classList.add("busy");
    const originalText = button.textContent;
    button.textContent = action === "free-space" ? t("ui.freeing_space") : t("ui.starting");
    try {
      let body = {path, is_directory: isDirectory};
      if (action === "recover" && !isDirectory) {
        const confirmed = await confirmRecover(path);
        if (!confirmed) {
          button.disabled = false;
          button.classList.remove("busy");
          button.textContent = originalText;
          return;
        }
        body = {...body, ...confirmed};
      }
      if (action === "cloud-archive") {
        const settings = await request("/api/vault/cloud-deletion");
        const ok = window.confirm(
          `${settings.delete_marker_explanation}\n\nHide ${path} in the cloud with a Delete Marker? Noncurrent Archive Versions stay recoverable.`
        );
        if (!ok) {
          button.disabled = false;
          button.classList.remove("busy");
          button.textContent = originalText;
          return;
        }
      }
      if (action === "cloud-purge") {
        const confirmed = await confirmCloudPurge(path);
        if (!confirmed) {
          button.disabled = false;
          button.classList.remove("busy");
          button.textContent = originalText;
          return;
        }
        body = {...body, ...confirmed};
      }
      const data = await request(`/api/${action}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      notify(data.message);
      await refresh();
    } catch (error) {
      notify(error.message, true);
      button.disabled = false;
      button.classList.remove("busy");
      button.textContent = originalText;
    }
    return;
  }
  const directoryControl = event.target.closest("[data-directory]");
  if (directoryControl) openDirectory(directoryControl.dataset.directory);
});

async function confirmRecover(path) {
  const versions = await request(`/api/files/versions?path=${encodeURIComponent(path)}`);
  const recoverable = (versions.items || []).filter(item => item.recoverable);
  if (!recoverable.length) {
    throw new Error("No recoverable Archive Version is available");
  }
  let archiveVersionId = versions.default_archive_version_id || recoverable[0].id;
  if (recoverable.length > 1) {
    const choices = recoverable.map(item =>
      `#${item.version_number} · ${item.storage_class || "STANDARD"} · ${formatBytes(item.size)}`
    ).join("\n");
    const picked = window.prompt(
      `Select Archive Version number to recover:\n${choices}`,
      String(recoverable[0].version_number)
    );
    if (picked == null) return null;
    const match = recoverable.find(item => String(item.version_number) === String(picked).trim());
    if (!match) throw new Error("Unknown Archive Version selection");
    archiveVersionId = match.id;
  }
  const selected = recoverable.find(item => item.id === archiveVersionId) || recoverable[0];
  const estimate = await request("/api/recover/estimate", {
    method: "POST",
    body: JSON.stringify({
      path,
      archive_version_id: archiveVersionId,
      restore_tier: versions.default_restore_tier,
      restore_days: versions.default_restore_days,
    }),
  });
  const lines = [
    `Recover ${path}?`,
    `Version #${selected.version_number} (${selected.storage_class || "STANDARD"})`,
  ];
  if (estimate.requires_restore && estimate.estimate) {
    lines.push(
      `Restore tier: ${estimate.estimate.tier} for ${estimate.estimate.days} days`,
      `Estimate: ~€${Number(estimate.estimate.estimated_cost_eur).toFixed(2)} / ~${estimate.estimate.estimated_hours}h`,
      "S3 RestoreObject cannot be cancelled after AWS accepts it."
    );
    if (estimate.high_impact) {
      lines.push("High-impact restore: primary-owner approval will be required before AWS is contacted.");
    }
  }
  if (!window.confirm(lines.join("\n"))) return null;
  return {
    archive_version_id: archiveVersionId,
    restore_tier: estimate.estimate?.tier || versions.default_restore_tier,
    restore_days: estimate.estimate?.days || versions.default_restore_days,
  };
}

async function confirmCloudPurge(path) {
  const settings = await request("/api/vault/cloud-deletion");
  const preview = await request("/api/cloud-deletion/preview", {
    method: "POST",
    body: JSON.stringify({path, is_directory: false}),
  });
  const phrase = settings.generated_phrase;
  const reason = window.prompt(
    [
      "Permanent purge deletes every selected Archive Version and Delete Marker.",
      settings.delete_marker_explanation,
      "",
      `Selection: ${preview.object_count} object(s), ${preview.version_count} version(s), ${preview.delete_marker_count} marker(s), ${preview.byte_count} bytes.`,
      `A ${settings.purge_delay_seconds}-second cancellable delay applies before any deletion call.`,
      "",
      "Enter a reason for this permanent purge:",
    ].join("\n"),
    ""
  );
  if (!reason || !reason.trim()) return null;
  const confirmation = window.prompt(
    [
      `Type the vault name (${vaultName || "vault"}) or this phrase to confirm:`,
      phrase,
    ].join("\n"),
    ""
  );
  if (!confirmation) return null;
  return {
    confirmation,
    reason: reason.trim(),
    generated_phrase: phrase,
  };
}

document.querySelector("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { currentPage = 1; refresh(); }, 250);
});
document.querySelector("#state-filter").addEventListener("change", () => { currentPage = 1; refresh(); });
document.querySelector("#previous").addEventListener("click", () => { currentPage--; refresh(); });
document.querySelector("#next").addEventListener("click", () => { currentPage++; refresh(); });
document.querySelector("#breadcrumbs").addEventListener("click", event => {
  const button = event.target.closest("button[data-directory]");
  if (button) openDirectory(button.dataset.directory);
});
document.querySelector("#up-directory").addEventListener("click", () => {
  openDirectory(currentDirectory.split("/").slice(0, -1).join("/"));
});
window.addEventListener("popstate", () => {
  currentDirectory = new URLSearchParams(window.location.search).get("directory") || "";
  currentPage = 1;
  document.querySelector("#search").value = "";
  renderBreadcrumbs();
  refresh();
});
document.querySelector("#vault-selector").addEventListener("change", async event => {
  await request("/api/vaults/select", {method: "POST", body: JSON.stringify({vault_id: Number(event.target.value)})});
  window.location.reload();
});
document.querySelector("#logout-button").addEventListener("click", async () => {
  await request("/api/logout", {method: "POST"});
  window.location.href = "/login";
});

async function loadIdentity() {
  try {
    const data = await request("/api/me");
    authMethod = data.auth_method || null;
  } catch (_) {
    // Non-fatal: reauth step-up defaults to a password prompt.
  }
}

loadIdentity();
loadVaults();
renderBreadcrumbs();
refresh();
