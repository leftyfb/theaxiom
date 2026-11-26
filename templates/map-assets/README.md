# Offline map reader assets

The following versions are pinned to the public npm registry. The five runtime
files are byte-for-byte copies of the listed package files.

| npm package | Version | Vendored files or generated output |
| --- | --- | --- |
| [`maplibre-gl`](https://registry.npmjs.org/maplibre-gl/-/maplibre-gl-6.19.0.tgz) | `6.19.0` | `dist/maplibre-gl.mjs`, `dist/maplibre-gl-worker.mjs`, `dist/maplibre-gl-shared.mjs`, `dist/maplibre-gl.css` |
| [`pmtiles`](https://registry.npmjs.org/pmtiles/-/pmtiles-4.5.0.tgz) | `4.5.0` | `dist/pmtiles.js` |
| [`@protomaps/basemaps`](https://registry.npmjs.org/@protomaps/basemaps/-/basemaps-5.7.2.tgz) | `5.7.2` | `style.json` generated from `layers('protomaps', namedFlavor('light'), {lang: 'en'})` |

The zero-byte `maplibre-gl-shared.mjs` is as shipped upstream. The original
source-map comments are preserved; debugging source maps are not vendored.

The style's only resource URL is `pmtiles://LOCAL_ARCHIVE`, a placeholder for
an archive supplied by the reader. The style uses local fonts: no `glyphs` or
`sprite` URL is configured. Icon layout properties and symbol layers without
`text-field` were removed. No tile archive is included.

## Upstream notices

- `LICENSE-MapLibre.txt`: BSD-3-Clause license and notices from the MapLibre GL JS npm package.
- `LICENSE-PMTiles.txt`: [PMTiles BSD-3-Clause notice](https://github.com/protomaps/PMTiles/blob/aec8fa1341222fdddb3318e9ffa8e18e19b312f7/LICENSE).
- `LICENSE-basemaps.txt`: [Protomaps basemaps BSD-3-Clause notice and attribution](https://github.com/protomaps/basemaps/blob/42ffaaa4a85a41bfcb23e43cc0f5b492a5eca123/LICENSE.md).
- `LICENSE-fflate.txt`: MIT license from the [`fflate` 0.8.2 npm package](https://registry.npmjs.org/fflate/-/fflate-0.8.2.tgz), bundled upstream in `pmtiles.js`.
