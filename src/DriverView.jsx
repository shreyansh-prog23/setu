import React, { useState, useRef, useEffect } from 'react';
import { apiFetch, getDriverSession, setDriverSession, clearDriverSession } from './apiClient';
import { queuePendingRequest, getPendingCount, onQueueChange } from './offlineQueue';
import {
  Truck, Wifi, WifiOff, ShieldAlert, ArrowRight, AlertTriangle,
  Navigation, Siren, CheckCircle2, Send, X, ImagePlus, ChevronDown,
  MapPin, Loader2, Mountain, Waves, Wrench, HeartPulse, Construction,
  Clock, Info, TrendingUp, Phone, LogOut,
} from 'lucide-react';

const cx = (...a) => a.filter(Boolean).join(' ');

const HOLD_MS = 1800;
const RING_R = 54;
const RING_CIRC = 2 * Math.PI * RING_R;
// Display-only fallback before a real dispatch has set lastDispatch - India's
// geographic centroid, not a specific place, since this app is pan-India now.
const SOS_LAT = 22.9734;
const SOS_LNG = 78.6569;

// --- Quick-pick chips for the location fields - a handful of major cities as
// a convenience shortcut. Free-text search (see LocationField/useGeocodeSearch
// below) resolves any place in India via the real backend geocoder, not just
// this list - these are shortcuts, not the only way to pick a location. ---
const HUBS = [
  { key: 'guwahati', name: 'Guwahati', lat: 26.1445, lng: 91.7362 },
  { key: 'delhi', name: 'Delhi', lat: 28.6139, lng: 77.209 },
  { key: 'mumbai', name: 'Mumbai', lat: 19.076, lng: 72.8777 },
  { key: 'chennai', name: 'Chennai', lat: 13.0827, lng: 80.2707 },
  { key: 'kolkata', name: 'Kolkata', lat: 22.5726, lng: 88.3639 },
  { key: 'bengaluru', name: 'Bengaluru', lat: 12.9716, lng: 77.5946 },
];

// --- Static hazard zones checked against the route polyline in OFFLINE
// mode only (the live path uses the real multi-hazard backend, plus this
// app's own persisted driver-reported hazards - see hazard_avoid_coords in
// fetchTomTomRoute). Left empty now that the app is pan-India, not one
// hardcoded NE spot - a real offline hazard list would need to be synced
// from the backend before going offline, not hardcoded here. ---
const HAZARD_ZONES = [];

const ROUTE_TIMEOUT_MS = 40000; // real call scores 3 TomTom candidates x live rainfall+elevation each - measured ~9-10s on local dev, but the deployed Render backend measured ~25s for a real long-distance route (Delhi-Chennai) - cross-region network latency to TomTom/Open-Meteo from Render's servers, not a bug - 18s (the old value, tuned only against local timing) was silently falling back to the offline estimate on every real deployed request for any non-trivial distance
const GEOLOCATION_TIMEOUT_MS = 8000;
const AVG_OFFLINE_SPEED_KMH = 42; // used for offline cached ETA estimates
const ROAD_WINDING_FACTOR = 1.35; // straight-line -> approximate hill-road distance

const INSTANT_SOS_CATEGORY = 'Immediate Panic / Unknown Distress';

const INCIDENT_TYPES = [
  { key: 'Landslide / Mudslide', icon: Mountain },
  { key: 'Flash Flood / Inundation', icon: Waves },
  { key: 'Vehicle Breakdown', icon: Wrench },
  { key: 'Medical Emergency', icon: HeartPulse },
  { key: 'Structural Road Collapse', icon: Construction },
];

const SEVERITY_LEVELS = [
  { key: 'Critical', sub: 'Blocked', icon: AlertTriangle, cls: 'border-red-500/50 bg-red-500/15 text-red-600 dark:text-red-300' },
  { key: 'Moderate', sub: 'Delay', icon: Clock, cls: 'border-amber-500/50 bg-amber-500/15 text-amber-600 dark:text-amber-300' },
  { key: 'Informational', sub: '', icon: Info, cls: 'border-sky-500/50 bg-sky-500/15 text-sky-600 dark:text-sky-300' },
];

function toSmsCode(str) {
  return (str || 'UNKNOWN').toString().toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

function haversineKm(lat1, lng1, lat2, lng2) {
  const R = 6371;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLng = ((lng2 - lng1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLng / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

const BANNER_TONE = {
  red: { border: 'border-red-500/40 bg-red-500/10', text: 'text-red-700 dark:text-red-200', icon: 'text-red-600 dark:text-red-400' },
  amber: { border: 'border-amber-500/40 bg-amber-500/10', text: 'text-amber-700 dark:text-amber-200', icon: 'text-amber-600 dark:text-amber-400' },
  emerald: { border: 'border-emerald-500/40 bg-emerald-500/10', text: 'text-emerald-700 dark:text-emerald-200', icon: 'text-emerald-600 dark:text-emerald-400' },
};

// Single, honest top-line verdict combining the two signals the backend
// returns separately: hazard_detected (a SPECIFIC reported obstacle/SOS on
// this road) and ai_risk_level (the live multi-hazard MODEL's read of
// current conditions - rain, soil saturation, seismic zone, etc). These can
// legitimately disagree (no one has reported anything, but conditions are
// objectively bad) - showing only one of them as "the verdict" reads as a
// flat contradiction when the other one is also on screen. This always
// surfaces whichever signal is worse, so the banner and the AI Corridor
// Risk card never contradict each other.
function overallRouteBanner(primary, rerouted) {
  const isHigh = typeof primary.aiRiskLevel === 'string' && primary.aiRiskLevel.startsWith('HIGH_');
  const isModerate = primary.aiRiskLevel === 'MODERATE';

  if (primary.hazard) {
    return { tone: 'red', title: 'Reported Hazard: Route Blocked', Icon: AlertTriangle };
  }
  if (isHigh) {
    return {
      tone: 'red',
      title: rerouted ? 'Rerouted, But Conditions Still High Risk' : 'High Risk Conditions Along Route',
      Icon: AlertTriangle,
    };
  }
  if (isModerate) {
    return {
      tone: 'amber',
      title: rerouted ? 'Rerouted: Bypass Still Has Moderate Risk' : 'Moderate Risk Conditions',
      Icon: AlertTriangle,
    };
  }
  return {
    tone: 'emerald',
    title: rerouted ? 'AI Rerouted: Hazard-Clear Bypass Selected' : primary.aiRouteLabel || 'No Reported Blockages · Conditions Normal',
    Icon: Navigation,
  };
}

function formatKm(km) {
  return `${km.toFixed(0)} km`;
}

function formatDuration(hrs) {
  const totalMin = Math.round(hrs * 60);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// Straight-line interpolation used to hazard-check an offline/cached corridor
function lineHitsHazard(a, b) {
  const steps = 24;
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const lat = a.lat + (b.lat - a.lat) * t;
    const lng = a.lng + (b.lng - a.lng) * t;
    if (HAZARD_ZONES.some((hz) => haversineKm(lat, lng, hz.lat, hz.lng) <= hz.radiusKm)) return true;
  }
  return false;
}

// Debounced real place-name search against the backend's geocoder (see
// backend/geocoding.py) - replaces matching free-text against a fixed hub
// list, so a driver can search any real place in India, not just the chips.
function useGeocodeSuggestions(query) {
  const [suggestions, setSuggestions] = useState([]);
  useEffect(() => {
    const q = query.trim();
    if (q.length < 3) {
      setSuggestions([]);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await apiFetch(`/api/geocode/search?q=${encodeURIComponent(q)}`, { signal: controller.signal });
        if (res.ok) setSuggestions(await res.json());
      } catch (e) {
        // Aborted (user kept typing) or offline - just leave suggestions as-is
      }
    }, 350);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query]);
  return suggestions;
}

// A submitted place that was never explicitly clicked from the suggestion
// dropdown (typed a valid name, then hit Enter/Plan Route directly - a very
// natural thing to do) would otherwise resolve to nothing. Falls back to the
// already-fetched suggestion list, then a fresh one-shot lookup for text
// typed faster than the 350ms suggestion debounce.
async function resolvePlace(text, coords, suggestions) {
  if (coords) return coords;
  if (suggestions.length > 0) {
    const top = suggestions[0];
    return { lat: top.lat, lng: top.lon, name: top.name };
  }
  const q = text.trim();
  if (!q) return null;
  try {
    const res = await apiFetch(`/api/geocode/search?q=${encodeURIComponent(q)}`);
    if (res.ok) {
      const results = await res.json();
      if (results.length > 0) return { lat: results[0].lat, lng: results[0].lon, name: results[0].name };
    }
  } catch (e) {
    // offline or request failed - nothing more we can do, caller treats null as unresolved
  }
  return null;
}

async function fetchTomTomRoute(src, dest, signal) {
  const payload = {
    origin: { lat: src.lat, lon: src.lng },
    destination: { lat: dest.lat, lon: dest.lng },
    avoid_unpaved: false,
    hazard_avoid_coords: HAZARD_ZONES.map((hz) => ({ lat: hz.lat, lon: hz.lng })),
  };
  const res = await apiFetch(`/api/v1/routes/calculate?geojson=true`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok) throw new Error(`Routing request failed (${res.status})`);
  return res.json(); // GeoJSON Feature<LineString>
}

// India's geographic centroid - used ONLY as an honest "we couldn't get your
// real location" fallback (denied/unsupported/timed out), never a specific
// place name implying false precision. Pan-India now, so there's no single
// region to assume a driver is in if GPS fails.
const LOCATION_UNAVAILABLE = { lat: 22.9734, lng: 78.6569, anchorLabel: 'Location unavailable' };

// Live device GPS for SOS dispatch - used as-is, anywhere in India.
function getSosCoords() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) {
      console.warn('SOS: geolocation unsupported on this device.');
      resolve(LOCATION_UNAVAILABLE);
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      (err) => {
        console.warn(`SOS: geolocation failed (${err.message}).`);
        resolve(LOCATION_UNAVAILABLE);
      },
      { enableHighAccuracy: true, timeout: GEOLOCATION_TIMEOUT_MS }
    );
  });
}

