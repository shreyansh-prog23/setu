import { useState } from 'react';
import Dashboard from './Dashboard.jsx';
import DriverView from './DriverView.jsx';

const cx = (...a) => a.filter(Boolean).join(' ');

const TABS = [
  { id: 'dashboard', label: '🖥️ Government Command Center' },
  { id: 'driver', label: '📱 Driver Mobile View' },
];

// Was 2 hardcoded NE-specific fake SOS alerts (Shillong bypass, NH-29) that
// showed up as permanent map markers on every load regardless of any real
// activity - same issue SOS_INITIAL in Dashboard.jsx already had and was
// emptied for. Real alerts come from the backend or deliberately-added
// simulated ones, not permanent fake seed data baked into the app shell.
const INITIAL_ALERTS = [];

export default function App() {
  const [view, setView] = useState('dashboard');
  const [alerts, setAlerts] = useState(INITIAL_ALERTS);

  const handleTriggerSOS = (newAlert) => {
    setAlerts((prev) => [newAlert, ...prev]);
  };

  return (
    <div className="flex min-h-screen flex-col bg-slate-950 text-white">
      <nav className="sticky top-0 z-50 flex shrink-0 items-center justify-center border-b border-slate-800 bg-slate-900/80 px-4 py-3 backdrop-blur">
        <div className="flex items-center gap-1 rounded-full border border-slate-800 bg-slate-950/60 p-1">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setView(tab.id)}
              className={cx(
                'rounded-full px-4 py-2 text-sm font-semibold transition',
                view === tab.id
                  ? 'bg-sky-500 text-slate-950 shadow-lg shadow-sky-500/30'
                  : 'text-slate-400 hover:text-slate-200'
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>
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
