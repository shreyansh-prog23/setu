import { useState, useEffect } from 'react';
import { Sun, Moon } from 'lucide-react';
import Dashboard from './Dashboard.jsx';
import DriverView from './DriverView.jsx';

const cx = (...a) => a.filter(Boolean).join(' ');

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
          <Dashboard alerts={alerts} />
        ) : (
          <DriverView onTriggerSOS={handleTriggerSOS} />
        )}
      </div>
    </div>
  );
}
