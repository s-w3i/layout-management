#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -x .tools/node/bin/node ]; then
  export PATH="$PWD/.tools/node/bin:$PATH"
fi
node scripts/check-port.mjs
if [ ! -d node_modules ]; then npm ci; fi
npm run build
npm start
