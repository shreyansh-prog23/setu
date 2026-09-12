import { useState, useEffect } from 'react';
import { Sun, Moon, ShieldCheck, AlertTriangle } from 'lucide-react';
import Dashboard from './Dashboard.jsx';
import DriverView from './DriverView.jsx';
import { apiFetch, getOperatorSession, setOperatorSession, clearOperatorSession } from './apiClient';

const cx = (...a) => a.filter(Boolean).join(' ');

// Command Center's login - phone + Twilio Verify OTP, same mechanism the
// driver login used to use (see backend/main.py's /api/operator/login/*).
// SOS reporting itself no longer requires a login (a driver in a real
// emergency shouldn't have to verify an OTP first) - this is the real gate
// now, moved to the side that actually holds sensitive data: live reports,
// contact numbers, dispatch controls.
function OperatorLoginGate({ onLoggedIn }) {
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
      const res = await apiFetch('/api/operator/login/start', {
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
      const res = await apiFetch('/api/operator/login/verify', {
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
            <ShieldCheck size={22} />
          </div>
          <h1 className="text-sm font-bold text-slate-900 dark:text-slate-100">Command Center Login</h1>
          <p className="text-[12px] text-slate-500 dark:text-slate-500">Verify your phone once — stays signed in until you sign out.</p>
        </div>

        {error && step === 'phone' && (
          <div className="mb-3 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-2.5 text-[11px] text-red-600 dark:text-red-400">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>{error}</span>
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
            <button
              onClick={sendCode}
              disabled={submitting}
              className="w-full rounded-lg bg-sky-600 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-500 disabled:opacity-50"
            >
              {submitting ? 'Sending…' : 'Send Code'}
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            <label className="block text-[11px] font-medium text-slate-500 dark:text-slate-400">Enter Code</label>
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="6-digit code"
              inputMode="numeric"
              className="w-full rounded-lg border border-slate-300 dark:border-slate-700 bg-white/80 dark:bg-slate-950/70 px-3 py-2.5 text-[13px] text-slate-800 dark:text-slate-200 placeholder:text-slate-600 dark:placeholder:text-slate-600 focus:border-sky-600 focus:outline-none"
            />
            {error && <p className="text-[11px] text-red-600 dark:text-red-400">{error}</p>}
            <button
              onClick={verifyCode}
              disabled={submitting}
              className="w-full rounded-lg bg-sky-600 py-2.5 text-sm font-semibold text-white transition hover:bg-sky-500 disabled:opacity-50"
            >
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

const TABS = [
  { id: 'dashboard', label: '🖥️ Government Command Center' },
  { id: 'driver', label: '📱 Driver Mobile View' },
];

const THEME_STORAGE_KEY = 'setu_theme';

// Defaults to dark - this app's original, only look - so anyone who hasn't
// explicitly chosen light mode keeps seeing exactly what they always have.
function getInitialTheme() {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

// Was 2 hardcoded NE-specific fake SOS alerts (Shillong bypass, NH-29) that
// showed up as permanent map markers on every load regardless of any real
// activity - same issue SOS_INITIAL in Dashboard.jsx already had and was
// emptied for. Real alerts come from the backend or deliberately-added
// simulated ones, not permanent fake seed data baked into the app shell.
const INITIAL_ALERTS = [];

export default function App() {
  const [view, setView] = useState('dashboard');
  const [alerts, setAlerts] = useState(INITIAL_ALERTS);
  const [theme, setTheme] = useState(getInitialTheme);
  const [operatorSession, setOperatorSessionState] = useState(() => getOperatorSession());

  const operatorLogout = () => {
    apiFetch('/api/operator/logout', { method: 'POST' }).catch(() => {});
    clearOperatorSession();
    setOperatorSessionState(null);
  };

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // localStorage can throw in a private/locked-down browser context -
      // the toggle still works for the rest of this session either way.
    }
  }, [theme]);

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'));

  const handleTriggerSOS = (newAlert) => {
    setAlerts((prev) => [newAlert, ...prev]);
  };

  return (
    <div className="flex min-h-screen flex-col bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-white">
      <nav className="sticky top-0 z-50 flex shrink-0 items-center justify-center gap-3 border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/80 px-4 py-3 backdrop-blur">
        <div className="flex items-center gap-1 rounded-full border border-slate-200 dark:border-slate-800 bg-white/70 dark:bg-slate-950/60 p-1">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setView(tab.id)}
              className={cx(
                'rounded-full px-4 py-2 text-sm font-semibold transition',
                view === tab.id
                  ? 'bg-sky-500 text-slate-950 shadow-lg shadow-sky-500/30'
                  : 'text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200'
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {view === 'dashboard' && operatorSession && (
          <button
            onClick={operatorLogout}
            title={`Signed in as ${operatorSession.phone} — click to sign out`}
            className="flex h-9 items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-800 bg-white/70 dark:bg-slate-950/60 px-3 text-[11px] font-semibold text-slate-500 dark:text-slate-400 transition hover:text-slate-800 dark:hover:text-slate-200"
          >
            <ShieldCheck size={14} /> Sign out
          </button>
        )}
        <button
          onClick={toggleTheme}
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          className="flex h-9 w-9 items-center justify-center rounded-full border border-slate-200 dark:border-slate-800 bg-white/70 dark:bg-slate-950/60 text-slate-500 dark:text-slate-400 transition hover:text-slate-800 dark:hover:text-slate-200"
        >
          {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
        </button>
      </nav>

      <div className="flex-1">
        {view === 'dashboard' ? (
          operatorSession ? (
            <Dashboard alerts={alerts} />
          ) : (
            <OperatorLoginGate
              onLoggedIn={(token, phone) => {
                setOperatorSession(token, phone);
                setOperatorSessionState({ token, phone });
              }}
            />
          )
        ) : (
          <DriverView onTriggerSOS={handleTriggerSOS} />
        )}
      </div>
    </div>
  );
}
