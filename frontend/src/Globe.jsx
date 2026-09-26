import { useEffect, useRef } from 'react';
import mapboxgl from 'mapbox-gl';
import 'mapbox-gl/dist/mapbox-gl.css';

const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

export default function Globe() {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const spinning = useRef(true);

  useEffect(() => {
    spinning.current = true;
    if (!containerRef.current || mapRef.current || !TOKEN) return undefined;

    mapboxgl.accessToken = TOKEN;

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: 'mapbox://styles/mapbox/satellite-streets-v12',
      projection: 'globe',
      center: [-122.42, 18],
      zoom: 1.42,
      interactive: true,
      attributionControl: false,
      scrollZoom: false,
      boxZoom: false,
      doubleClickZoom: false,
      dragRotate: false,
    });

    map.addControl(
      new mapboxgl.AttributionControl({ compact: true }),
      'bottom-right'
    );

    mapRef.current = map;

    const reducedMotion = window.matchMedia(
      '(prefers-reduced-motion: reduce)'
    ).matches;

    let frame = 0;
    let last = 0;

    const tick = (now) => {
      if (!spinning.current) return;
      if (!last) last = now;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const center = map.getCenter();
      let lng = center.lng - 10 * dt;
      if (lng < -180) lng += 360;
      map.setCenter([lng, 16]);
      frame = requestAnimationFrame(tick);
    };

    map.on('style.load', () => {
      map.setFog({
        color: 'rgb(42, 12, 88)',
        'high-color': 'rgb(176, 72, 255)',
        'horizon-blend': 0.1,
        'space-color': 'rgb(8, 5, 24)',
        'star-intensity': 0.65,
      });
      if (!reducedMotion) frame = requestAnimationFrame(tick);
    });

    const pause = () => {
      spinning.current = false;
      cancelAnimationFrame(frame);
      last = 0;
    };
    const resume = () => {
      if (spinning.current || reducedMotion) return;
      spinning.current = true;
      last = 0;
      frame = requestAnimationFrame(tick);
    };

    map.on('mousedown', pause);
    map.on('touchstart', pause);
    map.on('mouseup', resume);
    map.on('touchend', resume);
    map.on('dragend', resume);

    return () => {
      spinning.current = false;
      cancelAnimationFrame(frame);
      map.remove();
      mapRef.current = null;
    };
  }, []);

  return <div ref={containerRef} className="globe-map" aria-hidden="true" />;
}
