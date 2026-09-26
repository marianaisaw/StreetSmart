// Mapbox GL when a public token is configured (VITE_MAPBOX_TOKEN), otherwise MapLibre GL with free
// OpenFreeMap vector tiles, so the app always runs without keys.
import mapboxgl from 'mapbox-gl';
import * as maplibregl from 'maplibre-gl';
// MapLibre's ESM build loads its worker from a separate module; let Vite bundle it and hand over the URL.
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import 'mapbox-gl/dist/mapbox-gl.css';
import 'maplibre-gl/dist/maplibre-gl.css';

export const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN || '';
export const isMapbox = !!TOKEN && TOKEN.startsWith('pk.');
export const gl = isMapbox ? mapboxgl : maplibregl;
if (isMapbox) mapboxgl.accessToken = TOKEN;
else maplibregl.setWorkerUrl(maplibreWorkerUrl);

const ESRI_SAT = {
  version: 8,
  glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
  sources: { sat: { type: 'raster', tileSize: 256, maxzoom: 19, attribution: 'Imagery © Esri',
    tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'] } },
  layers: [{ id: 'sat', type: 'raster', source: 'sat' }],
};

export function styleFor(kind) {
  if (isMapbox) {
    return { dark: 'mapbox://styles/mapbox/dark-v11', light: 'mapbox://styles/mapbox/light-v11',
      satellite: 'mapbox://styles/mapbox/satellite-streets-v12' }[kind] || 'mapbox://styles/mapbox/dark-v11';
  }
  return { dark: 'https://tiles.openfreemap.org/styles/dark', light: 'https://tiles.openfreemap.org/styles/positron',
    satellite: ESRI_SAT }[kind] || 'https://tiles.openfreemap.org/styles/dark';
}

// 3D buildings layer for whichever vector source the style uses.
export function addBuildings(map, dark) {
  if (map.getLayer('bldg-3d')) return;
  const style = map.getStyle();
  const src = isMapbox ? (style.sources.composite ? 'composite' : null) : (style.sources.openmaptiles ? 'openmaptiles' : null);
  if (!src) return;
  map.addLayer({
    id: 'bldg-3d', type: 'fill-extrusion', source: src, 'source-layer': 'building', minzoom: 14,
    paint: {
      'fill-extrusion-color': dark ? '#23232b' : '#dcdce4',
      'fill-extrusion-height': isMapbox ? ['get', 'height'] : ['coalesce', ['get', 'render_height'], 8],
      'fill-extrusion-base': isMapbox ? ['get', 'min_height'] : ['coalesce', ['get', 'render_min_height'], 0],
      'fill-extrusion-opacity': 0.75,
    },
  });
}