async function dispatchSos(lat, lng, details = {}) {
  const { incidentType = INSTANT_SOS_CATEGORY, severity = 'Critical', notes = '', mode = 'instant' } = details;
  const requestOptions = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      // No truck_id here - the backend derives the real reporter identity
      // from the logged-in driver's session token (X-Driver-Token, sent
      // automatically by apiFetch), not anything the client asserts.
      latitude: lat,
      longitude: lng,
      timestamp: new Date().toISOString(),
      // Was a hardcoded "Emergency Medical Supplies" regardless of what was
      // actually being reported - a real SOS isn't always about medical
      // cargo. Reflects the actual reported incident instead (the place
      // name itself is resolved and shown on the Command Center side via
      // reverse-geocoding, not baked into this string, to keep it out of
      // the SOS submission's critical path).
      priority: `${severity}: ${incidentType}`,
      status: 'DISPATCH_TRIGGERED',
      incident_type: incidentType,
      severity,
      notes,
      mode,
    }),
  };
  // A real network failure (not just the manual "Zero Network" demo toggle
  // - that's a separate, perceived-state thing) queues this for automatic
  // retry (see offlineQueue.js) instead of just losing it - this used to
  // throw and get silently swallowed by the caller's catch block.
  try {
    const res = await apiFetch('/api/sos', requestOptions);
    if (res.status === 401) {
      // The session token is dead (not just this one request failing) -
      // retrying with the same token would never succeed, so surface it
      // immediately here rather than waiting for the next background
      // flush pass to notice the same thing (see offlineQueue.js). The
      // report itself still queues below so it auto-sends the moment the
      // driver logs back in with a fresh token.
      clearDriverSession();
      window.dispatchEvent(new Event('driver-session-expired'));
    }
    if (!res.ok) throw new Error(`SOS dispatch failed (${res.status})`);
  } catch (err) {
    queuePendingRequest('/api/sos', requestOptions);
    throw err;
  }
}

// Fully offline estimate: haversine distance * winding factor, avg hill-road
// speed. No fake bypass suggestion when a hazard is hit - a real alternate
// route can't be meaningfully computed without connectivity anyway, so this
// just surfaces the hazard flag honestly rather than inventing one waypoint
// that only ever made sense for a single NE corridor.
function computeCachedRoute(src, dest) {
  const straightKm = haversineKm(src.lat, src.lng, dest.lat, dest.lng);
  const distanceKm = straightKm * ROAD_WINDING_FACTOR;
  const durationHrs = distanceKm / AVG_OFFLINE_SPEED_KMH;
  const hazard = lineHitsHazard(src, dest);

  return {
    source: 'cached',
    primary: { distanceKm, durationHrs, hazard },
    recommended: null,
  };
}

const AI_RISK_TONE = {
  SAFE: { card: 'border-emerald-500/40 bg-emerald-500/10', text: 'text-emerald-700 dark:text-emerald-200', badge: 'border-emerald-500/50 bg-emerald-500/15 text-emerald-600 dark:text-emerald-300', tag: 'Safe' },
  MODERATE: { card: 'border-amber-500/40 bg-amber-500/10', text: 'text-amber-700 dark:text-amber-200', badge: 'border-amber-500/50 bg-amber-500/15 text-amber-600 dark:text-amber-300', tag: 'Moderate' },
  HIGH: { card: 'border-red-500/40 bg-red-500/10', text: 'text-red-700 dark:text-red-200', badge: 'border-red-500/50 bg-red-500/15 text-red-600 dark:text-red-300', tag: 'High Hazard Alert' },
};

// The unified engine's HIGH level is hazard-specific (HIGH_LANDSLIDE_RISK,
// HIGH_EARTHQUAKE_RISK, HIGH_FLOOD_RISK, HIGH_CYCLONE_RISK) - any of them
// means the same "high hazard" tone here, not just landslide's.
function riskTone(riskLevel) {
  if (typeof riskLevel === 'string' && riskLevel.startsWith('HIGH_')) return AI_RISK_TONE.HIGH;
  return AI_RISK_TONE[riskLevel];
}

const HAZARD_LABELS = { landslide: 'Landslide', earthquake: 'Earthquake', flood: 'Flood', cyclone: 'Cyclone' };
const HAZARD_ORDER = ['landslide', 'earthquake', 'flood', 'cyclone'];

// One compact row per hazard in the expanded breakdown - same tone/badge
// language as the top-line card, just smaller, so SAFE/MODERATE/HIGH reads
// consistently whether it's the headline number or a per-hazard detail.
function HazardBreakdownRow({ hazardKey, data }) {
  const tone = riskTone(data.ai_risk_level);
  if (!tone) return null;
  return (
    <div className="flex items-center justify-between gap-2 border-t border-slate-200/80 dark:border-slate-800/80 py-1.5 first:border-t-0">
      <span className="text-[11px] text-slate-600 dark:text-slate-300">{HAZARD_LABELS[hazardKey]}</span>
      <div className="flex items-center gap-1.5">
        <span className={cx('text-[11px] font-semibold', tone.text)}>{Math.round(data.ai_safety_score)}%</span>
        <span className={cx('rounded-full border px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide', tone.badge)}>
          {tone.tag}
        </span>
      </div>
    </div>
  );
}

