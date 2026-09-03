import React, { useState, useEffect, useMemo, useRef } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MapContainer, TileLayer, Marker, Polyline, Tooltip, Popup, useMap } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { apiFetch } from './apiClient';
import {
  Truck, AlertTriangle, MapPin, Radio, Search, Activity, Shield, Fuel,
  Package, HeartPulse, Wifi, WifiOff, X, Navigation, Clock, Siren, Plus,
  Building2, CloudRain, ZoomIn, ZoomOut, Layers, RefreshCw, CheckCircle2,
  Mountain, Signal, ChevronRight, Flame,
} from 'lucide-react';

/* ------------------------------------------------------------------ *
 * Real map setup — CARTO's free "Dark Matter" tiles (no API key) over
 * OpenStreetMap data, bounded pan-India (see INDIA_BOUNDS below).
 * ------------------------------------------------------------------ */
// Pan-India center/bounds (was NE-only - [25.15, 93.4] center with a
// [21.5,88]-[29,98.5] maxBounds - which would have made the pan-India
// ROUTES_SEED corridors literally unreachable/unpannable on the map).
const INDIA_CENTER = [22.5, 82.0];
const INDIA_BOUNDS = [[5.0, 66.0], [38.0, 99.0]];
const DEFAULT_ZOOM = 5;

// Three switchable base layers, all free/no-API-key Esri tile services (same
// provider/attribution family as the original single layer, just different
// map styles) - picked over adding a second provider so there's only one
// attribution line and one set of usage terms to think about.
const MAP_TYPES = {
  normal: {
    label: 'Standard',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors, and the GIS community',
  },
  satellite: {
    label: 'Satellite',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attribution: 'Tiles &copy; Esri &mdash; Esri, Maxar, Earthstar Geographics, and the GIS User Community',
  },
  physical: {
    // Esri's World_Terrain_Base (tried first) loads fine but is a very
    // pale, low-contrast style that reads as "mostly white" at a glance -
    // OpenTopoMap actually shows visible green/brown relief shading and
    // contour lines, closer to what "physical map" usually means. Different
    // provider than the other two (still free, no API key), so its own
    // {z}/{x}/{y} coordinate order and subdomain rotation, not Esri's.
    label: 'Physical',
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    subdomains: 'abc',
    attribution: 'Map data: &copy; OpenStreetMap contributors, SRTM | Map style: &copy; OpenTopoMap (CC-BY-SA)',
    maxNativeZoom: 17,
  },
};

function divIcon(node, size, popupAnchor) {
  return L.divIcon({
    html: renderToStaticMarkup(node),
    className: '',
    iconSize: size,
    iconAnchor: [size[0] / 2, size[1] / 2],
    ...(popupAnchor ? { popupAnchor } : {}),
  });
}

const cx = (...a) => a.filter(Boolean).join(' ');

function formatCoord(lat, lng) {
  return `${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lng).toFixed(4)}° ${lng >= 0 ? 'E' : 'W'}`;
}

function timeAgo(date, now) {
  const diff = Math.max(0, Math.floor((now - date) / 1000));
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function playAlertPing() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = 'sine';
    o.frequency.setValueAtTime(920, ctx.currentTime);
    o.frequency.exponentialRampToValueAtTime(420, ctx.currentTime + 0.35);
    g.gain.setValueAtTime(0.18, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.45);
    o.connect(g);
    g.connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + 0.45);
  } catch (e) {
    /* Web Audio unsupported — fail silently */
  }
}

/* ------------------------------------------------------------------ *
 * Mock data
 * ------------------------------------------------------------------ */
const CITIES = {
  Guwahati: [26.1445, 91.7362],
  Nagaon: [26.348, 92.684],
  Haflong: [25.167, 93.017],
  Silchar: [24.8333, 92.7789],
  Shillong: [25.5788, 91.8933],
  Jowai: [25.45, 92.2],
  Dimapur: [25.9091, 93.7267],
  Kohima: [25.6751, 94.1086],
  Mao: [25.49, 94.07],
  Imphal: [24.817, 93.9368],
  Tezpur: [26.6338, 92.8],
  Jorhat: [26.7509, 94.2037],
  // Pan-India corridors (see ROUTES_SEED) - spans landslide (Himalayan/Western
  // Ghats), earthquake (Zone V Kashmir/Gujarat), flood (Ganga/Kosi belt), and
  // cyclone (Bay of Bengal, Arabian Sea coast) prone regions, not NE-only.
  Jammu: [32.7266, 74.857],
  Srinagar: [34.0837, 74.7973],
  Delhi: [28.6139, 77.209],
  Jaipur: [26.9124, 75.7873],
  Agra: [27.1767, 78.0081],
  Kanpur: [26.4499, 80.3319],
  Bhubaneswar: [20.2961, 85.8245],
  Visakhapatnam: [17.6868, 83.2185],
  Mumbai: [19.076, 72.8777],
  Goa: [15.4909, 73.8278],
  Kochi: [9.9312, 76.2673],
  Coimbatore: [11.0168, 76.9558],
  Porbandar: [21.6417, 69.6293],
  Ahmedabad: [23.0225, 72.5714],
  Chennai: [13.0827, 80.2707],
  Bengaluru: [12.9716, 77.5946],
  Patna: [25.5941, 85.1376],
  Muzaffarpur: [26.1225, 85.3906],
  Puducherry: [11.9416, 79.8083],
};

/* ------------------------------------------------------------------ *
 * Quick Response Team dispatch — nearest-depot ETA for the "Acknowledge
 * & Dispatch QRT" action on SOS alert cards.
 * ------------------------------------------------------------------ */
// Pan-India spread - was 6 NE-only depots, meaning any SOS elsewhere in
// India would compute a nearest-depot ETA thousands of km away.
const QRT_DEPOTS = [
  { name: 'Guwahati QRT Base', coords: CITIES.Guwahati },
  { name: 'Shillong QRT Base', coords: CITIES.Shillong },
  { name: 'Silchar QRT Base', coords: CITIES.Silchar },
  { name: 'Dimapur QRT Base', coords: CITIES.Dimapur },
  { name: 'Kohima QRT Base', coords: CITIES.Kohima },
  { name: 'Imphal QRT Base', coords: CITIES.Imphal },
  { name: 'Delhi QRT Base', coords: CITIES.Delhi },
  { name: 'Mumbai QRT Base', coords: CITIES.Mumbai },
  { name: 'Chennai QRT Base', coords: CITIES.Chennai },
  { name: 'Bengaluru QRT Base', coords: CITIES.Bengaluru },
  { name: 'Bhubaneswar QRT Base', coords: CITIES.Bhubaneswar },
  { name: 'Ahmedabad QRT Base', coords: CITIES.Ahmedabad },
  { name: 'Patna QRT Base', coords: CITIES.Patna },
  { name: 'Srinagar QRT Base', coords: CITIES.Srinagar },
];
const QRT_SPEED_KMH = 32; // avg quick-response speed over hill/highway terrain
const QRT_PREP_MIN = 6; // mobilization buffer before wheels-up

