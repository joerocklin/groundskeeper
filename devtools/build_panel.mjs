// Minify the panel: `npx --yes esbuild@0.25.0` must be available (run via
// `node devtools/build_panel.mjs`). Minify only; no bundling or transpiling.
// Writes the .min.js plus a .sha1 stamp; panel.py serves the minified file
// only while the stamp matches the readable source.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const dir = new URL('../custom_components/groundskeeper/frontend/', import.meta.url).pathname;
const src = readFileSync(dir + 'groundskeeper-panel.js');
const min = execFileSync('npx', ['--yes', 'esbuild@0.25.0', '--minify', '--target=esnext', '--format=esm'], { input: src });
writeFileSync(dir + 'groundskeeper-panel.min.js', min);
writeFileSync(dir + 'groundskeeper-panel.min.js.sha1', createHash('sha1').update(src).digest('hex') + '\n');
