import * as maplibregl from './maplibre-gl.mjs';
const element = document.getElementById('map');
const message = document.getElementById('map-error');
function fail() { message.hidden = false; message.textContent = 'The offline map could not be loaded. Check that the map is published and WebGL is enabled.'; }
try {
    const url = new URL(element.dataset.archive, window.location.origin).href;
    const protocol = new window.pmtiles.Protocol();
    maplibregl.addProtocol('pmtiles', protocol.tile);
    const p = new window.pmtiles.PMTiles(url);
    protocol.add(p);
    const header = await p.getHeader();
    const response = await fetch('/api/map-assets/style.json');
    if (!response.ok) throw new Error('Map style unavailable');
    const style = await response.json();
    style.sources.protomaps.url = 'pmtiles://' + url;
    const map = new maplibregl.Map({container: element, style, center: [header.centerLon, header.centerLat], zoom: header.centerZoom, maxZoom: 20, attributionControl: true});
    map.addControl(new maplibregl.NavigationControl(), 'top-right');
    map.addControl(new maplibregl.ScaleControl());
    map.on('error', fail);
    map.once('idle', () => { element.dataset.ready = 'true'; });
} catch (error) { fail(); }