function haversineKm(lat1, lng1, lat2, lng2) {
  const R = 6371;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLng = ((lng2 - lng1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLng / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function nearestQrtEta(lat, lng) {
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
    return { depotName: 'Nearest QRT Base', etaMin: 25 };
  }
  let best = null;
  for (const depot of QRT_DEPOTS) {
    const distanceKm = haversineKm(lat, lng, depot.coords[0], depot.coords[1]);
    if (!best || distanceKm < best.distanceKm) best = { depotName: depot.name, distanceKm };
  }
  const etaMin = Math.round((best.distanceKm / QRT_SPEED_KMH) * 60 + QRT_PREP_MIN);
  return { depotName: best.depotName, etaMin };
}

// Pan-India watched corridors - a curated list (not user-editable, see
// project discussion), spanning all 4 hazard types instead of NE-landslide
// only. `risk`/`reason` below are just the initial pre-refresh placeholder;
// refreshCorridors() immediately overwrites both with the live unified
// ai_risk_level/risk_factors from POST /api/v1/routes/calculate.
const ROUTES_SEED = [
  {
    id: 'NH44-J',
    name: 'NH-44',
    label: 'Jammu – Srinagar Corridor',
    risk: 'moderate',
    reason: 'Himalayan corridor - landslide and Zone V seismic watch',
    updated: 14,
    waypoints: [CITIES.Jammu, CITIES.Srinagar],
  },
  {
    id: 'NH27',
    name: 'NH-27',
    label: 'Guwahati – Silchar Corridor',
    risk: 'moderate',
    reason: 'Landslide watch near Haflong hill section; intermittent heavy rain',
    updated: 14,
    waypoints: [CITIES.Guwahati, CITIES.Nagaon, CITIES.Haflong, CITIES.Silchar],
  },
  {
    id: 'NH29',
    name: 'NH-29',
    label: 'Kohima – Dimapur Corridor',
    risk: 'blocked',
    reason: 'Major landslide at Chumukedima Km 42 — road closed to all traffic',
    updated: 6,
    waypoints: [CITIES.Dimapur, CITIES.Kohima],
  },
  {
    id: 'NH48',
    name: 'NH-48',
    label: 'Delhi – Jaipur Corridor',
    risk: 'safe',
    reason: 'Clear, major national artery',
    updated: 20,
    waypoints: [CITIES.Delhi, CITIES.Jaipur],
  },
  {
    id: 'NH19',
    name: 'NH-19',
    label: 'Delhi – Agra – Kanpur Corridor',
    risk: 'safe',
    reason: 'GT Road, Ganga floodplain watch during monsoon',
    updated: 25,
    waypoints: [CITIES.Delhi, CITIES.Agra, CITIES.Kanpur],
  },
  {
    id: 'NH16',
    name: 'NH-16',
    label: 'Bhubaneswar – Visakhapatnam Corridor',
    risk: 'moderate',
    reason: 'Bay of Bengal coast - cyclone watch',
    updated: 10,
    waypoints: [CITIES.Bhubaneswar, CITIES.Visakhapatnam],
  },
  {
    id: 'NH66-MG',
    name: 'NH-66',
    label: 'Mumbai – Goa Corridor',
    risk: 'safe',
    reason: 'Konkan coast, monsoon/flood watch',
    updated: 18,
    waypoints: [CITIES.Mumbai, CITIES.Goa],
  },
  {
    id: 'NH544',
    name: 'NH-544',
    label: 'Kochi – Coimbatore Corridor',
    risk: 'moderate',
    reason: 'Western Ghats pass - landslide watch',
    updated: 16,
    waypoints: [CITIES.Kochi, CITIES.Coimbatore],
  },
  {
    id: 'NH27-PA',
    name: 'NH-27',
    label: 'Porbandar – Ahmedabad Corridor',
    risk: 'safe',
    reason: 'Gujarat, Kutch Zone V seismic watch',
    updated: 30,
    waypoints: [CITIES.Porbandar, CITIES.Ahmedabad],
  },
  {
    id: 'NH44-CB',
    name: 'NH-44',
    label: 'Chennai – Bengaluru Corridor',
    risk: 'safe',
    reason: 'Clear, major southern artery',
    updated: 22,
    waypoints: [CITIES.Chennai, CITIES.Bengaluru],
  },
  {
    id: 'NH28',
    name: 'NH-28',
    label: 'Patna – Muzaffarpur Corridor',
    risk: 'moderate',
    reason: 'Bihar, Kosi/Ganga flood-prone belt',
    updated: 12,
    waypoints: [CITIES.Patna, CITIES.Muzaffarpur],
  },
  {
    id: 'NH66-CP',
    name: 'NH-66',
    label: 'Chennai – Puducherry Corridor',
    risk: 'safe',
    reason: 'East coast, cyclone watch',
    updated: 28,
    waypoints: [CITIES.Chennai, CITIES.Puducherry],
  },
];

const RISK_CONFIG = {
  safe: { label: 'Safe / Clear', stroke: '#22c55e', text: 'text-emerald-600 dark:text-emerald-400', badge: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30', dot: 'bg-emerald-500' },
  moderate: { label: 'Moderate Risk Watch', stroke: '#f59e0b', text: 'text-amber-600 dark:text-amber-400', badge: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30', dot: 'bg-amber-500' },
  blocked: { label: 'Blocked / High Risk', stroke: '#ef4444', text: 'text-red-600 dark:text-red-400', badge: 'bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30', dot: 'bg-red-500' },
};

// The unified engine reports risk-level strings per-hazard (HIGH_LANDSLIDE_RISK,
// HIGH_EARTHQUAKE_RISK, HIGH_FLOOD_RISK, HIGH_CYCLONE_RISK) - any of them means
// "blocked" here, not just landslide's. Matching only the literal landslide
// string would silently render a live cyclone/flood/earthquake HIGH corridor
// as green "safe".
function mapRiskLevel(level) {
  if (typeof level === 'string' && level.startsWith('HIGH_')) return 'blocked';
  if (level === 'MODERATE') return 'moderate';
  return 'safe';
}

function mapHazardSeverity(severity) {
  const s = (severity || '').toUpperCase();
  if (s === 'SEVERE' || s === 'HIGH') return 'blocked';
  if (s === 'MINOR' || s === 'LOW') return 'safe';
  return 'moderate';
}

// Pan-India spread, matching ROUTES_SEED's corridors - was all 5 concentrated
// in NE India regardless of the app's actual coverage.
const CONVOYS_INITIAL = [
  { id: 'GHY-114', cargoType: 'Food Grains', priority: 'Standard', status: 'Moving', lat: 25.76, lng: 92.85, destination: 'Silchar', driver: 'R. Baruah', route: 'NH-27', eta: '2h 40m' },
  { id: 'MED-07', cargoType: 'Medical Supplies', priority: 'Critical', status: 'Halted', lat: 33.4, lng: 74.9, destination: 'Srinagar District Hospital', driver: 'A. Bhat', route: 'NH-44', eta: 'Blocked' },
  { id: 'FUEL-22', cargoType: 'Fuel', priority: 'Standard', status: 'Moving', lat: 19.9, lng: 73.2, destination: 'Goa', driver: 'S. Naik', route: 'NH-66', eta: '1h 05m' },
  { id: 'MED-11', cargoType: 'Medical Supplies', priority: 'High', status: 'Delayed', lat: 20.6, lng: 85.4, destination: 'Visakhapatnam', driver: 'B. Patra', route: 'NH-16', eta: '3h 15m (cyclone watch)' },
  { id: 'GHY-201', cargoType: 'Food Grains', priority: 'Standard', status: 'Moving', lat: 25.9, lng: 85.2, destination: 'Muzaffarpur', driver: 'V. Kumar', route: 'NH-28', eta: '55m' },
];

const DISTRICT_POOL = [
  { district: 'East Khasi Hills', state: 'Meghalaya', coords: [25.5788, 91.8933] },
  { district: 'Kohima', state: 'Nagaland', coords: [25.6751, 94.1086] },
  { district: 'Cachar', state: 'Assam', coords: [24.8333, 92.7789] },
  { district: 'Srinagar', state: 'Jammu and Kashmir', coords: [34.0837, 74.7973] },
  { district: 'Kutch', state: 'Gujarat', coords: [23.0225, 72.5714] },
  { district: 'Wayanad', state: 'Kerala', coords: [11.6854, 76.132] },
  { district: 'Ganjam', state: 'Odisha', coords: [19.4, 84.85] },
  { district: 'Muzaffarpur', state: 'Bihar', coords: [26.1225, 85.3906] },
];

// Was 4 hardcoded mock SOS entries that never expired or changed - removed so
// "Active SOS" (and the SOS layer) reflects only real backend alerts and
// deliberately-added simulated ones (see addSimulatedSOS), not permanent fake data.
const SOS_INITIAL = [];

// Local-only SOS history (simulated demo SOS + their dispatch/resolve
// outcomes) used to live purely in React state and vanish on every refresh -
// real backend alerts never had this problem (their lifecycle is in
// logistics.db), only the "Add Simulated SOS" demo path did. Persisted here
// so a refresh mid-demo doesn't wipe it. Real (BE-) dispatched/resolved
// entries get written here too (harmless/redundant with the backend) which
// lets them render correctly immediately on load, before the first
// fetchAlerts() reconciliation pass (see the effect near backendAlerts)
// confirms them from the server.
const SOS_HISTORY_STORAGE_KEY = 'setu_dashboard_sos_history';
let cachedSosHistory; // module-level: parse localStorage once, not once per useState initializer
function loadStoredSosHistory() {
  if (cachedSosHistory !== undefined) return cachedSosHistory;
  try {
    const raw = localStorage.getItem(SOS_HISTORY_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    cachedSosHistory = parsed
      ? {
          sosPings: (parsed.sosPings || []).map((s) => ({ ...s, timestamp: new Date(s.timestamp) })),
          dispatched: Object.fromEntries(
            Object.entries(parsed.dispatched || {}).map(([id, d]) => [id, { ...d, dispatchedAt: new Date(d.dispatchedAt) }])
          ),
          resolved: Object.fromEntries(
            Object.entries(parsed.resolved || {}).map(([id, r]) => [
              id,
              {
                ...r,
                resolvedAt: new Date(r.resolvedAt),
                dispatchedAt: r.dispatchedAt ? new Date(r.dispatchedAt) : undefined,
                receivedAt: r.receivedAt ? new Date(r.receivedAt) : undefined,
              },
            ])
          ),
        }
      : null;
  } catch (err) {
    console.warn('Failed to read stored SOS history:', err);
    cachedSosHistory = null;
  }
  return cachedSosHistory;
}

// Pan-India spread, matching ROUTES_SEED's corridors - was all 4 concentrated
// in NE India.
const ROAD_ALERTS = [
  { id: 'AL-1', ageMin: 6, severity: 'blocked', state: 'Nagaland', district: 'Kohima', route: 'NH-29', title: 'Major Landslide — NH-29 Km 42', message: 'Chumukedima section fully blocked. Heavy machinery deployed for clearance.' },
  { id: 'AL-2', ageMin: 14, severity: 'moderate', state: 'Jammu and Kashmir', district: 'Srinagar', route: 'NH-44', title: 'Landslide Watch — Jammu-Srinagar Corridor', message: 'Continuous rainfall since morning, loose boulders reported on the shoulder.' },
  { id: 'AL-3', ageMin: 19, severity: 'moderate', state: 'Odisha', district: 'Ganjam', route: 'NH-16', title: 'Cyclone Watch — Bay of Bengal Coast', message: 'Bay of Bengal system tracking toward the coast; convoys advised to monitor advisories.' },
  { id: 'AL-4', ageMin: 52, severity: 'safe', state: 'Bihar', district: 'Muzaffarpur', route: 'NH-28', title: 'Flash Flood Advisory — Ganga Belt', message: 'River nearing danger mark near the Patna-Muzaffarpur belt; monitoring in progress.' },
];

const VEHICLE_TYPES = ['Ambulance', '4x4 Relief Truck', 'Motorbike Courier', 'Private Vehicle', 'District Rescue Van'];
const CARGO_PRIORITIES = [
  'Critical: Oxygen Cylinders', 'Critical: Snake Bite Antivenom', 'High: Medical Evacuation',
  'High: Insulin Delivery', 'Critical: Infant Formula & Medicines', 'Medium: Trapped Family Rescue',
];
// Pan-India state filter list, matching the states ROUTES_SEED's corridors
// touch - the underlying mock SOS/alert seed data (SOS_INITIAL, ROAD_ALERTS)
// is still NE-only, so filtering by a newly-added state here will correctly
// show zero mock items rather than fabricated ones, until that seed data
// gets a real pan-India pass too.
const STATE_LIST = [
  'All', 'Assam', 'Meghalaya', 'Nagaland', 'Manipur',
  'Jammu and Kashmir', 'Delhi', 'Rajasthan', 'Uttar Pradesh', 'Odisha',
  'Andhra Pradesh', 'Maharashtra', 'Goa', 'Kerala', 'Tamil Nadu',
  'Karnataka', 'Gujarat', 'Bihar',
];
const ISOLATED_DISTRICTS = ['Kohima Rural Belt', 'Peren', 'Kutch Rural Belt'];

const CARGO_ICON = { 'Food Grains': Package, 'Medical Supplies': HeartPulse, Fuel: Fuel };
const CARGO_STYLE = {
  'Food Grains': { ring: 'ring-amber-400', bg: 'bg-amber-500', glow: 'shadow-amber-500/50' },
  'Medical Supplies': { ring: 'ring-rose-400', bg: 'bg-rose-500', glow: 'shadow-rose-500/50' },
  Fuel: { ring: 'ring-sky-400', bg: 'bg-sky-500', glow: 'shadow-sky-500/50' },
};
const CONVOY_STATUS_DOT = { Moving: 'bg-emerald-400 animate-pulse', Delayed: 'bg-amber-400 animate-pulse', Halted: 'bg-red-500' };
const SOS_STATUS_CONFIG = {
  'Live Online': { icon: Wifi, cls: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30' },
  'SMS Fallback Alert': { icon: WifiOff, cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30' },
  'WhatsApp Voice SOS': { icon: Radio, cls: 'bg-violet-500/15 text-violet-300 border-violet-500/30' },
};
const SEVERITY_BADGE_CONFIG = {
  Critical: 'bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30',
  Moderate: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30',
  Informational: 'bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30',
};
const URGENCY_BADGE_CONFIG = {
  CRITICAL: 'bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30',
  HIGH: 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30',
  MODERATE: 'bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30',
};

/* ------------------------------------------------------------------ *
 * Small presentational pieces
 * ------------------------------------------------------------------ */
function StatPill({ icon: Icon, label, value, tone }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 px-3 py-2 sm:px-4 sm:py-2.5 min-w-[150px]">
      <div className={cx('flex h-9 w-9 shrink-0 items-center justify-center rounded-md', tone.iconBg)}>
        <Icon className={cx('h-4.5 w-4.5', tone.iconText)} size={18} />
      </div>
      <div className="min-w-0">
        <div className="text-[11px] uppercase tracking-wide text-slate-500 dark:text-slate-400 truncate">{label}</div>
        <div className={cx('text-lg font-semibold leading-tight', tone.value)}>{value}</div>
      </div>
    </div>
  );
}

function SosStatusBadge({ status }) {
  const cfg = SOS_STATUS_CONFIG[status];
  const Icon = cfg.icon;
  return (
    <span className={cx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide', cfg.cls)}>
      <Icon size={11} /> {status}
    </span>
  );
}

const OUTCOME_CONFIG = {
  CLEARED: { label: 'Route Cleared' },
  EVACUATED: { label: 'Evacuated' },
  CARGO_LOST: { label: 'Cargo Lost' },
  FALSE_ALARM: { label: 'False Alarm', warning: true }, // no real incident at the location - distinct from a genuine resolution, styled differently everywhere it shows up
  OTHER: { label: 'Resolved' },
};

function DispatchBadge({ dispatch, resolved }) {
  if (resolved) {
    const outcome = OUTCOME_CONFIG[resolved.outcomeType] || OUTCOME_CONFIG.OTHER;
    return (
      <span
        className={cx(
          'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
          outcome.warning ? 'border-red-500/40 bg-red-500/15 text-red-600 dark:text-red-300' : 'border-sky-500/40 bg-sky-500/15 text-sky-600 dark:text-sky-300'
        )}
      >
        {outcome.warning ? <AlertTriangle size={11} /> : <CheckCircle2 size={11} />} {outcome.label}
      </span>
    );
  }
  if (dispatch) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
        <CheckCircle2 size={11} /> Rescue Dispatched
      </span>
    );
  }
  return (
    <span className="inline-flex animate-pulse items-center gap-1 rounded-full border border-red-500/40 bg-red-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-600 dark:text-red-400">
      <Siren size={11} /> Active SOS
    </span>
  );
}

// Only renders when there's an actual pattern worth an operator's attention
// (a real prior false alarm) - a first-time or clean-history reporter shows
// nothing extra, so this doesn't turn into noise on every card.
function DriverHistoryFlag({ history }) {
  if (!history || !history.falseAlarms) return null;
  return (
    <div className="mt-1.5 flex items-center gap-1.5 rounded-md border border-red-500/30 bg-red-500/10 px-2 py-1 text-[10px] font-medium text-red-600 dark:text-red-300">
      <AlertTriangle size={11} className="shrink-0" />
      {history.total} report{history.total === 1 ? '' : 's'} from this number · {history.falseAlarms} previously flagged false alarm{history.falseAlarms === 1 ? '' : 's'}
    </div>
  );
}

function minutesBetween(from, to) {
  return Math.max(0, (new Date(to) - new Date(from)) / 60000);
}

function timeAgoMinutes(from, to) {
  const min = Math.round(minutesBetween(from, to));
  return min < 1 ? '<1m' : `${min}m`;
}

function OutcomePicker({ onPick, onCancel }) {
  const [note, setNote] = useState('');
  return (
    <div className="mt-2 space-y-1.5 rounded-md border border-sky-500/30 bg-sky-500/10 p-2" onClick={(e) => e.stopPropagation()}>
      <div className="text-[10px] font-semibold uppercase tracking-wide text-sky-600 dark:text-sky-300">Mark Resolved — outcome</div>
      <div className="flex flex-wrap gap-1.5">
        {Object.entries(OUTCOME_CONFIG).map(([key, cfg]) => (
          <button
            key={key}
            onClick={() => onPick(key, note)}
            className={cx(
              'rounded-md border px-2 py-1 text-[10px] font-medium transition',
              cfg.warning
                ? 'border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-200 hover:bg-red-500/25'
                : 'border-sky-500/40 bg-sky-500/15 text-sky-700 dark:text-sky-200 hover:bg-sky-500/25'
            )}
          >
            {cfg.label}
          </button>
        ))}
      </div>
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Optional note…"
        className="w-full rounded-md border border-slate-300 dark:border-slate-700 bg-white/80 dark:bg-slate-950/70 px-2 py-1 text-[11px] text-slate-800 dark:text-slate-200 placeholder:text-slate-500 dark:placeholder:text-slate-500 focus:border-sky-600 focus:outline-none"
      />
      <button onClick={onCancel} className="text-[10px] text-slate-500 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300">Cancel</button>
    </div>
  );
}

function CategoryTag({ category }) {
  return (
    <span className="inline-flex items-center rounded-full border border-slate-300 dark:border-slate-700 bg-slate-100/80 dark:bg-slate-800/70 px-2 py-0.5 text-[10px] font-medium text-slate-600 dark:text-slate-300">
      {category}
    </span>
  );
}

function UrgencyBadge({ urgency }) {
  const cls = URGENCY_BADGE_CONFIG[urgency];
  if (!cls) return null;
  return (
    <span className={cx('inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide', cls)}>
      {urgency}
    </span>
  );
}

function TranscriptToggle({ transcript }) {
  const [open, setOpen] = useState(false);
  if (!transcript) return null;
  return (
    <div className="mt-1">
      <button
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        className="flex items-center gap-1 text-[10px] font-medium text-violet-300 hover:text-violet-200"
      >
        <ChevronRight size={10} className={cx('transition-transform', open && 'rotate-90')} />
        {open ? 'Hide' : 'View'} Raw Transcript
      </button>
      {open && <p className="mt-1 rounded-md bg-slate-100/80 dark:bg-slate-900/60 p-1.5 text-[11px] italic text-slate-500 dark:text-slate-400">"{transcript}"</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * Heat Zones — isolated add-on. Own fetch, own state, own panel; doesn't
 * touch the Leaflet map, SOS markers, or the main alert feed above.
 * ------------------------------------------------------------------ */
function HeatZonePanel({ zones, onClose, onDispatch, dispatchingId }) {
  return (
    <div className="absolute inset-0 z-[2000] flex justify-end bg-white/70 dark:bg-slate-950/60" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="flex h-full w-full max-w-sm flex-col border-l border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 shadow-2xl">
        <div className="flex shrink-0 items-center justify-between border-b border-slate-200 dark:border-slate-800 px-4 py-3">
          <span className="flex items-center gap-1.5 text-sm font-semibold text-orange-600 dark:text-orange-300"><Flame size={15} /> Active Heat Zones</span>
          <button onClick={onClose} className="text-slate-500 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"><X size={16} /></button>
        </div>
        <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto p-3">
          {zones.length === 0 && <div className="pt-10 text-center text-xs text-slate-500 dark:text-slate-500">No high-density clusters right now.</div>}
          {zones.map((z) => (
            <div key={z.zone_id} className={cx('rounded-lg border p-3', z.severity === 'CRITICAL' ? 'border-red-500/50 bg-red-500/10' : 'border-orange-500/40 bg-orange-500/10')}>
              <div className="mb-1 flex items-center justify-between">
                <span className={cx('text-xs font-semibold', z.severity === 'CRITICAL' ? 'text-red-600 dark:text-red-300' : 'text-orange-600 dark:text-orange-300')}>{z.top_hazard}</span>
                <span className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">{z.severity}</span>
              </div>
              <p className="text-[11px] text-slate-600 dark:text-slate-300">{z.label}</p>
              <p className="mt-0.5 text-[10px] text-slate-500 dark:text-slate-500">{formatCoord(z.center_lat, z.center_lon)} · {z.total_reports} reports</p>
              <button
                disabled={dispatchingId === z.zone_id}
                onClick={() => onDispatch(z)}
                className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-orange-500/40 bg-orange-500/15 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-orange-600 dark:text-orange-300 transition hover:bg-orange-500/25 disabled:opacity-50"
              >
                <Truck size={12} /> {dispatchingId === z.zone_id ? 'Dispatching…' : 'Dispatch Group Taskforce'}
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function RecoveryPanel({ stats, resolvedAlerts, onClose }) {
  const fmtMin = (m) => (m == null ? '—' : m < 1 ? '<1m' : `${Math.round(m)}m`);
  return (
    <div className="absolute inset-0 z-[2000] flex justify-end bg-white/70 dark:bg-slate-950/60" onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} className="flex h-full w-full max-w-sm flex-col border-l border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 shadow-2xl">
        <div className="flex shrink-0 items-center justify-between border-b border-slate-200 dark:border-slate-800 px-4 py-3">
          <span className="flex items-center gap-1.5 text-sm font-semibold text-sky-600 dark:text-sky-300"><CheckCircle2 size={15} /> Recovery Analytics</span>
          <button onClick={onClose} className="text-slate-500 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"><X size={16} /></button>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
          <div className="grid grid-cols-2 gap-2.5">
            <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 p-3">
              <div className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">Resolved Today</div>
              <div className="mt-1 text-xl font-bold text-sky-600 dark:text-sky-300">{stats?.resolved_today ?? '—'}</div>
            </div>
            <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 p-3">
              <div className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">Total Resolved</div>
              <div className="mt-1 text-xl font-bold text-sky-600 dark:text-sky-300">{stats?.resolved_count ?? '—'}</div>
            </div>
            <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 p-3">
              <div className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">Avg Response</div>
              <div className="mt-1 text-xl font-bold text-emerald-600 dark:text-emerald-300">{fmtMin(stats?.avg_response_minutes)}</div>
            </div>
            <div className="rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 p-3">
              <div className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">Avg Recovery</div>
              <div className="mt-1 text-xl font-bold text-emerald-600 dark:text-emerald-300">{fmtMin(stats?.avg_recovery_minutes)}</div>
            </div>
          </div>

          <div>
            <div className="mb-1.5 text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-500">Recently Closed Out</div>
            {resolvedAlerts.length === 0 && <div className="pt-4 text-center text-xs text-slate-500 dark:text-slate-500">Nothing resolved yet.</div>}
            <div className="space-y-2">
              {resolvedAlerts.slice(0, 5).map((a) => {
                const outcome = OUTCOME_CONFIG[a.outcome_type] || OUTCOME_CONFIG.OTHER;
                return (
                  <div key={a.id} className={cx('rounded-lg border p-2.5 text-[11px]', outcome.warning ? 'border-red-500/30 bg-red-500/5 text-red-100' : 'border-slate-200 dark:border-slate-800 bg-slate-100/70 dark:bg-slate-900/50 text-slate-600 dark:text-slate-300')}>
                    <div className="flex items-center justify-between">
                      <span className={cx('font-medium', outcome.warning ? 'text-red-600 dark:text-red-300' : 'text-slate-900 dark:text-slate-100')}>{outcome.warning && <AlertTriangle size={11} className="mr-1 inline" />}{outcome.label}</span>
                      <span className="text-slate-500 dark:text-slate-500">{a.id}</span>
                    </div>
                    <div className="text-slate-500 dark:text-slate-500">{formatCoord(a.lat, a.lng)}</div>
                    {a.outcome_note && <div className="mt-0.5 italic text-slate-500 dark:text-slate-400">"{a.outcome_note}"</div>}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function SeverityBadge({ severity }) {
  const cls = SEVERITY_BADGE_CONFIG[severity];
  if (!cls) return null;
  return (
    <span className={cx('inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide', cls)}>
      {severity}
    </span>
  );
}

function ConvoyMarker({ convoy, onSelect, active }) {
  const Icon = CARGO_ICON[convoy.cargoType];
  const style = CARGO_STYLE[convoy.cargoType];
  const icon = useMemo(
    () =>
      divIcon(
        <div className={cx('relative flex h-6 w-6 items-center justify-center rounded-full ring-2 shadow-lg', style.bg, style.ring, style.glow, active && 'ring-4 scale-110')}>
          <Icon size={11} className="text-slate-950" strokeWidth={2.5} />
          <span className={cx('absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full border border-slate-950', CONVOY_STATUS_DOT[convoy.status])} />
        </div>,
        [24, 24]
      ),
    [Icon, style, active, convoy.status]
  );
  return (
    <Marker position={[convoy.lat, convoy.lng]} icon={icon} eventHandlers={{ click: () => onSelect(convoy) }}>
      <Tooltip direction="top" offset={[0, -16]}>
        <span className="font-semibold">{convoy.id}</span> · {convoy.cargoType} · {convoy.status}
      </Tooltip>
    </Marker>
  );
}

// Module-level cache (not component state) - shared across every marker
// instance and persists across re-renders/remounts, keyed by rounded
// coordinate so nearby markers reuse one lookup instead of each firing
// their own reverse-geocode call.
const _cityNameCache = new Map();

function useCityName(lat, lng) {
  const key = `${lat.toFixed(2)},${lng.toFixed(2)}`;
  const [name, setName] = useState(() => _cityNameCache.get(key) ?? null);
  useEffect(() => {
    if (_cityNameCache.has(key)) {
      setName(_cityNameCache.get(key));
      return undefined;
    }
    let cancelled = false;
    apiFetch(`/api/geocode/reverse?lat=${lat}&lon=${lng}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const resolved = data?.city ? `${data.city}${data.state ? `, ${data.state}` : ''}` : null;
        _cityNameCache.set(key, resolved);
        if (!cancelled) setName(resolved);
      })
      .catch(() => {
        _cityNameCache.set(key, null);
      });
    return () => {
      cancelled = true;
    };
  }, [key]);
  return name;
}

function SosMarker({ sos, onSelect, active, isNew, dispatch }) {
  const cityName = useCityName(sos.lat, sos.lng);
  const icon = useMemo(
    () =>
      divIcon(
        <div className="relative flex h-5 w-5 items-center justify-center">
          {!dispatch && (
            <span className={cx('absolute inset-0 rounded-full bg-red-500/40', isNew ? 'animate-ping' : 'animate-ping [animation-duration:2s]')} />
          )}
          <div
            className={cx(
              'relative flex h-4 w-4 items-center justify-center rounded-full ring-1 shadow-lg',
              dispatch ? 'bg-emerald-600 ring-emerald-300/60 shadow-emerald-600/60' : 'bg-red-600 ring-red-300/60 shadow-red-600/60',
              active && 'ring-2 scale-125'
            )}
          >
            {dispatch ? <Truck size={9} className="text-white" strokeWidth={2.5} /> : <Siren size={9} className="text-white" strokeWidth={2.5} />}
          </div>
        </div>,
        [20, 20],
        [0, 18] // pushes the click-popup below the pin instead of Leaflet's default of above it - see popupAnchor in divIcon()
      ),
    [isNew, active, dispatch]
  );
  return (
    <Marker position={[sos.lat, sos.lng]} icon={icon} eventHandlers={{ click: () => onSelect(sos) }}>
      <Tooltip direction="top" offset={[0, -14]}>
        {dispatch
          ? `✅ Rescue Dispatched · ETA ${dispatch.etaMin}m${cityName ? ` | ${cityName}` : ''} | Lat: ${sos.lat.toFixed(4)}, Lon: ${sos.lng.toFixed(4)}`
          : `🚨 SOS Active${cityName ? ` | ${cityName}` : ''} | Lat: ${sos.lat.toFixed(4)}, Lon: ${sos.lng.toFixed(4)}`}
      </Tooltip>
      {/* Quick glanceable when/where on click - the fuller detail (dispatch
          controls, outcome, history) still lives in the existing bottom-
          right selection card; this is just the fast answer to "what is
          this pin and when did it come in," right at the pin itself. */}
      <Popup minWidth={170} closeButton={false}>
        <div className="space-y-0.5 text-[12px]">
          <div className="flex items-center gap-1 font-semibold text-slate-900">
            <Clock size={12} /> {timeAgo(sos.timestamp, new Date())}
          </div>
          <div className="flex items-center gap-1 text-slate-700">
            <MapPin size={12} /> {cityName || `${sos.lat.toFixed(4)}°, ${sos.lng.toFixed(4)}°`}
          </div>
        </div>
      </Popup>
    </Marker>
  );
}

function HazardMarker({ hazard, onSelect, active }) {
  const cfg = RISK_CONFIG[mapHazardSeverity(hazard.severity)];
  const icon = useMemo(
    () =>
      divIcon(
        <div
          className={cx('flex h-4 w-4 items-center justify-center rounded-full ring-1 shadow-lg ring-slate-950/60', active && 'ring-2 scale-110')}
          style={{ background: cfg.stroke }}
        >
          <AlertTriangle size={9} className="text-slate-950" strokeWidth={2.5} />
        </div>,
        [18, 18]
      ),
    [cfg.stroke, active]
  );
  return (
    <Marker position={[hazard.latitude, hazard.longitude]} icon={icon} eventHandlers={{ click: () => onSelect(hazard) }}>
      <Tooltip direction="top" offset={[0, -14]}>
        {hazard.type} · confirmed by {hazard.confirmations} report{hazard.confirmations > 1 ? 's' : ''}
      </Tooltip>
    </Marker>
  );
}

function RoutePath({ route }) {
  const cfg = RISK_CONFIG[route.risk];
  const positions = route.waypoints;
  const mid = route.waypoints[Math.floor((route.waypoints.length - 1) / 2)];
  const midB = route.waypoints[Math.ceil((route.waypoints.length - 1) / 2)];
  const anchor = [(mid[0] + midB[0]) / 2, (mid[1] + midB[1]) / 2];
  const warnIcon = useMemo(
    () =>
      route.risk === 'safe'
        ? null
        : divIcon(
            <div
              className="flex h-5 w-5 items-center justify-center rounded-full border text-[11px] font-bold"
              style={{ background: '#0b1220', borderColor: cfg.stroke, color: cfg.stroke, borderWidth: 1.5 }}
            >
              !
            </div>,
            [20, 20]
          ),
    [route.risk, cfg.stroke]
  );
  return (
    <>
      <Polyline positions={positions} pathOptions={{ color: cfg.stroke, weight: 9, opacity: 0.18, lineCap: 'round', lineJoin: 'round' }} />
      <Polyline
        positions={positions}
        pathOptions={{
          color: cfg.stroke, weight: 4, opacity: 1, lineCap: 'round', lineJoin: 'round',
          dashArray: route.risk === 'blocked' ? '2 9' : undefined,
        }}
      >
        <Tooltip direction="top" sticky>
          <span className="font-semibold">{route.name}</span> · {route.label}
          {route.aiSafetyScore != null && <> · AI Safety {route.aiSafetyScore}%</>}
        </Tooltip>
      </Polyline>
      {warnIcon && <Marker position={anchor} icon={warnIcon} />}
    </>
  );
}

/* Recenters/zooms the live map whenever a new fly-to target is set (e.g. an
 * incoming SOS beacon) - the real map.flyTo() this app's SOS flow asked for. */
function MapFlyTo({ target }) {
  const map = useMap();
  useEffect(() => {
    if (!target) return;
    map.flyTo([target.lat, target.lng], Math.max(map.getZoom(), 10), { duration: 1.25 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.key]);
  return null;
}

/* Exposes imperative map controls (zoom/reset) to plain overlay buttons,
 * since those live outside react-leaflet's component tree. */
function MapControls({ onZoomIn, onZoomOut, onReset }) {
  const map = useMap();
  useEffect(() => {
    onZoomIn.current = () => map.zoomIn();
    onZoomOut.current = () => map.zoomOut();
    onReset.current = () => map.flyTo(INDIA_CENTER, DEFAULT_ZOOM, { duration: 1 });
  }, [map, onZoomIn, onZoomOut, onReset]);
  return null;
}

/* ------------------------------------------------------------------ *
 * Main dashboard component
 * ------------------------------------------------------------------ */
export default function Dashboard({ alerts = [] }) {
  const [now, setNow] = useState(() => new Date());
  const [convoys] = useState(CONVOYS_INITIAL);
  const [sosPings, setSosPings] = useState(
    () => loadStoredSosHistory()?.sosPings ?? SOS_INITIAL.map((s) => ({ ...s, timestamp: new Date(Date.now() - s.ageMin * 60000) }))
  );
  const [roadAlerts] = useState(
    ROAD_ALERTS.map((a) => ({ ...a, timestamp: new Date(Date.now() - a.ageMin * 60000) }))
  );
  const [routes, setRoutes] = useState(ROUTES_SEED);
  const [backendAlerts, setBackendAlerts] = useState([]);
  const [liveConnStatus, setLiveConnStatus] = useState('connecting'); // 'connecting' | 'live' | 'reconnecting' - see fetchAlerts
  const [liveHazards, setLiveHazards] = useState([]);

  // Combines two genuinely different sources: locally-queued offline/SMS-
  // fallback SOS (never reaches the backend, so it can only ever be shown
  // from the lifted `alerts` prop) and real online SOS pulled fresh from the
  // backend's own persisted store (see fetchAlerts below) - kept separate so
  // an online dispatch never renders twice (once locally, once from the
  // backend fetch a moment later).
  const driverSos = useMemo(() => {
    const known = new Set(sosPings.map((s) => s.id));
    const offlineOnly = alerts
      .filter((a) => a.offline && !known.has(a.id))
      .map((a, i) => ({
        id: a.id,
        kind: 'sos',
        timestamp: new Date(Date.now() - i * 1000),
        status: 'SMS Fallback Alert',
        vehicleType: a.vehicle,
        cargoPriority: a.cargo,
        category: a.category,
        severity: a.severity,
        note: a.note,
        lat: a.lat,
        lng: a.lng,
        district: a.locationName || 'Unknown District',
        state: '',
        message: a.description,
        locationLabel: a.location,
        timeLabel: a.time,
      }));
    const fromBackend = backendAlerts
      .filter((a) => !known.has(`BE-${a.id}`))
      .map((a) => ({
        id: `BE-${a.id}`,
        kind: 'sos',
        timestamp: new Date(a.received_at),
        status:
          a.source === 'whatsapp_voice' ? 'WhatsApp Voice SOS' : a.source === 'SMS FALLBACK ALERT' ? 'SMS Fallback Alert' : 'Live Online',
        vehicleType: a.source === 'whatsapp_voice' ? 'Voice Report' : a.source === 'SMS FALLBACK ALERT' ? 'SMS Relay' : 'Mobile Unit',
        cargoPriority: a.cargo,
        lat: a.lat,
        lng: a.lng,
        district: '',
        state: '',
        message: a.reason,
        locationLabel: formatCoord(a.lat, a.lng),
        isVoice: a.source === 'whatsapp_voice',
        urgency: a.urgency,
        actionNeeded: a.action_needed,
        summary: a.summary,
        transcript: a.raw_message,
        lifecycleStatus: a.status,
        dispatchedAt: a.dispatched_at,
        resolvedAt: a.resolved_at,
        outcomeType: a.outcome_type,
        outcomeNote: a.outcome_note,
        receivedAt: a.received_at,
        reportedBy: a.reported_by, // real, server-verified identity (driver phone / Twilio From) - used to look up this reporter's history below, not anything the client could fake
      }));
    return [...offlineOnly, ...fromBackend];
  }, [alerts, sosPings, backendAlerts]);

  // Per-reporter track record, derived entirely from data already being
  // polled (backendAlerts, via fetchAlerts) - no new endpoint needed. Lets
  // an operator see "this identity has a history of false alarms" right on
  // the card, before they commit a QRT, which is the actual point of having
  // real driver identity + the False Alarm outcome - attribution the
  // operator can act on, not just a login screen.
  const driverHistoryByPhone = useMemo(() => {
    const map = {};
    for (const a of backendAlerts) {
      if (!a.reported_by) continue;
      const entry = map[a.reported_by] || { total: 0, falseAlarms: 0 };
      entry.total += 1;
      if (a.outcome_type === 'FALSE_ALARM') entry.falseAlarms += 1;
      map[a.reported_by] = entry;
    }
    return map;
  }, [backendAlerts]);

  const [search, setSearch] = useState('');
  const [stateFilter, setStateFilter] = useState('All');
  const [selected, setSelected] = useState(null);
  const [flyTarget, setFlyTarget] = useState(null); // {lat, lng, key} — flyTo target for the live map
  const [layers, setLayers] = useState({ convoys: true, sos: true, routes: true, hazards: true });
  const [layersOpen, setLayersOpen] = useState(false);
  const [mapType, setMapType] = useState('normal'); // 'normal' | 'satellite' | 'physical'

  // Heat Zones — isolated add-on state, own polling, doesn't touch the main
  // alerts/hazards/corridors polling effect below.
  const [heatZones, setHeatZones] = useState([]);
  const [heatZonesOpen, setHeatZonesOpen] = useState(false);
  const [dispatchingZoneId, setDispatchingZoneId] = useState(null);

  const fetchHeatZones = () => {
    apiFetch(`/api/clusters/heat-zones`)
      .then((r) => r.json())
      .then(setHeatZones)
      .catch((err) => console.warn('Heat zone fetch failed:', err));
  };

  useEffect(() => {
    fetchHeatZones();
    const t = setInterval(fetchHeatZones, 15000);
    return () => clearInterval(t);
  }, []);

  const dispatchHeatZone = (zone) => {
    setDispatchingZoneId(zone.zone_id);
    apiFetch(`/api/clusters/dispatch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ alert_ids: zone.alert_ids }),
    })
      .then(() => {
        // Mirrors dispatchQrt's local optimistic state, one taskforce/ETA
        // shared across the whole cluster - without this, group-dispatched
        // alerts stay stuck showing "Active SOS" + the single-dispatch
        // button (dispatched[id] never gets set for them otherwise), and
        // can never reach Mark Resolved / Recovery Analytics.
        const { depotName, etaMin } = nearestQrtEta(zone.center_lat, zone.center_lon);
        const dispatchedAt = new Date();
        setDispatched((prev) => {
          const next = { ...prev };
          zone.alert_ids.forEach((id) => { next[`BE-${id}`] = { depotName, etaMin, dispatchedAt }; });
          return next;
        });
        fetchHeatZones();
        fetchAlerts();
      })
      .catch((err) => console.warn('Group dispatch failed:', err))
      .finally(() => setDispatchingZoneId(null));
  };
  const [flashId, setFlashId] = useState(null);
  const [dispatched, setDispatched] = useState(() => loadStoredSosHistory()?.dispatched ?? {}); // { [sosId]: { depotName, etaMin, dispatchedAt } }
  const [resolved, setResolved] = useState(() => loadStoredSosHistory()?.resolved ?? {}); // { [sosId]: { resolvedAt, outcomeType, outcomeNote } }
  const [resolvingId, setResolvingId] = useState(null); // sosId whose outcome-picker is expanded
  const [feedTab, setFeedTab] = useState('active'); // 'active' | 'resolved'

  // Recovery Analytics — isolated add-on state, own polling, doesn't touch
  // the main alerts/hazards/corridors polling effect below (same pattern as
  // Heat Zones above).
  const [recoveryStats, setRecoveryStats] = useState(null);
  const [resolvedAlerts, setResolvedAlerts] = useState([]);
  const [recoveryOpen, setRecoveryOpen] = useState(false);

  const fetchRecovery = () => {
    apiFetch(`/api/recovery/stats`).then((r) => r.json()).then(setRecoveryStats).catch((err) => console.warn('Recovery stats fetch failed:', err));
    apiFetch(`/api/sos/resolved`).then((r) => r.json()).then(setResolvedAlerts).catch((err) => console.warn('Resolved SOS fetch failed:', err));
  };

  useEffect(() => {
    fetchRecovery();
    const t = setInterval(fetchRecovery, 15000);
    return () => clearInterval(t);
  }, []);

  // Blends the backend's real resolved SOS (recoveryStats/resolvedAlerts,
  // from fetchRecovery above) with locally-resolved simulated demo SOS
  // (resolved[id] for non-BE- ids, which never reach the backend) so the
  // Recovery Analytics panel counts every "Mark Resolved" click, not just
  // real ones - matches this app's existing convention of demo actions
  // updating the UI live even where they don't persist server-side.
  const combinedResolvedList = useMemo(() => {
    const backendList = resolvedAlerts.map((a) => ({
      id: `BE-${a.id}`, lat: a.lat, lng: a.lng,
      outcome_type: a.outcome_type, outcome_note: a.outcome_note,
      resolvedAt: new Date(a.resolved_at),
    }));
    const simulatedList = Object.entries(resolved)
      .filter(([id]) => !id.startsWith('BE-'))
      .map(([id, r]) => ({
        id, lat: r.lat, lng: r.lng,
        outcome_type: r.outcomeType, outcome_note: r.outcomeNote,
        resolvedAt: new Date(r.resolvedAt),
      }));
    return [...backendList, ...simulatedList].sort((a, b) => b.resolvedAt - a.resolvedAt);
  }, [resolvedAlerts, resolved]);

  const combinedRecoveryStats = useMemo(() => {
    const backendSamples = resolvedAlerts.map((a) => ({ receivedAt: a.received_at, dispatchedAt: a.dispatched_at, resolvedAt: a.resolved_at }));
    const simulatedSamples = Object.entries(resolved)
      .filter(([id]) => !id.startsWith('BE-'))
      .map(([, r]) => ({ receivedAt: r.receivedAt, dispatchedAt: r.dispatchedAt, resolvedAt: r.resolvedAt }));
    const all = [...backendSamples, ...simulatedSamples];
    const avg = (nums) => (nums.length ? Math.round((nums.reduce((s, n) => s + n, 0) / nums.length) * 10) / 10 : null);
    return {
      resolved_count: all.length,
      resolved_today: all.filter((s) => new Date(s.resolvedAt).toDateString() === now.toDateString()).length,
      avg_response_minutes: avg(all.filter((s) => s.dispatchedAt && s.receivedAt).map((s) => minutesBetween(s.receivedAt, s.dispatchedAt))),
      avg_recovery_minutes: avg(all.filter((s) => s.receivedAt).map((s) => minutesBetween(s.receivedAt, s.resolvedAt))),
    };
  }, [resolvedAlerts, resolved, now]);

  const flashTimer = useRef(null);
  const prevAlertsRef = useRef(alerts);
  const prevBackendIdsRef = useRef(null);
  const zoomInRef = useRef(() => {});
  const zoomOutRef = useRef(() => {});
  const resetViewRef = useRef(() => {});

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => () => flashTimer.current && clearTimeout(flashTimer.current), []);

  const fetchAlerts = async () => {
    try {
      const res = await apiFetch(`/api/alerts`);
      if (!res.ok) throw new Error(`alerts fetch failed (${res.status})`);
      const rows = await res.json();
      // Seed the "seen" set from whatever's already on the backend the first
      // time this resolves, so pre-existing alerts don't all read as "new" and
      // yank the map/selection on a cold page load.
      if (prevBackendIdsRef.current === null) {
        prevBackendIdsRef.current = new Set(rows.map((r) => r.id));
      }
      setBackendAlerts(rows);
      setLiveConnStatus('live');
    } catch (err) {
      console.warn('Live SOS fetch failed:', err);
      // Doesn't clear backendAlerts - a transient failure (the free-tier
      // backend cold-starting after ~15min idle takes 10-15s+ to answer,
      // observed directly against production) shouldn't make already-loaded
      // real SOS vanish from the feed. It does flip the header's status dot
      // though (see liveConnStatus below) - the dot used to be a hardcoded
      // permanent green pulse regardless of whether this fetch was actually
      // succeeding, which made a real backend outage/cold-start look
      // indistinguishable from "confirmed zero SOS, everything's fine."
      setLiveConnStatus('reconnecting');
    }
  };

  const fetchHazards = async () => {
    try {
      const res = await apiFetch(`/api/hazards`);
      if (!res.ok) throw new Error(`hazards fetch failed (${res.status})`);
      setLiveHazards(await res.json());
    } catch (err) {
      console.warn('Live hazard fetch failed:', err);
    }
  };

  // Re-scores every monitored corridor against the live backend (real TomTom
  // geometry + the ML risk model), replacing the fixed ROUTES_SEED shape with
  // whatever the backend actually returns - falls back to the last-known/seed
  // path for a corridor if its call fails, rather than dropping it.
  //
  // Fired one at a time (not Promise.all) with a short stagger: each call
  // fans out into several TomTom + Open-Meteo sub-requests server-side
  // (routing.py scores every candidate route), and doing all 5 corridors at
  // once was enough concurrent traffic to trip TomTom's free-tier rate limit
  // (429s surfacing here as 502s) during testing.
  const refreshCorridors = async () => {
    const results = [];
    for (const seed of ROUTES_SEED) {
      try {
        const [oLat, oLon] = seed.waypoints[0];
        const [dLat, dLon] = seed.waypoints[seed.waypoints.length - 1];
        const res = await apiFetch(`/api/v1/routes/calculate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ origin: { lat: oLat, lon: oLon }, destination: { lat: dLat, lon: dLon } }),
        });
        if (!res.ok) throw new Error(`route calc failed (${res.status})`);
        const data = await res.json();
        results.push({
          ...seed,
          waypoints: data.coordinates,
          risk: mapRiskLevel(data.ai_risk_level),
          reason: data.risk_factors?.length ? data.risk_factors.join(' · ') : seed.reason,
          aiSafetyScore: data.ai_safety_score,
        });
      } catch (err) {
        console.warn(`Corridor ${seed.id} live refresh failed, keeping cached path:`, err);
        results.push(seed);
      }
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
    setRoutes(results);
  };

  useEffect(() => {
    fetchAlerts();
    fetchHazards();
    refreshCorridors();
    const liveDataTimer = setInterval(() => {
      fetchAlerts();
      fetchHazards();
    }, 8000);
    // 10min, not 60s - re-scoring 12 pan-India corridors (each up to 4 TomTom
    // candidates x multiple live weather/elevation lookups) every minute was
    // hammering Open-Topo-Data's free-tier rate limit hard enough to stall
    // the whole backend. A demo doesn't need minute-fresh corridor risk.
    const corridorTimer = setInterval(refreshCorridors, 600000);
    return () => {
      clearInterval(liveDataTimer);
      clearInterval(corridorTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Local-only SOS (offline/SMS-fallback queued, never reaches the backend)
  // flashes/flies immediately straight from the shared alerts feed. A real
  // online SOS instead triggers an immediate fetchAlerts() so the Command
  // Center reflects the persisted backend row - the actual flash/fly for
  // that case happens in the effect below, once backendAlerts updates.
  useEffect(() => {
    const prevIds = new Set(prevAlertsRef.current.map((a) => a.id));
    const added = alerts.find((a) => !prevIds.has(a.id));
    prevAlertsRef.current = alerts;
    if (!added) return;

    if (!added.offline) {
      fetchAlerts();
      return;
    }
    if (!Number.isFinite(added.lat) || !Number.isFinite(added.lng)) return;

    flyToCoordinate(added.lat, added.lng);
    setFlashId(added.id);
    playAlertPing();
    const mapped = driverSos.find((d) => d.id === added.id);
    if (mapped) setSelected({ kind: 'sos', ...mapped });
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlashId((cur) => (cur === added.id ? null : cur)), 5000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alerts]);

  // Flash/fly to any SOS that just appeared in the polled backend feed -
  // covers both "just dispatched online in this tab" (via the immediate
  // fetchAlerts() above) and a genuinely different tab/device sending one.
  useEffect(() => {
    if (prevBackendIdsRef.current === null) return; // fetchAlerts hasn't resolved for the first time yet
    const prevIds = prevBackendIdsRef.current;
    const added = backendAlerts.find((a) => !prevIds.has(a.id));
    prevBackendIdsRef.current = new Set(backendAlerts.map((a) => a.id));
    if (!added || !Number.isFinite(added.lat) || !Number.isFinite(added.lng)) return;

    const flashKey = `BE-${added.id}`;
    flyToCoordinate(added.lat, added.lng);
    setFlashId(flashKey);
    playAlertPing();
    const mapped = driverSos.find((d) => d.id === flashKey);
    if (mapped) setSelected({ kind: 'sos', ...mapped });
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlashId((cur) => (cur === flashKey ? null : cur)), 5000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendAlerts]);

  function flyToCoordinate(lat, lng) {
    setFlyTarget({ lat, lng, key: Date.now() });
  }

  const stats = useMemo(() => {
    const activeConvoys = convoys.filter((c) => c.status !== 'Halted').length;
    const criticalBlockades = routes.filter((r) => r.risk === 'blocked').length;
    const moderate = routes.filter((r) => r.risk === 'moderate').length;
    const networkHealth = Math.max(20, 100 - criticalBlockades * 18 - moderate * 6);
    return {
      activeConvoys,
      totalConvoys: convoys.length,
      criticalBlockades,
      isolatedDistricts: ISOLATED_DISTRICTS.length,
      networkHealth,
      activeSOS: [...sosPings, ...driverSos].filter((s) => !dispatched[s.id]).length,
    };
  }, [convoys, routes, sosPings, driverSos, dispatched]);

  const liveRoadAlerts = useMemo(
    () =>
      liveHazards.map((h) => ({
        id: `HZ-${h.id}`,
        kind: 'road',
        timestamp: new Date(h.created_at),
        severity: mapHazardSeverity(h.severity),
        state: '',
        district: '',
        route: 'Driver-Reported',
        title: `${h.type} — ${h.confirmations} report${h.confirmations > 1 ? 's' : ''}`,
        message: h.description || `${h.type} reported near ${formatCoord(h.latitude, h.longitude)}.`,
        lat: h.latitude,
        lng: h.longitude,
      })),
    [liveHazards]
  );

  const feed = useMemo(() => {
    const sos = sosPings.map((s) => ({ ...s, kind: 'sos' }));
    const road = roadAlerts.map((a) => ({ ...a, kind: 'road' }));
    let items = [...driverSos, ...sos, ...road, ...liveRoadAlerts].sort((a, b) => b.timestamp - a.timestamp);
    items = items.filter((i) => (i.kind === 'sos' ? Boolean(resolved[i.id]) === (feedTab === 'resolved') : feedTab === 'active'));
    if (stateFilter !== 'All') items = items.filter((i) => i.state === stateFilter);
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      items = items.filter((i) =>
        [i.district, i.state, i.vehicleType, i.cargoPriority, i.message, i.title, i.route]
          .filter(Boolean)
          .some((f) => f.toLowerCase().includes(q))
      );
    }
    return items;
  }, [sosPings, roadAlerts, driverSos, liveRoadAlerts, stateFilter, search, feedTab, resolved]);

  const addSimulatedSOS = () => {
    const pick = DISTRICT_POOL[Math.floor(Math.random() * DISTRICT_POOL.length)];
    const jitter = () => (Math.random() - 0.5) * 0.3;
    const id = `SOS-SIM-${Math.floor(Math.random() * 90000 + 10000)}`;
    const newSos = {
      id,
      timestamp: new Date(),
      lat: pick.coords[0] + jitter(),
      lng: pick.coords[1] + jitter(),
      vehicleType: VEHICLE_TYPES[Math.floor(Math.random() * VEHICLE_TYPES.length)],
      cargoPriority: CARGO_PRIORITIES[Math.floor(Math.random() * CARGO_PRIORITIES.length)],
      status: Math.random() < 0.4 ? 'SMS Fallback Alert' : 'Live Online',
      district: pick.district,
      state: pick.state,
      message: 'Simulated distress signal — awaiting dispatch confirmation.',
      simulated: true,
    };
    setSosPings((prev) => [newSos, ...prev]);
    setFlashId(id);
    setSelected({ kind: 'sos', ...newSos });
    playAlertPing();
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlashId((cur) => (cur === id ? null : cur)), 5000);
  };

  const toggleLayer = (key) => setLayers((l) => ({ ...l, [key]: !l[key] }));

  // Acknowledge & Dispatch QRT — moves an SOS from passive "Active" monitoring
  // into an active dispatch workflow, with an ETA from the nearest depot.
  // Persists to the backend for real (BE-prefixed) alerts, via the direct
  // status transition endpoint - no clustering/aggregation, just this one
  // alert's status flipping from PENDING to DISPATCHED so the server's own
  // active-SOS count (GET /api/sos/active) actually decrements.
  const dispatchQrt = (item) => {
    if (dispatched[item.id]) return;
    const { depotName, etaMin } = nearestQrtEta(item.lat, item.lng);
    setDispatched((prev) => ({ ...prev, [item.id]: { depotName, etaMin, dispatchedAt: new Date() } }));

    if (typeof item.id === 'string' && item.id.startsWith('BE-')) {
      const alertId = item.id.slice(3);
      apiFetch(`/api/sos/${alertId}/dispatch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'DISPATCHED' }),
      }).catch((err) => console.warn('Failed to persist SOS dispatch:', err));
    }
  };

  // "After"-phase close-out — mirrors dispatchQrt exactly: optimistic local
  // state for every item (so simulated demo SOS resolve too), persisted to
  // the backend only for real (BE-prefixed) alerts via the same direct
  // status-transition endpoint, now carrying the outcome capture. Refreshes
  // the Recovery Analytics numbers right after, since they've just changed.
  // Snapshots receivedAt/dispatchedAt/lat/lng too, not just for BE- alerts -
  // simulated SOS have no backend row to source them from later, so the
  // combined recovery stats below (which blend real + simulated resolves)
  // need them captured here, at resolve time.
  const resolveSos = (item, outcomeType, outcomeNote) => {
    if (resolved[item.id]) return;
    setResolved((prev) => ({
      ...prev,
      [item.id]: {
        resolvedAt: new Date(),
        outcomeType,
        outcomeNote,
        receivedAt: item.timestamp,
        dispatchedAt: dispatched[item.id]?.dispatchedAt,
        lat: item.lat,
        lng: item.lng,
      },
    }));
    setResolvingId(null);

    if (typeof item.id === 'string' && item.id.startsWith('BE-')) {
      const alertId = item.id.slice(3);
      apiFetch(`/api/sos/${alertId}/dispatch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'RESOLVED', outcome_type: outcomeType, outcome_note: outcomeNote }),
      })
        .then(fetchRecovery)
        .catch((err) => console.warn('Failed to persist SOS resolution:', err));
    }
  };

  // Persists simulated-demo SOS + the local dispatch/resolve UI state to
  // localStorage so "Add Simulated SOS" history survives a refresh instead
  // of vanishing (see loadStoredSosHistory above for why this exists).
  useEffect(() => {
    try {
      localStorage.setItem(SOS_HISTORY_STORAGE_KEY, JSON.stringify({ sosPings, dispatched, resolved }));
    } catch (err) {
      console.warn('Failed to persist SOS history:', err);
    }
  }, [sosPings, dispatched, resolved]);

  // Reconciles dispatched/resolved UI badges for REAL (BE-) alerts against
  // the backend's own status on every fetch - the backend is the sole
  // source of truth for these ids, so this both fills in a missing local
  // entry AND corrects/clears a stale one, in either direction.
  //
  // The "clear a stale one" half is not hypothetical: the deployed backend
  // (Render free tier, no persistent disk) loses its database on every
  // redeploy, so ids restart from 1 - a brand new PENDING alert can land on
  // the exact same "BE-1" this browser's localStorage still remembers as
  // RESOLVED from a completely different alert in a previous deploy
  // generation. Only ever adding (never correcting) meant that stale
  // "resolved" from the old generation would permanently hide the new,
  // unrelated alert with the same id - confirmed happening live: backend
  // had 2 PENDING, Command Center showed "Active SOS: 0."
  useEffect(() => {
    if (!backendAlerts.length) return;
    setDispatched((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const a of backendAlerts) {
        const id = `BE-${a.id}`;
        const shouldBeDispatched = a.status === 'DISPATCHED' || a.status === 'RESOLVED';
        if (shouldBeDispatched && !next[id]) {
          const { depotName, etaMin } = nearestQrtEta(a.lat, a.lng);
          next[id] = { depotName, etaMin, dispatchedAt: a.dispatched_at ? new Date(a.dispatched_at) : new Date() };
          changed = true;
        } else if (!shouldBeDispatched && next[id]) {
          delete next[id];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    setResolved((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const a of backendAlerts) {
        const id = `BE-${a.id}`;
        if (a.status === 'RESOLVED' && !next[id]) {
          next[id] = {
            resolvedAt: a.resolved_at ? new Date(a.resolved_at) : new Date(),
            outcomeType: a.outcome_type,
            outcomeNote: a.outcome_note,
            receivedAt: new Date(a.received_at),
            dispatchedAt: a.dispatched_at ? new Date(a.dispatched_at) : undefined,
            lat: a.lat,
            lng: a.lng,
          };
          changed = true;
        } else if (a.status !== 'RESOLVED' && next[id]) {
          delete next[id];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [backendAlerts]);

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden bg-slate-50 dark:bg-slate-950 text-slate-900 dark:text-slate-100">
      {/* ---------------- Header ---------------- */}
      <header className="flex shrink-0 flex-col gap-3 border-b border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/70 px-4 py-3 backdrop-blur sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500 to-emerald-500 shadow-lg shadow-sky-500/20">
            <Shield size={20} className="text-slate-950" strokeWidth={2.5} />
          </div>
          <div>
            <h1 className="text-sm font-bold tracking-wide text-slate-900 dark:text-slate-100 sm:text-base">SETU DISASTER LOGISTICS COMMAND CENTER</h1>
            <p className="text-[11px] text-slate-500 dark:text-slate-400">AI-Powered Emergency Response — Pan-India Multi-Hazard Coverage</p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 sm:gap-3">
          <StatPill icon={Truck} label="Active Convoys" value={`${stats.activeConvoys}/${stats.totalConvoys}`} tone={{ iconBg: 'bg-sky-500/15', iconText: 'text-sky-600 dark:text-sky-400', value: 'text-slate-900 dark:text-slate-100' }} />
          <StatPill icon={Siren} label="Active SOS" value={stats.activeSOS} tone={{ iconBg: 'bg-red-500/15', iconText: 'text-red-600 dark:text-red-400', value: 'text-red-600 dark:text-red-400' }} />
          <StatPill icon={AlertTriangle} label="Critical Blockades" value={stats.criticalBlockades} tone={{ iconBg: 'bg-red-500/15', iconText: 'text-red-600 dark:text-red-400', value: 'text-red-600 dark:text-red-400' }} />
          <StatPill icon={Building2} label="Isolated Districts" value={stats.isolatedDistricts} tone={{ iconBg: 'bg-amber-500/15', iconText: 'text-amber-600 dark:text-amber-400', value: 'text-amber-600 dark:text-amber-400' }} />
          <StatPill icon={Signal} label="Network Health" value={`${stats.networkHealth}%`} tone={{ iconBg: 'bg-emerald-500/15', iconText: 'text-emerald-600 dark:text-emerald-400', value: 'text-emerald-600 dark:text-emerald-400' }} />

          <button
            onClick={addSimulatedSOS}
            className="flex items-center gap-1.5 rounded-lg bg-red-600 px-3 py-2.5 text-xs font-semibold uppercase tracking-wide text-white shadow-lg shadow-red-600/30 transition hover:bg-red-500 active:scale-95"
          >
            <Plus size={15} strokeWidth={3} /> Add Simulated SOS
          </button>

          <button
            onClick={() => setHeatZonesOpen(true)}
            className="flex items-center gap-1.5 rounded-lg border border-orange-500/40 bg-orange-500/15 px-3 py-2.5 text-xs font-semibold uppercase tracking-wide text-orange-600 dark:text-orange-300 transition hover:bg-orange-500/25"
          >
            <Flame size={15} /> Active Heat Zones ({heatZones.length})
          </button>

          <button
            onClick={() => setRecoveryOpen(true)}
            className="flex items-center gap-1.5 rounded-lg border border-sky-500/40 bg-sky-500/15 px-3 py-2.5 text-xs font-semibold uppercase tracking-wide text-sky-600 dark:text-sky-300 transition hover:bg-sky-500/25"
          >
            <CheckCircle2 size={15} /> Recovery Analytics
          </button>

          <div
            className="hidden items-center gap-2 rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-100/80 dark:bg-slate-900/60 px-3 py-2 text-xs text-slate-600 dark:text-slate-300 lg:flex"
            title={
              liveConnStatus === 'reconnecting'
                ? "Lost contact with the live SOS feed - retrying automatically. The free-tier backend can take 10-15s to wake up after being idle; already-loaded alerts stay on screen while this reconnects."
                : 'Connected to the live SOS feed'
            }
          >
            <span className="relative flex h-2 w-2">
              <span
                className={cx(
                  'absolute inline-flex h-full w-full animate-ping rounded-full opacity-75',
                  liveConnStatus === 'reconnecting' ? 'bg-amber-400' : 'bg-emerald-400'
                )}
              />
              <span className={cx('relative inline-flex h-2 w-2 rounded-full', liveConnStatus === 'reconnecting' ? 'bg-amber-500' : 'bg-emerald-500')} />
            </span>
            <Clock size={13} />
            {now.toLocaleTimeString('en-IN', { hour12: false })}
            {liveConnStatus === 'reconnecting' && <span className="font-semibold text-amber-600 dark:text-amber-400">Reconnecting…</span>}
          </div>
        </div>
      </header>

      {/* ---------------- Body ---------------- */}
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        {/* ---- Map viewport ---- */}
        <div className="relative flex min-h-[360px] flex-1 overflow-hidden bg-[#050810] p-3 sm:p-6">
          <div className="relative h-full w-full overflow-hidden rounded-2xl border border-slate-200 dark:border-slate-800 shadow-2xl ring-1 ring-slate-800/60">
            <MapContainer
              center={INDIA_CENTER}
              zoom={DEFAULT_ZOOM}
              minZoom={4}
              maxZoom={14}
              maxBounds={INDIA_BOUNDS}
              maxBoundsViscosity={0.7}
              zoomControl={false}
              className="absolute inset-0 h-full w-full"
            >
              <TileLayer
                key={mapType}
                url={MAP_TYPES[mapType].url}
                attribution={MAP_TYPES[mapType].attribution}
                maxNativeZoom={MAP_TYPES[mapType].maxNativeZoom}
                subdomains={MAP_TYPES[mapType].subdomains || 'abc'}
              />
              <MapFlyTo target={flyTarget} />
              <MapControls onZoomIn={zoomInRef} onZoomOut={zoomOutRef} onReset={resetViewRef} />

              {layers.routes && routes.map((r) => <RoutePath key={r.id} route={r} />)}

              {layers.convoys && convoys.map((c) => (
                <ConvoyMarker key={c.id} convoy={c} onSelect={(v) => setSelected({ kind: 'convoy', ...v })} active={selected?.kind === 'convoy' && selected.id === c.id} />
              ))}
              {layers.sos && [...driverSos.filter((s) => Number.isFinite(s.lat) && Number.isFinite(s.lng)), ...sosPings].map((s) => (
                <SosMarker
                  key={s.id}
                  sos={s}
                  isNew={s.id === flashId}
                  dispatch={dispatched[s.id]}
                  onSelect={(v) => {
                    setSelected({ kind: 'sos', ...v });
                    flyToCoordinate(v.lat, v.lng);
                  }}
                  active={selected?.kind === 'sos' && selected.id === s.id}
                />
              ))}
              {layers.hazards && liveHazards.map((h) => (
                <HazardMarker key={h.id} hazard={h} onSelect={(v) => setSelected({ kind: 'hazard', ...v })} active={selected?.kind === 'hazard' && selected.id === h.id} />
              ))}
            </MapContainer>

            {/* map type switcher */}
            <div className="absolute left-3 top-3 z-[1000] flex overflow-hidden rounded-lg border border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-950/85 text-[11px] font-medium shadow-lg backdrop-blur">
              {Object.entries(MAP_TYPES).map(([key, cfg]) => (
                <button
                  key={key}
                  onClick={() => setMapType(key)}
                  className={cx(
                    'px-2.5 py-1.5 transition',
                    mapType === key ? 'bg-sky-600 text-white' : 'text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800'
                  )}
                >
                  {cfg.label}
                </button>
              ))}
            </div>

            {/* legend */}
            <div className="pointer-events-none absolute bottom-3 left-3 z-[1000] rounded-lg border border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-950/85 px-3 py-2 text-[11px] backdrop-blur">
              <div className="mb-1.5 font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Route Risk</div>
              {Object.entries(RISK_CONFIG).map(([key, cfg]) => (
                <div key={key} className="flex items-center gap-1.5 py-0.5">
                  <span className="h-2 w-4 rounded-full" style={{ backgroundColor: cfg.stroke }} />
                  <span className="text-slate-600 dark:text-slate-300">{cfg.label}</span>
                </div>
              ))}
            </div>

            {/* zoom + layer controls */}
            <div className="absolute right-3 top-3 z-[1000] flex flex-col gap-1.5">
              <button onClick={() => zoomInRef.current()} className="flex h-8 w-8 items-center justify-center rounded-md border border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-950/85 text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800"><ZoomIn size={15} /></button>
              <button onClick={() => zoomOutRef.current()} className="flex h-8 w-8 items-center justify-center rounded-md border border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-950/85 text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800"><ZoomOut size={15} /></button>
              <button onClick={() => resetViewRef.current()} className="flex h-8 w-8 items-center justify-center rounded-md border border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-950/85 text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800"><Navigation size={14} /></button>
              <div className="relative">
                <button onClick={() => setLayersOpen((o) => !o)} className="flex h-8 w-8 items-center justify-center rounded-md border border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-950/85 text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800"><Layers size={15} /></button>
                {layersOpen && (
                  <div className="absolute right-9 top-0 w-40 rounded-lg border border-slate-200 dark:border-slate-800 bg-white/95 dark:bg-slate-950/95 p-2 text-[11px] shadow-xl">
                    {Object.keys(layers).map((k) => (
                      <label key={k} className="flex items-center gap-2 py-1 capitalize text-slate-600 dark:text-slate-300">
                        <input type="checkbox" checked={layers[k]} onChange={() => toggleLayer(k)} className="accent-sky-500" />
                        {k}
                      </label>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* selection detail card */}
            {selected && (
              <div className="absolute bottom-3 right-3 z-[1000] w-72 rounded-xl border border-slate-300 dark:border-slate-700 bg-white/95 dark:bg-slate-950/95 p-3 text-xs shadow-2xl backdrop-blur">
                <div className="mb-2 flex items-start justify-between">
                  <span className="font-semibold text-slate-900 dark:text-slate-100">{selected.id}</span>
                  <button onClick={() => setSelected(null)} className="text-slate-500 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"><X size={14} /></button>
                </div>
                {selected.kind === 'convoy' ? (
                  <div className="space-y-1 text-slate-600 dark:text-slate-300">
                    <div>Cargo: <span className="text-slate-900 dark:text-slate-100">{selected.cargoType}</span> ({selected.priority})</div>
                    <div>Status: <span className="text-slate-900 dark:text-slate-100">{selected.status}</span></div>
                    <div>Route: {selected.route} → {selected.destination}</div>
                    <div>Driver: {selected.driver} · ETA: {selected.eta}</div>
                    <div>{formatCoord(selected.lat, selected.lng)}</div>
                  </div>
                ) : selected.kind === 'hazard' ? (
                  <div className="space-y-1 text-slate-600 dark:text-slate-300">
                    <div className="text-slate-900 dark:text-slate-100 font-medium">{selected.type}</div>
                    <div>{selected.description || 'No additional details provided.'}</div>
                    <div>{formatCoord(selected.latitude, selected.longitude)}</div>
                    <div className="text-amber-600 dark:text-amber-300">
                      Confirmed by {selected.confirmations} report{selected.confirmations > 1 ? 's' : ''}
                    </div>
                  </div>
                ) : (
                  <div className="space-y-1 text-slate-600 dark:text-slate-300">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <DispatchBadge dispatch={dispatched[selected.id]} resolved={resolved[selected.id]} />
                      <SosStatusBadge status={selected.status} />
                      {selected.category && <CategoryTag category={selected.category} />}
                      {selected.severity && <SeverityBadge severity={selected.severity} />}
                    </div>
                    <DriverHistoryFlag history={driverHistoryByPhone[selected.reportedBy]} />
                    <div className="pt-1">{selected.vehicleType} — <span className="text-red-600 dark:text-red-400 font-medium">{selected.cargoPriority}</span></div>
                    <div>{selected.district}{selected.state ? `, ${selected.state}` : ''}</div>
                    <div>{selected.locationLabel || formatCoord(selected.lat, selected.lng)}</div>
                    <div className="pt-1 text-slate-500 dark:text-slate-400">{selected.message}</div>
                    {selected.note && <div className="pt-1 italic text-slate-500 dark:text-slate-400">"{selected.note}"</div>}
                    {resolved[selected.id] ? (
                      <div className="mt-2 space-y-1 rounded-md border border-sky-500/30 bg-sky-500/10 px-2 py-1.5 text-sky-600 dark:text-sky-300">
                        <div className="flex items-center gap-1.5"><CheckCircle2 size={12} /> Resolved {timeAgo(resolved[selected.id].resolvedAt, now)}</div>
                        {selected.receivedAt && (
                          <div className="text-sky-700/80 dark:text-sky-200/70">
                            Closed {timeAgoMinutes(selected.receivedAt, resolved[selected.id].resolvedAt)} after report
                          </div>
                        )}
                        {resolved[selected.id].outcomeNote && <div className="italic text-sky-700/90 dark:text-sky-200/80">"{resolved[selected.id].outcomeNote}"</div>}
                      </div>
                    ) : dispatched[selected.id] ? (
                      <>
                        <div className="mt-2 flex items-center gap-1.5 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-1.5 text-emerald-600 dark:text-emerald-300">
                          <Truck size={12} /> QRT from {dispatched[selected.id].depotName} · ETA {dispatched[selected.id].etaMin}m
                        </div>
                        {resolvingId === selected.id ? (
                          <OutcomePicker onPick={(type, note) => resolveSos(selected, type, note)} onCancel={() => setResolvingId(null)} />
                        ) : (
                          <button
                            onClick={() => setResolvingId(selected.id)}
                            className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-sky-500/40 bg-sky-500/15 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-sky-600 dark:text-sky-300 transition hover:bg-sky-500/25"
                          >
                            <CheckCircle2 size={12} /> Mark Resolved
                          </button>
                        )}
                      </>
                    ) : (
                      <button
                        onClick={() => dispatchQrt(selected)}
                        className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/15 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-300 transition hover:bg-amber-500/25"
                      >
                        <Truck size={12} /> Acknowledge &amp; Dispatch QRT
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}

            {heatZonesOpen && (
              <HeatZonePanel
                zones={heatZones}
                onClose={() => setHeatZonesOpen(false)}
                onDispatch={dispatchHeatZone}
                dispatchingId={dispatchingZoneId}
              />
            )}

            {recoveryOpen && (
              <RecoveryPanel
                stats={combinedRecoveryStats}
                resolvedAlerts={combinedResolvedList}
                onClose={() => setRecoveryOpen(false)}
              />
            )}
          </div>
        </div>

        {/* ---- Sidebar ---- */}
        <aside className="flex w-full shrink-0 flex-col border-t border-slate-200 dark:border-slate-800 bg-slate-100/70 dark:bg-slate-900/50 lg:h-full lg:w-[400px] lg:border-l lg:border-t-0">
          <div className="shrink-0 space-y-3 border-b border-slate-200 dark:border-slate-800 p-3">
            <div className="relative">
              <Search size={15} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500 dark:text-slate-500" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search district, vehicle, cargo, route…"
                className="w-full rounded-lg border border-slate-200 dark:border-slate-800 bg-white/80 dark:bg-slate-950/70 py-2 pl-8 pr-3 text-xs text-slate-800 dark:text-slate-200 placeholder:text-slate-500 dark:placeholder:text-slate-500 focus:border-sky-600 focus:outline-none"
              />
            </div>
            <div className="flex flex-wrap gap-1.5">
              {STATE_LIST.map((s) => (
                <button
                  key={s}
                  onClick={() => setStateFilter(s)}
                  className={cx(
                    'rounded-full border px-2.5 py-1 text-[11px] font-medium transition',
                    stateFilter === s ? 'border-sky-500 bg-sky-500/15 text-sky-600 dark:text-sky-300' : 'border-slate-200 dark:border-slate-800 bg-white/60 dark:bg-slate-950/50 text-slate-500 dark:text-slate-400 hover:border-slate-300 dark:hover:border-slate-700'
                  )}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>

          <div className="flex shrink-0 items-center justify-between border-b border-slate-200 dark:border-slate-800 px-3 py-2 text-[11px] uppercase tracking-wide text-slate-500 dark:text-slate-400">
            <span className="flex items-center gap-1.5"><Radio size={13} className="text-red-600 dark:text-red-400" /> Live Alert Feed</span>
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setFeedTab('active')}
                className={cx('rounded-full border px-2 py-0.5 text-[10px] font-medium normal-case tracking-normal', feedTab === 'active' ? 'border-sky-500 bg-sky-500/15 text-sky-600 dark:text-sky-300' : 'border-slate-200 dark:border-slate-800 text-slate-500 dark:text-slate-500 hover:border-slate-300 dark:hover:border-slate-700')}
              >
                Active
              </button>
              <button
                onClick={() => setFeedTab('resolved')}
                className={cx('rounded-full border px-2 py-0.5 text-[10px] font-medium normal-case tracking-normal', feedTab === 'resolved' ? 'border-sky-500 bg-sky-500/15 text-sky-600 dark:text-sky-300' : 'border-slate-200 dark:border-slate-800 text-slate-500 dark:text-slate-500 hover:border-slate-300 dark:hover:border-slate-700')}
              >
                Resolved
              </button>
              <span>{feed.length}</span>
            </div>
          </div>

          <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
            {feed.length === 0 && (
              <div className="pt-10 text-center text-xs text-slate-500 dark:text-slate-500">
                {feedTab === 'resolved' ? 'No SOS alerts resolved yet this session.' : 'No alerts match the current filters.'}
              </div>
            )}

            {feed.map((item) =>
              item.kind === 'sos' ? (
                <div
                  key={item.id}
                  onClick={() => setSelected({ kind: 'sos', ...item })}
                  className={cx(
                    'cursor-pointer rounded-lg border p-3 transition',
                    item.id === flashId ? 'border-red-500 bg-red-500/10 animate-pulse' : 'border-slate-200 dark:border-slate-800 bg-white/60 dark:bg-slate-950/50 hover:border-slate-300 dark:hover:border-slate-700'
                  )}
                >
                  <div className="mb-1.5 flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-xs font-semibold text-red-600 dark:text-red-400">
                      <Siren size={13} /> SOS · {item.id}
                    </span>
                    <span className="text-[10px] text-slate-500 dark:text-slate-500">{item.timeLabel || timeAgo(item.timestamp, now)}</span>
                  </div>
                  <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                    <DispatchBadge dispatch={dispatched[item.id]} resolved={resolved[item.id]} />
                    <SosStatusBadge status={item.status} />
                    {item.category && <CategoryTag category={item.category} />}
                    {item.severity && <SeverityBadge severity={item.severity} />}
                  </div>
                  <DriverHistoryFlag history={driverHistoryByPhone[item.reportedBy]} />
                  <div className="space-y-0.5 text-[11px] text-slate-600 dark:text-slate-300">
                    <div className="text-slate-900 dark:text-slate-100 font-medium">{item.cargoPriority}</div>
                    <div>{item.vehicleType} · {item.district}{item.state ? `, ${item.state}` : ''}</div>
                    <div className="text-slate-500 dark:text-slate-500">{item.locationLabel || formatCoord(item.lat, item.lng)}</div>
                  </div>
                  <p className="mt-1.5 text-[11px] text-slate-500 dark:text-slate-400">{item.message}</p>
                  {item.note && <p className="mt-1 text-[11px] italic text-slate-500 dark:text-slate-400">"{item.note}"</p>}
                  {item.isVoice && (
                    <div className="mt-1.5 space-y-1 rounded-md border border-violet-500/30 bg-violet-500/10 p-2">
                      <div className="flex items-center gap-1.5">
                        <Radio size={11} className="text-violet-300" />
                        <span className="text-[10px] font-semibold uppercase tracking-wide text-violet-300">AI Voice Triage</span>
                        {item.urgency && <UrgencyBadge urgency={item.urgency} />}
                      </div>
                      {item.summary && <p className="text-[11px] text-slate-800 dark:text-slate-200">{item.summary}</p>}
                      {item.actionNeeded && (
                        <p className="text-[11px] text-violet-200"><span className="font-semibold">Action needed:</span> {item.actionNeeded}</p>
                      )}
                      <TranscriptToggle transcript={item.transcript} />
                    </div>
                  )}
                  {resolved[item.id] ? (
                    <div className="mt-2 space-y-1 rounded-md border border-sky-500/30 bg-sky-500/10 px-2 py-1.5 text-[11px] text-sky-600 dark:text-sky-300">
                      <div className="flex items-center gap-1.5">
                        <CheckCircle2 size={12} />
                        {(OUTCOME_CONFIG[resolved[item.id].outcomeType] || OUTCOME_CONFIG.OTHER).label}
                        {item.receivedAt && ` · closed ${timeAgoMinutes(item.receivedAt, resolved[item.id].resolvedAt)} after report`}
                      </div>
                      {resolved[item.id].outcomeNote && <div className="italic text-sky-700/90 dark:text-sky-200/80">"{resolved[item.id].outcomeNote}"</div>}
                    </div>
                  ) : dispatched[item.id] ? (
                    <>
                      <div className="mt-2 flex items-center gap-1.5 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-1.5 text-[11px] text-emerald-600 dark:text-emerald-300">
                        <Truck size={12} /> QRT from {dispatched[item.id].depotName} · ETA {dispatched[item.id].etaMin}m
                      </div>
                      {resolvingId === item.id ? (
                        <OutcomePicker onPick={(type, note) => resolveSos(item, type, note)} onCancel={() => setResolvingId(null)} />
                      ) : (
                        <button
                          onClick={(e) => { e.stopPropagation(); setResolvingId(item.id); }}
                          className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-sky-500/40 bg-sky-500/15 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-sky-600 dark:text-sky-300 transition hover:bg-sky-500/25"
                        >
                          <CheckCircle2 size={12} /> Mark Resolved
                        </button>
                      )}
                    </>
                  ) : (
                    <button
                      onClick={(e) => { e.stopPropagation(); dispatchQrt(item); }}
                      className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/15 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-300 transition hover:bg-amber-500/25"
                    >
                      <Truck size={12} /> Acknowledge &amp; Dispatch QRT
                    </button>
                  )}
                </div>
              ) : (
                <div
                  key={item.id}
                  onClick={() => setSelected(null)}
                  className={cx(
                    'cursor-pointer rounded-lg border-l-4 border border-slate-200 dark:border-slate-800 bg-white/60 dark:bg-slate-950/50 p-3 hover:border-slate-300 dark:hover:border-slate-700',
                    item.severity === 'blocked' && 'border-l-red-500',
                    item.severity === 'moderate' && 'border-l-amber-500',
                    item.severity === 'safe' && 'border-l-sky-500'
                  )}
                >
                  <div className="mb-1 flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-xs font-semibold text-slate-800 dark:text-slate-200">
                      {item.severity === 'blocked' ? <AlertTriangle size={13} className="text-red-600 dark:text-red-400" /> : item.severity === 'moderate' ? <CloudRain size={13} className="text-amber-600 dark:text-amber-400" /> : <CheckCircle2 size={13} className="text-sky-600 dark:text-sky-400" />}
                      {item.title}
                    </span>
                    <span className="text-[10px] text-slate-500 dark:text-slate-500">{timeAgo(item.timestamp, now)}</span>
                  </div>
                  <div className="text-[11px] text-slate-500 dark:text-slate-400">{item.route} · {item.district}, {item.state}</div>
                  <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">{item.message}</p>
                </div>
              )
            )}
          </div>

          <div className="flex shrink-0 items-center justify-between border-t border-slate-200 dark:border-slate-800 px-3 py-2 text-[10px] text-slate-500 dark:text-slate-500">
            <span className="flex items-center gap-1.5"><Mountain size={12} /> {routes.length} corridors monitored</span>
            <button
              onClick={() => {
                setNow(new Date());
                fetchAlerts();
                fetchHazards();
                refreshCorridors();
              }}
              className="flex items-center gap-1 text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200"
            >
              <RefreshCw size={11} /> Synced {timeAgo(now, now)}
            </button>
          </div>
        </aside>
      </div>
    </div>
  );
}
