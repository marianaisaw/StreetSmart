import Globe from './Globe.jsx';
import './App.css';

export default function App() {
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
          <a className="enter" href="http://127.0.0.1:8765/">
            Open the project
          </a>
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
