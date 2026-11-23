#!/bin/bash
# Mashup Studio, started by its app: runs `mf app` from the folder install.sh was run in (that path is in mf-path,
# next to this file). Quit or Restart in the page, or close this window, to stop it.
mf="$(cat "$(dirname "$0")/mf-path")"
exec "$mf" app
