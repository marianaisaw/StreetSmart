import { useEffect, useState } from 'react';
import Globe from './Globe.jsx';
import StreetSmart from './StreetSmart.jsx';
import './App.css';

const wantsApp = () => {
  const q = new URLSearchParams(location.search);
  return location.hash === '#app' || q.has('watch') || q.has('demo') || q.has('app');
};

export default function App() {
  const [app, setApp] = useState(wantsApp());
  useEffect(() => {
    const on = () => setApp(wantsApp());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);

  if (app) return <StreetSmart onHome={() => { history.pushState('', '', location.pathname); setApp(false); }} />;

  return (
    <div className="page">
      <header className="hero" id="top">
        <div className="wash" />
        <div className="orb">
          <div className="orb-halo" />
          <div className="orb-frame">
            <Globe />
            <div className="orb-shade" />
          </div>
          <span className="bubble bubble-a" />
          <span className="bubble bubble-b" />
          <span className="bubble bubble-c" />
        </div>

        <div className="topbar">
          <a className="logo" href="#top" aria-label="StreetSmart home">
            SS
          </a>
        </div>

        <div className="hero-copy">
          <h1>
            Street
            <br />
            Smart
          </h1>
          <p className="tagline">
            Google Maps knows the roads.
            <br />
            Streetsmart knows the streets.
          </p>
          <div className="cta-row">
            <a className="cta" href="#app">Plan a safe trip</a>
            <a className="cta ghost" href="?demo=1#app">Try the 11 PM demo</a>
          </div>
          <ul className="feats">
            <li><b>Live</b> Muni + BART departures</li>
            <li><b>Live</b> SFPD reports, X + news via Apify</li>
            <li><b>Claude Opus 5.5</b> picks the safest route in your budget</li>
          </ul>
        </div>

        <ul className="marks" aria-hidden="true">
          <li />
          <li />
          <li />
        </ul>
      </header>
    </div>
  );
}
