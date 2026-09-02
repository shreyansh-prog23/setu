// Persists SOS/hazard-report requests that failed to send (real network
// failure, not just the manual "Zero Network" demo toggle in DriverView.jsx
// - that toggle simulates a driver's perceived state, it isn't real
// connectivity) and retries them automatically once the browser reports
// real connectivity back. Without this, a failed submission was just
// dropped silently - see the "offline thing" gaps this replaces in
// DriverView.jsx's dispatchSos/submitObstacleReport.
import { apiFetch, clearDriverSession } from './apiClient';

const QUEUE_KEY = 'setu_pending_requests';
const RETRY_INTERVAL_MS = 30000; // fallback poll - the browser 'online' event fires on network-interface change, which isn't the same as "can actually reach the backend"

let listeners = [];

function readQueue() {
  try {
    return JSON.parse(localStorage.getItem(QUEUE_KEY) || '[]');
  } catch {
    return [];
  }
}

function writeQueue(items) {
  localStorage.setItem(QUEUE_KEY, JSON.stringify(items));
  listeners.forEach((fn) => fn(items.length));
}

export function getPendingCount() {
  return readQueue().length;
}

export function onQueueChange(callback) {
  listeners.push(callback);
  return () => { listeners = listeners.filter((fn) => fn !== callback); };
}

export function queuePendingRequest(path, options) {
  const items = readQueue();
  items.push({ id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, path, options, queuedAt: Date.now() });
  writeQueue(items);
}

// Only drops an item on a real 2xx response - a network failure or a
// server error leaves it queued for the next attempt. Known simplification:
// a permanently-invalid request (bad payload) would also retry forever
// rather than being detected and dropped - acceptable for this scope, not
// hidden.
//
// One specific case of that IS handled: a 401 on a driver-token request
// means the session itself is dead (e.g. the backend's SQLite database -
// Render's free tier has no persistent disk - got wiped by a redeploy,
// orphaning every token issued before it) and will NEVER start succeeding
// just by retrying, unlike a real network blip. Retrying it forever with
// the same dead token would silently eat every future SOS from that
// device. Instead: drop the stale session and tell DriverView to show the
// login gate again - the report itself STAYS queued (not dropped), so the
// moment the driver logs back in, apiFetch attaches the fresh token and
// this exact queued item sends automatically on the next flush.
export async function flushPendingRequests() {
  const items = readQueue();
  if (!items.length) return;
  const stillPending = [];
  let sessionExpired = false;
  for (const item of items) {
    try {
      const res = await apiFetch(item.path, item.options);
      if (res.status === 401) {
        sessionExpired = true;
        stillPending.push(item);
      } else if (!res.ok) {
        stillPending.push(item);
      }
    } catch {
      stillPending.push(item);
    }
  }
  if (sessionExpired) {
    clearDriverSession();
    window.dispatchEvent(new Event('driver-session-expired'));
  }
  if (stillPending.length !== items.length) writeQueue(stillPending);
}

if (typeof window !== 'undefined') {
  window.addEventListener('online', flushPendingRequests);
  // Mobile browsers throttle/suspend setInterval timers heavily once a tab
  // is backgrounded or the screen locks - a queued SOS could otherwise sit
  // untouched until the 30s poll happens to land in a moment the tab is
  // foregrounded, which on a real phone can be a long, unpredictable wait.
  // Retrying the instant the driver reopens/refocuses the app (a real,
  // observable moment, not a guess) closes that gap without needing them to
  // manually reload.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') flushPendingRequests();
  });
  window.addEventListener('focus', flushPendingRequests);
  flushPendingRequests(); // in case connectivity was already back before this module loaded
  setInterval(flushPendingRequests, RETRY_INTERVAL_MS);
}
