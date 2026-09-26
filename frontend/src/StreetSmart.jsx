import { useEffect } from 'react';
import { start } from './controller.js';
import './streetsmart.css';

const MARKUP = `

<div class="aside">
  <button class="home" onclick="window.__ssHome && window.__ssHome()" title="Back to home">SS</button>
  <h1>Street<br>Smart</h1>
  <p style="margin-top:14px">Google Maps knows the roads. StreetSmart knows the streets.</p>
  <p>Search anywhere in San Francisco, pick a budget, and a Claude Opus 5.5 agent picks the safest way there, or score it for free with no AI tokens.</p>
  <div class="kv"><div><b>Live</b> Muni + BART departures (Transitous)</div><div><b>Live</b> SFPD reports, refreshed every 10 min</div><div><b>Apify</b> X, news + Google Maps stop photos</div><div><b>H3</b> safety hexes with one-line reasons</div></div>
</div>

<div class="phone">
  <div class="side-btn"></div>
  <div class="screen" id="screen">
    <div class="island"></div>
    <div class="statusbar"><span id="clock">--:--</span>
      <span class="icons">
        <svg width="18" height="12" viewBox="0 0 18 12" fill="currentColor"><rect x="0" y="8" width="3" height="4" rx="1"/><rect x="5" y="5.5" width="3" height="6.5" rx="1"/><rect x="10" y="3" width="3" height="9" rx="1"/><rect x="15" y="0" width="3" height="12" rx="1"/></svg>
        <svg width="16" height="12" viewBox="0 0 16 12" fill="currentColor"><path d="M8 2.3c2.3 0 4.4.9 6 2.4l1.2-1.3A10.4 10.4 0 0 0 8 .5 10.4 10.4 0 0 0 .8 3.4L2 4.7a8.6 8.6 0 0 1 6-2.4Zm0 3.6c1.3 0 2.5.5 3.4 1.3l1.2-1.3A6.7 6.7 0 0 0 8 4.1a6.7 6.7 0 0 0-4.6 1.8l1.2 1.3c.9-.8 2.1-1.3 3.4-1.3Zm0 3.5c-.5 0-1 .2-1.3.5L8 11.5l1.3-1.6c-.3-.3-.8-.5-1.3-.5Z"/></svg>
        <svg width="27" height="13" viewBox="0 0 27 13" fill="none"><rect x=".5" y=".5" width="23" height="12" rx="3.5" stroke="currentColor" opacity=".4"/><rect x="2" y="2" width="17" height="9" rx="2" fill="currentColor"/><path d="M25 4.5v4c.8-.3 1.3-1.1 1.3-2s-.5-1.7-1.3-2Z" fill="currentColor" opacity=".45"/></svg>
      </span>
    </div>

    <div id="map"></div>

    <div id="search" class="blur">
      <div class="brandbar"><button onclick="window.__ssHome && window.__ssHome()">SS</button><b>StreetSmart</b><span>San Francisco</span></div>
      <div class="banner" id="share-banner"></div>
      <div id="search-main">
        <div class="od-wrap">
          <div class="od-row"><i class="ic" style="background:var(--tint)"></i><input id="from" aria-label="From" autocomplete="off" placeholder="Start"></div>
          <div class="od-row"><i class="ic" style="background:var(--red)"></i><input id="to" aria-label="To" autocomplete="off" placeholder="Where to?"></div>
          <button class="swap" title="Swap start and destination" onclick="swapOD()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M7 4v16M7 4 3 8M7 4l4 4M17 20V4m0 16-4-4m4 4 4-4"/></svg></button>
        </div>
        <div class="sugg" id="sugg"></div>
        <div id="search-rest">
          <div class="when" id="when"></div>
          <div class="seg" id="tiers"></div>
        </div>
      </div>
    </div>

    <div id="fabs" class="blur">
      <button onclick="openSettings()" title="Settings"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg></button>
      <button id="fab-heat" onclick="toggleHeat()" title="Safety heatmap"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 2 21 7v10l-9 5-9-5V7z"/><path d="m12 7 4.5 2.5v5L12 17l-4.5-2.5v-5z"/></svg></button>
      <button id="fab-ref" onclick="refreshAll()" title="Refresh live data"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></svg></button>
      <button onclick="locateMe()" title="My location"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M21 3 3 10.5l7.3 2.2L12.5 20z"/></svg></button>
      <button onclick="fitView(true)" title="Show whole trip"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button>
    </div>

    <div id="legend" class="blur"><span><i style="background:var(--safe)"></i>Very safe</span><span><i style="background:var(--mid)"></i>Kind of safe</span><span><i style="background:var(--bad)"></i>Unsafe</span></div>
    <div id="live" class="blur" onclick="openSettings('agent')" title="Live data status"><span class="dot off" id="live-dot"></span><span id="live-text">Connecting…</span></div>

    <div id="sheet">
      <div class="grab" id="grab"><div></div></div>
      <div class="sheet-body" id="sheet-body"><div class="loading"><div class="spin"></div>Loading map…</div></div>
    </div>

    <div class="scrim" id="scrim" onclick="closeModals(true)"></div>

    <div class="modal" id="stop-modal">
      <div class="navbar"><span></span><span class="ttl" id="stop-title">Stop</span><button class="r" onclick="closeModals()">Done</button></div>
      <div class="modal-body" id="stop-body"></div>
    </div>

    <div class="modal" id="settings-modal">
      <div class="navbar"><button onclick="closeModals(true)">Cancel</button><span class="ttl">Settings</span><button class="r" onclick="applySettings()">Done</button></div>
      <div class="modal-body" id="settings-body"></div>
    </div>

    <div id="drive-hud" class="blur"></div>
    <div id="toast" class="blur"></div>
    <div class="home-ind"></div>
  </div>
</div>

`;

export default function StreetSmart({ onHome }) {
  useEffect(() => {
    window.__ssHome = onHome;
    document.title = 'StreetSmart';
    start();
  }, [onHome]);
  return <div className="ss-root" dangerouslySetInnerHTML={{ __html: MARKUP }} />;
}
