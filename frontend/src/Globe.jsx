import { useEffect, useRef } from 'react';
import { gl, isMapbox } from './engine.js';

export default function Globe() {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const spinning = useRef(true);

  useEffect(() => {
    spinning.current = true;
    if (!containerRef.current || mapRef.current) return undefined;

    const map = new gl.Map({
      container: containerRef.current,
      style: isMapbox ? 'mapbox://styles/mapbox/satellite-streets-v12' : {
        version: 8,
        sources: { sat: { type: 'raster', tileSize: 256, attribution: 'Imagery © Esri',
          tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'] } },
        layers: [{ id: 'sat', type: 'raster', source: 'sat' }],
      },
      ...(isMapbox ? { projection: 'globe' } : {}),
      center: [-122.42, 18],
      zoom: 1.42,
      interactive: true,
      attributionControl: false,
      scrollZoom: false,
      boxZoom: false,
      doubleClickZoom: false,
      dragRotate: false,
    });

    map.addControl(new gl.AttributionControl({ compact: true }), 'bottom-right');
    mapRef.current = map;

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
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
      if (isMapbox) {
        map.setFog({
          color: 'rgb(42, 12, 88)',
          'high-color': 'rgb(176, 72, 255)',
          'horizon-blend': 0.1,
          'space-color': 'rgb(8, 5, 24)',
          'star-intensity': 0.65,
        });
      } else {
        map.setProjection({ type: 'globe' });
      }
      if (!reducedMotion) frame = requestAnimationFrame(tick);
    });

    const pause = () => { spinning.current = false; cancelAnimationFrame(frame); last = 0; };
    const resume = () => {
      if (spinning.current || reducedMotion) return;
      spinning.current = true; last = 0; frame = requestAnimationFrame(tick);
    };
    map.on('mousedown', pause);
    map.on('touchstart', pause);
    map.on('mouseup', resume);
    map.on('touchend', resume);
    map.on('dragend', resume);

    return () => { spinning.current = false; cancelAnimationFrame(frame); map.remove(); mapRef.current = null; };
  }, []);

  return <div ref={containerRef} className="globe-map" aria-hidden="true" />;
}
