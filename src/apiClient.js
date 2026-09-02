// Shared fetch wrapper - every backend call goes through here so the
// X-API-Key header (see backend/security.py) can never be forgotten on a
// call site. VITE_SETU_API_KEY (root .env.local) ends up baked into the
// built JS bundle, same as any Vite client env var - visible to anyone who
// opens dev tools. That's an inherent limit of key-in-a-browser-app auth,
// not something this file can fix: it raises the bar against casual/
// opportunistic abuse of the open API, not a targeted attacker who's
// already loaded the app.
// VITE_API_BASE_URL (root .env.local / Vercel project env) points this at
// the deployed backend in production; falls back to the local dev backend
// when unset, so the same code works unmodified in both environments.
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

const API_KEY = import.meta.env.VITE_SETU_API_KEY;

// Driver session (see backend/driver_auth.py) - unlike the API key above,
// this is per-driver and server-revocable (sign-out actually deletes the
// session row), not a single secret shared by everyone who loads the app.
// Centralized here, not read/written ad hoc in DriverView.jsx, so there's
// one place that can't disagree with itself about the storage key.
const DRIVER_TOKEN_KEY = 'setu_driver_token';
const DRIVER_PHONE_KEY = 'setu_driver_phone';

export function getDriverSession() {
  const token = localStorage.getItem(DRIVER_TOKEN_KEY);
  const phone = localStorage.getItem(DRIVER_PHONE_KEY);
  return token && phone ? { token, phone } : null;
}

export function setDriverSession(token, phone) {
  localStorage.setItem(DRIVER_TOKEN_KEY, token);
  localStorage.setItem(DRIVER_PHONE_KEY, phone);
}

export function clearDriverSession() {
  localStorage.removeItem(DRIVER_TOKEN_KEY);
  localStorage.removeItem(DRIVER_PHONE_KEY);
}

export function apiFetch(path, options = {}) {
  const driverToken = localStorage.getItem(DRIVER_TOKEN_KEY);
  return fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      ...(options.headers || {}),
      'X-API-Key': API_KEY,
      ...(driverToken ? { 'X-Driver-Token': driverToken } : {}),
    },
  });
}