function AiCorridorRiskCard({ safetyScore, riskLevel, riskFactors, hazardBreakdown }) {
  const [expanded, setExpanded] = useState(false);
  const tone = riskTone(riskLevel);
  if (!tone) {
    return (
      <div className="rounded-lg border border-slate-300 dark:border-slate-700 bg-slate-100 dark:bg-slate-800/40 p-2.5 text-[11px] text-slate-500 dark:text-slate-500">
        AI Corridor Risk Assessment unavailable (offline mode)
      </div>
    );
  }
  return (
    <div className={cx('rounded-lg border p-2.5', tone.card)}>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className={cx('flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide', tone.text)}>
          <ShieldAlert size={13} /> AI Corridor Risk Assessment
        </span>
        <span className={cx('shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide', tone.badge)}>
          {tone.tag}
        </span>
      </div>
      <div className={cx('text-lg font-bold leading-none', tone.text)}>
        {Math.round(safetyScore)}%<span className="ml-1.5 text-[11px] font-normal text-slate-500 dark:text-slate-400">Safety Score</span>
      </div>
      {riskFactors?.length > 0 && (
        <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">Key driver: {riskFactors.join(' + ')}</div>
      )}
      {hazardBreakdown && (
        <>
          <button
            onClick={() => setExpanded((v) => !v)}
            className={cx('mt-2 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide', tone.text)}
          >
            <ChevronDown size={12} className={cx('transition-transform', expanded && 'rotate-180')} />
            {expanded ? 'Hide' : 'View'} 4-Hazard Breakdown
          </button>
          {expanded && (
            <div className="mt-1.5 rounded-md border border-slate-200/80 dark:border-slate-800/80 bg-white/50 dark:bg-slate-950/40 px-2">
              {HAZARD_ORDER.map((key) => (
                <HazardBreakdownRow key={key} hazardKey={key} data={hazardBreakdown[key]} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// Small SVG sparkline of real Open-Meteo elevation samples along the chosen
// route, so the hill-terrain ML feature (elevation_gradient_pct) has a
// visible, at-a-glance counterpart in the UI.
function ElevationSparkline({ elevations, maxGradientPct, steepestSegmentIndex }) {
  if (!elevations || elevations.length < 2) return null;

  const w = 260;
  const h = 46;
  const pad = 4;
  const min = Math.min(...elevations);
  const max = Math.max(...elevations);
  const range = Math.max(max - min, 1);
  const points = elevations.map((e, i) => [
    pad + (i / (elevations.length - 1)) * (w - pad * 2),
    pad + (1 - (e - min) / range) * (h - pad * 2),
  ]);
  const linePath = points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const areaPath = `${linePath} L${points[points.length - 1][0].toFixed(1)},${h - pad} L${points[0][0].toFixed(1)},${h - pad} Z`;

  // Highlights exactly which stretch drives the max-gradient number, instead
  // of the chart only ever showing one aggregate figure for the whole route.
  const hasSteepMarker = Number.isInteger(steepestSegmentIndex) && steepestSegmentIndex >= 0 && steepestSegmentIndex < points.length - 1;
  const steepSegment = hasSteepMarker
    ? `M${points[steepestSegmentIndex][0].toFixed(1)},${points[steepestSegmentIndex][1].toFixed(1)} L${points[steepestSegmentIndex + 1][0].toFixed(1)},${points[steepestSegmentIndex + 1][1].toFixed(1)}`
    : null;
  const steepMidX = hasSteepMarker ? (points[steepestSegmentIndex][0] + points[steepestSegmentIndex + 1][0]) / 2 : null;
  const steepMidY = hasSteepMarker ? Math.min(points[steepestSegmentIndex][1], points[steepestSegmentIndex + 1][1]) : null;

  return (
    <div className="rounded-lg border border-slate-300/60 dark:border-slate-700/60 bg-white dark:bg-slate-900/40 p-2.5">
      <div className="mb-1.5 flex items-center justify-between text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400">
        <span className="flex items-center gap-1"><Mountain size={11} /> Elevation Profile</span>
        <span className="flex items-center gap-1 font-semibold text-amber-600 dark:text-amber-300">
          {hasSteepMarker && <AlertTriangle size={10} />} Max Gradient: {maxGradientPct.toFixed(1)}%
        </span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} className="h-11 w-full">
        <defs>
          <linearGradient id="elevFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.35" />
            <stop offset="100%" stopColor="#38bdf8" stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={areaPath} fill="url(#elevFill)" stroke="none" />
        <path d={linePath} fill="none" stroke="#38bdf8" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
        {steepSegment && <path d={steepSegment} fill="none" stroke="#f59e0b" strokeWidth={3} strokeLinecap="round" />}
        {hasSteepMarker && <circle cx={steepMidX} cy={steepMidY} r={2.5} fill="#f59e0b" />}
      </svg>
      <div className="mt-1 flex justify-between text-[9px] text-slate-500 dark:text-slate-500">
        <span>{Math.round(min)}m</span>
        {hasSteepMarker && <span className="text-amber-600 dark:text-amber-400">⚠ Steepest stretch marked</span>}
        <span>{Math.round(max)}m</span>
      </div>
    </div>
  );
}

function Modal({ onClose, children }) {
  return (
    <div
      className="absolute inset-0 z-50 flex items-end justify-center bg-white/85 dark:bg-slate-950/80 p-4 backdrop-blur-sm sm:items-center"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[340px] rounded-2xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 shadow-2xl"
      >
        {children}
      </div>
    </div>
  );
}

// One-time phone login (Twilio Verify, see backend/driver_auth.py) - gates
// the whole screen, not just the SOS button, so every action a driver takes
// here (SOS, route planning, hazard reports) is tied to a real, server-
// verified identity rather than the old hardcoded 'TRK-IN-0921'. Only the
// login itself needs connectivity - once logged in, the session token is
// read from localStorage on every request (see apiClient.js), so the app
// stays exactly as offline-capable as before for everything after this.
function DriverLoginGate({ onLoggedIn, notice }) {
  const [step, setStep] = useState('phone'); // 'phone' | 'code'
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  const digits = phone.replace(/\D/g, '');
  const e164 = `+91${digits}`;

  const sendCode = async () => {
    if (digits.length !== 10) { setError('Enter a 10-digit phone number.'); return; }
    setSubmitting(true);
    setError(null);
    try {
      const res = await apiFetch('/api/driver/login/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone_number: e164 }),
      });
      if (!res.ok) throw new Error('Could not send code. Check the number and try again.');
      setStep('code');
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const verifyCode = async () => {
    if (!code.trim()) { setError('Enter the code you received.'); return; }
    setSubmitting(true);
    setError(null);
    try {
      const res = await apiFetch('/api/driver/login/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone_number: e164, code: code.trim() }),
      });
      if (!res.ok) throw new Error('Incorrect or expired code.');
      const data = await res.json();
      onLoggedIn(data.token, data.phone_number);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-slate-50 dark:bg-slate-950 p-4">
      <div className="w-full max-w-[400px] rounded-[2rem] border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 p-6 shadow-2xl">
        <div className="mb-6 flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-sky-500/15 text-sky-600 dark:text-sky-400">
            <Phone size={22} />
          </div>
          <h1 className="text-sm font-bold text-slate-900 dark:text-slate-100">Driver Login</h1>
          <p className="text-[12px] text-slate-500 dark:text-slate-500">Verify your phone once — stays signed in until you sign out.</p>
        </div>

        {notice && (
          <div className="mb-4 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5 text-[11px] text-amber-600 dark:text-amber-300">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>{notice}</span>
          </div>
        )}

        {step === 'phone' ? (
          <div className="space-y-3">
            <label className="block text-[11px] font-medium text-slate-500 dark:text-slate-400">Phone Number</label>
            <div className="flex items-center gap-2">
              <span className="rounded-lg border border-slate-300 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/70 px-3 py-2.5 text-[13px] text-slate-500 dark:text-slate-400">+91</span>
              <input
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="98XXXXXXXX"
                inputMode="numeric"
                maxLength={10}
                className="w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white/80 dark:bg-slate-950/70 px-3 py-2.5 text-[13px] text-slate-800 dark:text-slate-200 placeholder:text-slate-600 dark:placeholder:text-slate-600 focus:border-sky-600 focus:outline-none"
              />
            </div>
            {error && <p className="text-[11px] text-red-600 dark:text-red-400">{error}</p>}
            <button
              onClick={sendCode}
              disabled={submitting}
              className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-sky-600 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-500 disabled:opacity-50"
            >
              {submitting ? <Loader2 size={15} className="animate-spin" /> : <Send size={14} />}
              {submitting ? 'Sending…' : 'Send Code'}
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            <label className="block text-[11px] font-medium text-slate-500 dark:text-slate-400">Code sent to +91 {digits}</label>
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="6-digit code"
              inputMode="numeric"
              className="w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white/80 dark:bg-slate-950/70 px-3 py-2.5 text-center text-[15px] tracking-[0.3em] text-slate-800 dark:text-slate-200 placeholder:text-slate-600 dark:placeholder:text-slate-600 placeholder:tracking-normal focus:border-sky-600 focus:outline-none"
            />
            {error && <p className="text-[11px] text-red-600 dark:text-red-400">{error}</p>}
            <button
              onClick={verifyCode}
              disabled={submitting}
              className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-sky-600 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-500 disabled:opacity-50"
            >
              {submitting ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={14} />}
              {submitting ? 'Verifying…' : 'Verify'}
            </button>
            <button onClick={() => { setStep('phone'); setCode(''); setError(null); }} className="w-full text-center text-[11px] text-slate-500 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300">
              Use a different number
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function LocationField({ label, value, onChange, onSubmit, onChip, activeName, suggestions, onSelectSuggestion }) {
  const [focused, setFocused] = useState(false);
  const showSuggestions = focused && suggestions.length > 0;

  return (
    <div className="relative">
      <label className="mb-1 block text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">{label}</label>
      <div className="flex items-center gap-1.5 rounded-lg border border-slate-300 dark:border-slate-700 bg-white/80 dark:bg-slate-950/70 px-2.5 py-1.5">
        <MapPin size={13} className="shrink-0 text-slate-500 dark:text-slate-500" />
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onSubmit();
          }}
          placeholder="Search any place in India…"
          className="w-full bg-transparent text-[13px] text-slate-800 dark:text-slate-200 placeholder:text-slate-600 dark:placeholder:text-slate-600 focus:outline-none"
        />
      </div>
      {showSuggestions && (
        <div className="absolute inset-x-0 top-full z-20 mt-1 max-h-48 overflow-y-auto rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-xl">
          {suggestions.map((s, i) => (
            <button
              key={`${s.lat},${s.lon}-${i}`}
              // onMouseDown (not onClick) + preventDefault: fires BEFORE the
              // input's blur, so selecting a suggestion never races against
              // the dropdown closing first - the old approach (a 150ms
              // setTimeout delaying blur) was a guess at "long enough",
              // which is exactly why it sometimes failed to register.
              onMouseDown={(e) => {
                e.preventDefault();
                onSelectSuggestion(s);
              }}
              className="block w-full truncate px-2.5 py-1.5 text-left text-[12px] text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800"
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {HUBS.map((hub) => (
          <button
            key={hub.key}
            onClick={() => onChip(hub)}
            className={cx(
              'rounded-full border px-2 py-0.5 text-[10px] font-medium transition',
              activeName === hub.name
                ? 'border-sky-500/50 bg-sky-500/15 text-sky-600 dark:text-sky-300'
                : 'border-slate-300 dark:border-slate-700 text-slate-500 dark:text-slate-400 hover:border-slate-300 dark:hover:border-slate-600 hover:text-slate-800 dark:hover:text-slate-200'
            )}
          >
            {hub.name}
          </button>
        ))}
      </div>
    </div>
  );
}

// Compact SVG polyline preview so a driver-reported hazard's effect on the
// route (visible detour, or unchanged path when clear) is actually visible,
// not just implied by the distance/ETA numbers above it.
function RoutePreviewMap({ coordinates, hazards, hazardDetected }) {
  if (!coordinates || coordinates.length < 2) return null;
  const W = 300;
  const H = 120;
  const lats = coordinates.map((c) => c[0]);
  const lngs = coordinates.map((c) => c[1]);
  const latSpan = Math.max(...lats) - Math.min(...lats) || 0.1;
  const lngSpan = Math.max(...lngs) - Math.min(...lngs) || 0.1;
  const minLat = Math.min(...lats) - latSpan * 0.1;
  const maxLat = Math.max(...lats) + latSpan * 0.1;
  const minLng = Math.min(...lngs) - lngSpan * 0.1;
  const maxLng = Math.max(...lngs) + lngSpan * 0.1;
  const project = (lat, lng) => ({
    x: ((lng - minLng) / (maxLng - minLng)) * W,
    y: ((maxLat - lat) / (maxLat - minLat)) * H,
  });
  const points = coordinates
    .map(([lat, lng]) => {
      const p = project(lat, lng);
      return `${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    })
    .join(' ');

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-[100px] w-full rounded-lg border border-slate-200 dark:border-slate-800 bg-white/80 dark:bg-slate-950/70">
      <polyline
        points={points}
        fill="none"
        stroke={hazardDetected ? '#ef4444' : '#22c55e'}
        strokeWidth={3}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {hazards.map((hz, i) => {
        const p = project(hz.lat, hz.lng);
        return <circle key={i} cx={p.x} cy={p.y} r={4.5} fill="#ef4444" stroke="#0b1220" strokeWidth={1.5} />;
      })}
    </svg>
  );
}

export default function DriverView({ onTriggerSOS }) {
  const [driverSession, setDriverSessionState] = useState(() => getDriverSession());
  const [sessionExpiredNotice, setSessionExpiredNotice] = useState(null);
  const [pendingCount, setPendingCount] = useState(() => getPendingCount());
  const [online, setOnline] = useState(true);
  const [activeModal, setActiveModal] = useState(null); // 'sos-online' | 'sos-offline' | 'sos-report' | 'obstacle' | null
  const [holding, setHolding] = useState(false);
  const [holdProgress, setHoldProgress] = useState(0);
  const [smsSent, setSmsSent] = useState(false);
  const [obstacleType, setObstacleType] = useState('Landslide');
  const [obstacleDescription, setObstacleDescription] = useState('');
  const [obstacleSubmitting, setObstacleSubmitting] = useState(false);
  const [photoName, setPhotoName] = useState(null);
  const [reportSubmitted, setReportSubmitted] = useState(false);
  const [reportOutcome, setReportOutcome] = useState(null); // { isNew, confirmations } — from the backend's merge-or-create response
  const [reportQueued, setReportQueued] = useState(false); // true if the real send failed and got queued for retry, not actually delivered yet
  const [toast, setToast] = useState(null);
  const [lastDispatch, setLastDispatch] = useState(null); // { lat, lng, category, severity, note }
  const [hazardMarkers, setHazardMarkers] = useState([]); // driver-reported hazards, this session
  const [zoneWarning, setZoneWarning] = useState(null); // { hazard, level, score } - live "entering a high-risk area" popup
  const [risingWarning, setRisingWarning] = useState(null); // { hazard, projectedScore } - "conditions worsening nearby" popup, distinct from zoneWarning (not HIGH yet, but trending that way)
  const zoneWatchRef = useRef({
    lastCheckedAt: 0,
    high: { lastAlertedKey: null, lastAlertedAt: 0 },
    rising: { lastAlertedKey: null, lastAlertedAt: 0 },
  });

  const [incidentType, setIncidentType] = useState(null);
  const [incidentSeverity, setIncidentSeverity] = useState('Critical');
  const [incidentNote, setIncidentNote] = useState('');

  const [sourceInput, setSourceInput] = useState('Guwahati');
  const [destInput, setDestInput] = useState('Silchar');
  // The resolved {lat, lng, name} behind each text field - what actually gets
  // routed on. Kept separate from the display text so a real geocoded search
  // result (or a chip click) can set a precise coordinate without needing to
  // re-parse the text later.
  const [sourceCoords, setSourceCoords] = useState({ lat: 26.1445, lng: 91.7362, name: 'Guwahati' });
  const [destCoords, setDestCoords] = useState({ lat: 24.8333, lng: 92.7789, name: 'Silchar' });
  const sourceSuggestions = useGeocodeSuggestions(sourceInput);
  const destSuggestions = useGeocodeSuggestions(destInput);
  const [routeResult, setRouteResult] = useState(null);
  const [routeLoading, setRouteLoading] = useState(false);
  const [routeError, setRouteError] = useState(null);

  const holdStartRef = useRef(null);
  const rafRef = useRef(null);
  const toastTimer = useRef(null);
  const plannerAbortRef = useRef(null);
  const plannerTokenRef = useRef(0);
  const holdCompletedRef = useRef(false);
  const holdCompletedResetTimer = useRef(null);

  useEffect(
    () => () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      if (toastTimer.current) clearTimeout(toastTimer.current);
      if (holdCompletedResetTimer.current) clearTimeout(holdCompletedResetTimer.current);
      if (plannerAbortRef.current) plannerAbortRef.current.abort();
    },
    []
  );

  // Live count of SOS/hazard reports queued for retry (see offlineQueue.js)
  // - toasts a confirmation the moment the count actually drops, i.e. a
  // queued item just got through, not just "the button was tapped".
  useEffect(() => {
    const unsubscribe = onQueueChange((count) => {
      setPendingCount((prev) => {
        if (count < prev) {
          setToast('Queued report sent successfully');
          if (toastTimer.current) clearTimeout(toastTimer.current);
          toastTimer.current = setTimeout(() => setToast(null), 2800);
        }
        return count;
      });
    });
    return unsubscribe;
  }, []);

  // A queued SOS/report can fail forever with a dead session token (see
  // dispatchSos/offlineQueue.js - most commonly after the backend's
  // database gets wiped by a redeploy, since Render's free tier has no
  // persistent disk, orphaning every token issued before it). That code
  // clears the stored token and fires this event instead of retrying
  // blindly - drop back to the login gate so the driver visibly has to
  // re-verify, rather than the app silently sitting "logged in" while
  // every real send 401s. The queued report itself is untouched and will
  // send automatically on the next retry once they're logged in again.
  useEffect(() => {
    const onExpired = () => {
      setDriverSessionState(null);
      setSessionExpiredNotice('Your session expired, likely after a server update. Please log in again — your queued report will send automatically once you do.');
    };
    window.addEventListener('driver-session-expired', onExpired);
    return () => window.removeEventListener('driver-session-expired', onExpired);
  }, []);

  // Passive "entering a high-risk area" watch - runs the whole time this
  // screen is open, independent of whether a route is planned. Continuous
  // GPS (watchPosition, not the one-shot getCurrentPosition SOS/hazard-report
  // use) throttled to a real hazard-check call at most every 2 minutes -
  // checking on every GPS tick would be both wasteful and, since a live
  // weather lookup takes a moment, way more often than the conditions this
  // is checking (rainfall, river discharge) actually change. A zone already
  // warned about doesn't re-pop for 15 minutes, so sitting still in one
  // high-risk spot doesn't spam the same popup over and over.
  useEffect(() => {
    if (!navigator.geolocation) return undefined;
    const CHECK_INTERVAL_MS = 120000;
    const REALERT_INTERVAL_MS = 900000;

    const checkZone = async (lat, lng) => {
      try {
        const res = await apiFetch(`/api/hazard-check?lat=${lat}&lon=${lng}`);
        if (!res.ok) return;
        const data = await res.json();
        const now = Date.now();

        if (typeof data.overall_risk_level === 'string' && data.overall_risk_level.startsWith('HIGH_')) {
          const key = data.primary_hazard;
          const last = zoneWatchRef.current.high;
          if (key === last.lastAlertedKey && now - last.lastAlertedAt < REALERT_INTERVAL_MS) return;
          zoneWatchRef.current = { ...zoneWatchRef.current, high: { lastAlertedKey: key, lastAlertedAt: now } };
          setZoneWarning({ hazard: key, level: data.overall_risk_level, score: data.overall_risk_score });
          return;
        }

        // Not HIGH right now - but is any weather-driven hazard forecast to
        // get there? Surfaces the worst (lowest projected score) RISING
        // hazard, same re-alert throttle pattern as the HIGH warning above,
        // tracked independently so a RISING flood warning can't get
        // suppressed by an unrelated recent HIGH landslide alert or vice
        // versa. See multi_hazard.evaluate_point_with_trend on the backend.
        const rising = Object.entries(data.hazard_breakdown || {})
          .filter(([, entry]) => entry.trend === 'RISING')
          .sort((a, b) => a[1].projected_safety_score - b[1].projected_safety_score)[0];
        if (!rising) return;
        const [hazard, entry] = rising;
        const last = zoneWatchRef.current.rising;
        if (hazard === last.lastAlertedKey && now - last.lastAlertedAt < REALERT_INTERVAL_MS) return;
        zoneWatchRef.current = { ...zoneWatchRef.current, rising: { lastAlertedKey: hazard, lastAlertedAt: now } };
        setRisingWarning({ hazard, projectedScore: entry.projected_safety_score });
      } catch (e) {
        // offline or request failed - just skip this check, next GPS tick retries
      }
    };

    const watchId = navigator.geolocation.watchPosition(
      (pos) => {
        const now = Date.now();
        if (now - zoneWatchRef.current.lastCheckedAt < CHECK_INTERVAL_MS) return;
        zoneWatchRef.current = { ...zoneWatchRef.current, lastCheckedAt: now };
        checkZone(pos.coords.latitude, pos.coords.longitude);
      },
      () => {}, // ignore errors - this is a passive background feature, not a user-initiated action
      { enableHighAccuracy: false, maximumAge: 60000 }
    );
    return () => navigator.geolocation.clearWatch(watchId);
  }, []);

  const runPlanner = async (src, dest, offline) => {
    if (!src || !dest) {
      setRouteError('Search and select both an origin and destination.');
      return;
    }
    setRouteError(null);
    setRouteLoading(true);

    const token = ++plannerTokenRef.current;
    if (plannerAbortRef.current) plannerAbortRef.current.abort();

    if (offline) {
      setRouteResult(computeCachedRoute(src, dest));
      setRouteLoading(false);
      return;
    }

    const controller = new AbortController();
    plannerAbortRef.current = controller;
    const timeoutId = setTimeout(() => controller.abort(), ROUTE_TIMEOUT_MS);

    try {
      const feature = await fetchTomTomRoute(src, dest, controller.signal);
      const { properties, geometry } = feature;
      const coordinates = geometry.coordinates.map(([lon, lat]) => [lat, lon]);
      const rerouted = properties.hazard_detected && properties.rerouted;
      const primary = {
        distanceKm: properties.distance_km,
        durationHrs: properties.travel_time_minutes / 60,
        delayMinutes: properties.traffic_delay_minutes,
        congestionLevel: properties.congestion_level,
        hazard: properties.hazard_detected && !properties.rerouted,
        aiSafetyScore: properties.ai_safety_score,
        aiRiskLevel: properties.ai_risk_level,
        riskFactors: properties.risk_factors,
        riskSegment: properties.risk_segment,
        primaryHazard: properties.primary_hazard,
        hazardBreakdown: properties.hazard_breakdown,
        aiRouteLabel: properties.ai_route_label,
        elevationProfile: properties.elevation_profile,
        maxGradientPct: properties.max_gradient_pct,
        steepestSegmentIndex: properties.steepest_segment_index,
      };

      if (token === plannerTokenRef.current) {
        setRouteResult({ source: 'live', primary, recommended: null, rerouted, coordinates });
      }
    } catch (e) {
      // Network failure or timeout: degrade gracefully to a local offline estimate
      if (token === plannerTokenRef.current) {
        setRouteResult(computeCachedRoute(src, dest));
      }
    } finally {
      clearTimeout(timeoutId);
      if (token === plannerTokenRef.current) setRouteLoading(false);
    }
  };

  const handlePlanRoute = async () => {
    const [src, dest] = await Promise.all([
      resolvePlace(sourceInput, sourceCoords, sourceSuggestions),
      resolvePlace(destInput, destCoords, destSuggestions),
    ]);
    // A name like "Ayodhya" can match multiple real places (TomTom's own
    // top-ranked result for it is a small Andhra Pradesh village, not the
    // well-known UP city) - auto-resolving without ever showing which one
    // was picked would risk silently routing to the wrong place. Writing
    // the full resolved name back into the field makes the pick visible so
    // it can be caught and corrected before relying on it.
    if (src) {
      setSourceCoords(src);
      setSourceInput(src.name);
    }
    if (dest) {
      setDestCoords(dest);
      setDestInput(dest.name);
    }
    runPlanner(src, dest, !online);
  };

  const selectSourceHub = (hub) => {
    setSourceInput(hub.name);
    setSourceCoords({ lat: hub.lat, lng: hub.lng, name: hub.name });
    setRouteError(null);
  };

  const selectDestHub = (hub) => {
    setDestInput(hub.name);
    setDestCoords({ lat: hub.lat, lng: hub.lng, name: hub.name });
    setRouteError(null);
  };

  const selectSourcePlace = (place) => {
    setSourceInput(place.name);
    setSourceCoords({ lat: place.lat, lng: place.lon, name: place.name });
    setRouteError(null);
  };

  const selectDestPlace = (place) => {
    setDestInput(place.name);
    setDestCoords({ lat: place.lat, lng: place.lon, name: place.name });
    setRouteError(null);
  };

  // Typing a fresh search invalidates whatever place was previously resolved,
  // so stale coordinates from an earlier selection can't silently get routed.
  const handleSourceInputChange = (text) => {
    setSourceInput(text);
    setSourceCoords(null);
  };

  const handleDestInputChange = (text) => {
    setDestInput(text);
    setDestCoords(null);
  };

  // Deletes the session row server-side (see /api/driver/logout) so the old
  // token can't keep being used after sign-out, not just something this
  // device forgets locally - then drops back to the login gate.
  const signOut = () => {
    apiFetch('/api/driver/logout', { method: 'POST' }).catch(() => {});
    clearDriverSession();
    setDriverSessionState(null);
  };

  const toggleOnline = () => {
    setOnline((o) => {
      const next = !o;
      if (routeResult) runPlanner(sourceCoords, destCoords, !next);
      return next;
    });
  };

  // Shared dispatch path for both Instant SOS (long-press) and the Detailed
  // Incident Report modal — both capture live GPS and POST to /api/sos.
  const dispatchIncident = async ({ category, severity, note, source }) => {
    const isOffline = !online;
    // Immediate feedback the instant the hold completes/tap registers -
    // without this, the gap between releasing the button and the real
    // dispatchSos response landing (which on the deployed Render free tier
    // can genuinely take several seconds, sometimes 10+) showed nothing at
    // all on screen, reading exactly like "the SOS button does nothing."
    // Fixing the earlier false-success bug (showing success before the
    // send was even attempted) accidentally introduced this - honesty
    // about the eventual result doesn't have to mean silence in the
    // meantime.
    setActiveModal(isOffline ? 'sos-offline' : 'sos-sending');

    const { lat, lng, anchorLabel } = await getSosCoords();
    setLastDispatch({ lat, lng, category, severity, note });

    if (isOffline) {
      onTriggerSOS?.({
        id: `SOS-${Date.now().toString().slice(-4)}`,
        type: 'sos',
        source: 'SMS FALLBACK ALERT',
        offline: true, // local-only display item (Dashboard renders it from this alone) - the real send below is separate and queued, not tied to this flag
        category,
        severity,
        note,
        cargo: `${severity}: ${category}`,
        vehicle: 'Ambulance',
        lat,
        lng,
        locationName: anchorLabel,
        location: `${lat.toFixed(4)}° N, ${lng.toFixed(4)}° E`,
        description: note || `${category} — SMS fallback relay, no cellular network.`,
        time: 'Just now',
      });
      // dispatchSos queues this for real delivery on failure (see
      // offlineQueue.js) instead of the report just vanishing once this
      // toast disappears - this used to be a purely local simulation with
      // no real send attempted at all.
      dispatchSos(lat, lng, { incidentType: category, severity, notes: note, mode: source }).catch(() => {});
      setToast('SOS queued — will send automatically once signal returns');
    } else {
      // Success is shown only once the send is actually confirmed - this
      // used to show "Instant SOS Dispatched" unconditionally before even
      // attempting the send, so a real failure (expired session, dropped
      // connection, a cold-starting backend) looked identical to success
      // and gave the driver zero warning that dispatchSos below had queued
      // it for silent background retry instead.
      let delivered = false;
      try {
        await dispatchSos(lat, lng, { incidentType: category, severity, notes: note, mode: source });
        delivered = true;
      } catch (err) {
        console.warn('SOS backend dispatch failed, queued for automatic retry:', err);
      }
      setActiveModal(delivered ? 'sos-online' : 'sos-online-failed');

      onTriggerSOS?.({
        id: `SOS-${Date.now().toString().slice(-4)}`,
        type: 'sos',
        source: source === 'instant' ? 'LIVE ONLINE' : 'ROUTE INCIDENT REPORT',
        category,
        severity,
        note,
        cargo: `${severity}: ${category}`,
        vehicle: 'Ambulance',
        lat,
        lng,
        locationName: anchorLabel || 'Live GPS Distress Beacon',
        location: `${lat.toFixed(4)}° N, ${lng.toFixed(4)}° E`,
        description:
          source === 'instant'
            ? 'Emergency SOS triggered from mobile terminal. Immediate Panic / Unknown Distress protocol activated.'
            : `${category} reported along the active route. Severity: ${severity}.`,
        time: 'Just now',
      });
      setToast(
        delivered
          ? `🚨 Dispatched! Live GPS: [${lat.toFixed(4)}, ${lng.toFixed(4)}] transmitted to emergency network`
          : 'Could not confirm delivery — queued, retrying automatically'
      );
    }

    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2800);
  };

  const triggerInstantSos = () => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    setHolding(false);
    setHoldProgress(0);
    holdCompletedRef.current = true;
    // The confirmation modal opens immediately and can cover the button before
    // the finger actually lifts, which sometimes swallows the trailing browser
    // "click" event that would otherwise clear this flag — reset it on a timer
    // too so a stuck flag can never eat the user's next tap.
    if (holdCompletedResetTimer.current) clearTimeout(holdCompletedResetTimer.current);
    holdCompletedResetTimer.current = setTimeout(() => {
      holdCompletedRef.current = false;
    }, 500);
    dispatchIncident({ category: INSTANT_SOS_CATEGORY, severity: 'Critical', note: '', source: 'instant' });
  };

  const startHold = () => {
    if (activeModal) return;
    holdStartRef.current = performance.now();
    setHolding(true);
    const tick = (t) => {
      const elapsed = t - holdStartRef.current;
      const pct = Math.min(100, (elapsed / HOLD_MS) * 100);
      setHoldProgress(pct);
      if (pct >= 100) {
        triggerInstantSos();
        return;
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  };

  const cancelHold = () => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    setHolding(false);
    setHoldProgress(0);
  };

  // A quick tap (released before the hold completes) opens the detailed
  // report modal instead — the click event still fires after a completed
  // long-press, so holdCompletedRef suppresses that duplicate trigger.
  const handleSosClick = () => {
    if (holdCompletedRef.current) {
      holdCompletedRef.current = false;
      return;
    }
    if (activeModal) return;
    setActiveModal('sos-report');
  };

  const dispatchIncidentReport = () => {
    if (!incidentType) return;
    dispatchIncident({ category: incidentType, severity: incidentSeverity, note: incidentNote.trim(), source: 'detailed' });
  };

  // Reports a hazard with live GPS to the backend, then re-plans the current
  // route so the polyline can detour around it (ACTIVE_HAZARDS in routing.py).
  const submitObstacleReport = async () => {
    setObstacleSubmitting(true);
    setReportQueued(false);
    try {
      const { lat, lng } = await getSosCoords();
      const requestOptions = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ latitude: lat, longitude: lng, type: obstacleType, description: obstacleDescription.trim() }),
      };
      try {
        const res = await apiFetch('/api/alerts', requestOptions);
        if (!res.ok) throw new Error(`Hazard report failed (${res.status})`);
        const result = await res.json();
        setReportOutcome({ isNew: result.is_new, confirmations: result.confirmations });
        setHazardMarkers((prev) => [...prev, { lat, lng, type: obstacleType }]);
        runPlanner(sourceCoords, destCoords, !online);
      } catch (err) {
        // A real failure queues this for retry instead of silently
        // dropping it while the UI still claimed success (see offlineQueue.js).
        queuePendingRequest('/api/alerts', requestOptions);
        setReportQueued(true);
        console.warn('Hazard report failed, queued for retry:', err);
      }
    } finally {
      setObstacleSubmitting(false);
      setReportSubmitted(true);
    }
  };

  const closeModal = () => {
    setActiveModal(null);
    setSmsSent(false);
    setReportSubmitted(false);
    setReportOutcome(null);
    setReportQueued(false);
    setPhotoName(null);
    setObstacleType('Landslide');
    setObstacleDescription('');
    setIncidentType(null);
    setIncidentSeverity('Critical');
    setIncidentNote('');
  };

  if (!driverSession) {
    return (
      <DriverLoginGate
        notice={sessionExpiredNotice}
        onLoggedIn={(token, phone) => {
          setDriverSession(token, phone);
          setDriverSessionState({ token, phone });
          setSessionExpiredNotice(null);
        }}
      />
    );
  }

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-slate-50 dark:bg-slate-950 p-4">
      <div className="relative flex h-[800px] w-full max-w-[400px] flex-col overflow-y-auto overflow-x-hidden rounded-[2rem] border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 shadow-2xl">
        {toast && (
          <div className="pointer-events-none absolute left-1/2 top-4 z-[60] w-[calc(100%-2rem)] max-w-[320px] -translate-x-1/2 rounded-lg border border-emerald-500/40 bg-white dark:bg-slate-900/95 px-3 py-2.5 text-center text-xs font-semibold text-emerald-600 dark:text-emerald-300 shadow-xl backdrop-blur">
            <CheckCircle2 size={14} className="-mt-0.5 mr-1 inline" />
            {toast}
          </div>
        )}
        {/* Header */}
        <header className="shrink-0 space-y-2.5 border-b border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 px-4 py-3">
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-1.5 text-slate-900 dark:text-slate-100">
              <Truck size={16} className="shrink-0 text-sky-600 dark:text-sky-400" />
              <span className="truncate text-sm font-bold tracking-wide">{driverSession.phone}</span>
              <button onClick={signOut} title="Sign out" className="ml-1 shrink-0 text-slate-500 dark:text-slate-500 transition hover:text-red-600 dark:hover:text-red-400">
                <LogOut size={14} />
              </button>
            </div>
            <button
              onClick={toggleOnline}
              className={cx(
                'flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wide transition',
                online
                  ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                  : 'border-red-500/40 bg-red-500/15 text-red-600 dark:text-red-400'
              )}
            >
              {online ? <Wifi size={12} /> : <WifiOff size={12} />}
              {online ? '4G Online' : 'Zero Network'}
            </button>
          </div>

          {pendingCount > 0 && (
            <div className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/30 bg-amber-500/10 px-2.5 py-1 text-[11px] font-semibold text-amber-600 dark:text-amber-300">
              <Clock size={12} />
              {pendingCount} report{pendingCount === 1 ? '' : 's'} queued — sending when signal returns
            </div>
          )}
        </header>

        {/* Navigation card */}
        <section className="mx-4 mt-4 shrink-0 space-y-3 rounded-xl border border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 p-3.5">
          <LocationField
            label="Origin"
            value={sourceInput}
            onChange={handleSourceInputChange}
            onSubmit={handlePlanRoute}
            onChip={selectSourceHub}
            activeName={sourceCoords?.name}
            suggestions={sourceSuggestions}
            onSelectSuggestion={selectSourcePlace}
          />
          <div className="flex justify-center">
            <ArrowRight size={14} className="rotate-90 text-slate-600 dark:text-slate-600" />
          </div>
          <LocationField
            label="Destination"
            value={destInput}
            onChange={handleDestInputChange}
            onSubmit={handlePlanRoute}
            onChip={selectDestHub}
            activeName={destCoords?.name}
            suggestions={destSuggestions}
            onSelectSuggestion={selectDestPlace}
          />

          <button
            onClick={handlePlanRoute}
            disabled={routeLoading}
            className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-sky-600/40 bg-sky-600/15 py-2 text-xs font-semibold text-sky-600 dark:text-sky-300 transition hover:bg-sky-600/25 disabled:opacity-60"
          >
            {routeLoading ? <Loader2 size={13} className="animate-spin" /> : <Navigation size={13} />}
            {routeLoading ? 'Calculating Route…' : 'Plan Route'}
          </button>

          {routeError && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-2 text-[11px] text-amber-600 dark:text-amber-300">
              {routeError}
            </div>
          )}

          {routeResult && (() => {
            const banner = overallRouteBanner(routeResult.primary, routeResult.rerouted);
            const tone = BANNER_TONE[banner.tone];
            const seg = routeResult.primary.riskSegment;
            return (
            <>
              <div className={cx('rounded-lg border p-2.5', tone.border)}>
                <div className="flex items-start gap-2">
                  <banner.Icon size={15} className={cx('mt-0.5 shrink-0', tone.icon)} />
                  <div className={cx('text-[12px] leading-snug', tone.text)}>
                    <div className="font-semibold">{banner.title}</div>
                    <div>
                      {formatKm(routeResult.primary.distanceKm)} • {formatDuration(routeResult.primary.durationHrs)} ETA
                      {routeResult.source === 'live' &&
                        ` • ${Math.round(routeResult.primary.delayMinutes)}m traffic delay (${routeResult.primary.congestionLevel})`}
                    </div>
                    {seg && (
                      <div className="mt-1 flex items-center gap-1 text-[11px] opacity-90">
                        <MapPin size={11} className="shrink-0" />
                        Risk concentrated ~{Math.round(seg.km_from_origin)}km into the route
                        {seg.fraction <= 0.15 ? ' (near origin)' : seg.fraction >= 0.85 ? ' (near destination)' : ''}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              <AiCorridorRiskCard
                safetyScore={routeResult.primary.aiSafetyScore}
                riskLevel={routeResult.primary.aiRiskLevel}
                riskFactors={routeResult.primary.riskFactors}
                hazardBreakdown={routeResult.primary.hazardBreakdown}
              />

              {routeResult.primary.elevationProfile && (
                <ElevationSparkline
                  elevations={routeResult.primary.elevationProfile}
                  maxGradientPct={routeResult.primary.maxGradientPct}
                  steepestSegmentIndex={routeResult.primary.steepestSegmentIndex}
                />
              )}

              {routeResult.coordinates && (
                <RoutePreviewMap
                  coordinates={routeResult.coordinates}
                  hazards={hazardMarkers}
                  hazardDetected={routeResult.primary.hazard}
                />
              )}

              <div className="text-right text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">
                {routeResult.source === 'live' ? 'Live TomTom traffic routing' : 'Offline cached estimate'}
              </div>
            </>
            );
          })()}
        </section>

        {/* SOS button */}
        <section className="flex flex-1 flex-col items-center justify-center gap-3 px-4">
          <div className="relative flex h-[140px] w-[140px] items-center justify-center">
            {!holding && (
              <span className="absolute inset-2 animate-ping rounded-full bg-red-600/40" />
            )}
            <svg width={140} height={140} viewBox="0 0 140 140" className="absolute inset-0 -rotate-90">
              <circle cx={70} cy={70} r={RING_R} stroke="#1e293b" strokeWidth={6} fill="none" />
              <circle
                cx={70}
                cy={70}
                r={RING_R}
                stroke="#ef4444"
                strokeWidth={6}
                fill="none"
                strokeLinecap="round"
                strokeDasharray={RING_CIRC}
                strokeDashoffset={RING_CIRC - (holdProgress / 100) * RING_CIRC}
                style={{ transition: holding ? 'none' : 'stroke-dashoffset 0.2s ease-out' }}
              />
            </svg>
            <button
              onPointerDown={startHold}
              onPointerUp={cancelHold}
              onPointerLeave={cancelHold}
              onPointerCancel={cancelHold}
              onClick={handleSosClick}
              onContextMenu={(e) => e.preventDefault()}
              style={{ touchAction: 'none' }}
              className={cx(
                'relative z-10 flex h-[108px] w-[108px] select-none flex-col items-center justify-center gap-1 rounded-full bg-gradient-to-b from-red-500 to-red-700 text-white shadow-[0_0_35px_rgba(239,68,68,0.55)] transition-transform',
                holding && 'scale-95 from-red-400 to-red-600'
              )}
            >
              <Siren size={26} strokeWidth={2.3} />
              <span className="text-[11px] font-bold leading-tight tracking-wide">
                {holding ? `${Math.round(holdProgress)}%` : 'EMERGENCY'}
              </span>
              <span className="text-[9px] font-semibold tracking-wide opacity-90">
                {holding ? 'HOLD…' : 'SOS'}
              </span>
            </button>
          </div>
          <p className="text-[11px] text-slate-500 dark:text-slate-500">
            Tap to report an incident · Hold {(HOLD_MS / 1000).toFixed(1)}s for instant SOS
          </p>
        </section>

        {/* Ground reporting */}
        <div className="shrink-0 border-t border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 p-4">
          <button
            onClick={() => setActiveModal('obstacle')}
            className="flex w-full items-center justify-center gap-2 rounded-xl border border-slate-300 dark:border-slate-700 bg-slate-100/80 dark:bg-slate-800/70 py-3 text-sm font-medium text-slate-800 dark:text-slate-200 transition hover:bg-slate-100 dark:hover:bg-slate-800 active:scale-[0.98]"
          >
            <AlertTriangle size={16} className="text-amber-600 dark:text-amber-400" />
            Report Road Obstacle
          </button>
        </div>

        {/* Immediate feedback while the real send is in flight - see
            dispatchIncident. Not dismissible (no-op onClose) so a driver
            can't tap SOS again mid-send and fire a duplicate report while
            waiting on a slow response. */}
        {activeModal === 'sos-sending' && (
          <Modal onClose={() => {}}>
            <div className="space-y-3 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-sky-500/15">
                <Loader2 size={26} className="animate-spin text-sky-600 dark:text-sky-400" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">Sending SOS…</h3>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Transmitting your location to the emergency network. This can take a few seconds.</p>
              </div>
            </div>
          </Modal>
        )}

        {/* SOS Online modal */}
        {activeModal === 'sos-online' && (
          <Modal onClose={closeModal}>
            <div className="space-y-3 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-emerald-500/15">
                <CheckCircle2 size={26} className="text-emerald-600 dark:text-emerald-400" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">
                  {lastDispatch?.category === INSTANT_SOS_CATEGORY ? 'Instant SOS Dispatched' : 'Incident Report Dispatched'}
                </h3>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">Dispatch and the nearest response unit have been notified in real time.</p>
              </div>
              {lastDispatch && (
                <div className="flex flex-wrap justify-center gap-1.5">
                  <span className="rounded-full border border-slate-300 dark:border-slate-700 bg-slate-100/80 dark:bg-slate-800/70 px-2 py-0.5 text-[10px] font-medium text-slate-600 dark:text-slate-300">
                    {lastDispatch.category}
                  </span>
                  <span
                    className={cx(
                      'rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide',
                      SEVERITY_LEVELS.find((s) => s.key === lastDispatch.severity)?.cls
                    )}
                  >
                    {lastDispatch.severity}
                  </span>
                </div>
              )}
              {lastDispatch?.note && (
                <p className="text-left text-[11px] italic text-slate-500 dark:text-slate-400">"{lastDispatch.note}"</p>
              )}
              <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-white/70 dark:bg-slate-950/60 p-2.5 text-left font-mono text-[11px] text-slate-600 dark:text-slate-300">
                Lat: {(lastDispatch?.lat ?? SOS_LAT).toFixed(4)}, Lng: {(lastDispatch?.lng ?? SOS_LNG).toFixed(4)}
              </div>
              <button
                onClick={closeModal}
                className="w-full rounded-lg bg-emerald-600 py-2.5 text-sm font-semibold text-white transition hover:bg-emerald-500"
              >
                Acknowledge
              </button>
            </div>
          </Modal>
        )}

        {/* SOS dispatch failed (real send failed despite "4G Online" -
            expired session, dropped connection, backend unreachable, etc).
            Honest about the failure, distinct from the offline/SMS-fallback
            modal since this is a genuine send failure, not a deliberate
            no-signal simulation - the request is already queued (see
            dispatchSos/offlineQueue.js) and will retry automatically. */}
        {activeModal === 'sos-online-failed' && (
          <Modal onClose={closeModal}>
            <div className="space-y-3 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-amber-500/15">
                <AlertTriangle size={26} className="text-amber-600 dark:text-amber-400" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">Couldn't Confirm Delivery</h3>
                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                  Your report is queued and will send automatically the moment a connection succeeds — it has not been lost.
                </p>
              </div>
              {pendingCount > 0 && (
                <p className="text-[11px] text-amber-600 dark:text-amber-300">{pendingCount} report{pendingCount > 1 ? 's' : ''} pending delivery</p>
              )}
              <button
                onClick={closeModal}
                className="w-full rounded-lg bg-amber-600 py-2.5 text-sm font-semibold text-white transition hover:bg-amber-500"
              >
                Acknowledge
              </button>
            </div>
          </Modal>
        )}

        {/* SOS Offline modal */}
        {activeModal === 'sos-offline' && (
          <Modal onClose={closeModal}>
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-amber-500/15">
                  <WifiOff size={20} className="text-amber-600 dark:text-amber-400" />
                </div>
                <div>
                  <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">No Cellular Data Detected</h3>
                  <p className="text-[11px] text-slate-500 dark:text-slate-400">Falling back to compressed SMS relay</p>
                </div>
              </div>
              <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-white/80 dark:bg-slate-950/70 p-2.5">
                <div className="mb-1 text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">SMS Payload</div>
                <code className="block break-all font-mono text-[12px] text-amber-600 dark:text-amber-300">
                  SOS|{(lastDispatch?.lat ?? SOS_LAT).toFixed(4)},{(lastDispatch?.lng ?? SOS_LNG).toFixed(4)}|
                  {toSmsCode(lastDispatch?.severity)}|{toSmsCode(lastDispatch?.category)}
                </code>
              </div>
              {!smsSent ? (
                <button
                  onClick={() => setSmsSent(true)}
                  className="flex w-full items-center justify-center gap-2 rounded-lg bg-amber-600 py-2.5 text-sm font-semibold text-white transition hover:bg-amber-500"
                >
                  <Send size={15} /> Send SMS Fallback via Native Carrier
                </button>
              ) : (
                <div className="flex items-center justify-center gap-2 rounded-lg bg-emerald-500/15 py-2.5 text-sm font-semibold text-emerald-600 dark:text-emerald-400">
                  <CheckCircle2 size={16} /> Dispatched via SMS Gateway
                </div>
              )}
              <button
                onClick={closeModal}
                className="w-full rounded-lg border border-slate-300 dark:border-slate-700 py-2 text-xs font-medium text-slate-500 dark:text-slate-400 transition hover:text-slate-800 dark:hover:text-slate-200"
              >
                Close
              </button>
            </div>
          </Modal>
        )}

        {/* Detailed route incident report modal (Option B) */}
        {activeModal === 'sos-report' && (
          <Modal onClose={closeModal}>
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">Report Route Incident</h3>
                <button onClick={closeModal}>
                  <X size={16} className="text-slate-500 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300" />
                </button>
              </div>

              <div>
                <label className="mb-1.5 block text-[11px] font-medium text-slate-500 dark:text-slate-400">Incident Type</label>
                <div className="grid grid-cols-2 gap-1.5">
                  {INCIDENT_TYPES.map(({ key, icon: Icon }) => (
                    <button
                      key={key}
                      onClick={() => setIncidentType(key)}
                      className={cx(
                        'flex items-center gap-1.5 rounded-lg border px-2 py-2 text-left text-[11px] font-medium leading-tight transition',
                        incidentType === key
                          ? 'border-sky-500/60 bg-sky-500/15 text-sky-600 dark:text-sky-300'
                          : 'border-slate-300 dark:border-slate-700 bg-white/60 dark:bg-slate-950/50 text-slate-600 dark:text-slate-300 hover:border-slate-300 dark:hover:border-slate-600'
                      )}
                    >
                      <Icon size={14} className="shrink-0" />
                      {key}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="mb-1.5 block text-[11px] font-medium text-slate-500 dark:text-slate-400">Severity Level</label>
                <div className="grid grid-cols-3 gap-1.5">
                  {SEVERITY_LEVELS.map(({ key, sub, icon: Icon, cls }) => (
                    <button
                      key={key}
                      onClick={() => setIncidentSeverity(key)}
                      className={cx(
                        'flex flex-col items-center gap-0.5 rounded-lg border px-1.5 py-2 text-center transition',
                        incidentSeverity === key ? cls : 'border-slate-300 dark:border-slate-700 bg-white/60 dark:bg-slate-950/50 text-slate-500 dark:text-slate-400 hover:border-slate-300 dark:hover:border-slate-600'
                      )}
                    >
                      <Icon size={14} />
                      <span className="text-[10px] font-semibold leading-tight">{key}</span>
                      {sub && <span className="text-[9px] opacity-80">{sub}</span>}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="mb-1 block text-[11px] font-medium text-slate-500 dark:text-slate-400">Optional Notes</label>
                <input
                  value={incidentNote}
                  onChange={(e) => setIncidentNote(e.target.value)}
                  placeholder="e.g. Culvert washed out 5km ahead"
                  maxLength={140}
                  className="w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white/80 dark:bg-slate-950/70 px-3 py-2 text-[12px] text-slate-800 dark:text-slate-200 placeholder:text-slate-600 dark:placeholder:text-slate-600 focus:border-sky-600 focus:outline-none"
                />
              </div>

              <p className="flex items-center gap-1.5 text-[10px] text-slate-500 dark:text-slate-500">
                <MapPin size={11} /> Live GPS coordinates are attached automatically
              </p>

              <div className="flex gap-2 pt-1">
                <button
                  onClick={closeModal}
                  className="flex-1 rounded-lg border border-slate-300 dark:border-slate-700 py-2.5 text-sm font-medium text-slate-600 dark:text-slate-300 transition hover:bg-slate-100 dark:hover:bg-slate-800"
                >
                  Cancel
                </button>
                <button
                  onClick={dispatchIncidentReport}
                  disabled={!incidentType}
                  className="flex-[1.4] rounded-lg bg-red-600 py-2.5 text-sm font-semibold text-white transition hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Dispatch Report
                </button>
              </div>
            </div>
          </Modal>
        )}

        {/* Obstacle report modal */}
        {activeModal === 'obstacle' && (
          <Modal onClose={closeModal}>
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">Report Road Obstacle</h3>
                <button onClick={closeModal}>
                  <X size={16} className="text-slate-500 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300" />
                </button>
              </div>

              {!reportSubmitted ? (
                <>
                  <div>
                    <label className="mb-1 block text-[11px] font-medium text-slate-500 dark:text-slate-400">Obstacle Type</label>
                    <div className="relative">
                      <select
                        value={obstacleType}
                        onChange={(e) => setObstacleType(e.target.value)}
                        className="w-full appearance-none rounded-lg border border-slate-300 dark:border-slate-700 bg-white/80 dark:bg-slate-950/70 px-3 py-2.5 text-sm text-slate-800 dark:text-slate-200 focus:border-sky-600 focus:outline-none"
                      >
                        <option>Landslide</option>
                        <option>Flood</option>
                        <option>Fallen Tree</option>
                      </select>
                      <ChevronDown size={15} className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 dark:text-slate-500" />
                    </div>
                  </div>

                  <div>
                    <label className="mb-1 block text-[11px] font-medium text-slate-500 dark:text-slate-400">Description</label>
                    <textarea
                      value={obstacleDescription}
                      onChange={(e) => setObstacleDescription(e.target.value)}
                      placeholder="e.g. Boulder blocking single lane"
                      rows={2}
                      maxLength={200}
                      className="w-full resize-none rounded-lg border border-slate-300 dark:border-slate-700 bg-white/80 dark:bg-slate-950/70 px-3 py-2 text-[12px] text-slate-800 dark:text-slate-200 placeholder:text-slate-600 dark:placeholder:text-slate-600 focus:border-sky-600 focus:outline-none"
                    />
                  </div>

                  <div>
                    <label className="mb-1 block text-[11px] font-medium text-slate-500 dark:text-slate-400">Photo Evidence</label>
                    <label className="flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-slate-300 dark:border-slate-700 bg-white/60 dark:bg-slate-950/50 py-4 text-center text-xs text-slate-500 dark:text-slate-400 transition hover:border-slate-300 dark:hover:border-slate-600">
                      <ImagePlus size={16} className="shrink-0" />
                      <span className="truncate">{photoName ? photoName : 'Tap to attach photo'}</span>
                      <input
                        type="file"
                        accept="image/*"
                        className="hidden"
                        onChange={(e) => setPhotoName(e.target.files?.[0]?.name || null)}
                      />
                    </label>
                  </div>

                  <p className="flex items-center gap-1.5 text-[10px] text-slate-500 dark:text-slate-500">
                    <MapPin size={11} /> Live GPS coordinates are attached automatically
                  </p>

                  <button
                    onClick={submitObstacleReport}
                    disabled={obstacleSubmitting}
                    className="flex w-full items-center justify-center gap-2 rounded-lg bg-sky-600 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {obstacleSubmitting ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
                    {obstacleSubmitting ? 'Reporting…' : 'Submit Report'}
                  </button>
                </>
              ) : (
                <div className="flex flex-col items-center gap-2 py-4 text-center">
                  {reportQueued ? <Clock size={30} className="text-amber-600 dark:text-amber-400" /> : <CheckCircle2 size={30} className="text-emerald-600 dark:text-emerald-400" />}
                  <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                    {reportQueued ? 'Report queued' : reportOutcome && !reportOutcome.isNew ? 'Hazard confirmed' : 'Report submitted'}
                  </p>
                  <p className="text-xs text-slate-500 dark:text-slate-400">
                    {reportQueued
                      ? 'Could not reach the command center right now — will send automatically once signal returns.'
                      : reportOutcome && !reportOutcome.isNew
                      ? `Already reported nearby — confirmed by ${reportOutcome.confirmations} driver${reportOutcome.confirmations === 1 ? '' : 's'}. Route re-checked for a safe detour.`
                      : 'Command center has been notified — route re-checked for a safe detour.'}
                  </p>
                </div>
              )}
            </div>
          </Modal>
        )}

        {/* Passive live GPS "entering a high-risk area" warning - independent
            of the SOS/obstacle modals above, can pop up any time this screen
            is open regardless of whether a route is even planned. */}
        {zoneWarning && (
          <Modal onClose={() => setZoneWarning(null)}>
            <div className="space-y-3 text-center">
              <AlertTriangle size={32} className="mx-auto text-red-600 dark:text-red-400" />
              <h3 className="text-sm font-bold text-red-600 dark:text-red-300">Entering a High-Risk Zone</h3>
              <p className="text-[12px] leading-snug text-slate-600 dark:text-slate-300">
                Your current location has elevated <span className="font-semibold text-red-600 dark:text-red-300">{HAZARD_LABELS[zoneWarning.hazard] || zoneWarning.hazard}</span> risk
                right now (safety score {Math.round(zoneWarning.score)}%). Drive with caution.
              </p>
              <button
                onClick={() => setZoneWarning(null)}
                className="w-full rounded-lg bg-red-600 py-2.5 text-sm font-semibold text-white transition hover:bg-red-500"
              >
                Acknowledge
              </button>
            </div>
          </Modal>
        )}

        {/* Distinct from zoneWarning above - nothing is HIGH-risk right now,
            but a weather-driven hazard (rainfall/wind) is forecast to
            worsen over the next few hours (multi_hazard.evaluate_point_with_
            trend on the backend). Earlier, softer heads-up than waiting for
            conditions to actually cross into HIGH before saying anything. */}
        {risingWarning && (
          <Modal onClose={() => setRisingWarning(null)}>
            <div className="space-y-3 text-center">
              <TrendingUp size={32} className="mx-auto text-amber-600 dark:text-amber-400" />
              <h3 className="text-sm font-bold text-amber-600 dark:text-amber-300">Conditions Worsening Nearby</h3>
              <p className="text-[12px] leading-snug text-slate-600 dark:text-slate-300">
                <span className="font-semibold text-amber-600 dark:text-amber-300">{HAZARD_LABELS[risingWarning.hazard] || risingWarning.hazard}</span> risk
                near your location is forecast to rise over the next few hours (projected safety score {Math.round(risingWarning.projectedScore)}%). Not an emergency yet - stay alert.
              </p>
              <button
                onClick={() => setRisingWarning(null)}
                className="w-full rounded-lg bg-amber-600 py-2.5 text-sm font-semibold text-white transition hover:bg-amber-500"
              >
                Acknowledge
              </button>
            </div>
          </Modal>
        )}
      </div>
    </div>
  );
}
