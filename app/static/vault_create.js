const csrfCookieName = "frostvault_csrf";
const mutatingMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);
let authMethod = null;
let createdVaultId = null;

function readCookie(name) {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const cookie = part.trim();
    if (cookie.startsWith(prefix)) return decodeURIComponent(cookie.slice(prefix.length));
  }
  return "";
}

function errorMessage(data, status) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) {
    return data.detail.map(item => item.msg || "Invalid value").join("; ");
  }
  return status >= 500
    ? `Internal server error (HTTP ${status})`
    : `Operation failed (HTTP ${status})`;
}

async function stepUpReauthentication() {
  if (authMethod === "oidc") {
    const returnTo = window.location.pathname + window.location.search;
    window.location.href = `/auth/oidc/reauth?return_to=${encodeURIComponent(returnTo)}`;
    return false;
  }
  const password = window.prompt("Confirm your password to continue with this sensitive action.");
  if (!password) return false;
  const response = await fetch("/api/reauth", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readCookie(csrfCookieName),
    },
    body: JSON.stringify({password}),
  });
  return response.ok;
}

async function api(url, options = {}, allowReauthRetry = true) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (mutatingMethods.has(method)) headers["X-CSRF-Token"] = readCookie(csrfCookieName);
  const response = await fetch(url, {...options, headers});
  const body = await response.text();
  let data = {};
  if (body) {
    try { data = JSON.parse(body); } catch (_) {
      if (response.ok) throw new Error(`Invalid response from the server (HTTP ${response.status})`);
    }
  }
  if (response.status === 403 && data.error === "reauth_required" && allowReauthRetry) {
    if (await stepUpReauthentication()) return api(url, options, false);
    throw new Error("Reauthentication required for this action.");
  }
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return data;
}

async function loadIdentity() {
  try {
    const data = await api("/api/me");
    authMethod = data.auth_method || null;
  } catch (_) {
    // The page is already authenticated; the form will show any API failure.
  }
}

async function openCreatedVault(vaultId) {
  await api("/api/vaults/select", {
    method: "POST",
    body: JSON.stringify({vault_id: vaultId}),
  });
  window.location.assign("/");
}

document.querySelector("#vault-create-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  const errorBox = document.querySelector("#creation-error");
  const values = new FormData(form);
  const name = String(values.get("name") || "").trim();
  const slug = String(values.get("slug") || "").trim();
  const encryptionMode = String(values.get("encryption_mode") || "plain");
  const payload = {name, encryption_mode: encryptionMode};
  if (slug) payload.slug = slug;

  errorBox.classList.add("hidden");
  button.disabled = true;
  try {
    const vault = await api("/api/vaults", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (vault.encryption_mode === "crypt" && vault.recovery_export) {
      createdVaultId = vault.id;
      form.classList.add("hidden");
      const panel = document.querySelector("#recovery-panel");
      document.querySelector("#recovery-export").value = vault.recovery_export;
      panel.classList.remove("hidden");
      return;
    }
    await openCreatedVault(vault.id);
  } catch (error) {
    errorBox.textContent = error.message || "Vault creation failed.";
    errorBox.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#confirm-recovery").addEventListener("click", async () => {
  const button = document.querySelector("#confirm-recovery");
  const errorBox = document.querySelector("#recovery-error");
  errorBox.classList.add("hidden");
  button.disabled = true;
  try {
    if (createdVaultId == null) throw new Error("Vault was not created.");
    await api("/api/vaults/select", {
      method: "POST",
      body: JSON.stringify({vault_id: createdVaultId}),
    });
    await api("/api/vault/recovery/confirm", {
      method: "POST",
      body: JSON.stringify({acknowledged: true}),
    });
    window.location.assign("/");
  } catch (error) {
    errorBox.textContent = error.message || "Could not confirm recovery custody.";
    errorBox.classList.remove("hidden");
  } finally {
    button.disabled = false;
  }
});

loadIdentity();
