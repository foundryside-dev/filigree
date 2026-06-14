// Sparse-fetch the shared @weft/site-kit into vendor/site-kit/.
//
// The kit lives in a SUBDIRECTORY (packages/site-kit) of a DIFFERENT repo
// (the weft hub). npm cannot install a git subdirectory directly, so the
// sanctioned realization of the "git subdirectory dependency" decision
// (IA §1.3, §6 item 5) is: clone the hub repo sparsely, copy just the kit
// subdirectory into ./vendor/site-kit, and depend on it via
// "file:./vendor/site-kit". The vendor copy is regenerated, never committed
// (it is .gitignored) — so the kit always refreshes from the hub at
// install/build time and the member site never drifts from the source.
//
// Runs automatically via the package.json "preinstall" hook, and again in
// CI before `npm install` (see .github/workflows/deploy-site.yml).
import { cp, rm, mkdir } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';

const here = dirname(fileURLToPath(import.meta.url));
const siteRoot = join(here, '..');
const dest = join(siteRoot, 'vendor', 'site-kit');

const REPO = process.env.WEFT_SITE_KIT_REPO || 'https://github.com/foundryside-dev/weft.git';
const REF = process.env.WEFT_SITE_KIT_REF || 'main';
const SUBDIR = 'packages/site-kit';

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (r.status !== 0) {
    throw new Error(`[fetch-site-kit] command failed (${r.status}): ${cmd} ${args.join(' ')}`);
  }
}

// Escape hatch for offline builds: if a local kit is already present and
// SKIP is set, do nothing. Lets a build proceed without network when the
// vendor copy has already been populated.
if (process.env.WEFT_SITE_KIT_SKIP_FETCH && existsSync(join(dest, 'package.json'))) {
  console.log('[fetch-site-kit] WEFT_SITE_KIT_SKIP_FETCH set and vendor/site-kit present — skipping fetch.');
  process.exit(0);
}

const tmp = join(tmpdir(), `weft-site-kit-${process.pid}-${Date.now()}`);

try {
  console.log(`[fetch-site-kit] sparse-cloning ${REPO}#${REF} (${SUBDIR})…`);
  run('git', ['clone', '--depth', '1', '--filter=blob:none', '--sparse', '--branch', REF, REPO, tmp]);
  run('git', ['-C', tmp, 'sparse-checkout', 'set', SUBDIR]);

  const src = join(tmp, SUBDIR);
  if (!existsSync(src)) {
    throw new Error(`[fetch-site-kit] ${SUBDIR} not found in the cloned repo`);
  }

  await rm(dest, { recursive: true, force: true });
  await mkdir(dirname(dest), { recursive: true });
  await cp(src, dest, { recursive: true });
  console.log(`[fetch-site-kit] copied ${src} -> ${dest}`);
} catch (err) {
  // If the network is unavailable but a previously-fetched vendor copy
  // exists, fall through with a warning rather than hard-failing the
  // install — the existing copy is usable.
  if (existsSync(join(dest, 'package.json'))) {
    console.warn(`[fetch-site-kit] fetch failed (${err.message}); using existing vendor/site-kit.`);
  } else {
    console.error(err.message);
    process.exit(1);
  }
} finally {
  await rm(tmp, { recursive: true, force: true });
}
